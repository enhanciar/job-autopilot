"""The screening questions the system could not answer, and the conversation that turns them into permanent answers.

Every form the worker cannot finish leaves behind the exact questions that stopped it. Rather than making you open ten
applications and retype the same answer, those questions are collected here, grouped so rewordings of the same question
count once, and answered by chatting. Each answer becomes a rule in the answer bank (or a capability / declaration in your
profile) so the next employer who asks it is handled without you.

Nothing here invents an answer: the words are yours. The model only decides where an answer belongs and writes the
pattern that will recognise the question next time.
"""
from __future__ import annotations
import hashlib
import re
from datetime import datetime

from backend.app.db import session
from backend.app.models import ChatTurn, Question
from backend.core import llm, profile

NOISE = re.compile(r"\s*\((?:options|choices):[^)]*\)|\*+|\s+", re.I)
JUNK = re.compile(r"^(yes|no|q|ok|submit|next|continue|apply|my information|join the conversation|"
                  r"unable to process this file.*|[a-z]{1,2})$", re.I)


def normalise(text: str) -> str:
    """One fingerprint for every wording of the same question, so 'Notice period?' and 'Notice period *' group together."""
    clean = NOISE.sub(" ", (text or "")).strip().strip("?:.").lower()
    clean = re.sub(r"[^a-z0-9 ]+", " ", clean)
    return " ".join(clean.split())[:300]


def is_useful(text: str, company: str | None = None) -> bool:
    """Skip labels that are not questions: stray headings, single letters, a company name picked up by mistake."""
    clean = normalise(text)
    if not clean or len(clean) <= 3 or JUNK.match(clean):
        return False
    if company and clean == normalise(company):      # the page heading, not a question
        return False
    return " " in clean or len(clean) > 8            # a lone word is almost always a stray label


def record(questions, company: str | None = None) -> int:
    """Store the questions that stopped one application. Returns how many are newly open."""
    new = 0
    with session() as db:
        for raw in questions or []:
            text = (raw or "").strip()
            if not is_useful(text, company):
                continue
            options = re.search(r"\((?:options|choices):\s*([^)]*)\)", text, re.I)
            choices = [o.strip() for o in options.group(1).split(",") if o.strip()][:12] if options else None
            clean = NOISE.sub(" ", text).strip().strip("*: ")
            fp = hashlib.sha1(normalise(text).encode()).hexdigest()[:40]
            row = db.query(Question).filter_by(fingerprint=fp).one_or_none()
            if row:
                row.times_seen += 1
                row.last_seen = datetime.utcnow()
                if company and company not in (row.companies or []):
                    row.companies = [*(row.companies or []), company][:20]
                if choices and not row.options:
                    row.options = choices
                if len(clean) > len(row.text):        # keep the clearest wording seen
                    row.text = clean
            else:
                db.add(Question(fingerprint=fp, text=clean, options=choices, companies=[company] if company else [],
                                status="open"))
                db.flush()          # the next line of the same form may repeat this question
                new += 1
    return new


def backfill(db_path=None) -> int:
    """Pick up questions recorded before this inbox existed, from the errors stored on applications.
    Pass a database file to read them out of a backup taken before those applications were cleared."""
    if db_path:
        import sqlite3
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute("""SELECT a.error, j.company FROM applications a JOIN jobs j ON j.id = a.job_id
                                  WHERE a.error LIKE 'Unanswered fields%'""").fetchall()
        finally:
            con.close()
    else:
        from backend.app.models import Application
        with session() as db:
            rows = [(a.error, a.job.company) for a in db.query(Application).filter(Application.error.like("Unanswered fields%")).all()]
    total = 0
    for error, company in rows:
        total += record([f.strip() for f in error[len("Unanswered fields:"):].split("|")], company)
    return total


def open_questions(limit: int = 50) -> list[dict]:
    with session() as db:
        rows = db.query(Question).filter_by(status="open").order_by(Question.times_seen.desc(), Question.id).limit(limit).all()
        return [{"id": q.id, "text": q.text, "options": q.options, "companies": q.companies or [],
                 "times_seen": q.times_seen} for q in rows]


def summary() -> dict:
    with session() as db:
        counts = {status: db.query(Question).filter_by(status=status).count() for status in ("open", "answered", "skipped")}
    return counts


