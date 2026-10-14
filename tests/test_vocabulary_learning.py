"""The business vocabulary (phase 2): learning from corrections, and CSV import / export.

* "Remember 'X' as Y" is saved only once the invoice or document is RECORDED — a review that fails, is refused or is
  abandoned teaches nothing — and only for someone who may manage the vocabulary.
* A learned alias is stored the way it will be compared: a product's pack size is left out.
* An import is previewed row by row (add / duplicate / conflict / unmatched) and nothing is saved until it is run;
  running it re-checks every row rather than trusting the preview.
"""
import csv
import io
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.test_assistant_actions import admin, cashier, configured, run, session_with_data  # noqa: F401
from tests.test_telegram_bot import TEXT_SPOILAGE, in_review, telegram  # noqa: F401
from tests.test_telegram_bot import admin as field_admin
from tests.test_telegram_bot import scenario as field_scenario

from app.core.log import ActivityLog
from app.models.vocabulary import VocabularyEntry
from app.services import assistant_actions as act
from app.services import field_service as field
from app.services import vocabulary_service as vocab

MANAGER = SimpleNamespace(id=2, name="Manager", role="manager", permissions=None)


def scenario(fn):
    async def go():
        engine, db = await session_with_data()
        vocab.invalidate()
        try:
            return await fn(db)
        finally:
            await db.close()
            await engine.dispose()
    return run(go())


async def learned(db):
    return sorted((e.entity_type, e.alias, e.record_id, e.source, e.created_by_name)
                  for e in (await db.execute(select(VocabularyEntry).where(VocabularyEntry.source == "learned"))).scalars())


BASE = {"number": "1043", "date": "2025-09-14", "customer_id": 2, "paid": True, "discount": 0, "filename": "s.pdf",
        "items": [{"product_id": 2, "qty": 2, "unit_price": 150}, {"product_id": 1, "qty": 100, "unit_price": 0.8}]}
REMEMBER = [{"type": "pos_customer", "alias": "مدام منى", "record_id": 2},
            {"type": "product", "alias": "عسل جبلي 500g", "record_id": 2}]


# ── Learning: only on confirm ────────────────────────────────────────────────

def test_a_correction_is_remembered_when_the_invoice_is_recorded():
    async def go(db):
        out = await act.record_invoice(db, admin(), dict(BASE, pdf_total=380, remember=REMEMBER))
        assert out["ok"] and out["learned"] == 2
        # The pack size is left out: matching compares names without it.
        assert await learned(db) == [("pos_customer", "مدام منى", 2, "learned", "Boss"),
                                     ("product", "عسل جبلي", 2, "learned", "Boss")]
        vocab.invalidate()
        (inv,) = await act.match_invoices(db, [{"number": "1050", "date": "2025-09-20", "customer": "مدام منى",
                                                "discount": 0, "paid": True, "items": [
                                                    {"description": "عسل جبلي 250g", "sku": None, "qty": 1,
                                                     "unit_price": 75, "line_total": 75}]}])
        assert inv["customer_match"]["id"] == 2 and inv["lines"][0]["product"]["id"] == 2
    scenario(go)


def test_nothing_is_learned_when_recording_fails():
    async def go(db):
        with pytest.raises(HTTPException):
            await act.record_invoice(db, admin(), dict(BASE, pdf_total=999, remember=REMEMBER))   # total mismatch
        assert await learned(db) == []
    scenario(go)


def test_nothing_is_learned_without_the_vocabulary_permission():
    async def go(db):
        out = await act.record_invoice(db, MANAGER, dict(BASE, pdf_total=380, remember=REMEMBER))
        assert out["ok"] and out["learned"] == 0 and await learned(db) == []
    scenario(go)


def test_a_clashing_correction_is_a_note_not_an_error():
    async def go(db):
        await vocab.create_entry(db, admin(), kind="pos_customer", alias="مدام منى", record_id=1)
        out = await act.record_invoice(db, admin(), dict(BASE, pdf_total=380, remember=REMEMBER))
        assert out["ok"] and out["learned"] == 1                       # the product alias still saved
        assert any("already means" in n for n in out["notes"])
        assert ("pos_customer", "مدام منى", 2, "learned", "Boss") not in await learned(db)
    scenario(go)


