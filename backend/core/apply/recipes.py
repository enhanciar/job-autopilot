"""Per-ATS apply recipes: learn a site's flow once with the LLM, then replay it deterministically.

Why: today's numbers showed the worker submits ~50% on the two ATS families that were hand-coded (Ashby, Greenhouse) and
0% on the ones that were not (Workday 27 applications, Workable 6, Lever 2). Hand-coding every family does not scale, and
selectors rot (LinkedIn switched to hashed class names mid-session).

How it works:
  1. `family_of(url)` maps a URL to an ATS family (workday, greenhouse, ashby, lever, ...) or a per-domain key for
     company-hosted forms.
  2. `load(family)` returns a stored recipe: an ordered list of steps, each with the selectors to fill, the control that
     advances the step, and the signal that proves the page moved on. Recipes are plain JSON in data/recipes/.
  3. `derive(page, family, log)` is the learning path: it hands the model a compact map of the visible form (labels,
     input types, selectors, buttons) and asks for a recipe. No clicking by the model — it only reads and writes data.
  4. `apply_recipe(...)` replays a recipe with zero LLM calls, except for free-text questions that have no canned answer.
  5. When a replay fails, the recipe is marked stale so the next run re-derives it. That is the self-healing loop.
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from backend.core import config, llm, humanize
from backend.core.apply import forms

RECIPE_DIR = config.ROOT / "data" / "recipes"
RECIPE_DIR.mkdir(parents=True, exist_ok=True)

FAMILIES = {
    "workday": r"myworkdayjobs\.com|workday\.com",
    "greenhouse": r"greenhouse\.io",
    "ashby": r"ashbyhq\.com",
    "lever": r"lever\.co",
    "workable": r"workable\.com",
    "recruitee": r"recruitee\.com",
    "smartrecruiters": r"smartrecruiters\.com",
    "freshteam": r"freshteam\.com",
    "teamtailor": r"teamtailor\.com",
    "successfactors": r"successfactors|sapsf",
    "icims": r"icims\.com",
    "jobvite": r"jobvite\.com",
    "bamboohr": r"bamboohr\.com",
}

DERIVE_SYSTEM = """You map job-application forms so an automation can fill them without you next time.
You are given a JSON description of what is currently visible on ONE page of an application form.
Return JSON only:
{"step_name": str,
 "fields": [{"selector": str, "kind": "text|select|radio|checkbox|file|combobox", "question": str, "answer_key": str}],
 "advance": {"selector": str, "label": str},
 "advanced_when": {"text_gone": str, "text_appears": str},
 "is_final": bool,
 "confirmation": str}
Rules:
- `selector` must be a CSS selector that appears in the input, copied EXACTLY as given (prefer the `id` form when present).
- `answer_key` is a short stable name for what the field wants: full_name, first_name, last_name, email, phone, resume,
  linkedin, github, portfolio, location, country, current_company, current_title, notice_period, expected_salary,
  work_authorization, sponsorship_required, years_experience, cover_letter, gender, race, veteran, disability,
  how_did_you_hear, or free_text for anything open-ended.
- `advance` is the button that moves to the next step or submits (Next / Continue / Save and Continue / Submit).
- `advanced_when.text_gone` is a phrase visible now that should disappear once the step is done; `text_appears` is a
  phrase expected on the following step. Either may be "".
