"""Telegram field bot: linked chats, link codes, the update queue and field drafts.

Only what the bot needs is kept: the chat id, the Telegram username and the AZed person a chat is linked to — never a
phone number. Pending work (updates, album parts) lives here in the database, not in worker memory, so several
gunicorn workers can share it and a restart loses nothing.
"""
from sqlalchemy import BigInteger, Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base


class TelegramChat(Base):
    """A Telegram chat linked to one Employee or User, with what it may send."""

    __tablename__ = "telegram_chats"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(BigInteger, nullable=False, unique=True, index=True)
    username = Column(String(64))
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    # The AZed login of the same person, when they have one: a User link, or an Employee who also logs in. This is
    # what stops someone confirming their own submissions.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    allowed_kinds = Column(String(200), nullable=False, default="")   # comma separated
    daily_limit = Column(Integer, nullable=False, default=30)
    language = Column(String(2), nullable=False, default="ar")        # ar | en
    is_active = Column(Boolean, nullable=False, default=True)
    linked_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    linked_at = Column(DateTime(timezone=True), server_default=func.now())


class TelegramLinkCode(Base):
    """A one-time code an admin hands to a worker; only its hash is stored."""

    __tablename__ = "telegram_link_codes"

    id = Column(Integer, primary_key=True, index=True)
    code_hash = Column(String(64), nullable=False, unique=True, index=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    allowed_kinds = Column(String(200), nullable=False, default="")
    daily_limit = Column(Integer, nullable=False, default=30)
    language = Column(String(2), nullable=False, default="ar")
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    used_by_chat_id = Column(BigInteger, nullable=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TelegramChatGuard(Base):
    """Per chat and day: wrong link codes tried, and whether an unlinked chat was already told how to link."""

    __tablename__ = "telegram_chat_guard"

    chat_id = Column(BigInteger, primary_key=True)
    day = Column(Date, nullable=False)
    wrong_codes = Column(Integer, nullable=False, default=0)
    told_unlinked = Column(Boolean, nullable=False, default=False)


class TelegramUpdate(Base):
    """One update from Telegram, queued for processing. ``update_id`` is the primary key, so a retried update is
    stored — and processed — once. The payload is cleared once processed."""

    __tablename__ = "telegram_updates"

    update_id = Column(BigInteger, primary_key=True)
    chat_id = Column(BigInteger, nullable=True, index=True)
    media_group_id = Column(String(64), nullable=True, index=True)
    status = Column(String(12), nullable=False, default="pending", index=True)  # pending|processing|done|failed
    claim = Column(String(32), nullable=True, index=True)
    attempts = Column(Integer, nullable=False, default=0)
    payload = Column(Text, nullable=True)
    received_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)


class FieldDraft(Base):
    """Something a worker sent, read into a draft. Nothing is recorded until a manager confirms it in the Field inbox.

    status: awaiting_worker (the bot showed its summary) → in_review → confirmed | rejected; or cancelled / expired.
    The images are in document_files under ``doc_key`` — linked to records only when confirmed.
    """

    __tablename__ = "field_drafts"

    id = Column(Integer, primary_key=True, index=True)
    chat_id = Column(BigInteger, nullable=False, index=True)
    kind = Column(String(30), nullable=False)
    status = Column(String(20), nullable=False, default="awaiting_worker", index=True)
    draft_json = Column(Text, nullable=False)
    doc_key = Column(String(36), nullable=True, index=True)
    source_text = Column(Text, nullable=True)           # the worker's text or caption
    worker_note = Column(Text, nullable=True)           # replies to the bot's summary
    warnings = Column(Text, nullable=True)              # JSON list
    model_call = Column(Boolean, nullable=False, default=True)
    bot_message_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    sent_for_review_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reject_reason = Column(String(300), nullable=True)
    result_refs = Column(String(300), nullable=True)
    images_deleted_at = Column(DateTime(timezone=True), nullable=True)
