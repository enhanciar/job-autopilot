from __future__ import annotations
from datetime import datetime
from sqlalchemy import String, Integer, Float, Text, DateTime, Boolean, ForeignKey, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from backend.app.db import Base


def now() -> datetime:
    return datetime.utcnow()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(40), index=True)        # remoteok, greenhouse, linkedin ...
    ats: Mapped[str | None] = mapped_column(String(40), index=True)    # greenhouse/lever/ashby/... detected from apply_url
    company: Mapped[str] = mapped_column(String(200), index=True)
    company_domain: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(Text)
    apply_url: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(300))
    remote_scope: Mapped[str | None] = mapped_column(String(200))      # worldwide / india / us-only / onsite
    country: Mapped[str | None] = mapped_column(String(60), index=True)   # derived from location (normalize.country_of)
    employment_type: Mapped[str | None] = mapped_column(String(60))
    salary: Mapped[str | None] = mapped_column(String(200))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSON)
    sponsor_flag: Mapped[bool] = mapped_column(Boolean, default=False)  # company on a sponsor register or JD mentions sponsorship
    eligible: Mapped[bool | None] = mapped_column(Boolean)              # passed hard rules
    eligibility_reason: Mapped[str | None] = mapped_column(Text)
    fit_score: Mapped[int | None] = mapped_column(Integer, index=True)
    fit_reasons: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="new", index=True)  # new, filtered, scored, queued, applied, skipped, expired
    raw: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    applications: Mapped[list["Application"]] = relationship(back_populates="job")


class Application(Base):
    claim_run_id: Mapped[int | None] = mapped_column(Integer)
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40))                 # greenhouse, linkedin, email, wellfound ...
    method: Mapped[str] = mapped_column(String(30))                   # ats_form, easy_apply, email, platform_apply, dm
    status: Mapped[str] = mapped_column(String(30), default="pending_review", index=True)
    # pending_review, approved, submitting, submitted, failed, rejected_by_user, needs_human, replied, interview, rejected, offer
    resume_path: Mapped[str | None] = mapped_column(Text)
    cover_note: Mapped[str | None] = mapped_column(Text)
    answers: Mapped[dict | None] = mapped_column(JSON)                # screening Q&A used
    screenshot_before: Mapped[str | None] = mapped_column(Text)
    screenshot_after: Mapped[str | None] = mapped_column(Text)
    confirmation_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    job: Mapped["Job"] = relationship(back_populates="applications")


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(200), index=True)
    name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    email_confidence: Mapped[str | None] = mapped_column(String(20))  # found, pattern, unknown
    linkedin_url: Mapped[str | None] = mapped_column(Text)
    x_handle: Mapped[str | None] = mapped_column(String(100))
    source: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Outreach(Base):
    claim_run_id: Mapped[int | None] = mapped_column(Integer)
    __tablename__ = "outreach"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), index=True)
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    channel: Mapped[str] = mapped_column(String(30))                  # email, linkedin_connect, linkedin_dm, x_dm, x_reply, reddit
    step: Mapped[int] = mapped_column(Integer, default=1)            # 1 initial, 2 follow-up, 3 final
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending_review", index=True)
    # pending_review, approved, sent, failed, replied, bounced, stopped
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime)
    thread_id: Mapped[str | None] = mapped_column(String(200))
    provider_message_id: Mapped[str | None] = mapped_column(String(200))   # Gmail message id / tweet id: reconciliation evidence
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Run(Base):
    """One execution of a collector or skill."""
    __tablename__ = "runs"
    spec: Mapped[dict | None] = mapped_column(JSON)
    checkpoints: Mapped[list | None] = mapped_column(JSON)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))                     # collector, skill, pipeline
    name: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running, done, failed, paused_for_human, stopped
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    stats: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    events: Mapped[list["Event"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")     # info, warn, error, human
    platform: Mapped[str | None] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON)
    screenshot: Mapped[str | None] = mapped_column(Text)
    run: Mapped["Run"] = relationship(back_populates="events")


class PlatformSession(Base):
    """Login state per platform for the persistent browser profile."""
    __tablename__ = "platform_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(40), unique=True)
    logged_in: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime)
    last_used: Mapped[datetime | None] = mapped_column(DateTime)
    note: Mapped[str | None] = mapped_column(Text)


class Question(Base):
    """A screening question the system could not answer, kept so you can answer it once and never see it again."""
    __tablename__ = "questions"
    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)   # normalised text: groups rewordings
    text: Mapped[str] = mapped_column(Text)                            # the clearest wording seen so far
    options: Mapped[list | None] = mapped_column(JSON)                 # visible choices, when it was a dropdown/radio
    companies: Mapped[list | None] = mapped_column(JSON)               # who asked it
    job_ids: Mapped[list | None] = mapped_column(JSON)                 # the postings it came from, so we can go and look
    times_seen: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)     # open, answered, skipped
    answer: Mapped[str | None] = mapped_column(Text)
    rule_match: Mapped[str | None] = mapped_column(Text)               # regex written into the answer bank
    stored_in: Mapped[str | None] = mapped_column(String(20))          # answers, capabilities, declarations
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChatTurn(Base):
    """The question-answering conversation, kept so the page survives a refresh."""
    __tablename__ = "chat_turns"
    id: Mapped[int] = mapped_column(primary_key=True)
    role: Mapped[str] = mapped_column(String(12))                      # user, assistant
    text: Mapped[str] = mapped_column(Text)
    question_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ActionLog(Base):
    """Every capped action, for daily cap accounting."""
    __tablename__ = "action_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    platform: Mapped[str] = mapped_column(String(40), index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    ref: Mapped[str | None] = mapped_column(String(200))


Index("ix_action_day", ActionLog.platform, ActionLog.action, ActionLog.ts)