def test_each_screen_learns_only_the_types_it_lets_the_user_correct():
    async def go(db):
        out = await vocab.learn(db, admin(), [{"type": "employee", "alias": "عم طه", "record_id": 1},
                                             {"type": "farm", "alias": "المزرعة ٢", "record_id": 1}], "spoilage")
        assert out["learned"] == 0
        out = await vocab.learn(db, admin(), [{"type": "farm", "alias": "المزرعة ٢", "record_id": 1}], "harvest_sheet")
        assert out["learned"] == 1
        # An "alias" that is the record's own name, or one already known, is not saved twice.
        out = await vocab.learn(db, admin(), [{"type": "farm", "alias": "Farm 2", "record_id": 1},
                                             {"type": "farm", "alias": "المزرعه 2", "record_id": 1}], "harvest_sheet")
        assert out["learned"] == 0
        actions = [a for (a,) in (await db.execute(select(ActivityLog.action).where(
            ActivityLog.module == "Vocabulary"))).all()]
        assert actions == ["add_alias"]
    scenario(go)


def test_a_field_draft_teaches_only_when_confirmed(telegram):  # noqa: F811
    async def go(db, factory):
        d = await in_review(db, factory, telegram, text="تالف ٣ كيلو خيار بلدي", payload=TEXT_SPOILAGE)
        body = {"date": field.today_local().isoformat(), "reason": "حر",
                "items": [{"product_id": 2, "qty": 3, "farm_id": None}],
                "remember": [{"type": "product", "alias": "خيار بلدي", "record_id": 2}]}
        # Rejected: nothing learned.
        rejected = await in_review(db, factory, telegram, text="تالف ٢ كيلو خيار بلدي", payload=TEXT_SPOILAGE)
        await field.reject(db, field_admin(), rejected.id, "مكرر")
        assert (await db.execute(select(VocabularyEntry).where(VocabularyEntry.source == "learned"))).first() is None
        out = await field.confirm(db, field_admin(), d.id, body)
        assert out["learned"] == 1
        entry = (await db.execute(select(VocabularyEntry).where(VocabularyEntry.source == "learned"))).scalar_one()
        assert (entry.entity_type, entry.alias, entry.record_id) == ("product", "خيار بلدي", 2)
    field_scenario(go)


# ── CSV export / import ──────────────────────────────────────────────────────

def _csv(*rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["alias", "type", "record", "note"])
    w.writerows(rows)
    return buf.getvalue()


def test_export_lists_every_alias_and_round_trips():
    async def go(db):
        await vocab.create_entry(db, admin(), kind="product", alias="ريحان", record_id=1, note="kitchen word")
        text = await vocab.export_csv(db)
        rows = list(csv.reader(io.StringIO(text)))
        assert rows[0] == ["alias", "type", "record", "note"]
        assert ["ريحان", "product", "Organic Italian Basil", "kitchen word"] in rows
        assert ["شرم", "area", "sharm", rows[[r[0] for r in rows].index("شرم")][3]] in rows
        # Importing the export changes nothing: every row is already there.
        assert {r["status"] for r in await vocab.preview_import(db, text)} == {"duplicate"}
    scenario(go)


def test_the_preview_says_what_each_row_would_do_and_saves_nothing():
    async def go(db):
        await vocab.create_entry(db, admin(), kind="product", alias="ريحان", record_id=1)
        text = _csv(["عسل جبلي", "product", "Raw Honey 500g", ""],          # add, by name
                    ["عسل", "Product", "2", ""],                           # add, by id and by label
                    ["ريحان", "product", "Organic Italian Basil", ""],     # duplicate: already there
                    ["ريحان", "product", "Raw Honey 500g", ""],            # conflict: means basil
                    ["عسل جبلي", "product", "Raw Honey 500g", ""],         # duplicate: earlier in the file
                    ["زيت", "product", "Olive Oil", ""],                   # unmatched: no such product
                    ["x", "vehicle", "Truck", ""],                         # unmatched: unknown type
                    ["شرم الشيخ", "area", "Sharm", ""],                    # add to the existing group
                    ["مرسى علم", "area", "marsa alam", ""])                # add, as a new group
        rows = await vocab.preview_import(db, text)
        assert [r["status"] for r in rows] == ["add", "add", "duplicate", "conflict", "duplicate", "unmatched",
                                               "unmatched", "add", "add"]
        assert "already means 'Organic Italian Basil'" in rows[3]["message"]
        assert rows[8]["message"] == "New area group 'marsa alam'"
        assert (await db.execute(select(VocabularyEntry).where(VocabularyEntry.source != "seed"))).scalars().all() \
            .__len__() == 1                                                  # still only the one added above
    scenario(go)


