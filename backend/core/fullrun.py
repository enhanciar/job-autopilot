"""End-to-end run for one platform, or for every platform, as a single stoppable job.

A platform run is always the same chain, whatever the source:

    discover  ->  (hydrate)  ->  score  ->  prepare  ->  [your approval]  ->  apply

`discover` is the platform's own collector or skill; `hydrate` only exists for LinkedIn, whose search cards carry no
description. `score` and `prepare` are shared pipeline stages and run over everything eligible, so a platform run also
picks up anything an earlier run left unscored. Applying is deliberately NOT part of the chain while review_mode is on:
the run stops at the review queue and the apply worker submits only what you approved.

Set apply=True to continue through applying in the same run (used when review_mode is off, or when you explicitly ask).
"""
from __future__ import annotations
from backend.core import registry, config, pipeline
from backend.core.runner import RunContext

# platform key -> how to discover from it. Collectors are plain functions; skills need a mode.
SKILL_DISCOVER_MODE = {
    "linkedin": "discover", "jobright": "discover", "ycombinator": "discover", "wellfound": "discover",
    "instahyre": "discover", "cutshort": "discover", "hirist": "discover", "naukri": "discover", "peerlist": "discover",
}
# platform key -> the skill that submits applications on that platform (ATS-hosted forms go to the shared worker)
PLATFORM_APPLY = {
    "linkedin": ("linkedin", "easy_apply"), "ycombinator": ("ycombinator", "apply"), "wellfound": ("wellfound", "apply"),
    "instahyre": ("instahyre", "apply"), "cutshort": ("cutshort", "apply"), "hirist": ("hirist", "apply"),
    "naukri": ("naukri", "apply"), "peerlist": ("peerlist", "apply"),
}


def _platform_keys() -> list[str]:
    return [p["key"] for p in registry.PLATFORMS
            if p.get("status") == "ready" and p["type"] in ("collector", "skill")
            and p["key"] not in ("ats_apply", "linkedin_people", "x_outreach")]


def _run_stage(ctx: RunContext, label: str, fn, **kw):
    if ctx.should_stop() or ctx.is_paused():
        return False
    if ctx.checkpoint_done(label): return True
    ctx.log("info", f"▶ {label}")
    try:
        before = ctx.stats.get("failed", 0)
        fn(ctx, **kw)
        if ctx.should_stop() or ctx.is_paused() or ctx.stats.get("failed", 0) > before: return False
        ctx.checkpoint(label)
        return True
    except Exception as e:  # noqa: BLE001
        ctx.bump("failed")
        ctx.log("warn", f"{label} failed: {str(e)[:250]}")
        return False


def _discover(ctx: RunContext, key: str):
    """Run the platform's collector, or its skill in discover mode."""
    if key in registry.COLLECTORS:
        registry.COLLECTORS[key](ctx)
        return
    cls = registry.SKILLS.get(key)
    if not cls:
        ctx.log("warn", f"{key}: nothing to discover with"); return
    cls(ctx).run(mode=SKILL_DISCOVER_MODE.get(key, "discover"))


def platform(ctx: RunContext, name: str, apply: bool = False, score_limit: int = 400, prepare_limit: int = 120):
    """Full chain for one platform."""
    ctx.log("info", f"=== full run: {name} ===")
    if not _run_stage(ctx, f"discover · {name}", lambda c: _discover(c, name)): return
    if name == "linkedin":
        if not _run_stage(ctx, "hydrate · linkedin descriptions",
                   lambda c: registry.SKILLS["linkedin"](c).run(mode="hydrate", limit=200)): return
    from backend.core.hydration import hydrate
    if not _run_stage(ctx, "hydrate · employer descriptions", hydrate, sources=[name]): return
    if not _run_stage(ctx, "score", pipeline.score, limit=score_limit, sources=[name]): return
    if not _run_stage(ctx, "prepare · tailor + fact-check", pipeline.prepare, limit=prepare_limit, sources=[name]): return
    if apply and not ctx.should_stop():
        _apply_for(ctx, name)
    left = "review queue — approve there to submit" if not apply else "applied"
    ctx.log("info", f"=== {name} done · {left} · {ctx.stats} ===")


def _apply_for(ctx: RunContext, name: str):
    if name in (config.load().get("outreach_only_sources") or []):
        # This board charges to apply through it; the way in is a person at the company.
        _run_stage(ctx, f"draft outreach · {name}", lambda c: registry.PIPELINES["outreach_draft"](c, limit=30, sources=[name]))
        return
    if name in PLATFORM_APPLY:
        key, mode = PLATFORM_APPLY[name]
        if not _run_stage(ctx, f"apply · {name}", lambda c: registry.SKILLS[key](c).run(mode=mode, limit=50)): return
        _run_stage(ctx, f"apply · ATS handoff · {name}", lambda c: registry.SKILLS["ats_apply"](c).run(limit=80, sources=[name]))
    else:                                    # collector-sourced jobs are hosted on employer ATS forms
        _run_stage(ctx, f"apply · ATS worker · {name}", lambda c: registry.SKILLS["ats_apply"](c).run(limit=80, sources=[name]))


def all_platforms(ctx: RunContext, apply: bool = False, only: list[str] | None = None):
    """Discover from every ready platform, then score and prepare once over the whole pool."""
    keys = only or _platform_keys()
    ctx.log("info", f"=== full run: {len(keys)} platform(s) → {', '.join(keys)} ===")
    completed = []
    for k in keys:
        if ctx.should_stop() or ctx.is_paused(): break
        if _run_stage(ctx, f"discover · {k}", lambda c, k=k: _discover(c, k)): completed.append(k)
    if ctx.should_stop() or ctx.is_paused(): return
    keys = completed
    if not keys: return
    scope = ",".join(sorted(keys))
    if "linkedin" in keys:
        if not _run_stage(ctx, "hydrate · linkedin descriptions",
                   lambda c: registry.SKILLS["linkedin"](c).run(mode="hydrate", limit=300)): return
    from backend.core.hydration import hydrate
    if not _run_stage(ctx, f"hydrate · employer descriptions · {scope}", hydrate, sources=keys): return
    if not _run_stage(ctx, f"score · {scope}", pipeline.score, limit=800, sources=keys): return
    if not _run_stage(ctx, f"prepare · tailor + fact-check · {scope}", pipeline.prepare, limit=200, sources=keys): return
    if apply and not ctx.should_stop():
        for k in keys:
            if ctx.should_stop() or ctx.is_paused(): break
            _apply_for(ctx, k)
    ctx.log("info", f"=== full run done · {ctx.stats} ===")


FULLRUNS = {"platform": platform, "all": all_platforms}
