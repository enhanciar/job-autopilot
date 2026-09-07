from __future__ import annotations
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.models import PlatformSession
from backend.core import registry, llm, runner, browser, google_auth

router = APIRouter(prefix="/platforms", tags=["platforms"])


@router.get("")
def list_platforms(db: Session = Depends(get_db)):
    sess = {s.platform: s for s in db.query(PlatformSession).all()}
    out = []
    for p in registry.PLATFORMS:
        key = browser.login_key(p["key"])
        s = sess.get(key)
        out.append({**p, "implemented": p["key"] in registry.COLLECTORS or p["key"] in registry.SKILLS or p["key"] in ("gmail", "sheets"),
                    "login_supported": key in browser.LOGIN_MARKERS and p["type"] == "skill",
                    "configured": (google_auth.configured() and google_auth.authorized()) if p["key"] == "gmail" else google_auth.sheets_configured() if p["key"] == "sheets" else None,
                    "logged_in": (s.logged_in if s else None), "session_note": (s.note if s else None), "last_checked": (s.last_checked if s else None)})
    return out


@router.get("/llm")
def llm_status():
    return llm.providers_status()


@router.post("/{key}/login")
def open_login(key: str):
    """Open the platform's persistent Chrome profile so the user can log in; waits and records the result."""
    from fastapi import HTTPException
    key = browser.login_key(key)
    if key not in browser.LOGIN_MARKERS: raise HTTPException(422, "Unknown login platform")
    return {"run_id": runner.enqueue("login", key)}
