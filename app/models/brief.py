from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from app.database import Base


class WeeklyBriefSettings(Base):
    """The weekly brief e-mailed to stakeholders — one row (id 1), edited by an admin on the Ask page.

    ``send_weekday`` is 0=Monday … 6=Sunday, in the farm's local time. The brief covers the 7 days ending the day
    before it is sent. ``last_sent_week`` is the last day of the week that last went out (YYYY-MM-DD); claiming a
    week is one conditional UPDATE on it, which is what lets exactly one worker send it."""

    __tablename__ = "weekly_brief_settings"

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    send_weekday = Column(Integer, nullable=False, default=5)        # Saturday
    send_time = Column(String(5), nullable=False, default="09:00")   # HH:MM, local
    recipients = Column(Text, nullable=False, default="")
    include_ai_summary = Column(Boolean, nullable=False, default=True)
    last_sent_week = Column(String(10))
    last_status = Column(String(300))
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
