"""Generic form filler shared by every ATS handler. Maps visible labels to profile answers, uploads the resume, fills
unknown free-text questions via LLM (or pauses for the human in review mode), never touches CAPTCHAs."""
from __future__ import annotations
import re
from backend.core import profile, humanize, llm, config

UNKNOWN = object()
import threading
from collections.abc import MutableMapping

class ApplicationContext(MutableMapping):
    """Thread-local compatibility mapping; never shares answers between workers."""
    def __init__(self): self.local = threading.local()
    def _data(self):
        if not hasattr(self.local, "data"): self.local.data = {}
        return self.local.data
    def __getitem__(self, key): return self._data()[key]
    def __setitem__(self, key, value): self._data()[key] = value
    def __delitem__(self, key): del self._data()[key]
    def __iter__(self): return iter(self._data())
    def __len__(self): return len(self._data())

CTX = ApplicationContext()


EXPLICIT_COUNTRIES = [  # a country named in the question itself overrides the job's location
    (r"\b(india)\b", "india"), (r"\b(united states|u\.s\.a?|usa|us)\b|\bthe us\b", "united states"), (r"\b(united kingdom|uk|u\.k\.|great britain)\b", "united kingdom"),
    (r"\b(germany)\b", "germany"), (r"\b(canada)\b", "canada"), (r"\b(australia)\b", "australia"), (r"\b(netherlands)\b", "netherlands"),
    (r"\b(singapore)\b", "singapore"), (r"\b(ireland)\b", "ireland"), (r"\b(france)\b", "france"), (r"\b(japan)\b", "japan"),
    (r"\b(uae|united arab emirates|dubai)\b", "uae"), (r"\b(eu|european union|europe|eea)\b", "europe"), (r"\b(switzerland)\b", "switzerland"),
]


def question_country(label: str) -> str | None:
    """The jurisdiction a question names explicitly ("authorized to work in the United States"), if any."""
    l = label.lower()
    for rx, country in EXPLICIT_COUNTRIES:
        if re.search(rx, l): return country
    return None


def _country_aware(label: str) -> str | None:
    """Work-authorization / sponsorship answers depend on where the job is. India = authorized, no sponsorship; elsewhere the reverse.
    A country named in the question wins over the job's location."""
    l = label.lower()
    c = question_country(label) or (CTX.get("country") or "").lower()
    # Unknown/ambiguous jurisdiction is a human decision, not an implicit foreign country.
    if not c or c in ("unknown", "remote (worldwide)", "europe (remote)"): return None
    in_india = c == "india"
    if re.search(r"authori[sz]ed to work|legally (able|eligible) to work|right to work|work authori[sz]ation|eligible to work", l):
        return "Yes" if in_india else "No"
    if re.search(r"sponsorship|sponsor", l) and re.search(r"require|need|will you", l):
        return "No" if in_india else "Yes"
    return None


GENERIC_PLACEHOLDERS = ("start typing", "pick date", "select", "type here", "your answer", "enter", "search", "search for option", "select an option", "select option")


def _label_for(page, el) -> str:
    """Best-effort visible label text for an input: <label for>, aria-labelledby, aria-label, then the nearest field wrapper's
    heading text, then placeholder (skipping generic ones like 'Start typing...')."""
    try:
        aid = el.get_attribute("id")
        if aid:
            lab = page.locator(f"label[for='{aid}']")
            if lab.count():
                t = lab.first.inner_text(timeout=1000).strip()
                if t: return t
        lb = el.get_attribute("aria-labelledby")
        if lb:
            t = " ".join(page.locator(f"#{i}").first.inner_text(timeout=500) for i in lb.split() if page.locator(f"#{i}").count()).strip()
            if t: return t
        al = el.get_attribute("aria-label")
        if al and al.strip().lower() not in GENERIC_PLACEHOLDERS: return al.strip()
        wrap = el.evaluate("""e => { let n = e; for (let i = 0; i < 5 && n; i++) { n = n.parentElement; if (!n) break;
            const lab = n.querySelector('label, legend, h3, h4, [class*=label], [class*=Label], [class*=title], [class*=Title]');
            if (lab && lab.innerText && lab.innerText.trim().length > 1 && lab.innerText.trim().length < 450 && !lab.contains(e)) return lab.innerText.trim(); }
            return ''; }""")
        if wrap: return wrap
        ph = el.get_attribute("placeholder") or ""
        if ph and not any(ph.strip().lower().startswith(g) for g in GENERIC_PLACEHOLDERS): return ph.strip()
        nm = el.get_attribute("name") or ""
        return nm.strip() if nm and not nm.startswith("_") else ph.strip()
    except Exception:
        return ""


def factcheck_answer(question: str, answer: str) -> dict:
    """Same gate the resume passes: every claim in a generated screening answer must be supported by the profile."""
    from backend.core.validation import FactCheck
    from backend.core.pipeline import FACTCHECK_SYSTEM
    return FactCheck.model_validate(llm.complete_json("factcheck", f"PROFILE:\n{profile.as_text()}\n\nCANDIDATE OUTPUT:\nQ: {question}\nA: {answer}",
                                                      FACTCHECK_SYSTEM, use_cache=False)).model_dump()


