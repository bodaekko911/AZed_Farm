from sqlalchemy import Column, DateTime, ForeignKey, Integer, LargeBinary, String
from sqlalchemy.sql import func

from app.database import Base


class DocumentFile(Base):
    """One image of a paper document read by Ask — a receipt, a delivery note, a harvest sheet — kept as evidence.

    The images live in the database, not on disk: the app's own folder is wiped on every deploy, and anything under
    /static is served to anyone with the link. Here they are backed up with the records they support and only served
    through a logged-in route that checks the matching page permission (see app/routers/documents.py).

    ``doc_key`` groups the images of one document (a long receipt photographed in two parts). What is stored is the
    resized JPEG the model read, and ``sha256`` is its hash — the record was made from exactly this image.
    """

    __tablename__ = "document_files"

    id = Column(Integer, primary_key=True, index=True)
    doc_key = Column(String(36), nullable=False, index=True)
    page_no = Column(Integer, nullable=False, default=1)
    kind = Column(String(30), nullable=False)                 # expense_receipt | delivery_note | harvest_sheet
    content_type = Column(String(40), nullable=False, default="image/jpeg")
    size_bytes = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    data = Column(LargeBinary, nullable=False)
    filename = Column(String(150))
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DocumentLink(Base):
    """Which record a document supports. One document can back several records — a receipt split into one expense
    per category, or a delivery note received as several product lines."""

    __tablename__ = "document_links"

    id = Column(Integer, primary_key=True, index=True)
    doc_key = Column(String(36), nullable=False, index=True)
    ref_type = Column(String(30), nullable=False)             # expense | product_receipt | farm_delivery
    ref_id = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
