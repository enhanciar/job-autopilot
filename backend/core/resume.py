"""Render a single-column ATS-friendly resume PDF from the master profile plus an optional tailoring overlay.
The overlay may only REORDER / SELECT / REPHRASE facts; the fact-checker in pipeline.py enforces that."""
from __future__ import annotations
import re, hashlib
from datetime import datetime
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright
from backend.core import config, profile

TPL_DIR = config.ROOT / "backend" / "core" / "templates"
OUT_DIR = config.ARTIFACTS / "resumes"
OUT_DIR.mkdir(parents=True, exist_ok=True)
_env = Environment(loader=FileSystemLoader(str(TPL_DIR)), autoescape=select_autoescape(["html"]))

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _period(e: dict) -> str:
    def f(s):
        if not s: return ""
        s = str(s)
        if len(s) >= 7 and s[4] == "-":
            return f"{MONTHS[int(s[5:7]) - 1]} {s[:4]}"
        return s
    return f"{f(e.get('start'))} – {'Present' if e.get('current') else f(e.get('end'))}"


def build_context(overlay: dict | None = None) -> dict:
    """overlay keys (all optional): headline, summary, skills_order [labels], experience_bullets {company: [bullets]}.

    Projects always appear: they are the candidate's own work and no tailoring pass may drop them."""
    p = profile.load()
    o = overlay or {}
    known_labels = {"llm_agents": "LLM & Agents", "rag_knowledge": "RAG & Knowledge", "llmops": "LLMOps", "languages": "Languages",
                    "backend_infra": "Backend & Infra", "frontend": "Frontend", "delivery": "Delivery", "data_ml": "Data & ML", "cloud_devops": "Cloud & DevOps"}
    hidden = {"models_used"}                       # profile bookkeeping, not a resume section
    label = lambda k: known_labels.get(k) or k.replace("_", " ").title()
    order = o.get("skills_order") or [k for k in p["skills"] if k not in hidden]
    skills = [(label(k), p["skills"][k]) for k in order if k in p["skills"]]
    exp, earlier = [], []
    # Current role(s) and the most recent previous employer get full bullets; older roles collapse to one line.
    recent_previous = next((e["company"] for e in p["experience"] if not e.get("current")), None)
    for e in p["experience"]:
        ob = (o.get("experience_bullets") or {}).get(e["company"])
        if e.get("current") or e["company"] == recent_previous or ob:
            bullets = ob or (e.get("bullets", []) if e.get("current") else e.get("bullets", [])[:1])
            cap = o.get("_max_bullets_current") if e.get("current") else o.get("_max_bullets_old")
            exp.append({"title": e["title"], "company": e["company"], "location": e.get("location", ""), "period": _period(e),
                        "bullets": bullets[:cap] if cap else bullets})
        else:
            earlier.append(f"{e['title']} @ {e['company']} ({_period(e)})")
    # Every project the profile lists is shown. Editing notes to yourself ("repo URL TODO") are stripped from the text
    # rather than used as a reason to hide the whole project, which is what silently emptied this section before.
    projects = []
    for pr in p.get("projects", []):
        summary = re.sub(r"\s*\([^)]*\bTODO\b[^)]*\)|\s*;?[^;.()]*\bTODO\b[^;.()]*", "", str(pr.get("summary") or ""), flags=re.I)
        summary = re.sub(r"\s+", " ", summary).strip(" ;,.")
        if not pr.get("name"):
            continue
        entry = {**pr, "summary": summary}
        cap = o.get("_max_bullets_project")
        if cap and entry.get("bullets"):
            entry["bullets"] = entry["bullets"][:cap]
        projects.append(entry)
    summary_text = (o.get("summary") or p["positioning"]["summary"]).strip()
    if o.get("_short_summary"):
        sentences = re.split(r"(?<=[.!?])\s+", summary_text)
        summary_text = " ".join(sentences[:2])
    return {"p": p, "headline": o.get("headline") or p["identity"]["headline"], "summary": summary_text,
            "skills": skills, "experience": exp, "earlier": earlier, "projects": projects}


def render_html(overlay: dict | None = None) -> str:
    return _env.get_template("resume.html").render(**build_context(overlay))


def _fit_variants(overlay: dict | None):
    """Content to try, densest first, when everything does not fit on one page.

    Projects are the candidate's own work and are never among the things dropped: the trimming happens to older-role
    bullets, then to the current role's, then to the summary."""
    o = dict(overlay or {})
    yield o, {}
    for cut in (3, 2):
        variant = dict(o); variant["_max_bullets_old"] = 1; variant["_max_bullets_current"] = cut
        variant["_max_bullets_project"] = cut
        yield variant, {"trimmed": f"role and project bullets to {cut}"}
    variant = dict(o); variant["_max_bullets_old"] = 1; variant["_max_bullets_current"] = 2
    variant["_max_bullets_project"] = 1; variant["_short_summary"] = True
    yield variant, {"trimmed": "bullets and summary"}


def max_pages() -> int:
    """How long the resume may be. Two pages by default; set resume.max_pages in config.yaml to change it."""
    value = (config.load().get("resume") or {}).get("max_pages", 2)
    return value if isinstance(value, int) and 1 <= value <= 3 else 2


def render_pdf(overlay: dict | None = None, tag: str = "base") -> str:
    """Render the resume, keeping every project. Content is only trimmed if it runs past the page limit.
    Returns the path relative to the project root."""
    from pypdf import PdfReader
    last_error = None
    for variant, note in _fit_variants(overlay):
        html = render_html(variant)
        h = hashlib.sha1(html.encode()).hexdigest()[:8]
        safe = re.sub(r"[^a-z0-9]+", "_", tag.lower())[:40]
        who = re.sub(r"[^A-Za-z0-9]+", "_", profile.get("identity.name") or "Resume").strip("_") or "Resume"
        out = OUT_DIR / f"{who}_{safe}_{h}.pdf"
        if not out.exists():
            with sync_playwright() as pw:
                b = pw.chromium.launch(channel="chrome", headless=True)
                pg = b.new_page()
                pg.set_content(html, wait_until="load")
                pg.pdf(path=str(out), format="A4", print_background=True, margin={"top": "0", "bottom": "0", "left": "0", "right": "0"})
                b.close()
        reader = PdfReader(str(out))
        extracted = " ".join((page.extract_text() or "") for page in reader.pages)
        if profile.get("identity.name") not in extracted:
            raise ValueError("The rendered resume does not contain the candidate name; check the profile and template")
        if len(reader.pages) <= max_pages():
            return str(out.relative_to(config.ROOT))
        last_error = f"{len(reader.pages)} pages{' after trimming ' + note['trimmed'] if note else ''}"
        out.unlink(missing_ok=True)
    raise ValueError(f"Resume runs past {max_pages()} page(s) ({last_error}); shorten the profile summary or project descriptions")