def generate_answer(question: str, job_text: str, cover_note: str | None) -> str:
    """LLM answer for an open question, accepted only when the fact-check finds no unsupported claim (one repair attempt)."""
    system = "You write concise, truthful job-application answers using only the profile facts."
    prompt = (f"PROFILE:\n{profile.as_text()}\n\nJOB:\n{job_text[:3000]}\n\nCOVER NOTE ALREADY WRITTEN:\n{cover_note or ''}\n\n"
              f"Answer this application question in first person, 40-120 words, specific, no invented facts:\nQ: {question}")
    answer = llm.complete("outreach", prompt, system).strip()
    check = factcheck_answer(question, answer)
    if check.get("ok") is not True or check.get("violations"):
        fix = prompt + "\n\nREMOVE OR REWRITE THESE UNSUPPORTED CLAIMS:\n" + "\n".join(f"- {v.get('claim')}: {v.get('why')}" for v in check.get("violations") or [])
        answer = llm.complete("outreach", fix, system, use_cache=False).strip()
        check = factcheck_answer(question, answer)
    if check.get("ok") is not True or check.get("violations"):
        raise ValueError("generated answer failed the fact-check; left for the human")
    return answer


SALARY_RX = re.compile(r"salary|compensation|\bctc\b|\bpay\b|remuneration|package", re.I)


def numeric_answer(label: str, answer: str) -> str | None:
    """Value for a numeric control. Salary amounts are chosen by the currency or country the question or the job names;
    when neither is clear the field stays unanswered rather than sending an Indian rupee figure to a US employer."""
    if re.fullmatch(r"\s*\d+(\.\d+)?\s*", answer or ""): return answer.strip()
    l = label.lower()
    if not SALARY_RX.search(l):
        digits = re.findall(r"\d+(?:\.\d+)?", answer or "")
        return digits[0] if len(digits) == 1 else None
    prefs = profile.load().get("preferences") or {}
    country = question_country(label) or (CTX.get("country") or "").lower()
    monthly = bool(re.search(r"month", l))
    if re.search(r"\b(usd|us\$|\$|dollar)", l) or country in ("united states", "canada", "australia", "singapore", "uae", "japan", "remote (worldwide)"):
        yearly = prefs.get("min_salary_usd_remote")
    elif re.search(r"\b(eur|€|euro)", l) or country in ("germany", "netherlands", "ireland", "france", "europe", "europe (remote)", "switzerland", "united kingdom"):
        yearly = prefs.get("wttj_min_salary_eur")
    elif re.search(r"\b(inr|₹|rs\.?|rupee|lpa|lakh)", l) or country == "india":
        yearly = (prefs.get("expected_salary_lpa") or 0) * 100000
    else:
        return None
    if not yearly: return None
    return str(int(round(yearly / 12))) if monthly else str(int(yearly))


def answer_question(label: str, job_text: str, cover_note: str | None, log) -> str | None:
    if re.search(r"authori[sz]|right to work|sponsorship|sponsor", label, re.I) and CTX.get("country") in (None, "", "Unknown", "Remote (worldwide)", "Europe (remote)"):
        return None
    a = _country_aware(label) or profile.answer_for(label)
    if a == "__COVER__":
        return cover_note or generate_answer("Why are you a good fit for this role?", job_text, cover_note)
    if a == "__DATE_PLUS_30__":
        from datetime import date, timedelta
        return (date.today() + timedelta(days=int(profile.get("preferences.notice_period_days", default=30)))).strftime("%m/%d/%Y")
    if a == "__LLM__":
        try:
            return generate_answer(label, job_text, cover_note)
        except Exception as e:  # noqa: BLE001
            log("warn", f"LLM answer failed for '{label[:60]}': {e}")
            return None
    return a


