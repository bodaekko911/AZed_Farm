# Backups

Every AZed Farm instance must have automated, **offsite** database backups
before it holds real business data. Railway's built-in Postgres backups (paid
plans) are a good extra layer, but they live inside the same platform account —
an offsite copy in your own bucket is the one that saves you if the project is
deleted, the account is compromised, or you need to move providers.

The setup below costs roughly nothing (Cloudflare R2 has a 10 GB free tier and
no egress fees; a nightly dump of a farm's database is a few MB).

## One-time bucket setup (once, shared across AZed products)

1. Create a Cloudflare R2 bucket (or Backblaze B2 / AWS S3), e.g. `azed-backups`.
2. Create an API token scoped to that bucket with read + write.
3. Note the endpoint URL: `https://<accountid>.r2.cloudflarestorage.com`.

One bucket can serve all AZed products and customers — each instance writes
under its own `BACKUP_PREFIX` folder (e.g. `azed-farm`, `sinai-lodge`, …), so
this can be the same bucket already used by AZed Hospitality.

## Per-instance setup (part of every deployment)

In the instance's Railway project, add a **new service** from the same GitHub
repo:

1. Service settings → Source → Root Directory: `backup`
   (this makes Railway use `backup/Dockerfile` and `backup/railway.json`,
   which already sets the nightly cron schedule `0 2 * * *`)
2. Variables:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` (service reference) |
   | `S3_BUCKET` | `azed-backups` |
   | `S3_ENDPOINT_URL` | your R2 endpoint |
   | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | bucket token |
   | `BACKUP_PREFIX` | the instance's slug, e.g. `azed-farm` |
   | `BACKUP_KEEP_DAYS` | `30` |

3. Trigger the service once manually and confirm an object appears in the
   bucket under `azed-farm/…`.

The cron service runs, uploads, prunes old copies, and exits. It shares no code
with the app, so app deploys never touch it.

## Restoring

```sh
# list what's available
./backup/restore.sh

# restore a specific backup into $DATABASE_URL
./backup/restore.sh azed-farm/azed-farm-20260715-020000.dump
```

Run it from any machine with `pg_restore` + `aws` (or shell into the backup
service). It drops and recreates tables, so for rehearsals point
`DATABASE_URL` at a scratch database, never production.

After any restore: the app's entrypoint runs `alembic upgrade head` on boot,
so if the dump predates the code you're running the schema catches up
automatically — then spot-check Sales, Inventory and B2B invoices.

## The monthly restore drill (do not skip)

A backup that has never been restored is a hope, not a backup. Once a month:

1. Create a throwaway Postgres service in Railway.
2. Restore the latest dump into it.
3. Point a local run of the app at it (`DATABASE_URL=…`) and confirm login,
   recent sales orders, stock levels, and a B2B invoice page load (including
   the auto-paid fully-discounted invoices).
4. Delete the throwaway database.

Ten minutes, and it converts "I think we have backups" into "I have restored
one this month".

## What is deliberately NOT backed up

The `app/static/uploads` directory is created defensively but nothing is
persisted there — Excel imports (products, stock, customers, sales) are parsed
in memory and written straight to the database. Railway's filesystem is
ephemeral by design, so the Postgres dump covers all business data. If a
future feature ever stores files on disk, move that storage into the database
or the bucket first.
