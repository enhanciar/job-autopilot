"""When the scripted filler runs out of rules, hand the same page to a model and let it finish the form.

The script handles the common shape of an application form cheaply and deterministically: it matches labels against the
answer bank, types, uploads, ticks. What it cannot do is meet a widget nobody wrote a rule for, and that is where every
stuck application comes from — a react-select that renders nothing until clicked, a conditional field, a two-step page.

So this is a fallback, not a replacement. It takes over the page the script was already working on and runs a small
loop: read the form as an accessibility tree, ask the model for ONE action, perform it, look again. The model never
sees pixels and never invents a selector — it picks an element by the reference number in the snapshot it was given.

What the model may not do, whatever it decides:
  * press anything on the never-click list (buy, upgrade, subscribe, install …)
  * continue on a page asking the candidate for money, or showing a CAPTCHA or a login wall
  * submit the application — pressing submit stays with the caller, which verifies the outcome
  * answer a question with a fact that is not in the profile: values come from the same answer bank the script uses,
    and free text goes through the same fact-check
"""
from __future__ import annotations

import json
import re

from backend.core import browser, llm, profile
from backend.core.apply import forms

MAX_STEPS = 14
SNAPSHOT_CHARS = 9000

SYSTEM = """You are finishing a job application form that a scripted filler could not complete.

You are given the form as an accessibility tree. Every interactive element has a number in [brackets]. You are also
given the candidate's answer to each open question where one is known.

Return JSON only, ONE action:
{"action": "fill"|"select"|"click"|"press"|"done"|"blocked",
 "ref": int|null, "value": str|null, "why": str}

- "fill": type `value` into element `ref` (text boxes, comboboxes, search inputs).
- "select": choose the option whose visible text is `value` from element `ref`.
- "click": press element `ref` — use it to open a dropdown, tick a checkbox, or reveal a hidden field.
- "press": send the key in `value` (Enter, ArrowDown, Escape) to element `ref`.
- "done": every required field is answered. Do NOT submit; the caller does that.
- "blocked": you cannot proceed honestly — say why in `why`.

Rules you may not break:
- Use only refs that appear in the snapshot you were given.
- Never state something untrue about the candidate. If a required question has no supplied answer and the honest answer
  is not derivable from the facts given, return "blocked" and name the question.
- Never press anything that buys, subscribes, upgrades, installs, creates an account, or accepts terms you were not
  told to accept.
- Never press Submit, Apply, or Send. Return "done" instead.
- One action per reply. Prefer the required field nearest the top that is still empty."""


class AgentBlocked(Exception):
    """The agent stopped on purpose: a question it cannot answer honestly, or a page it must not act on."""


def _snapshot(scope) -> tuple[str, list]:
    """The form as an accessibility tree with a numbered handle for each interactive element.

    Numbering the elements ourselves is what keeps the model honest: it can only name a number we gave it, so it can
    never invent a selector or reach a control that is not on the page in front of it.
    """
    handles, lines = [], []
    controls = scope.locator("input:not([type=hidden]), textarea, select, [role='combobox'], [role='option'], "
                             "button, [role='button'], [role='checkbox'], [role='radio'], a[href]")
    for i in range(min(controls.count(), 120)):
        el = controls.nth(i)
        try:
            if not el.is_visible():
                continue
            info = el.evaluate("""e => ({
                tag: e.tagName.toLowerCase(), type: e.getAttribute('type') || '', role: e.getAttribute('role') || '',
                text: (e.innerText || e.value || '').slice(0, 80), label: e.getAttribute('aria-label') || '',
                placeholder: e.getAttribute('placeholder') || '', required: e.required || e.getAttribute('aria-required') === 'true',
                checked: e.checked === true, value: (e.value || '').slice(0, 60)
            })""")
            label = info["label"] or forms._label_for(scope, el) or info["placeholder"] or info["text"]
            if not label and info["tag"] not in ("button", "a"):
                continue
            kind = info["role"] or info["type"] or info["tag"]
            state = ""
            if info["value"]:
                state = f" = {info['value']!r}"
            elif info["checked"]:
                state = " = checked"
            elif info["required"]:
                state = " (required, empty)"
            lines.append(f"[{len(handles)}] {kind}: {label[:90]}{state}")
            handles.append(el)
        except Exception:
            continue
    return "\n".join(lines)[:SNAPSHOT_CHARS], handles


