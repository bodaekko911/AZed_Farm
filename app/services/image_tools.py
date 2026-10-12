"""Evidence images: small grayscale JPEGs, made with as little memory as possible.

Only the Telegram path uses this — a web upload is converted in the browser, so the server does no image work there.
Telegram photos are already downloaded at about 1600 px, so this mostly re-encodes; a JPEG sent as a file is decoded
at reduced size with Pillow's draft mode (the decoder skips detail it would throw away), and a PDF page is rendered
straight at the target size. Pillow and pypdfium2 are imported only when needed.
"""
from __future__ import annotations

import base64
import io

MAX_SIDE = 1600
QUALITY = 70
TARGET_BYTES = 200 * 1024
MAX_PDF_PAGES = 10


def _encode(img) -> bytes:
    """JPEG at QUALITY, stepping down only if it is still over TARGET_BYTES."""
    for quality in (QUALITY, 60, 50):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= TARGET_BYTES:
            return data
    return data


def to_evidence_jpeg(raw: bytes) -> bytes:
    """Any photo Pillow can open → grayscale JPEG, at most MAX_SIDE px on the long side."""
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(raw)) as img:
        if img.format == "JPEG":
            # Decode at reduced size (1/2, 1/4 …), already gray. Draft compares each side, so ask for the target
            # size in the photo's own shape — a square request would keep a landscape photo at full size.
            w, h = img.size
            scale = MAX_SIDE / max(w, h)
            if scale < 1:
                img.draft("L", (max(1, int(w * scale)), max(1, int(h * scale))))
            else:
                img.draft("L", (w, h))
        img = ImageOps.exif_transpose(img)            # upright, as the phone showed it
        img = img.convert("L")
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE))
        return _encode(img)


def pdf_to_jpegs(raw: bytes, max_pages: int = MAX_PDF_PAGES) -> list[bytes]:
    """Each page rendered straight at about MAX_SIDE px on the long side, gray — one page in memory at a time."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(raw)
    try:
        if len(pdf) > max_pages:
            raise ValueError(f"The PDF has {len(pdf)} pages — at most {max_pages}.")
        out = []
        for index in range(len(pdf)):
            page = pdf[index]
            width, height = page.get_size()
            bitmap = page.render(scale=MAX_SIDE / max(width, height), grayscale=True)
            img = bitmap.to_pil().convert("L")
            out.append(_encode(img))
            img.close(), bitmap.close(), page.close()
        return out
    finally:
        pdf.close()


def data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
