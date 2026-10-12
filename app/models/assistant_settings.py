from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.sql import func

from app.database import Base


class AssistantSettings(Base):
    """Settings for the AI provider — one row (id 1), edited by an admin on the Ask page.

    ``provider_mode``: "auto" (the first provider, then the fallback when it doesn't answer), "primary" (the first
    provider only) or "fallback" (the fallback only). Every model call — Ask, document reading, the field bot — uses it.
    """

    __tablename__ = "assistant_settings"

    id = Column(Integer, primary_key=True)
    provider_mode = Column(String(10), nullable=False, default="auto")
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