- `confirmation` is the phrase that proves the whole application was accepted; only set it when is_final is true.
- Never invent a selector that was not in the input."""


def family_of(url: str) -> str:
    u = (url or "").lower()
    for fam, rx in FAMILIES.items():
        if re.search(rx, u):
            return fam
    host = urlparse(u).netloc.replace("www.", "")
    return f"site:{host}" if host else "unknown"


def _path(family: str) -> Path:
    return RECIPE_DIR / (re.sub(r"[^a-z0-9._-]", "_", family) + ".json")


RECIPE_VERSION = 2


def signature(snapshot):
    """Match controls and question meanings, excluding job title/headings and entered values."""
    import hashlib
    fields = sorted((f.get("selector", ""), f.get("tag", ""), f.get("type", ""),
                     f.get("label", ""), bool(f.get("required")), tuple(f.get("options", [])))
                    for f in snapshot.get("fields", []))
    buttons = sorted((b.get("selector", ""), b.get("text", "")) for b in snapshot.get("buttons", []))
    return hashlib.sha256(json.dumps([fields, buttons], sort_keys=True).encode()).hexdigest()


def matches(page, step, root=None):
    return step.get("signature") == signature(describe(page, root))


def _write(path, data):
    import os, tempfile
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as tmp:
        json.dump(data, tmp, indent=2)
    os.replace(tmp.name, path)


def load(family: str) -> dict | None:
    p = _path(family)
    if not p.exists():
        return None
    try:
        r = json.loads(p.read_text())
        return None if r.get("stale") or r.get("version") != RECIPE_VERSION else r
    except Exception:
        return None


def save(family: str, recipe: dict):
    recipe = dict(recipe)
    recipe["version"] = RECIPE_VERSION
    recipe["family"] = family
    recipe["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write(_path(family), recipe)


def mark_stale(family: str, why: str):
    p = _path(family)
    if not p.exists():
        return
    try:
        r = json.loads(p.read_text())
        r["stale"] = True
        r["stale_reason"] = why[:300]
        _write(p, r)
    except Exception:
        pass


# ---------------------------------------------------------------- reading the page
def describe(page, root=None, max_fields: int = 60) -> dict:
    """A compact, model-readable map of the current step: every visible control with its label and a usable selector."""
    scope = root or page
    out = {"url": page.url[:200], "fields": [], "buttons": [], "headings": []}
    try:
        out["headings"] = [t.strip()[:90] for t in scope.locator("h1, h2, h3, legend").all_inner_texts()][:8]
    except Exception:
        pass
    controls = scope.locator("input:not([type='hidden']), select, textarea, [role='combobox'], [role='radiogroup']")
    for i in range(min(controls.count(), max_fields)):
        el = controls.nth(i)
        try:
            if not el.is_visible():
                continue
            info = el.evaluate("""e => ({
                tag: e.tagName.toLowerCase(), type: e.getAttribute('type') || '', id: e.id || '',
                name: e.getAttribute('name') || '', label: e.getAttribute('aria-label') || '',
                placeholder: e.getAttribute('placeholder') || '', role: e.getAttribute('role') || '',
                automation: e.getAttribute('data-automation-id') || '', required: e.required || false,
                value: (e.value || '').slice(0, 40)
            })""")
            label = info["label"] or forms._label_for(page, el) or info["placeholder"] or info["name"]
            sel = (f"#{info['id']}" if info["id"] and re.match(r"^[A-Za-z][\w:.-]*$", info["id"]) else
                   f"[data-automation-id='{info['automation']}']" if info["automation"] else
                   f"[name='{info['name']}']" if info["name"] else "")
            if not sel:
                continue
            opts = []
            if info["tag"] == "select":
                try: opts = [o.strip()[:40] for o in el.locator("option").all_inner_texts()][:12]
                except Exception: pass
            out["fields"].append({"selector": sel, "tag": info["tag"], "type": info["type"], "role": info["role"],
                                  "label": (label or "")[:120], "required": info["required"],
                                  "filled": bool(info["value"]), "options": opts})
        except Exception:
            continue
    btns = scope.locator("button, a[role='button'], input[type='submit']")
    for i in range(min(btns.count(), 25)):
        b = btns.nth(i)
        try:
            if not b.is_visible():
                continue
            txt = (b.inner_text(timeout=600) or "").strip()
            aria = b.get_attribute("aria-label") or ""
            auto = b.get_attribute("data-automation-id") or ""
            if not (txt or aria):
                continue
            sel = (f"[data-automation-id='{auto}']" if auto else
                   f"button:has-text('{txt[:30]}')" if txt else f"[aria-label='{aria[:40]}']")
            out["buttons"].append({"selector": sel, "text": txt[:40], "aria": aria[:40]})
        except Exception:
            continue
    return out


def derive(page, family: str, log, root=None) -> dict | None:
    """Ask the model to turn the current page into a recipe step. Reading only — the model never drives the browser."""
    snap = describe(page, root)
    if not snap["fields"] and not snap["buttons"]:
        log("warn", f"recipe {family}: nothing visible to map"); return None
    try:
        step = llm.complete_json("classify", json.dumps(snap)[:12000], DERIVE_SYSTEM)
    except Exception as e:  # noqa: BLE001
        log("warn", f"recipe {family}: derive failed: {str(e)[:150]}"); return None
    known = {f["selector"] for f in snap["fields"]}
    buttons = {b["selector"] for b in snap["buttons"]}
    if not isinstance(step, dict) or not isinstance(step.get("fields"), list): return None
    mapped = []
    for f in step["fields"]:
        if not isinstance(f, dict) or f.get("selector") not in known: return None
        if f.get("kind") not in ("text", "select", "radio", "checkbox", "file", "combobox"): return None
        actual = next(item for item in snap["fields"] if item["selector"] == f["selector"])
        expected = ("select" if actual["tag"] == "select" else actual["type"] if actual["type"] in ("file", "checkbox", "radio") else "combobox" if actual.get("role") == "combobox" else "text")
        if f["kind"] != expected: return None
        # Use observed wording instead of a model-rewritten factual question.
        f["question"] = actual.get("label", "")
        if not isinstance(f.get("answer_key"), str): return None
        mapped.append(f)
    step["fields"] = mapped
    if not isinstance(step.get("advance"), dict): return None
    advance_selector = (step.get("advance") or {}).get("selector")
    if advance_selector not in buttons:
        log("warn", f"recipe {family}: unknown advance selector"); return None
    if not isinstance(step.get("is_final"), bool): return None
    if step["is_final"] and (not isinstance(step.get("confirmation"), str) or not step["confirmation"].strip()): return None
    if not isinstance(step.get("advanced_when", {}), dict): return None
    step["signature"] = signature(snap)
    return step


# ---------------------------------------------------------------- replaying a recipe
def answer_for(key: str, question: str, job_text: str, cover: str, log) -> str | None:
    """Resolve an answer_key to a value: profile facts and the answer bank first, LLM only for genuinely open questions."""
    from backend.core import profile
    ident = {"full_name": "identity.name", "first_name": "identity.first_name", "last_name": "identity.last_name",
             "email": "identity.email", "phone": "identity.phone", "linkedin": "identity.linkedin",
             "github": "identity.github", "portfolio": "identity.portfolio", "location": "identity.location"}
    if key in ident:
        return profile.get(ident[key])
    if key == "country":
        return profile.get("identity.country") or None
    if key == "cover_letter":
        return cover
    canned = forms.answer_question(question or key.replace("_", " "), job_text, cover, log)
    if canned and not canned.startswith("__"):
        return canned
    if key == "free_text" or canned == "__LLM__":
        return forms.answer_question(question or key, job_text, cover, log)
    return canned if canned else None


def fill_step(page, step: dict, job_text: str, cover: str, resume: str | None, log, root=None) -> list[str]:
    """Fill one recipe step. Returns the questions it could not answer."""
    scope = root or page
    unanswered = []
    for f in step.get("fields") or []:
        sel, kind, key, q = f.get("selector"), f.get("kind", "text"), f.get("answer_key", ""), f.get("question", "")
        try:
            el = scope.locator(sel).first
            if not el.count() or not el.is_visible():
                continue
            if kind == "file":
                if resume:
                    el.set_input_files(str(config.ROOT / resume)); page.wait_for_timeout(2500)
                continue
            if el.input_value() if kind in ("text", "combobox") else False:
                continue
            ans = answer_for(key, q, job_text, cover, log)
            if ans is None:
                unanswered.append(q or key); continue
            if kind == "select":
                opts = [o.strip() for o in el.locator("option").all_inner_texts()]
                pick = next((o for o in opts if o.strip() and ans.strip().lower() == o.strip().lower()), None)
                if pick: el.select_option(label=pick)
                else: unanswered.append(f"{q or key} (options: {', '.join(opts[:6])})")
            elif kind in ("radio", "checkbox"):
                if kind == "checkbox" and ans.lower() in ("yes", "no"):
                    el.set_checked(ans.lower() == "yes")
                else:
                    value = (el.get_attribute("value") or "").strip().lower()
                    if value and value == ans.strip().lower(): el.check()
                    else: unanswered.append(q or key)
            else:
                humanize.human_type(page, el, ans)
                if kind == "combobox":
                    page.wait_for_timeout(1200)
                    opts = scope.locator("[role='option']").filter(visible=True)
                    matched = [opts.nth(i) for i in range(opts.count()) if opts.nth(i).inner_text().strip().casefold() == ans.strip().casefold()]
                    if len(matched) == 1: matched[0].click()
                    else: unanswered.append(q or key)
        except Exception as e:  # noqa: BLE001
            unanswered.append(q or key or sel or "Unknown field")
            log("warn", f"recipe field '{(q or key)[:40]}': {str(e)[:80]}")
    return unanswered + forms.validate_required(page, scope)


def advance(page, step: dict, log, root=None, should_stop=lambda: False) -> bool:
    """Press the step's Next/Submit control and confirm the page actually moved."""
    if should_stop(): return False
    scope = root or page
    adv = step.get("advance") or {}
    sel = adv.get("selector")
    if not sel:
        return False
    before = ""
    try:
        before = page.inner_text("body", timeout=3000)[:4000]
    except Exception:
        pass
    try:
        btn = scope.locator(sel).filter(visible=True)
        if btn.count() != 1:
            log("warn", f"recipe: advance control not found ({sel})"); return False
        if should_stop(): return False
        btn.click()
    except Exception as e:  # noqa: BLE001
        log("warn", f"recipe: advance click failed: {str(e)[:100]}"); return False
    cond = step.get("advanced_when") or {}
    for _ in range(20):
        if should_stop(): return False
        page.wait_for_timeout(700)
        try:
            now = page.inner_text("body", timeout=3000)[:4000]
        except Exception:
            continue
        gone, appears = cond.get("text_gone") or "", cond.get("text_appears") or ""
        if appears and appears.lower() in now.lower():
            return True
        if gone and gone.lower() not in now.lower():
            return True
        if not gone and not appears and now[:600] != before[:600]:
            return True
    return False