def test_import_adds_the_new_rows_rechecked():
    async def go(db):
        text = _csv(["عسل جبلي", "product", "Raw Honey 500g", "from the shop"],
                    ["مرسى علم", "area", "marsa alam", ""],
                    ["مطار مرسى علم", "area", "Marsa Alam", ""])            # same group, written differently
        out = await vocab.import_csv(db, admin(), text)
        assert out["added"] == 3
        imported = sorted((e.entity_type, e.alias, e.record_id, e.target_key) for e in (await db.execute(
            select(VocabularyEntry).where(VocabularyEntry.source == "import"))).scalars())
        assert ("product", "عسل جبلي", 2, None) in imported
        assert {t for k, _a, _r, t in imported if k == "area"} == {"marsa alam"}
        # Running the same file again adds nothing.
        assert (await vocab.import_csv(db, admin(), text))["added"] == 0
    scenario(go)


def test_a_bad_file_is_refused_clearly():
    async def go(db):
        with pytest.raises(vocab.VocabularyError, match="header"):
            await vocab.preview_import(db, "name,thing\nx,y\n")
        with pytest.raises(vocab.VocabularyError, match="empty"):
            await vocab.preview_import(db, "﻿")
    scenario(go)


def test_import_and_learning_need_the_manage_permission():
    from tests.test_permission_enforcement import _make_client
    manager = SimpleNamespace(id=8, name="M", email="m@x", role="manager", permissions="", is_active=True)
    client, _ = _make_client(manager)
    assert client.post("/vocabulary/api/import/preview", json={"csv": "alias,type\n"}).status_code == 403
    assert client.post("/vocabulary/api/import", json={"csv": "alias,type\n"}).status_code == 403
    assert not vocab.may_learn(manager) and vocab.may_learn(admin())


def test_the_review_scripts_parse():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    for path in ("app/static/ask-actions.js", "app/static/ask-documents.js", "app/static/vocabulary.js"):
        out = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        assert out.returncode == 0, (path, out.stderr)


def test_the_remember_option_only_shows_for_a_real_correction():
    """The helper in ask-actions.js, run in node: hidden without permission, without written text, and while the
    record is still the one the server matched; shown, and sent, once the user picks a different one."""
    if not shutil.which("node"):
        pytest.skip("node not installed")
    script = r"""
    const nodes = [];
    function Node() { this.children = []; this.attrs = {}; this.hidden = false; this.checked = false; this.textContent = ""; }
    Node.prototype.append = function (...k) { this.children.push(...k); };
    Node.prototype.setAttribute = function (k, v) { this.attrs[k] = v; if (k === "hidden") this.hidden = true; };
    Node.prototype.addEventListener = function () {};
    Node.prototype.querySelector = function () { return null; };
    Object.defineProperty(Node.prototype, "className", { set(v) { this.cls = v; } });
    global.Node = Node;
    global.document = { createElement: () => { const n = new Node(); nodes.push(n); return n; } };
    global.window = {};
    require(process.argv[1]);
    const R = window.AskActions.remember;
    const out = {};
    let r = R(false, "product", "baladi lettuce", { id: 1, name: "A" }); r.update({ id: 2, name: "B" }); out.noPerm = r.el.hidden;
    r = R(true, "product", "", { id: 1, name: "A" }); r.update({ id: 2, name: "B" }); out.noText = r.el.hidden;
    r = R(true, "product", "baladi lettuce", { id: 1, name: "A" }); out.same = r.el.hidden;
    r.update({ id: 2, name: "B" }); out.changed = r.el.hidden;
    out.unticked = r.value();
    r.el.children[0].checked = true; out.ticked = r.value();
    r.update({ id: 1, name: "A" }); out.back = r.el.hidden; out.backValue = r.value();
    console.log(JSON.stringify(out));
    """
    res = subprocess.run(["node", "-e", script, "app/static/ask-actions.js"], capture_output=True, text=True,
                         cwd=".")
    if res.returncode != 0:
        # require() needs an absolute path
        import os
        res = subprocess.run(["node", "-e", script, os.path.abspath("app/static/ask-actions.js")],
                             capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["noPerm"] and out["noText"] and out["same"]
    assert out["changed"] is False and out["unticked"] is None
    assert out["ticked"] == {"type": "product", "alias": "baladi lettuce", "record_id": 2}
    assert out["back"] and out["backValue"] is None
