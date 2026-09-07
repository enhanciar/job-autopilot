"""Dashboard access to the master profile and answer bank, so a new person can set the system up with their own resume."""
from __future__ import annotations
import time
from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
import yaml
from backend.app.db import get_db
from backend.app.models import Application
from backend.core import config, profile

router = APIRouter(prefix="/profile", tags=["profile"])
UPLOADS = config.DATA / "uploads"
MAX_UPLOAD = 10 * 1024 * 1024


def _stale(db: Session, fp: str) -> int:
    rows = db.query(Application).filter(Application.status.in_(["pending_review", "approved"])).all()
    return sum(1 for a in rows if (a.answers or {}).get("profile_hash") != fp)


@router.get("")
def get_profile(db: Session = Depends(get_db)):
    prof = profile.load(); fp = profile.fingerprint()
    base = prof.get("resume_base") or ""
    return {"profile": prof, "yaml": yaml.safe_dump(prof, sort_keys=False, allow_unicode=True), "fingerprint": fp,
            "identity": {"name": prof.get("identity", {}).get("name"), "headline": prof.get("identity", {}).get("headline")},
            "resume_base": base if base and (config.ROOT / base).exists() else None,
            "answers_yaml": yaml.safe_dump(profile.answers(), sort_keys=False, allow_unicode=True), "answers_count": len(profile.answers()),
            "stale_applications": _stale(db, fp)}


@router.put("")
def put_profile(body: dict = Body(...), db: Session = Depends(get_db)):
    try:
        prof = yaml.safe_load(body.get("yaml") or "") if "yaml" in body else body.get("profile")
    except yaml.YAMLError as e:
        raise HTTPException(422, f"YAML could not be parsed: {str(e)[:200]}")
    errors = profile.validate_profile(prof)
    if errors: raise HTTPException(422, "; ".join(errors))
    if isinstance(prof.get("preferences"), dict): prof["preferences"].pop("_review", None)
    result = profile.save(prof)
    return {**result, "stale_applications": _stale(db, result["fingerprint"])}


@router.put("/answers")
def put_answers(body: dict = Body(...), db: Session = Depends(get_db)):
    try:
        rows = yaml.safe_load(body.get("yaml") or "") or []
    except yaml.YAMLError as e:
        raise HTTPException(422, f"YAML could not be parsed: {str(e)[:200]}")
    errors = profile.validate_answers(rows)
    if errors: raise HTTPException(422, "; ".join(errors))
    result = profile.save_answers(rows)
    return {**result, "stale_applications": _stale(db, result["fingerprint"])}


@router.post("/resume")
async def upload_resume(file: UploadFile = File(...)):
    """Store the resume locally and return a DRAFT profile extracted from it. Nothing is saved until PUT /profile."""
    suffix = "." + (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    if suffix not in (".pdf", ".docx", ".txt", ".md"): raise HTTPException(422, "Upload a PDF, DOCX or text resume")
    data = await file.read()
    if len(data) > MAX_UPLOAD: raise HTTPException(413, "Resume must be under 10 MB")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    path = UPLOADS / f"resume-{time.strftime('%Y%m%d-%H%M%S')}{suffix}"
    path.write_bytes(data)
    try:
        text = profile.resume_text(path)
        draft = profile.draft_from_resume(text, profile.load(), str(path.relative_to(config.ROOT)) if path.is_relative_to(config.ROOT) else str(path))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:  # noqa: BLE001 — LLM/provider failures
        raise HTTPException(502, f"Could not extract a profile: {type(e).__name__}: {str(e)[:200]}")
    return {"resume_path": str(path), "text_chars": len(text), "draft": draft, "yaml": yaml.safe_dump(draft, sort_keys=False, allow_unicode=True),
            "errors": profile.validate_profile(draft)}