def fill_text_inputs(page, job_text: str, cover_note: str | None, log, scope=None, simple_only: bool = False) -> list[str]:
    """Fill every empty text/textarea/email/tel input we can map. Returns labels we could not answer.
    simple_only=True is the late re-pass for fields a re-render cleared: canned answers only, no LLM, nothing reported."""
    root = scope or page
    unanswered = []
    inputs = root.locator("input[type='text'], input[type='email'], input[type='tel'], input[type='url'], input[type='number'], input:not([type]), textarea")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            if not el.is_visible() or el.input_value(timeout=800):
                continue
            label = _label_for(root, el)
            if not label or re.search(r"search|captcha", label, re.I):
                continue
            if simple_only:
                ans = _country_aware(label) or profile.answer_for(label)
                if not ans or ans.startswith("__"): continue
                ans = answer_question(label, job_text, cover_note, log) if ans == "__DATE_PLUS_30__" else ans
            else:
                ans = answer_question(label, job_text, cover_note, log)
            if ans is None:
                if el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true": unanswered.append(label)
                continue
            if (el.get_attribute("type") or "") == "number" or (el.get_attribute("inputmode") or "") in ("numeric", "decimal"):
                ans = numeric_answer(label, ans)
                if ans is None:
                    unanswered.append(f"{label} (numeric value needed)"); continue
            is_auto = (el.get_attribute("role") or "") == "combobox" or bool(el.get_attribute("aria-autocomplete")) or re.search(r"location|city|school|university|country|where are you|residence", label, re.I)
            if is_auto and "," in ans and re.search(r"location|city|where are you|residence", label, re.I):
                ans = ans.split(",")[0].strip()      # "Mumbai, India" -> "Mumbai": place autocompletes match the city name
            humanize.human_type(page, el, ans)
            # autocomplete / combobox inputs (Ashby location, Greenhouse school etc.): pick the first suggestion
            if is_auto:
                try:
                    page.wait_for_timeout(1500)
                    opt = None
                    for OPT in ("[role='listbox'] [role='option']", "[role='option']", "[role='listbox'] li, [role='listbox'] div", ".autocomplete-option, [class*='autocomplete'] [class*='result'], [class*='_option_']"):
                        cand = root.locator(OPT).filter(visible=True)
                        if not cand.count(): cand = page.locator(OPT).filter(visible=True)
                        if cand.count(): opt = cand.first; break
                    if opt is not None:
                        # prefer the suggestion that starts with what we typed ("India +91" over "British Indian Ocean Territory")
                        want = re.sub(r"[^a-z0-9]+", " ", ans.lower()).strip()
                        best = None
                        for k in range(min(cand.count(), 8)):
                            t = re.sub(r"[^a-z0-9]+", " ", cand.nth(k).inner_text(timeout=500).lower()).strip()
                            if t.startswith(want): best = cand.nth(k); break
                            if best is None and re.search(r"india|mumbai", ans, re.I) and re.search(r"\bindia\b", t): best = cand.nth(k)
                        opt = best
                    if opt is not None: humanize.human_click(page, opt)
                    else:
                        # Ashby/Google-places style: first suggestion is selected with ArrowDown+Enter
                        unanswered.append(f"Select a verified autocomplete option for {label}")
                    page.wait_for_timeout(500)
                except Exception:
                    pass
            humanize.pause("typing_gap_s") if "typing_gap_s" in config.load().get("humanize", {}) else None
        except Exception as e:  # noqa: BLE001
            log("warn", f"input fill error: {e}")
    return unanswered


def fill_selects(page, log, scope=None) -> list[str]:
    root = scope or page
    unanswered = []
    sels = root.locator("select")
    for i in range(sels.count()):
        el = sels.nth(i)
        try:
            if not el.is_visible(): continue
            label = _label_for(root, el)
            ans = answer_question(label, "", None, log)
            if not ans or ans == "__LLM__":
                if el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true": unanswered.append(label)
                continue
            opts = el.locator("option").all_inner_texts()
            pick = next((o for o in opts if ans.lower() in o.lower() or o.lower() in ans.lower()), None)
            if pick is None and ans.lower().startswith("yes"): pick = next((o for o in opts if o.strip().lower().startswith("yes")), None)
            if pick is None and ans.lower().startswith("no"): pick = next((o for o in opts if o.strip().lower().startswith("no")), None)
            if pick: el.select_option(label=pick)
            else: unanswered.append(label)
        except Exception as e:  # noqa: BLE001
            log("warn", f"select fill error: {e}")
    return unanswered


def fill_button_choices(page, log, scope=None) -> list[str]:
    """Question blocks answered by clicking one of several short-text buttons (Ashby 'Yes'/'No' options, Lever/Greenhouse custom
    selects rendered as buttons). Returns labels we could not answer."""
    root = scope or page
    unanswered = []
    entries = root.locator(".ashby-application-form-field-entry, [class*='field-entry'], [class*='FieldEntry'], fieldset, [role='group']")
    for i in range(entries.count()):
        e = entries.nth(i)
        try:
            if not e.is_visible(): continue
            btns = e.locator("button, [role='radio'], [role='option']")
            texts = [t.strip() for t in btns.all_inner_texts()]
            short = [t for t in texts if 0 < len(t) <= 40]
            if len(short) < 2 or len(short) > 12: continue
            if e.locator("input[type='checkbox']:checked, [aria-checked='true'], [aria-pressed='true'], [class*='selected'], [class*='Selected']").count(): continue
            label = e.locator("label, legend, [class*='heading'], [class*='label']").first
            q = label.inner_text(timeout=800).strip() if label.count() else ""
            if not q: continue
            if re.search(r"cover letter|resume|cv\b|attach|dropbox|google drive", q, re.I) or any(re.search(r"attach|dropbox|google drive|enter manually|upload", t, re.I) for t in short):
                continue   # file-source buttons (Attach / Dropbox / Enter manually) are not a question
            ans = answer_question(q, "", None, log)
            if not ans or ans == "__LLM__":
                unanswered.append(q[:120]); continue
            want = ans.split()[0].lower().strip(",.")
            pick = None
            for k in range(btns.count()):
                t = btns.nth(k).inner_text(timeout=500).strip().lower()
                if t == want or t.startswith(want) or (want in t and len(t) < 40):
                    pick = btns.nth(k); break
            if pick is None:
                unanswered.append(q[:120]); continue
            humanize.human_click(page, pick)
        except Exception:
            continue
    return unanswered


