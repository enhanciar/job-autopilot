"""The agent fallback: what it may do on a form, and what it must refuse."""
import pytest

from backend.core.apply import agent


class Element:
    def __init__(self, label, kind="textbox", value="", required=False, visible=True, text=""):
        self.label, self.kind, self.value, self.required, self.visible, self.text = label, kind, value, required, visible, text
        self.actions = []

    def is_visible(self): return self.visible
    def inner_text(self, timeout=None): return self.text or self.label
    def evaluate(self, js, *a):
        return {"tag": "input", "type": "" if self.kind == "textbox" else self.kind, "role": "",
                "text": self.text, "label": self.label, "placeholder": "", "required": self.required,
                "checked": False, "value": self.value}
    def click(self, timeout=None): self.actions.append("click")
    def fill(self, value, timeout=None): self.actions.append(("fill", value)); self.value = value
    def press(self, key, timeout=None): self.actions.append(("press", key))
    def select_option(self, label=None, timeout=None): self.actions.append(("select", label)); self.value = label
    def type(self, text, delay=None): self.value = text


class Scope:
    def __init__(self, elements): self.elements = elements
    def locator(self, selector):
        elements = self.elements
        class L:
            def count(inner): return len(elements)
            def nth(inner, i): return elements[i]
        return L()


class Page:
    def __init__(self, body="Apply for this job. Upload your resume."): self.body = body
    def inner_text(self, sel, timeout=None): return self.body
    def wait_for_timeout(self, ms): pass
    def locator(self, selector):
        class L:
            def filter(inner, **k): return inner
            def count(inner): return 0
            def all(inner): return []
        return L()
    def get_by_role(self, role, name=None, exact=None):
        class L:
            def count(inner): return 0
            @property
            def first(inner): return inner
        return L()


def log(level, message, **kw): pass


def required_from(scope):
    """Stand in for forms.validate_required: whatever is required and still empty."""
    return [e.label for e in scope.elements if e.required and not e.value]


def test_snapshot_numbers_only_what_is_on_screen(monkeypatch):
    monkeypatch.setattr(agent.forms, "_label_for", lambda scope, el: el.label)
    scope = Scope([Element("First Name", required=True),
                   Element("Hidden Field", visible=False),
                   Element("Country", value="India")])
    snapshot, handles = agent._snapshot(scope)
    assert len(handles) == 2, "an invisible control is not offered to the model"
    assert "[0]" in snapshot and "First Name" in snapshot and "(required, empty)" in snapshot
    assert "= 'India'" in snapshot, "already-answered fields show their value so the model leaves them alone"
    assert "Hidden Field" not in snapshot


def test_agent_fills_from_the_answer_bank_and_stops_when_done(monkeypatch):
    monkeypatch.setattr(agent.forms, "_label_for", lambda scope, el: el.label)
    monkeypatch.setattr(agent.forms, "answer_question", lambda label, *a, **k: "India" if "Country" in label else None)
    monkeypatch.setattr(agent.forms, "payment_wall", lambda page: None)
    monkeypatch.setattr(agent.forms, "has_captcha", lambda page: False)
    monkeypatch.setattr(agent.profile, "as_text", lambda: "profile")
    country = Element("Country", required=True)
    scope, page = Scope([country]), Page()
    monkeypatch.setattr(agent.forms, "validate_required", lambda p, s: required_from(s))

    calls = {"n": 0}
    def reply(*a, **k):
        calls["n"] += 1
        return {"action": "fill", "ref": 0, "value": "India", "why": "profile says India"}
    monkeypatch.setattr(agent.llm, "complete_json", reply)
    monkeypatch.setattr(agent, "humanize_type", lambda page, el, text: el.type(text))

    out = agent.finish_form(page, scope, job_text="job", cover=None, log=log)
    assert out["done"] and out["steps"] == 1
    assert country.value == "India"
    assert calls["n"] == 1, "once nothing is required and empty, it stops without asking the model again"


def test_agent_may_not_press_anything_that_costs_money(monkeypatch):
    monkeypatch.setattr(agent.forms, "_label_for", lambda scope, el: el.label)
    upgrade = Element("Upgrade to Turbo", kind="button", text="Upgrade to Turbo")
    assert not agent._act(Page(), [upgrade], {"action": "click", "ref": 0}, log)
    assert upgrade.actions == [], "the button must not be pressed"


