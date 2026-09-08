# Job Autopilot

A local, self-hosted assistant that finds jobs that fit you, prepares a truthful tailored resume and cover note for each one,
fills the application forms, and drafts recruiter outreach. Everything runs on your own computer with your own logins;
you approve what goes out. Built for one person first, but any user can set it up from their own resume.

## How it works

```
discover  →  hydrate  →  score  →  prepare  →  YOU REVIEW  →  apply  →  outreach  →  track
```

1. **Discover.** Collectors pull postings from public boards and ATS job boards (Greenhouse, Ashby, Lever, HiringCafe,
   Arbeitnow, Hacker News "Who is hiring", Reddit, Relocate.me, Japan Dev …). Browser "skills" read the platforms that need a
   login (LinkedIn, Jobright, Wellfound, Y Combinator, Naukri, Instahyre, Cutshort, Hirist, Peerlist) using one dedicated
   Chrome profile that you sign into once.
2. **Hydrate.** Postings with no description are fetched from the employer page (schema.org JobPosting first). Closed
   postings are marked expired.
3. **Filter and score.** Hard rules (title list, location, work authorization, sponsorship signals) decide eligibility.
   The LLM then scores fit 0–100 against your profile and lists blockers and keywords.
4. **Prepare.** For every job above the threshold the LLM tailors your resume (it may only reorder, select and rephrase
   facts from your profile) and writes a cover note. A second, independent fact-check must pass or the application is not
   queued. A one-page PDF is rendered.
5. **Review.** Nothing is submitted until you approve it in the **Review queue**. Editing your profile after preparation
   marks those documents stale until you regenerate them.
6. **Apply.** A form worker fills ATS forms (Greenhouse, Ashby, Lever, Workable, generic career pages) from your profile
   and answer bank; platform skills use the platform's own apply flow (LinkedIn Easy Apply, Naukri, Instahyre …). Sites
   that need a human step (CAPTCHA, emailed security code, employer account) stop with `needs_human`; `assist.py` opens
   the filled form so you only do the human part. Unknown flows are learned once as a "recipe" and replayed later.
7. **Outreach.** For applied companies the system finds a recruiter or hiring manager, drafts an email and a LinkedIn note
   from your profile, and sends only after you approve. Day-4 and day-9 follow-ups are drafted for review. Replies and
   bounces stop the sequence. LinkedIn invitations and X/Twitter DMs work the same way.
8. **Track.** Applications, outreach and every run's log are in the dashboard; optional Google Sheets sync.

### Safety rules built in
- Every fact comes from `data/master_profile.yaml`. Generated text is fact-checked; unsupported claims block the document.
- Nothing is sent without approval. Approval covers the exact text; if a message changes it goes back to review.
- CAPTCHAs, bot checks and account creation are never bypassed. Security codes are typed only after you supply them.
- Daily caps per platform and human-like pacing. Runs stop on platform warnings.
- One dedicated automation Chrome profile, separate from your personal browser. Credentials are never stored by the app.
- Local only: the API accepts connections from this machine, and no data leaves except the LLM calls you configure.

## Requirements
- macOS or Linux, Python 3.13, Node 20+, Google Chrome installed.
- An LLM: the default is the Claude CLI (`claude` logged in on a Max plan, no API key). Gemini, OpenRouter or Ollama can
  be configured in `config.yaml` → `llm`.
- Optional: Google OAuth client secret for Gmail sending/polling and a service account for Sheets sync.

## Setup
```bash
git clone <this repo> job-autopilot && cd job-autopilot
uv sync --frozen                     # or: python3.13 -m venv .venv && .venv/bin/pip install -e .
npm --prefix frontend install && npm --prefix frontend run build
cp .env.example .env                 # only needed for Gemini/OpenRouter keys
./start.sh                           # backs up the DB, migrates, starts API (:8000) + worker
```
Open http://localhost:8000.