def fill_checkbox_groups(page, log, scope=None) -> list[str]:
    """Ashby checkbox/radio groups (a fieldset with a question title and N labelled checkboxes, e.g. country of residence:
    Other / Canada / U.S.). Picks the option matching the answer; location answers fall back to Other / International."""
    root = scope or page
    unanswered = []
    groups = root.locator(".ashby-application-form-input-checkbox-group, fieldset:has(input[type='checkbox']), fieldset:has(input[type='radio'])")
    norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
    for i in range(groups.count()):
        g = groups.nth(i)
        try:
            if not g.is_visible(): continue
            if g.locator("input:checked").count(): continue
            title = g.locator("legend, label[class*='question-title'], [class*='question-title'], [class*='heading']").first
            q = title.inner_text(timeout=800).strip() if title.count() else ""
            if not q or re.search(r"cover letter|resume|attach", q, re.I): continue
            opts = g.locator("[class*='checkbox-group-option'], [class*='radio-group-option'], [class*='checkbox__wrapper'], [class*='radio__wrapper'], label:has(input), .option, li:has(input)")
            texts = [t.strip() for t in opts.all_inner_texts()]
            texts = [t for t in texts if t and t != q]
            if len(texts) < 2 or len(texts) > 30: continue
            ans = answer_question(q, "", None, log)
            if re.search(r"preferred location|which (office|location|city)|locations? .*(interested|prefer)|select all", q, re.I) and sum(1 for t in texts if "," in t) >= 3:
                # office/city multi-select: tick every city on the relocation list, else fall back to the first Remote entry
                prefs = [norm(c.split(",")[0]) for c in (profile.get("preferences.relocation_cities") or [])]
                hits = [k for k, t in enumerate(texts) if norm(t.split(",")[0]) in prefs]
                if not hits: hits = [k for k, t in enumerate(texts) if re.search(r"remote|anywhere|other", t, re.I)][:1]
                if hits:
                    for k in hits[:6]:
                        target = opts.nth(k).locator("label").first if opts.nth(k).locator("label").count() else opts.nth(k)
                        humanize.human_click(page, target); page.wait_for_timeout(250)
                    continue
            if not ans or ans in ("__LLM__", "__COVER__"):
                unanswered.append(q[:120]); continue
            na = norm(ans); key = norm(ans.split("/")[0].split("(")[0].split(",")[0])
            pick = next((k for k, t in enumerate(texts) if norm(t) == na or norm(t) == key), None)
            if pick is None: pick = next((k for k, t in enumerate(texts) if norm(t).startswith(key) or key in norm(t)), None)
            if pick is None and na.startswith(("yes", "no")):
                first = na.split()[0]; pick = next((k for k, t in enumerate(texts) if norm(t) == first or norm(t).startswith(first + " ")), None)
            if pick is None and re.search(r"india|mumbai|relocate", ans, re.I):
                pick = next((k for k, t in enumerate(texts) if re.search(r"other|international|outside|none of|elsewhere|not listed|open to relocat|willing to relocat|remote", t, re.I)), None)
            if pick is None and "decline" in na:
                pick = next((k for k, t in enumerate(texts) if re.search(r"decline|prefer not|don.t wish", t, re.I)), None)
            if pick is None:
                unanswered.append(f"{q[:80]} (choices: {', '.join(texts[:6])})"); continue
            target = opts.nth(pick).locator("label").first if opts.nth(pick).locator("label").count() else opts.nth(pick)
            humanize.human_click(page, target); page.wait_for_timeout(300)
        except Exception as e:  # noqa: BLE001
            log("warn", f"checkbox group: {str(e)[:80]}")
    return unanswered


def refill_phone(page, log, scope=None) -> int:
    """Phone inputs get cleared when the country picker changes after them (Greenhouse intl-tel-input): fill any empty tel field."""
    root = scope or page
    n = 0
    try:
        digits = re.sub(r"[^\d]", "", profile.get("identity.phone") or "")
        local = digits[-10:] if len(digits) > 10 else digits
        for el in root.locator("input[type='tel'], input[name*='phone' i], input[id*='phone' i]").all():
            try:
                if el.is_visible() and not el.input_value(timeout=500):
                    el.click(); el.type(local, delay=30); n += 1
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        log("warn", f"refill phone: {str(e)[:60]}")
    return n


def form_errors(page) -> list[str]:
    """Validation messages shown after a failed submit (Ashby 'Your form needs corrections', Greenhouse/Lever field errors)."""
    out = []
    for sel in ["[class*='error'] li", "[class*='Error'] li", ".field-error-msg", "[role='alert'] li", "[role='alert']", ".artdeco-inline-feedback--error"]:
        loc = page.locator(sel)
        for k in range(min(loc.count(), 8)):
            try:
                t = loc.nth(k).inner_text(timeout=500).strip()
                if t and t not in out and len(t) < 300: out.append(t)
            except Exception:
                pass
    return out