def _known_answers(snapshot: str, job_text: str, cover: str | None, log) -> dict:
    """What the answer bank already knows for the questions on screen, so the model fills rather than invents."""
    answers = {}
    for line in snapshot.splitlines():
        label = re.sub(r"^\[\d+\]\s*[a-z-]+:\s*", "", line).split(" = ")[0].replace(" (required, empty)", "").strip()
        if len(label) < 4 or label in answers:
            continue
        try:
            value = forms.answer_question(label, job_text, cover, log)
        except Exception:
            value = None
        if value and not str(value).startswith("__"):
            answers[label] = str(value)
    return answers


def _act(page, handles, decision, log) -> bool:
    """Perform one decided action. Returns False when the action could not be carried out."""
    ref, value = decision.get("ref"), decision.get("value")
    if not isinstance(ref, int) or not 0 <= ref < len(handles):
        log("warn", f"agent named an element that is not on the page (ref {ref})")
        return False
    el = handles[ref]
    action = decision.get("action")
    try:
        if action == "fill":
            el.click(timeout=5000)
            el.fill("", timeout=3000)
            humanize_type(page, el, str(value or ""))
        elif action == "select":
            try:
                el.select_option(label=str(value), timeout=4000)
            except Exception:
                el.click(timeout=5000); page.wait_for_timeout(900)
                option = page.get_by_role("option", name=str(value), exact=False).first
                if not option.count():
                    return False
                option.click(timeout=4000)
        elif action == "click":
            label = (el.inner_text(timeout=800) or "").lower()
            if any(w in label for w in browser.NEVER_CLICK):
                log("warn", f"agent refused: '{label[:40]}' is on the never-click list")
                return False
            el.click(timeout=5000)
        elif action == "press":
            el.press(str(value or "Enter"), timeout=4000)
        else:
            return False
        page.wait_for_timeout(700)
        return True
    except Exception as e:  # noqa: BLE001
        log("warn", f"agent action {action} failed: {str(e)[:90]}")
        return False


def humanize_type(page, el, text: str):
    from backend.core import humanize
    humanize.human_type(page, el, text)


def finish_form(page, scope, *, job_text: str, cover: str | None, log, should_stop=lambda: False,
                max_steps: int = MAX_STEPS) -> dict:
    """Take over a partly filled form and answer what is left.

    Returns {"done": bool, "steps": int, "blocked": str|None}. Never presses submit: the caller owns that, because the
    caller is what verifies the employer actually received an application.
    """
    steps, stuck, last_remaining = 0, 0, None
    while steps < max_steps:
        if should_stop():
            return {"done": False, "steps": steps, "blocked": "stopped by user"}
        wall = forms.payment_wall(page)
        if wall:
            return {"done": False, "steps": steps, "blocked": f"page asks for payment ({wall})"}
        if forms.has_captcha(page):
            return {"done": False, "steps": steps, "blocked": "CAPTCHA"}

        snapshot, handles = _snapshot(scope)
        if not handles:
            return {"done": False, "steps": steps, "blocked": "nothing on the page to act on"}
        # The validator, not the DOM's `required` attribute, decides whether the form is finished. react-select's search
        # box carries no required flag, so a snapshot-based view thought the form was complete with six fields empty.
        remaining = forms.validate_required(page, scope)
        if not remaining:
            return {"done": True, "steps": steps, "blocked": None}
        if remaining == last_remaining:
            stuck += 1
            if stuck >= 3:
                return {"done": False, "steps": steps,
                        "blocked": f"could not fill {remaining[0]!r} after {stuck} attempts"}
        else:
            stuck = 0
        last_remaining = remaining

        known = _known_answers(snapshot, job_text, cover, log)
        payload = {"form": snapshot, "answers_we_already_know": known,
                   "candidate": profile.as_text()[:1500],
                   "still_required": remaining[:12],
                   "note": ("These field names are what the form still reports as empty. If your last action did not "
                            "change them, try a different element or a different kind of action." if stuck else "")}
        try:
            decision = llm.complete_json("form_agent", json.dumps(payload)[:14000], SYSTEM, use_cache=False)
        except Exception as e:  # noqa: BLE001
            return {"done": False, "steps": steps, "blocked": f"could not reach the model: {type(e).__name__}"}

        action = (decision or {}).get("action")
        why = (decision or {}).get("why") or ""
        if action == "done":
            still = forms.validate_required(page, scope)
            if still:
                log("info", f"agent said done with {len(still)} field(s) still empty; continuing")
                steps += 1
                continue
            return {"done": True, "steps": steps, "blocked": None}
        if action == "blocked" or not action:
            return {"done": False, "steps": steps, "blocked": why[:200] or "the agent could not proceed"}

        log("info", f"agent step {steps + 1}: {action} — {why[:70]}")
        if not _act(page, handles, decision, log):
            steps += 1
            continue
        steps += 1
    return {"done": False, "steps": steps, "blocked": f"gave up after {max_steps} steps"}
