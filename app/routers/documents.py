"""Source documents — the photos and scans Ask read, kept as evidence for the records made from them.

Served only to a logged-in user who may open the page of a record the document supports: an expense's receipt needs
the Expenses page, a receive line's delivery note the Receive page, a farm delivery's harvest sheet the Farm page.
Nothing here lives under /static, which is public.
"""
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import get_current_user, has_permission
from app.database import get_async_session
from app.models.document import DocumentFile, DocumentLink
from app.models.user import User

router = APIRouter(prefix="/documents", tags=["Documents"])

# Which page a record belongs to — seeing the record's source document needs that page.
PAGE_FOR = {"expense": "page_expenses", "product_receipt": "page_receive_products", "farm_delivery": "page_farm"}


def _may_see(user: User, ref_types: set[str]) -> bool:
    return any(has_permission(user, PAGE_FOR[t]) for t in ref_types if t in PAGE_FOR)


@router.get("/file/{file_id}")
async def document_file(file_id: int, db: AsyncSession = Depends(get_async_session),
                        user: User = Depends(get_current_user)):
    f = (await db.execute(select(DocumentFile).where(DocumentFile.id == file_id))).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="Not found")
    ref_types = {t for (t,) in (await db.execute(
        select(DocumentLink.ref_type).where(DocumentLink.doc_key == f.doc_key))).all()}
    if not _may_see(user, ref_types):
        raise HTTPException(status_code=404, detail="Not found")      # don't confirm it exists
    return Response(content=f.data, media_type="image/jpeg", headers={
        "Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="document-{f.id}.jpg"'})


@router.get("/view", response_class=HTMLResponse)
async def document_view(ref_type: str, ref_id: int, db: AsyncSession = Depends(get_async_session),
                        user: User = Depends(get_current_user)):
    """Every image of the document(s) behind one record, in a plain page that prints well."""
    if ref_type not in PAGE_FOR or not has_permission(user, PAGE_FOR[ref_type]):
        raise HTTPException(status_code=404, detail="Not found")
    keys = [k for (k,) in (await db.execute(select(DocumentLink.doc_key).where(
        DocumentLink.ref_type == ref_type, DocumentLink.ref_id == ref_id))).all()]
    files = (await db.execute(select(DocumentFile).where(DocumentFile.doc_key.in_(keys))
                              .order_by(DocumentFile.created_at, DocumentFile.page_no))).scalars().all() if keys else []
    if not files:
        raise HTTPException(status_code=404, detail="No source document for this record.")
    first = files[0]
    when = first.created_at.strftime("%Y-%m-%d %H:%M") if first.created_at else ""
    pages = "".join(
        f"<figure><img src='/documents/file/{f.id}' alt='Page {f.page_no}'>"
        f"<figcaption>Page {f.page_no} · {f.size_bytes // 1024} KB · SHA-256 {escape(f.sha256[:16])}…</figcaption>"
        f"</figure>" for f in files)
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="/static/theme-init.js"></script>
<title>Source document</title>
<style>
body{{font-family:'DM Sans',system-ui,sans-serif;background:#f4f5ef;color:#1a1e14;margin:0;padding:20px}}
h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#667;font-size:13px;margin-bottom:16px}}
figure{{margin:0 0 18px;background:#fff;border:1px solid #dde;border-radius:8px;padding:10px}}
img{{max-width:100%;height:auto;display:block;margin:0 auto}}
figcaption{{font-size:11.5px;color:#778;margin-top:6px;font-family:monospace}}
@media print{{body{{background:#fff;padding:0}} figure{{border:0;page-break-inside:avoid}}}}
</style></head><body>
<h1>Source document</h1>
<div class="sub">{escape(ref_type.replace('_', ' '))} #{ref_id} · read by Ask on {escape(when)}
 · {len(files)} image{'s' if len(files) != 1 else ''}</div>
{pages}
</body></html>""")