def test_agent_cannot_invent_an_element(monkeypatch):
    ok = Element("Name")
    assert not agent._act(Page(), [ok], {"action": "click", "ref": 7}, log)
    assert not agent._act(Page(), [ok], {"action": "click", "ref": None}, log)
    assert ok.actions == []


@pytest.mark.parametrize("body,reason", [
    ("Billed now $29.95. You'll be charged monthly. Payment Method.", "payment"),
    ("Please verify you are human before continuing", "CAPTCHA"),
])
def test_agent_refuses_pages_it_must_not_act_on(monkeypatch, body, reason):
    monkeypatch.setattr(agent.forms, "_label_for", lambda scope, el: el.label)
    monkeypatch.setattr(agent.llm, "complete_json", lambda *a, **k: pytest.fail("the model should never be asked"))
    out = agent.finish_form(Page(body), Scope([Element("Name")]), job_text="", cover=None, log=log)
    assert not out["done"] and reason.lower() in out["blocked"].lower()


def test_agent_reports_a_question_it_cannot_answer_honestly(monkeypatch):
    monkeypatch.setattr(agent.forms, "_label_for", lambda scope, el: el.label)
    monkeypatch.setattr(agent.forms, "answer_question", lambda *a, **k: None)
    monkeypatch.setattr(agent.forms, "payment_wall", lambda page: None)
    monkeypatch.setattr(agent.forms, "has_captcha", lambda page: False)
    monkeypatch.setattr(agent.profile, "as_text", lambda: "profile")
    monkeypatch.setattr(agent.forms, "validate_required", lambda p, s: required_from(s))
    monkeypatch.setattr(agent.llm, "complete_json", lambda *a, **k: {
        "action": "blocked", "why": "Do you have business-level Japanese? is not in the profile"})
    out = agent.finish_form(Page(), Scope([Element("Japanese fluency", required=True)]), job_text="", cover=None, log=log)
    assert not out["done"] and "Japanese" in out["blocked"]


def test_agent_gives_up_rather_than_looping_forever(monkeypatch):
    monkeypatch.setattr(agent.forms, "_label_for", lambda scope, el: el.label)
    monkeypatch.setattr(agent.forms, "answer_question", lambda *a, **k: None)
    monkeypatch.setattr(agent.forms, "payment_wall", lambda page: None)
    monkeypatch.setattr(agent.forms, "has_captcha", lambda page: False)
    monkeypatch.setattr(agent.profile, "as_text", lambda: "profile")
    monkeypatch.setattr(agent.forms, "validate_required", lambda p, s: required_from(s))
    monkeypatch.setattr(agent.llm, "complete_json", lambda *a, **k: {"action": "click", "ref": 0, "why": "again"})
    out = agent.finish_form(Page(), Scope([Element("Name", required=True)]), job_text="", cover=None, log=log, max_steps=9)
    assert not out["done"], out
    assert "could not fill" in out["blocked"], "repeating an action that changes nothing must stop the loop"


def test_the_validator_decides_completion_not_the_model(monkeypatch):
    """The agent once declared a Figma form finished with six required fields still empty, because react-select's
    search box carries no required flag. The form's own validation is the authority now."""
    monkeypatch.setattr(agent.forms, "_label_for", lambda scope, el: el.label)
    monkeypatch.setattr(agent.forms, "answer_question", lambda *a, **k: None)
    monkeypatch.setattr(agent.forms, "payment_wall", lambda page: None)
    monkeypatch.setattr(agent.forms, "has_captcha", lambda page: False)
    monkeypatch.setattr(agent.profile, "as_text", lambda: "profile")
    empty = Element("Country", required=True)
    monkeypatch.setattr(agent.forms, "validate_required", lambda p, s: required_from(s))

    said_done = {"n": 0}
    def reply(*a, **k):
        said_done["n"] += 1
        if said_done["n"] == 1:
            return {"action": "done", "why": "looks complete to me"}
        return {"action": "fill", "ref": 0, "value": "India", "why": "profile"}
    monkeypatch.setattr(agent.llm, "complete_json", reply)
    monkeypatch.setattr(agent, "humanize_type", lambda page, el, text: el.type(text))

    out = agent.finish_form(Page(), Scope([empty]), job_text="", cover=None, log=log)
    assert out["done"], out
    assert said_done["n"] == 2, "a premature 'done' is rejected and the agent carries on"