def fill_custom_dropdowns(page, log, scope=None) -> list[str]:
    """Non-native dropdowns: vue-select (.vs__dropdown-toggle / [role=combobox] div), react-select (.select__control),
    Personio 'Please select' buttons. Click, type the answer if there is an input, pick the best visible option."""
    root = scope or page
    unanswered = []
    ctls = root.locator("[role='combobox']:not(input), .vs__dropdown-toggle, .select__control, button:has-text('Please select'), [class*='select__control'], [class*='dropdown-toggle']")
    for i in range(ctls.count()):
        c = ctls.nth(i)
        try:
            if not c.is_visible(): continue
            cur = c.inner_text(timeout=500).strip().lower()
            if cur and cur not in ("", "please select", "select...", "select", "-", "choose", "choose...") and "select" not in cur: continue
            label = _label_for(page, c)
            if not label or re.search(r"search|sort|filter|language$|^(i am )?currently in\b|^(i can )?relocate to\b", label.strip(), re.I): continue   # Wellfound's own relocation picker is handled elsewhere
            ans = answer_question(label, "", None, log)
            if ans == "__DATE_PLUS_30__": ans = "1 month"
            if not ans or ans == "__LLM__":
                unanswered.append(label[:120]); continue
            humanize.human_click(page, c); page.wait_for_timeout(900)
            inp = c.locator("input").first
            key = ans.split("/")[0].split("(")[0].strip()
            OPT0 = "[role='option'], .vs__dropdown-option, .select__option, [class*='select__option'], [class*='dropdown-menu'] li, ul[role='listbox'] li"
            n_open = root.locator(OPT0).filter(visible=True).count() or page.locator(OPT0).filter(visible=True).count()
            if n_open == 0 and inp.count() and inp.is_visible():
                # menu did not open on click (react-select toggled shut): focus the input and open with ArrowDown
                inp.focus(); page.keyboard.press("ArrowDown"); page.wait_for_timeout(700)
                n_open = root.locator(OPT0).filter(visible=True).count() or page.locator(OPT0).filter(visible=True).count()
            if inp.count() and inp.is_visible() and (n_open == 0 or n_open > 15):
                # long lists (countries, schools) are searched by the first word; short yes/no lists are read as-is
                inp.type(key.split(",")[0].split()[0][:30] if n_open > 15 or not n_open else key[:30], delay=40); page.wait_for_timeout(900)
            OPT = "[role='option'], .vs__dropdown-option, .select__option, [class*='select__option'], [class*='dropdown-menu'] li, ul[role='listbox'] li"
            opts = root.locator(OPT).filter(visible=True)          # options render inside the same frame (Greenhouse iframe)
            if not opts.count(): opts = page.locator(OPT).filter(visible=True)
            texts = [t.strip() for t in opts.all_inner_texts()]
            if not texts and inp.count() and re.search(r"school|university|college", label, re.I):
                # school not in the employer's list: fall back to the 'Other' entry
                inp.fill(""); inp.type("Other", delay=40); page.wait_for_timeout(1000)
                opts = root.locator(OPT).filter(visible=True)
                if not opts.count(): opts = page.locator(OPT).filter(visible=True)
                texts = [t.strip() for t in opts.all_inner_texts()]; key = "Other"
            norm = lambda x: re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
            nk, na = norm(key), norm(ans)
            pick = next((k for k, t in enumerate(texts) if norm(t) == nk or norm(t) == na), None)          # exact ("No", "Yes")
            if pick is None and nk in ("yes", "no") and sum(1 for t in texts if norm(t).startswith(nk + " ")) > 1:
                pick = next((k for k, t in enumerate(texts) if norm(t).startswith(nk + " ") and re.search(r"not one of|other|none of|not listed", t, re.I)), None)
            if pick is None: pick = next((k for k, t in enumerate(texts) if norm(t).startswith(nk)), None)  # "India +91" before "British Indian Ocean"
            if pick is None: pick = next((k for k, t in enumerate(texts) if nk in norm(t) or norm(t) in na), None)
            if pick is None and na.startswith(("yes", "no")):
                first = na.split()[0]
                pick = next((k for k, t in enumerate(texts) if norm(t) == first), None) or next((k for k, t in enumerate(texts) if norm(t).startswith(first + " ")), None)
            if pick is None and "daily" in na:
                pick = next((k for k, t in enumerate(texts) if re.search(r"daily|every day|regularly|frequently|all the time", t, re.I)), None)
            if pick is None and "decline" in na:
                pick = next((k for k, t in enumerate(texts) if re.search(r"decline|prefer not|don.t wish|not to answer", t, re.I)), None)
            # Never select an unrelated first option as a fallback.
            if pick is None and texts and re.search(r"india|mumbai|relocate", ans, re.I):   # location list without India: pick Other / International
                pick = next((k for k, t in enumerate(texts) if re.search(r"other|international|outside|not listed|remote|elsewhere", t, re.I)), None)
            if pick is None and texts and ans.lower().startswith(("yes", "no")):
                pick = next((k for k, t in enumerate(texts) if t.lower().startswith(ans[:2].lower())), None)
            if pick is None:
                page.keyboard.press("Escape"); unanswered.append(f"{label[:80]} (options: {', '.join(texts[:6])})"); continue
            humanize.human_click(page, opts.nth(pick)); page.wait_for_timeout(600)
        except Exception as e:  # noqa: BLE001
            log("warn", f"custom dropdown '{label if 'label' in dir() else '?'}': {str(e)[:80]}")
    return unanswered