CHAT_SYSTEM = """You help one job seeker answer the screening questions that stopped their applications, so the answer can
be reused automatically next time.

You are given ONE question and what the person just replied. Their reply is the source of truth: never invent facts, never
soften or embellish, and never answer for them if the reply does not actually say. Return JSON only:
{"understood": bool,
 "reply": str (one or two sentences to show them; say plainly what you stored, or ask for the missing detail),
 "answer": str|null (exactly what should be typed into the form; for a multiple-choice question use one of the given options
                     verbatim; for yes/no use "Yes" or "No"),
 "store": "answers"|"capabilities"|"declarations"|null,
 "match": str|null (a lower-case regular expression matching this question and its likely rewordings in other forms:
                    use the distinctive words only, no anchors unless the question is a single short label),
 "capability": str|null (when store is "capabilities", the technology or skill name, lower case),
 "yes": bool|null (when store is "capabilities", whether they have it),
 "skip": bool (true only if they clearly want to leave this question unanswered)}

Where to store:
- "capabilities" for "have you worked with X / do you have experience with X" about a named technology.
- "declarations" for consent, acknowledgement, arbitration, policy agreement, or anything with a legal commitment.
- "answers" for everything else (numbers, dates, salary, preferences, short free text).
Set understood=false and ask a short follow-up when the reply is ambiguous, or when a multiple-choice question needs one
of the listed options and the reply does not clearly pick one."""


def interpret(question: dict, message: str) -> dict:
    """Turn what the person typed into a storable answer. The model classifies and phrases; it never supplies facts."""
    payload = {"question": question["text"], "options": question.get("options"),
               "asked_by": (question.get("companies") or [])[:5], "their_reply": message}
    import json
    out = llm.complete_json("classify", json.dumps(payload), CHAT_SYSTEM, use_cache=False)
    if not isinstance(out, dict):
        raise ValueError("Could not interpret that reply")
    return out


def apply_answer(question_id: int, decision: dict) -> dict:
    """Write the answer where it belongs and close the question. Returns what was saved, for display."""
    with session() as db:
        q = db.get(Question, question_id)
        if not q:
            raise ValueError("Unknown question")
        text = q.text
    if decision.get("skip"):
        with session() as db:
            db.get(Question, question_id).status = "skipped"
        return {"stored_in": None, "detail": "Left unanswered; it will keep coming to you."}

    store, answer = decision.get("store"), (decision.get("answer") or "").strip()
    if not answer and store != "capabilities":
        raise ValueError("No answer to store")

    if store == "capabilities":
        name = (decision.get("capability") or "").strip().lower()
        if not name:
            raise ValueError("No capability name")
        prof = profile.load()
        prof.setdefault("capabilities", {})[name] = bool(decision.get("yes"))
        profile.save(prof)
        detail = f"Recorded in your profile: {name} = {'yes' if decision.get('yes') else 'no'}."
    elif store == "declarations":
        prof = profile.load()
        prof.setdefault("declarations", {})[profile.declaration_key(text)] = answer
        profile.save(prof)
        detail = f"Recorded as your declaration for this exact wording: {answer!r}."
    else:
        match = (decision.get("match") or "").strip().lower() or re.escape(normalise(text))[:120]
        try:
            re.compile(match)
        except re.error:
            match = re.escape(normalise(text))[:120]
        rows = profile.answers()
        rows.insert(0, {"match": match, "answer": answer})      # specific first: earlier rules win
        profile.save_answers(rows)
        detail = f"Added to your answer bank: any question matching /{match}/ is answered {answer!r}."
        store = "answers"

    with session() as db:
        q = db.get(Question, question_id)
        q.status = "answered"; q.answer = answer or ("yes" if decision.get("yes") else "no")
        q.rule_match = decision.get("match"); q.stored_in = store
    return {"stored_in": store, "detail": detail}


def log_turn(role: str, text: str, question_id: int | None = None):
    with session() as db:
        db.add(ChatTurn(role=role, text=text, question_id=question_id))


def history(limit: int = 60) -> list[dict]:
    with session() as db:
        rows = db.query(ChatTurn).order_by(ChatTurn.id.desc()).limit(limit).all()
        return [{"role": t.role, "text": t.text, "question_id": t.question_id, "at": t.created_at} for t in reversed(rows)]
