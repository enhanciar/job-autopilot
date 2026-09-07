"""Config loader: config.yaml + .env. Single source for caps, routes, filters."""
from __future__ import annotations
import os, yaml
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("AUTOPILOT_DATA_DIR", str(ROOT / "data")))
ARTIFACTS = DATA / "artifacts"
PROFILES = DATA / "profiles"
CONFIG_PATH = ROOT / "config.yaml"
DB_PATH = DATA / "autopilot.db"

load_dotenv(ROOT / ".env")
for d in (DATA, ARTIFACTS, PROFILES):
    d.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def validate(cfg: dict) -> dict:
    if not isinstance(cfg, dict): raise ValueError("Configuration must be an object")
    for key in ("fit_threshold", "auto_submit_min_score"):
        value = cfg.get(key, 65 if key == "fit_threshold" else 75)
        if type(value) is not int or not 0 <= value <= 100: raise ValueError(f"{key} must be 0–100")
    if type(cfg.get("review_mode", True)) is not bool: raise ValueError("review_mode must be boolean")
    if type(cfg.get("target_per_day", 100)) is not int or cfg.get("target_per_day", 100) < 1: raise ValueError("target_per_day must be positive")
    for key in ("caps", "humanize", "llm", "filters", "schedule", "seed_companies"):
        if not isinstance(cfg.get(key, {}), dict): raise ValueError(f"{key} must be an object")
    for platform, caps in cfg.get("caps", {}).items():
        if not isinstance(caps, dict) or any(type(v) is not int or v < 0 for v in caps.values()): raise ValueError(f"Invalid caps for {platform}")
    providers = {"claude-cli", "gemini", "openrouter", "ollama"}
    llm = cfg.get("llm", {})
    if llm.get("default", "claude-cli") not in providers or any(v not in providers for v in llm.get("routes", {}).values()): raise ValueError("Unknown LLM provider")
    for key, value in cfg.get("humanize", {}).items():
        if key.endswith("_s") or key == "typing_cps":
            if not isinstance(value, list) or len(value) != 2 or not all(isinstance(v, (int,float)) and v >= 0 for v in value) or value[1] < value[0]: raise ValueError(f"Invalid pacing range: {key}")
    for key in ("routes", "models"):
        section = llm.get(key, {})
        if not isinstance(section, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in section.items()): raise ValueError(f"llm.{key} must map names to strings")
    task_models = llm.get("task_models", {})
    if not isinstance(task_models, dict) or any(not isinstance(v, dict) or any(not isinstance(m, str) or not m.strip() for m in v.values()) for v in task_models.values()): raise ValueError("llm.task_models must map provider -> task -> model")
    filters = cfg.get("filters", {})
    for key in ("titles_include", "titles_exclude", "locations_include", "locations_exclude"):
        value = filters.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value): raise ValueError(f"filters.{key} must be a list of strings")
    schedule = cfg.get("schedule", {})
    if type(schedule.get("enabled", False)) is not bool: raise ValueError("schedule.enabled must be boolean")
    jobs = schedule.get("jobs", [])
    if not isinstance(jobs, list): raise ValueError("schedule.jobs must be a list")
    seen_jobs = set()
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str) or not job["name"].strip(): raise ValueError("Each schedule job needs a name")
        if job.get("kind") not in ("collector", "collectors", "pipeline", "skill", "service", "fullrun"): raise ValueError(f"schedule job {job['name']}: unknown kind")
        every = job.get("every_minutes", 60)
        if type(every) is not int or not 1 <= every <= 7 * 24 * 60: raise ValueError(f"schedule job {job['name']}: every_minutes must be 1–10080")
        if type(job.get("business_hours_only", False)) is not bool: raise ValueError(f"schedule job {job['name']}: business_hours_only must be boolean")
        if not isinstance(job.get("params", {}), dict): raise ValueError(f"schedule job {job['name']}: params must be an object")
        if job["name"] in seen_jobs: raise ValueError(f"schedule job {job['name']} is listed twice")
        seen_jobs.add(job["name"])
    hours = cfg.get("business_hours") or {}
    if not isinstance(hours, dict): raise ValueError("business_hours must be an object")
    for key in ("start", "end"):
        if key in hours and (type(hours[key]) is not int or not 0 <= hours[key] <= 23): raise ValueError(f"business_hours.{key} must be an hour 0–23")
    browser = cfg.get("browser") or {}
    if not isinstance(browser, dict) or any(k in browser and type(browser[k]) is not bool for k in ("headless",)): raise ValueError("browser options must be booleans")
    return cfg


def save(cfg: dict) -> None:
    import tempfile
    cfg = validate(cfg)
    with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG_PATH.parent, delete=False) as f:
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
        temp = f.name
    os.replace(temp, CONFIG_PATH)


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)
