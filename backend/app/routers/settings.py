from __future__ import annotations
from fastapi import APIRouter, Body
from backend.core import config

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def get_settings():
    return config.load()


@router.put("")
def put_settings(cfg: dict = Body(...)):
    from fastapi import HTTPException
    try: config.save(cfg)
    except ValueError as e: raise HTTPException(422, str(e))
    return {"ok": True}
