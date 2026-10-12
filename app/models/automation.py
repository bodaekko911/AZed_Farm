"""Automations: rules that run on their own and e-mail what they find (managed on the Ask page, admins only).

* AutomationRule     — one per rule key: on/off, schedule or "events", settings, recipients, language, and the
                        claim columns that let exactly one gunicorn worker run each scheduled period.
* AutomationEvent    — one detected item waiting to be e-mailed in a digest; ``ref`` is unique per rule, so the same
                        record is never reported twice; ``run_id`` is set when a run claims it for sending.
* AutomationRun      — the run log (kept 90 days).
* AutomationSettings — one row: the daily e-mail cap, and the day the Gmail relay said its daily limit was reached.

Automations only read business data and send e-mail; these tables are their own bookkeeping.
"""
from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class AutomationRule(Base):
    __tablename__ = "automation_rules"

    key = Column(String(40), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    trigger = Column(String(10), nullable=False, default="daily")     # daily | weekly | monthly | events
    send_time = Column(String(5), nullable=False, default="07:00")    # HH:MM, local
    weekday = Column(Integer, nullable=False, default=5)              # 0 = Monday … 6 = Sunday
    month_day = Column(Integer, nullable=False, default=1)            # 1–28
    settings = Column(Text, nullable=True)                            # JSON: the rule's thresholds
    recipients = Column(Text, nullable=True)                          # empty = the brief's recipients
    language = Column(String(2), nullable=False, default="en")        # en | ar
    digest_minutes = Column(Integer, nullable=False, default=60)
    watermark = Column(Integer, nullable=True)                        # last record id an event rule looked at
    last_period = Column(String(20), nullable=True)                   # claim key of the last scheduled run
    last_digest_at = Column(DateTime(timezone=True), nullable=True)
    last_status = Column(String(300), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AutomationEvent(Base):
    __tablename__ = "automation_events"
    __table_args__ = (UniqueConstraint("rule_key", "ref", name="uq_automation_event_ref"),)

    id = Column(Integer, primary_key=True, index=True)
    rule_key = Column(String(40), nullable=False, index=True)
    ref = Column(String(80), nullable=False)
    payload = Column(Text, nullable=False)                            # JSON
    urgent = Column(Boolean, nullable=False, default=False)
    held = Column(Boolean, nullable=False, default=False)             # held back by the cap / Gmail's limit
    run_id = Column(Integer, nullable=True, index=True)               # the run that sent (or is sending) it
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class AutomationRun(Base):
    __tablename__ = "automation_runs"

    id = Column(Integer, primary_key=True, index=True)
    rule_key = Column(String(40), nullable=False, index=True)
    period = Column(String(20), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(12), nullable=False, default="running")    # running | sent | nothing | failed | capped | gmail_limit | test
    items = Column(Integer, nullable=False, default=0)
    recipients = Column(Text, nullable=True)
    error = Column(String(300), nullable=True)


class AutomationSettings(Base):
    __tablename__ = "automation_settings"

    id = Column(Integer, primary_key=True)
    daily_cap = Column(Integer, nullable=False, default=40)
    gmail_limit_day = Column(Date, nullable=True)                     # the relay refused for its daily limit on this day
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
