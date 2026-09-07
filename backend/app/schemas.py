from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class JobOut(ORM):
    id: int; source: str; ats: str | None; company: str; title: str; url: str; apply_url: str | None
    location: str | None; remote_scope: str | None; employment_type: str | None; salary: str | None
    posted_at: datetime | None; sponsor_flag: bool; eligible: bool | None; eligibility_reason: str | None
    fit_score: int | None; fit_reasons: str | None; status: str; tags: list | None; created_at: datetime


class JobDetail(JobOut):
    description: str | None


class ApplicationOut(ORM):
    id: int; job_id: int; platform: str; method: str; status: str; resume_path: str | None; cover_note: str | None
    answers: dict | None; screenshot_before: str | None; screenshot_after: str | None; confirmation_text: str | None
    error: str | None; submitted_at: datetime | None; created_at: datetime
    company: str | None = None; title: str | None = None; url: str | None = None


class OutreachOut(ORM):
    id: int; job_id: int | None; contact_id: int | None; channel: str; step: int; subject: str | None; body: str | None
    status: str; scheduled_for: datetime | None; sent_at: datetime | None; replied_at: datetime | None; error: str | None; created_at: datetime
    contact_name: str | None = None; contact_title: str | None = None; company: str | None = None


class RunOut(ORM):
    id: int; kind: str; name: str; status: str; started_at: datetime; ended_at: datetime | None; stats: dict | None; error: str | None


class EventOut(ORM):
    id: int; run_id: int | None; ts: datetime; level: str; platform: str | None; message: str; data: dict | None; screenshot: str | None


class StatusUpdate(BaseModel):
    status: str
    note: str | None = None


class TriggerRun(BaseModel):
    params: dict = {}
