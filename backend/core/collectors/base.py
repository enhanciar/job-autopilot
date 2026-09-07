from __future__ import annotations
import httpx
from backend.app.db import session
from backend.core.normalize import upsert

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36 job-autopilot/0.1"


def client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(90, connect=20), headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}, follow_redirects=True)


def ingest(ctx, source: str, items: list[dict]):
    """items: list of kwargs for normalize.upsert (without source)."""
    new = 0
    with session() as db:
        for it in items:
            try:
                with db.begin_nested():          # savepoint per item: one bad row never kills the batch
                    _, created = upsert(db, source=source, **it)
                    db.flush()
                new += int(created)
            except Exception as e:  # noqa: BLE001
                ctx.log("warn", f"{source}: skip item: {str(e)[:160]}", platform=source)
    ctx.bump("seen", len(items)); ctx.bump("new", new)
    ctx.log("info", f"{source}: {len(items)} seen, {new} new", platform=source)
    return new