### First-time configuration (about 15 minutes)
1. **Profile page** → upload your resume (PDF/DOCX) → *Extract profile from resume*. Review the draft: fix anything wrong
   and fill in what a resume cannot know (salary expectations in the currencies you care about, notice period, relocation
   cities, work modes). *Save profile.*
2. **Profile page → Answer bank.** These are your canned answers to screening questions (regular expression → answer).
   Placeholders like `{preferences.expected_salary_lpa}` pull from your profile. Add anything specific to you
   (e.g. `^title:?$` → your salutation). `__LLM__` means "write it from my profile, fact-checked".
3. **Projects.** Anything under `projects:` in your profile always appears on the resume. Write a real one-line
   summary for each: the tailoring pass may reorder and rephrase, but it can never invent detail, so a thin summary
   stays thin. `resume.max_pages` in config.yaml (default 2) sets the length; content is only trimmed past that limit,
   and projects are never what gets trimmed.
4. **Settings.** `filters.titles_include/exclude` (which job titles count), `fit_threshold`, per-platform daily caps,
   `review_mode: true` (keep it on until you trust the output).
5. **Platforms page** → *Log in* on each platform you use. A Chrome window opens on the automation profile; sign in
   normally. Sessions persist between runs.

## Daily use
- **Run the system** page: pick one platform or all, leave *apply* off, press Run. Watch the live log; Stop at any time.
  This discovers, hydrates, scores and prepares; results land in the Review queue.
- **Review queue**: read the resume and cover note, approve or reject. Fix your profile and press *Regenerate* if
  something is wrong.
- **Run the system** again with *apply* on (or run the ATS worker / a platform's apply skill from the Platforms page) to
  submit approved applications. Anything that needs you shows as `needs_human` with a screenshot and reason.
- `.venv/bin/python assist.py` opens each `needs_human` form filled in, for you to finish (codes, CAPTCHAs, Workday
  accounts). `assist.py workday 3` does the next three Workday applications.
- **Questions**: when a form asks something your profile does not cover, the application stops and the question lands
  here. Answer it in plain words once ("about 30 days", "no, never used Azure") and it is stored where it belongs: the
  answer bank, your capabilities, or a declaration. Every later employer asking the same thing is handled without you.
  Answering never invalidates resumes that are already prepared.
- **Outreach**: approve drafted emails and LinkedIn notes; *Send approved emails*, *Check replies*, *Draft follow-ups*.
- **Runs & logs**: worker status (heartbeat), every run, retry for interrupted runs.

Optional automation: set `schedule.enabled: true` in `config.yaml` to collect every 3 h, score/prepare hourly, apply in
business hours and sync Sheets. The worker owns the schedule.

## Stopping and maintenance
```bash
./stop.sh                                                     # stop API and worker; the browser stays open
.venv/bin/python -m backend.core.ops backup --path /abs/path/backup.db
.venv/bin/python -m backend.core.ops report                   # reconciliation report (uncertain submissions, duplicates)
.venv/bin/python -m pytest -q                                 # offline tests, isolated temp database
```

## Layout
```
backend/app        FastAPI API, SQLAlchemy models, routers
backend/core       pipeline (score/tailor/factcheck), collectors, browser skills, apply worker + form filler + recipes,
                   outreach, gmail, durable worker, scheduler
frontend           React dashboard (served from frontend/dist by the API)
data/              your database, profile, answer bank, artifacts, Chrome profile — personal files are git-ignored;
                   *.example.yaml are fictional templates used until you save your own
IMPLEMENTATION_PLAN.md   engineering status: what is done, what is unproven, what is left
```

## Status
Actively developed. Proven end-to-end on Greenhouse/Ashby forms, Naukri, Instahyre, Hirist, Wellfound, YC and LinkedIn
Easy Apply; Workday employer accounts, some CAPTCHA sites and a few custom career pages still need the assisted flow.
See IMPLEMENTATION_PLAN.md before relying on any platform at scale.