def fill_work_history(page, log, scope=None) -> list[str]:
    """Greenhouse-style employment/education blocks: Company name, Title, Start/End month+year, School, Degree, Discipline."""
    root = scope or page
    from backend.core import profile as _p
    prof = _p.load(); job = prof["experience"][0]; edu = prof["education"][0]
    vals = {"company": job["company"], "title": job["title"].split(" (")[0], "start_month": job["start"][5:7], "start_year": job["start"][:4],
            "school": edu["school"].split(" (")[0],
            "degree": "Bachelor's Degree" if re.search(r"b\.?\s*(tech|e|sc|a)\b|bachelor", str(edu.get("degree", "")), re.I) else "Master's Degree" if re.search(r"m\.?\s*(tech|s|sc|a|ba)\b|master", str(edu.get("degree", "")), re.I) else str(edu.get("degree", "")),
            "discipline": str(edu.get("degree", "")).split(",", 1)[1].strip() if "," in str(edu.get("degree", "")) else "Computer Science",
            "edu_start": str(edu.get("start", "")), "edu_end": str(edu.get("end", ""))}
    filled = []
    def put(sel, val, is_select=False):
        loc = root.locator(sel)
        if not loc.count() or not loc.first.is_visible(): return
        try:
            if is_select:
                opts = [o.strip() for o in loc.first.locator("option").all_inner_texts()]
                pick = next((o for o in opts if val.lower() in o.lower() or o.lower().startswith(val.lower()[:3])), None)
                if pick: loc.first.select_option(label=pick); filled.append(sel)
            elif not loc.first.input_value():
                loc.first.fill(val); filled.append(sel)
                page.wait_for_timeout(600)
                opt = page.locator("[role='option'], .select2-results li, li.select2-results__option").filter(visible=True).first
                if opt.count(): opt.click()
        except Exception:
            pass
    put("input[name*='company_name' i], input[id*='company' i]", vals["company"])
    put("input[name*='title' i][name*='employment' i], input[id*='job-title' i], input[id*='title' i]:not([id*='degree' i])", vals["title"])
    put("select[name*='start_date' i][name*='month' i], select[id*='start-date-month' i]", vals["start_month"], True)
    put("select[name*='start_date' i][name*='year' i], select[id*='start-date-year' i]", vals["start_year"], True)
    put("input[name*='school' i], input[id*='school' i]", vals["school"])
    put("select[name*='degree' i], input[name*='degree' i], input[id*='degree' i]", vals["degree"], root.locator("select[name*='degree' i]").count() > 0)
    put("select[name*='discipline' i], input[name*='discipline' i]", vals["discipline"], root.locator("select[name*='discipline' i]").count() > 0)
    put("select[name*='education' i][name*='end' i][name*='year' i], select[id*='education-end-year' i]", vals["edu_end"], True)
    # tick "I currently work here" if present
    cur = root.locator("label:has-text('currently work here') input, input[name*='current' i][type='checkbox']").first
    try:
        if cur.count() and not cur.is_checked(): cur.check()
    except Exception:
        pass
    return []


