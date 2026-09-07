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
    """overlay keys (all optional): headline, summary, skills_order [labels], experience_bullets {company: [bullets]}, include_projects bool."""
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
            exp.append({"title": e["title"], "company": e["company"], "location": e.get("location", ""), "period": _period(e),
                        "bullets": ob or (e.get("bullets", []) if e.get("current") else e.get("bullets", [])[:1])})
        else:
            earlier.append(f"{e['title']} @ {e['company']} ({_period(e)})")
    projects = [pr for pr in p.get("projects", []) if "TODO" not in pr.get("summary", "")] if o.get("include_projects", True) else []
    return {"p": p, "headline": o.get("headline") or p["identity"]["headline"], "summary": (o.get("summary") or p["positioning"]["summary"]).strip(),
            "skills": skills, "experience": exp, "earlier": earlier, "projects": projects}


def render_html(overlay: dict | None = None) -> str:
    return _env.get_template("resume.html").render(**build_context(overlay))


def render_pdf(overlay: dict | None = None, tag: str = "base") -> str:
    """Returns path relative to project root."""
    html = render_html(overlay)
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
    from pypdf import PdfReader
    reader = PdfReader(str(out))
    extracted = " ".join((page.extract_text() or "") for page in reader.pages)
    if len(reader.pages) != 1 or profile.get("identity.name") not in extracted:
        raise ValueError("Resume must be one readable page with the candidate name; revise the content before queuing")
    return str(out.relative_to(config.ROOT))
