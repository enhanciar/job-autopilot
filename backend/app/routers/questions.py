"""Answer the screening questions that stopped applications, once, by chatting."""
from __future__ import annotations
from fastapi import APIRouter, Body, HTTPException
from backend.core import questions

router = APIRouter(prefix="/questions", tags=["questions"])


@router.get("")
def list_questions(limit: int = 50):
    return {"summary": questions.summary(), "open": questions.open_questions(limit), "history": questions.history()}


@router.get("/bundles")
def bundles():
    """The open questions grouped into a few themes, so a handful of replies settles them all."""
    return {"summary": questions.summary(), "bundles": questions.bundles(), "history": questions.history()}


@router.post("/auto")
def auto():
    """Close everything the profile already answers."""
    return {"resolved": questions.auto_resolve(), "summary": questions.summary()}


@router.post("/bundle")
def answer_bundle(body: dict = Body(...)):
    ids, message = body.get("ids") or [], (body.get("message") or "").strip()
    if not ids or not message: raise HTTPException(422, "Pick a group and type an answer")
    questions.log_turn("user", message)
    try:
        result = questions.answer_bundle(ids, message)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Could not interpret that: {type(e).__name__}")
    lines = [result["reply"]]
    for item in result["stored"]: lines.append(f"• {item['detail']}")
    if result["still_open"]:
        lines.append("Still open, because your answer did not cover them: " + "; ".join(q[:70] for q in result["still_open"]))
    text = "\n".join(lines)
    questions.log_turn("assistant", text)
    return {"reply": text, "summary": questions.summary(), "bundles": questions.bundles()}


@router.post("/chat")
def chat(body: dict = Body(...)):
    """One turn: your words in, a stored answer out. The reply says exactly what was saved and where."""
    message = (body.get("message") or "").strip()
    qid = body.get("question_id")
    if not message:
        raise HTTPException(422, "Type an answer first")
    open_now = questions.open_questions(50)
    current = next((q for q in open_now if q["id"] == qid), None) or (open_now[0] if open_now else None)
    if not current:
        raise HTTPException(409, "There are no open questions")
    questions.log_turn("user", message, current["id"])
    try:
        decision = questions.interpret(current, message)
    except Exception as e:  # noqa: BLE001 — provider failures
        raise HTTPException(502, f"Could not reach the language model: {type(e).__name__}")
    saved = None
    if decision.get("understood") or decision.get("skip"):
        try:
            saved = questions.apply_answer(current["id"], decision)
        except ValueError as e:
            decision["reply"] = f"{decision.get('reply') or ''} I could not store that: {e}".strip()
    reply = (decision.get("reply") or "").strip() or "Saved."
    if saved and saved.get("detail"):
        reply = f"{reply}\n\n{saved['detail']}"
    questions.log_turn("assistant", reply, current["id"])
    remaining = questions.open_questions(50)
    return {"reply": reply, "saved": saved, "answered_question_id": current["id"] if saved else None,
            "next": remaining[0] if remaining else None, "summary": questions.summary(), "open": remaining}


@router.post("/{qid}/skip")
def skip(qid: int):
    questions.apply_answer(qid, {"skip": True})
    remaining = questions.open_questions(50)
    return {"next": remaining[0] if remaining else None, "summary": questions.summary(), "open": remaining}


@router.post("/backfill")
def backfill(body: dict | None = Body(None)):
    """Collect questions from applications already on record (optionally from a backup taken before they were cleared)."""
    path = (body or {}).get("db_path")
    return {"added": questions.backfill(path), "summary": questions.summary()}