def greenhouse_specifics(page, cover_note: str | None, log, scope=None) -> list[str]:
    """Greenhouse job_app form: cover letter block needs 'Enter manually' before a textarea exists; location is a type-and-pick
    autocomplete (#job_application_location / #candidate-location); education uses select2 comboboxes."""
    root = scope or page
    unanswered = []
    try:
        if root.locator("#job_application_location, #candidate-location, input[id*='location' i][role='combobox']").count():
            loc = root.locator("#job_application_location, #candidate-location, input[id*='location' i][role='combobox']").first
            if not loc.input_value():
                city = (profile.get("identity.location") or "").split(",")[0].strip() or profile.get("identity.location")
                loc.click(); loc.fill(city); page.wait_for_timeout(1500)
                opt = root.locator("[role='option'], .select2-results li, li[class*='option'], .pac-item").filter(visible=True).first
                if not opt.count(): opt = page.locator("[role='option'], .select2-results li, li[class*='option'], .pac-item").filter(visible=True).first
                if opt.count(): opt.click()
                else: page.keyboard.press("ArrowDown"); page.keyboard.press("Enter")
                page.wait_for_timeout(600)
    except Exception as e:  # noqa: BLE001
        log("warn", f"greenhouse location: {str(e)[:80]}")
    try:
        cl = root.locator("#cover_letter_text, textarea[name*='cover_letter' i]")
        if not cl.count() or not cl.first.is_visible():
            manual = root.locator("a:has-text('Enter manually'), button:has-text('Enter manually'), [data-source='paste']")
            for k in range(manual.count()):
                m = manual.nth(k)
                try:
                    if m.is_visible() and re.search(r"cover", (m.evaluate("e => (e.closest('[id*=cover], [class*=cover], .field') || e.parentElement.parentElement).innerText") or ""), re.I):
                        m.click(); page.wait_for_timeout(700); break
                except Exception:
                    continue
            cl = root.locator("#cover_letter_text, textarea[name*='cover_letter' i]")
        if cl.count() and cl.first.is_visible() and not cl.first.input_value() and cover_note:
            humanize.human_type(page, cl.first, cover_note)
    except Exception as e:  # noqa: BLE001
        log("warn", f"greenhouse cover letter: {str(e)[:80]}")
    # select2-style education / degree comboboxes
    edu = (profile.load().get("education") or [{}])[0]
    school = str(edu.get("school") or "").split(" (")[0]
    degree = str(edu.get("degree") or "")
    level = "Bachelor" if re.search(r"b\.?\s*(tech|e|sc|a)\b|bachelor", degree, re.I) else "Master" if re.search(r"m\.?\s*(tech|s|sc|a|ba)\b|master", degree, re.I) else degree.split(",")[0]
    discipline = degree.split(",", 1)[1].strip() if "," in degree else "Computer Science"
    for sel, val in [("[id*='school' i] input.select2-input, input[id*='school' i]", school), ("[id*='degree' i] .select2-choice, select[id*='degree' i]", level), ("[id*='discipline' i] .select2-choice, select[id*='discipline' i]", discipline)]:
        if not val: continue
        try:
            el = root.locator(sel).first
            if not el.count() or not el.is_visible(): continue
            tag = el.evaluate("e => e.tagName")
            if tag == "SELECT":
                opts = [o.strip() for o in el.locator("option").all_inner_texts()]
                pick = next((o for o in opts if val.lower() in o.lower()), None)
                if pick: el.select_option(label=pick)
            else:
                el.click(); page.wait_for_timeout(500); page.keyboard.type(val, delay=30); page.wait_for_timeout(1200)
                opt = root.locator(".select2-results li, [role='option']").filter(visible=True).first
                if not opt.count(): opt = page.locator(".select2-results li, [role='option']").filter(visible=True).first
                if opt.count(): opt.click()
        except Exception:
            continue
    return unanswered


def tick_certifications(page, log, scope=None) -> int:
    """Tick required 'I confirm / I certify / I agree / I acknowledge' checkboxes (not marketing opt-ins)."""
    n = 0
    boxes = (scope or page).locator("input[type='checkbox']")
    for i in range(boxes.count()):
        b = boxes.nth(i)
        try:
            if not b.is_visible() or b.is_checked(): continue
            label = _label_for(page, b) or b.evaluate("e => (e.closest('label') || e.parentElement).innerText || ''")
            if re.search(r"confirm|certify|agree|acknowledge|consent to (the )?processing|privacy policy|terms", label, re.I) and not re.search(r"newsletter|marketing|updates about|job alerts|promotional", label, re.I):
                explicit = (profile.load().get("declarations") or {}).get(label.lower().strip())
                if explicit is True or str(explicit).lower() == "yes":
                    humanize.human_click(page, b); n += 1
        except Exception:
            continue
    return n


def _upload_landed(page, el, scope) -> bool:
    """Proof the file actually attached: the input holds it, or the page now shows the file name."""
    try:
        if el.evaluate("e => e.files && e.files.length > 0"): return True
    except Exception:
        pass
    try:
        body = (scope or page).inner_text("body", timeout=1500).lower()
        return ".pdf" in body and "click or drag" not in body
    except Exception:
        return False


def upload_resume(page, resume_path: str, log, scope=None) -> bool:
    """Attach the resume. Handles a plain file input, a hidden input behind a drop zone, and drop zones that only
    respond to a click by opening the OS file chooser."""
    root = scope or page
    full = str(config.ROOT / resume_path)
    files = root.locator("input[type='file']")
    try:
        files.first.wait_for(state="attached", timeout=10000)
    except Exception:
        files = page.locator("input[type='file']")
        try: files.first.wait_for(state="attached", timeout=5000)
        except Exception: return _upload_via_chooser(page, root, full, log)
    last = None
    for attempt in range(3):
        try:
            target = files.first
            # prefer the input that sits inside a resume/CV block when several file inputs exist
            for i in range(files.count()):
                nm = ((files.nth(i).get_attribute("name") or "") + (files.nth(i).get_attribute("id") or "") +
                      (files.nth(i).get_attribute("aria-label") or "") + (files.nth(i).get_attribute("accept") or "")).lower()
                if re.search(r"resume|cv\b|pdf", nm): target = files.nth(i); break
            target.set_input_files(full)
            page.wait_for_timeout(2500)
            try:
                body = page.inner_text("body", timeout=2000).lower()
                if "failed to upload" in body or "upload failed" in body or "error uploading" in body:
                    log("warn", f"upload attempt {attempt + 1}: page reports upload failure; retrying")
                    page.wait_for_timeout(3000); continue
            except Exception:
                pass
            if _upload_landed(page, target, root): return True
            log("warn", f"upload attempt {attempt + 1}: the file did not attach; trying the drop zone")
            if _upload_via_chooser(page, root, full, log): return True
        except Exception as e:  # noqa: BLE001
            last = e; page.wait_for_timeout(1500)
    if _upload_via_chooser(page, root, full, log): return True
    log("warn", f"resume upload failed: {last}")
    return False


