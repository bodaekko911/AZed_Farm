/**
 * Gmail relay for AZed Farm's weekly brief.
 *
 * Railway's Hobby plan blocks outgoing SMTP, so the app can't log in to Gmail directly. This script runs inside the
 * sending Gmail account instead: the app POSTs the e-mail to it over HTTPS and Gmail sends it from this account.
 *
 * Set up (once, signed in as the Gmail account that should SEND the brief):
 *   1. Go to https://script.google.com → New project. Delete what's there and paste this whole file.
 *   2. Replace CHANGE-ME below with a long random secret (e.g. 40 random letters and digits). Keep it private.
 *   3. Give it permission to send mail: in the toolbar's function list pick "authorize", click Run, then
 *      Review permissions → your account → Advanced → Go to … (unsafe) → Allow. (Without this every send fails
 *      with "You do not have permission to call MailApp".)
 *   4. Deploy → New deployment → type "Web app".
 *        Execute as: Me            Who has access: Anyone
 *      Click Deploy, allow the permissions Google asks for, and copy the Web app URL (ends in /exec).
 *   5. In Railway → AZed Farm → Variables add:
 *        MAIL_RELAY_URL    = that /exec URL
 *        MAIL_RELAY_SECRET = the same secret as below
 *
 * After changing this code, Deploy → Manage deployments → Edit → Version: New version → Deploy (same URL).
 *
 * "Anyone" only lets someone who has BOTH the URL and the secret send mail; a wrong secret is refused.
 * Quota: a normal Gmail account can send to about 100 recipients a day this way (Google Workspace: 1,500).
 */
const SECRET = "CHANGE-ME";

/** Run this once from the editor (Run ▶) to grant the "send mail" permission. */
function authorize() {
  Logger.log("Mail quota left today: " + MailApp.getRemainingDailyQuota());
}

function doPost(e) {
  try {
    return handle(e);
  } catch (err) {
    return reply({ ok: false, error: String(err && err.message || err) });
  }
}

function handle(e) {
  let data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply({ ok: false, error: "bad request" });
  }
  if (!SECRET || SECRET === "CHANGE-ME" || data.secret !== SECRET) {
    return reply({ ok: false, error: "wrong secret" });
  }
  const bcc = (data.bcc || []).filter(function (a) { return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(a); });
  if (!bcc.length) return reply({ ok: false, error: "no recipients" });
  if (MailApp.getRemainingDailyQuota() < bcc.length) {
    return reply({ ok: false, error: "Gmail daily sending quota used up — try tomorrow" });
  }
  MailApp.sendEmail({
    to: Session.getEffectiveUser().getEmail(),   // to itself; the stakeholders are Bcc'd
    bcc: bcc.join(","),
    subject: String(data.subject || "").slice(0, 250),
    body: String(data.text || ""),
    htmlBody: String(data.html || ""),
    name: String(data.name || "AZed Farm").slice(0, 80),
  });
  return reply({ ok: true, sent: bcc.length });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
