"""Human pacing for browser skills. Random delays, curved mouse paths, jittered typing, business hours, daily caps.
Nothing here bypasses bot checks; it only paces the real logged-in user's actions."""
from __future__ import annotations
import math, random, time
from datetime import datetime, timedelta
from sqlalchemy import func
from backend.core import config
from backend.app.db import session
from backend.app.models import ActionLog


import threading
_local = threading.local()
FAST = {"action_delay_s": [0.3, 1.0], "between_items_s": [1, 3], "idle_pause_chance": 0.0, "idle_pause_s": [0, 0], "typing_cps": [30, 60]}


def set_pacing(name: str | None):
    """'fast' for company ATS forms (no rate limits); None/'human' for logged-in platforms."""
    _local.pacing = name


def _cfg():
    base = config.load().get("humanize", {})
    if getattr(_local, "pacing", None) == "fast":
        return {**base, **FAST}
    return base


def gauss_range(lo: float, hi: float) -> float:
    mu, sigma = (lo + hi) / 2, (hi - lo) / 4
    return max(lo, min(hi, random.gauss(mu, sigma)))


def pause(kind: str = "action_delay_s"):
    lo, hi = _cfg().get(kind, [2, 8])
    time.sleep(gauss_range(lo, hi))
    c = _cfg()
    if random.random() < c.get("idle_pause_chance", 0.08):
        ilo, ihi = c.get("idle_pause_s", [15, 60])
        time.sleep(gauss_range(ilo, ihi))


def in_business_hours(now: datetime | None = None) -> bool:
    bh = _cfg().get("business_hours", {"start": 9, "end": 19})
    h = (now or datetime.now()).hour
    return bh["start"] <= h < bh["end"]


def _bezier(p0, p1, p2, p3, t):
    return ((1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0],
            (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1])


def human_move(page, x: float, y: float, start: tuple[float, float] | None = None):
    """Move the mouse along a curved path with variable speed."""
    if getattr(_local, "pacing", None) == "fast":
        page.mouse.move(x, y); return
    sx, sy = start or (random.uniform(0, 800), random.uniform(0, 600))
    dist = math.hypot(x - sx, y - sy)
    steps = max(12, int(dist / 12))
    c1 = (sx + random.uniform(-100, 100), sy + random.uniform(-100, 100))
    c2 = (x + random.uniform(-80, 80), y + random.uniform(-80, 80))
    for i in range(1, steps + 1):
        t = i / steps
        t_e = t * t * (3 - 2 * t)  # ease in-out
        px, py = _bezier((sx, sy), c1, c2, (x, y), t_e)
        page.mouse.move(px, py)
        time.sleep(random.uniform(0.004, 0.02))


def human_click(page, locator, *, jitter: int = 4):
    """Scroll into view, move along a curve, tiny jitter, click.

    The scroll is capped: an element inside a form that will not settle used to hold the default 30 seconds, so one
    stubborn field could cost half an hour across a form. If it will not scroll, click it anyway.
    """
    try:
        locator.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    time.sleep(random.uniform(0.2, 0.7))
    try:
        box = locator.bounding_box(timeout=2000)
    except Exception:
        box = None
    if not box:
        locator.click(timeout=5000)
        return
    x = box["x"] + box["width"] / 2 + random.uniform(-jitter, jitter)
    y = box["y"] + box["height"] / 2 + random.uniform(-jitter, jitter)
    human_move(page, x, y)
    time.sleep(random.uniform(0.05, 0.25))
    page.mouse.down(); time.sleep(random.uniform(0.04, 0.12)); page.mouse.up()


PASTE_THRESHOLD = 120   # longer texts are pasted (insert_text) instead of typed key by key


def human_type(page, locator, text: str):
    """Short fields: type with per-character jitter and occasional typo + backspace. Long text (cover notes, answers): paste in one go
    after a brief think-pause, like a person pasting from their notes."""
    human_click(page, locator)
    if len(text) > PASTE_THRESHOLD:
        time.sleep(random.uniform(0.6, 1.6))
        try:
            page.keyboard.insert_text(text)
        except Exception:
            locator.fill(text)
        time.sleep(random.uniform(0.3, 0.9))
        return
    lo, hi = _cfg().get("typing_cps", [6, 14])
    for ch in text:
        if random.random() < 0.02 and ch.isalpha():
            page.keyboard.type(random.choice("asdfghjkl"))
            time.sleep(random.uniform(0.1, 0.3))
            page.keyboard.press("Backspace")
        page.keyboard.type(ch)
        time.sleep(1 / gauss_range(lo, hi))
        if ch in " .,\n" and random.random() < 0.15:
            time.sleep(random.uniform(0.2, 0.8))


def human_scroll(page, amount: int | None = None):
    total = amount or random.randint(300, 1200)
    done = 0
    while done < total:
        step = random.randint(80, 220)
        page.mouse.wheel(0, step)
        done += step
        time.sleep(random.uniform(0.05, 0.2))
    time.sleep(random.uniform(0.3, 1.2))


# ---- daily caps ----------------------------------------------------------

def count_today(platform: str, action: str) -> int:
    start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with session() as db:
        return db.query(func.count(ActionLog.id)).filter(ActionLog.platform == platform, ActionLog.action == action, ActionLog.ts >= start).scalar() or 0


def cap_for(platform: str, action: str) -> int | None:
    return (config.load().get("caps", {}).get(platform) or {}).get(action)


def under_cap(platform: str, action: str) -> bool:
    cap = cap_for(platform, action)
    return cap is None or count_today(platform, action) < cap


def record(platform: str, action: str, ref: str | None = None):
    with session() as db:
        db.add(ActionLog(platform=platform, action=action, ref=ref))


def caps_snapshot() -> dict:
    out = {}
    for platform, actions in (config.load().get("caps") or {}).items():
        out[platform] = {a: {"used": count_today(platform, a), "cap": c} for a, c in actions.items()}
    return out


def weekly_cap_for(platform: str, action: str):
    """A rolling seven-day ceiling, for platforms that count that way.

    LinkedIn allows roughly 100 invitations per rolling week on a free or Premium account, and counts them from your
    first invitation of the week rather than from Monday. A daily cap alone cannot express that: 40 a day is 280 a week,
    which trips the limit in under three days and risks a restriction on the account you actually need.
    """
    caps = config.load().get("weekly_caps", {}).get(platform, {})
    return caps.get(action)


def take(platform: str, action: str, ref: str | None = None) -> bool:
    """Atomically reserve an attempt, across threads and worker processes, against both the daily and weekly ceilings."""
    from sqlalchemy import text
    from datetime import timedelta
    now = datetime.utcnow()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    cap, weekly = cap_for(platform, action), weekly_cap_for(platform, action)
    with session() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        rows = db.query(ActionLog).filter(ActionLog.platform == platform, ActionLog.action == action)
        if cap is not None and rows.filter(ActionLog.ts >= start).count() >= cap: return False
        if weekly is not None and rows.filter(ActionLog.ts >= week_start).count() >= weekly: return False
        db.add(ActionLog(platform=platform, action=action, ref=ref))
    return True