DROPZONE = ("text=/click or drag/i", "text=/drag (and drop|your file)/i", "text=/upload your (resume|cv)/i",
            "[class*='dropzone']", "[class*='drop-zone']", "[class*='upload']", "button:has-text('Upload')")


def _upload_via_chooser(page, root, full_path: str, log) -> bool:
    """Drop zones that ignore a hidden input still open the file chooser when clicked; answer it directly."""
    for sel in DROPZONE:
        try:
            zone = root.locator(sel).filter(visible=True).first
            if not zone.count():
                zone = page.locator(sel).filter(visible=True).first
            if not zone.count():
                continue
            with page.expect_file_chooser(timeout=6000) as chooser:
                zone.click()
            chooser.value.set_files(full_path)
            page.wait_for_timeout(3000)
            if _upload_landed(page, root.locator("input[type='file']").first, root):
                log("info", "resume attached through the drop zone")
                return True
        except Exception:
            continue
    return False
PAYMENT_SIGNALS = (
    "billed now", "you'll be charged", "you will be charged", "auto-renews", "auto renews", "renewal terms",
    "subscription period", "start your subscription", "payment method", "card number", "billing address",
    "cardholder", "per month", "/month", "monthly for the remaining", "12-month commitment", "upgrade to premium",
    "choose your plan", "select a plan", "checkout", "order summary", "subtotal",
)
PAYMENT_CONTROLS = ("apple pay", "google pay", "pay now", "subscribe", "start free trial", "continue to payment",
                    "complete purchase", "place order")


def payment_wall(page) -> str | None:
    """A job application never asks for money. Some boards route 'Apply' through a paid-subscription funnel, so any page
    showing money, a plan or a payment control stops the application instead of being filled in.
    Returns the phrase that triggered it."""
    try:
        body = page.inner_text("body", timeout=2500).lower()
    except Exception:
        return None
    hits = [p for p in PAYMENT_SIGNALS if p in body]
    if len(hits) >= 2:
        return hits[0]
    try:
        for label in PAYMENT_CONTROLS:
            control = page.locator(f"button:has-text('{label}'), [role='button']:has-text('{label}')").filter(visible=True)
            if control.count() and (hits or re.search(r"[$€£₹]\s?\d", body)):
                return label
    except Exception:
        pass
    if re.search(r"[$€£₹]\s?\d+(\.\d{2})?\s*(/|per\s)?(mo|month|yr|year)", body) and any(
            w in body for w in ("plan", "subscription", "billed", "payment")):
        return "recurring charge"
    return None


def has_captcha(page) -> bool:
    """True only for a real challenge the user must solve: Cloudflare/Turnstile interstitials, a visible reCAPTCHA/hCaptcha
    checkbox or image challenge. The invisible reCAPTCHA v3 badge (bottom-right on every Greenhouse page) is NOT a challenge."""
    try:
        body = page.inner_text("body", timeout=2000).lower()
        if any(t in body for t in ("verify you are human", "performing security verification", "checking your browser", "i'm not a robot", "i am not a robot")):
            return True
    except Exception:
        pass
    try:
        for fr in page.locator("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='turnstile']").all():
            src = (fr.get_attribute("src") or "").lower()
            if "anchor" in src or "bframe" in src or "challenge" in src or "hcaptcha" in src or "turnstile" in src:
                box = fr.bounding_box()
                if box and box["width"] > 150 and box["height"] > 60 and fr.is_visible():
                    return True
        return False
    except Exception:
        return False


def validate_required(page, scope=None) -> list[str]:
    root = scope or page
    missing = []
    for el in root.locator("input[required], select[required], textarea[required], [aria-required='true']").all():
        try:
            if not el.is_visible() or not el.is_enabled(): continue
            kind = el.get_attribute('type') or ''
            if kind in ('checkbox','radio'):
                if kind == 'radio':
                    name = el.get_attribute('name')
                    if name and root.locator("input[type='radio']").evaluate_all("(els, name) => els.some(e => e.name === name && e.checked)",name): continue
                elif el.is_checked(): continue
                else: missing.append(_label_for(root,el)); continue
            elif kind == 'file':
                if el.evaluate('e => e.files && e.files.length'): continue
            elif el.input_value(timeout=500).strip(): continue
            missing.append(_label_for(root,el) or 'Required field')
        except Exception: missing.append('Required control could not be verified')
    return list(dict.fromkeys(missing))
