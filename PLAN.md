# Job Autopilot — Implementation Plan

Goal: find relevant roles daily across the researched platforms, tailor the resume and note to each job description, apply directly where a form exists, and otherwise reach the recruiter / hiring manager by email. Target: 100 applications + outreach touches per day, with a human review queue for anything the system is not confident about.

Owner: Pushkar Mishra. Positioning: Forward Deployed Engineer / Applied AI Engineer. Base: India (IST). Open to remote (global), and onsite/hybrid abroad with sponsorship.

---

## 1. Platform automation map

Legend: A = fully scriptable (public API/RSS/standard forms). B = browser automation with a logged-in session, moderate ban risk, human-paced. C = discovery only, human completes. D = one-time onboarding, no per-job automation. X = not automatable.

### A — Fully scriptable (core of the 100/day)

| Source | Discover | Apply | Notes |
|---|---|---|---|
| Greenhouse boards | Public JSON: boards-api.greenhouse.io/v1/boards/{co}/jobs | Playwright form fill (standardized) | Most YC / VC-backed startups. Build a company list (YC directory, Wellfound, HN) and poll each board. |
| Lever | api.lever.co/v0/postings/{co}?mode=json | Playwright form fill | Standard form + custom questions. |
| Ashby | api.ashbyhq.com/posting-api/job-board/{co} | Playwright form fill | Growing fast among AI startups. |
| Workable, SmartRecruiters, Rippling, Recruitee, Teamtailor, Personio, BambooHR, Breezy, JazzHR | Public board JSON/HTML | Playwright form fill | Add handlers incrementally by frequency seen. |
| Remote OK | remoteok.com/api (JSON) | Redirects to company ATS → route to handler | Include `Worldwide` filter. |
| Remotive | remotive.com/api/remote-jobs | Redirect → ATS | Good startup coverage. |
| Himalayas | himalayas.app/jobs/api | Redirect → ATS | Country-eligibility fields, useful for India filter. |
| Arbeitnow | arbeitnow.com/api/job-board-api | Redirect → ATS | Visa sponsorship flag in data. |
| We Work Remotely | RSS feeds by category | Redirect → ATS or email | |
| Working Nomads, NoDesk, Jobspresso, Remote.co | RSS / HTML scrape | Redirect → ATS | Lower volume. |
| HN Who is Hiring | HN Algolia API (monthly thread + comments) | Email in post → personalized email | Highest-signal channel for FDE/AI roles at small teams. |
| Reddit r/forhire, r/RemoteJobs, r/remotejs | Public .json endpoints | Email/link in post | Low volume, some noise. |
| Relocate.me, Japan Dev, TokyoDev | HTML scrape / RSS | Redirect → ATS | Sponsorship-friendly by design. |
| HiringCafe | Unofficial internal API (scrape) | Redirect → ATS | Broadest aggregator of career pages; treat as discovery multiplier. |
| UK Register of Licensed Sponsors, US H-1B LCA disclosure data | Public CSV downloads | Enrichment only | Tag every company with "known sponsor" for UK/US. |
| GitHub lists (tech-jobs-with-relocation, visa-sponsored-jobs) | Poll repo | Redirect | |

### B — Browser automation, logged-in, human-paced (caps enforced)

| Source | Discover | Apply | Risk / cap |
|---|---|---|---|
| YC Work at a Startup | Playwright search with your profile | Send interest message per company (their form) | One profile, ~20/day cap, low-moderate risk. |
| Wellfound | Playwright search (Cloudflare) | Apply + note per job | Moderate. ~20/day. |
| Welcome to the Jungle (Otta) | Playwright | Apply via redirect or in-app | Low. |
| Naukri | Playwright search | One-click apply | Low. India roles only. ~30/day. |
| Cutshort, Instahyre, Hirist | Playwright | Apply/show interest | Low-moderate. India + some remote. |
| Peerlist | Playwright | Apply | Low. |
| LinkedIn | Search scrape via logged-in session, slow pace | DECIDED: full automation — Easy Apply, connection requests with note, follow-up DMs to recruiters/hiring managers. Non-Easy-Apply redirects go to ATS handler. | HIGH ban risk (user accepted). Hard daily caps, randomized 20–90 s delays, business hours only, real Chrome profile, stop-on-warning banner detection. Caps set in config. |
| Levels.fyi Jobs, Built In, Startup.jobs, Cord | Scrape | Redirect → ATS | Low. |

### C — Discovery only, human completes

| Source | How |
|---|---|
| X / Twitter | Search "hiring" + ("forward deployed" OR "AI engineer" OR "agents") + remote via X API basic ($100/mo) or scheduled scrape of saved searches. Extract company → route to ATS/email. DMs not automated (ToS). |
| Threads | Keyword search scrape → company → ATS/email. |
| Discord / Slack #jobs | Read channels you belong to (user-account bots violate ToS; use official bot where allowed or manual skim). Output goes into the same jobs table. |
| tal.club, Hirect | Mobile apps only. Manual, Bengaluru-only for tal.club. |
| Blind, Fishbowl | Referral hunting, manual. |

### D — One-time onboarding (not per-job)

Toptal, Turing, Arc.dev, Lemon.io, Braintrust, A.Team, Gun.io, Crossover, Andela, Uplers, Contra, Upwork. Do the vetting once; inbound after that.

### X — Not worth automating

FlexJobs (paid, manual), Telegram (scam-heavy), LinkedIn DMs at volume, any CAPTCHA step (always human).

DECIDED: automate 100% of the list, not just A/B. Approach for tiers C/D/X:
- Every platform gets its own **skill** (a Claude Code skill under `.claude/skills/<platform>/` with SKILL.md + a Playwright script). Build order: user walks me through the platform once in a logged-in browser (a "recording session"); I turn that flow into the script + skill; the scheduler then runs the skill unattended.
- X / Threads: browser automation of search + DM/reply in the logged-in session, same humanization and caps as LinkedIn.
- Discord / Slack: browser automation reading #jobs channels of servers/workspaces the user is in; replies drafted and sent via browser at low caps.
- tal.club / Hirect (mobile-only): run the Android app in an emulator (Android Studio AVD) driven by Appium/adb, or iOS Simulator if an iOS build exists. Lowest priority; built last with the user's help.
- Vetted networks (Toptal, Turing, Arc, etc.): skill automates account creation steps that are form-based and pauses for the human at tests/interviews.
- FlexJobs and other paywalled boards: skill automates search + redirect; user decides on subscription.
- CAPTCHA / bot-check pages are never bypassed: the skill screenshots, pauses, and pings the user to solve, then resumes.

Expected volume mix is still ~80% ATS direct-apply, ~15% email/DM outreach, ~5% platform-native applies — but every source is wired in.

---

## 2. Pipeline design

```
Collectors ─► Normalize + dedupe ─► Eligibility rules ─► LLM fit score ─► Enrich (sponsor lists, company info)
     │                                                                        │
     └────────────────────────────── jobs table (SQLite → Postgres) ◄─────────┘
                                          │
                     ┌────────────────────┼─────────────────────┐
                     ▼                    ▼                     ▼
              Tailor (resume PDF     Apply worker            Outreach worker
              + cover note + Q&A)    (per-ATS handlers,      (contact finder →
                     │               generic form filler)    Gmail sequences)
                     └──────────► Review queue (dashboard) ◄─────────┘
                                          │
                                    Tracker + inbox parser (replies, interviews, rejections)
```

### 2.1 Collectors
- One module per source, run on a schedule (every 2–4 h for APIs, daily for scrapes). Output: normalized Job {source, company, title, url, apply_url, ats_type, location, remote_scope, salary, posted_at, description_md, raw}.
- ATS detection from apply_url domain (greenhouse.io, lever.co, ashbyhq.com, workable.com, ...).
- Company seed list: YC directory (public), Wellfound top AI companies, HN posters, "AI-native" lists. Greenhouse/Lever/Ashby boards polled directly for every seeded company (~500–1500 companies).
- Dedupe: hash(company, normalized title, location) + URL canonicalization. Never apply twice to the same company for the same role family within 90 days.

### 2.2 Eligibility rules (hard filters, no LLM)
- Remote scope must include India / APAC / Worldwide, OR role is onsite in a target country AND company is on a sponsor register or the JD mentions sponsorship/relocation.
- Seniority: exclude Staff/Principal/Director/Intern unless JD years ≤ 6.
- Title keywords: forward deployed, applied AI, AI engineer, LLM, agent, solutions engineer (AI), founding engineer, full-stack/backend at AI-native companies.
- Exclusions: clearance-required, US-citizen-only, agencies/staffing firms, duplicates.

### 2.3 LLM fit score
- LLM scores 0–100 with reasons against the master profile; threshold (default 65) to enter the apply queue. Stores must-have gaps, so the tailor can address or skip.
- DECIDED: pluggable LLM provider layer. `llm(task, prompt)` with per-task routing in config:
  - `claude-cli` (default): Claude Code CLI `claude -p --output-format json` on the user's subscription, no API key.
  - `gemini`: Google Gemini via user's key (google-genai SDK).
  - `openrouter`: any model via OpenRouter key (OpenAI-compatible endpoint).
  - `ollama`: local models (already installed).
  Defaults: scoring/classification → cheap fast model (gemini flash or ollama), resume tailoring + outreach copy → claude-cli, fact-check → a *different* provider than the one that wrote the text. Keys live in `.env`, never in chat or repo. Response cache keyed on (provider, prompt hash).

### 2.4 Tailoring (truthfulness-locked)
- master_profile.yaml: every fact, bullet (with 2–3 phrasings each), metric, skill, screening answer (notice period, sponsorship needed, salary expectation, work authorization, relocation willingness, AI-use disclosure).
- The LLM (via Claude Code CLI) selects and rephrases bullets, orders skills, writes a 3-line summary and a 120–180 word note keyed to the JD. Constraint: may only use facts in master_profile; a second pass (fact-checker) diffs claims against the master file and rejects anything new.
- Render: single-column HTML → PDF (Playwright print), ATS-safe fonts, filename `Pushkar_Mishra_<Company>_<Role>.pdf`. Keyword-coverage check against JD (report, not stuffing).
- Store every generated artifact per application for later reference in interviews.

### 2.5 Apply worker
- Humanization layer (shared by every skill, DECIDED): non-headless Chromium with the user's persistent profile; gaussian-distributed delays (2–8 s between actions, 20–90 s between applications, longer on LinkedIn/X); Bezier mouse paths and scroll-into-view before every click; per-character typing with jitter and occasional backspace; random idle pauses; business-hours schedule in the target region; per-domain daily caps and hourly ceilings from config; stop-on-warning (detects "unusual activity", captcha, rate-limit banners → pause + notify user); randomized viewport and run order. Nothing here bypasses a CAPTCHA or spoofs identity; it only paces the real logged-in user's actions.
- Playwright with a persistent Chrome profile per platform. Handlers: greenhouse, lever, ashby, workable, smartrecruiters, rippling, generic.
- Generic handler: reads form fields, maps to profile via LLM (label → field), fills, uploads PDF, answers custom questions from the answers bank; unknown question or CAPTCHA → pause and push to review queue with screenshot.
- Confidence gate: known ATS + all fields mapped + score ≥ 75 → auto-submit (if enabled). Otherwise → review queue, one-click approve.
- Screenshot before and after submit; store confirmation text.
- Rate limits and jitter per domain; run in business hours of target region.

### 2.6 Outreach worker
- For every applied job (and every email-only posting): find 1–2 humans — recruiter/talent partner, hiring manager, or eng lead. DECIDED (no paid APIs): name in JD/post → LinkedIn people search in the logged-in browser (title filter: recruiter, talent, hiring manager, head of eng) → company website team/about page → email pattern inference (first@, first.last@) with MX check; no paid verification. Where no email is found, outreach goes via LinkedIn connection note + DM instead.
- DECIDED: Gmail API with one-time OAuth consent, sending from shanumishra199@gmail.com (same address as resume and LinkedIn). Same OAuth grant covers Sheets and Drive. Sequence: day 0 (applied + why me, 120 words, tailored PDF attached or linked), day 4 follow-up, day 9 final. Stop on any reply. Cap: 40 new cold emails/day initially, ramp to 60. Unsubscribe-style courtesy line. No mass BCC.
- LinkedIn: automated connection request with 300-char note, then DM after acceptance (user decision). Daily caps in config.
- X/Threads posts: reply/DM text generated, manual send.

### 2.7 Tracker: Google Sheet (primary, for user review) + local DB
- DECIDED: a Google Sheet is the user-facing tracker, written via Google Sheets API using the same free OAuth consent as Gmail. SQLite stays as the system of record; a sync job mirrors to the sheet after every run.
- Sheet tabs: `Jobs` (every discovered job: source, company, title, url, location, remote scope, salary, sponsor flag, fit score, status), `Applications` (date, company, role, platform, method [ATS/EasyApply/email/DM], resume file link, note text, confirmation screenshot link, status), `Outreach` (contact name, title, channel, message, sent/replied dates, follow-up schedule), `Review Queue` (rows the user approves/rejects by editing a cell; the system reads the cell back), `Daily Stats` (per-source counts vs the 100/day target), `Errors` (skill failures with screenshot links).
- Generated PDFs and screenshots go to a Google Drive folder; the sheet links to them.
- Dashboard (FastAPI + small UI) is secondary for live queues and one-click approve; the sheet is the review surface.
- Inbox parser: Gmail label rules + LLM classification of replies (interview request / rejection / question) → status updates in DB and sheet.

### 2.8 Ops
- Runs on the MacBook via launchd (or a small VPS with a residential-ish IP for API collectors; browser workers stay on the Mac to keep logged-in sessions).
- Secrets in .env. Logs per run. Daily summary message (email or Telegram bot to yourself).

### Stack
Python 3.12, Playwright (persistent Chrome profile the user logs into once per site), SQLAlchemy + SQLite, FastAPI, Claude Code CLI as the LLM engine (no API keys), Jinja2 + Playwright PDF, launchd. No paid third-party APIs. n8n optional for glue if preferred.

### Decisions log (2026-09-05)
- LLM: Claude Code CLI by default, with Gemini / OpenRouter / Ollama selectable per task. Keys in .env.
- Email: Gmail API (free OAuth) from shanumishra199@gmail.com. Google Sheet + Drive as the review tracker via the same OAuth.
- LinkedIn caps: 15 Easy Apply, 20 connection requests, 20 DMs per day (conservative).
- Coverage: 100% of platforms, one skill per platform, built from user-assisted recording sessions. Mobile-only apps via Android emulator, last.
- Anti-ban: shared humanization layer (random delays, human-like clicks/typing, business hours, caps, stop-on-warning). CAPTCHAs always go to the user.
- Submission: review queue for 2 weeks, then auto-submit for high-confidence ATS applies.
- LinkedIn: full automation including Easy Apply, connection requests, DMs. User accepts ban risk; caps enforced.
- No paid APIs anywhere. Browser automation only; user logs in when prompted and provides session where needed.
- Employment types: remote full-time (EOR/direct), remote contractor/B2B, onsite/hybrid abroad with sponsorship, and India-based roles.

---

## 3. Build phases (≈3.5 weeks to 100/day across all platforms)

Phase 0 — Foundations (days 1–2)
- master_profile.yaml with metrics (needs your numbers), answers bank, single-column ATS resume template, LinkedIn headline/About/experience/skills rewrite, one email address, Open-to-Work (recruiter-only).

Phase 1 — Ingest + score (days 3–5)
- DB schema, collectors for all tier-A APIs/RSS + Greenhouse/Lever/Ashby company polling, dedupe, rules, LLM scoring, minimal dashboard. Target: 200–400 relevant new jobs/week visible.

Phase 2 — Tailor + apply (days 6–10)
- Tailor pipeline with fact-checker, PDF render, handlers for Greenhouse/Lever/Ashby/Workable + generic, review queue, screenshots. Target: 30 applications/day in review mode.

Phase 3 — Outreach (days 11–13)
- Contact finder, Gmail sequences, HN/Reddit email applies, inbox parser. Target: 30 emails/day.

Phase 4 — Platform skills, recorded with the user (days 14–19)
- One recording session per platform (user logged in, walks through search → apply/message once). Order: LinkedIn (search, Easy Apply, connect, DM), YC WaaS, Wellfound, Welcome to the Jungle, Naukri, Cutshort, Instahyre, Hirist, Peerlist, X, Threads, Discord, Slack, Reddit reply, Levels.fyi, Built In, Startup.jobs, Cord, Relocate.me, Japan Dev, TokyoDev, vetted-network onboarding (Toptal, Turing, Arc, Lemon.io, Braintrust, Crossover, Uplers), FlexJobs, then tal.club / Hirect via Android emulator.
- Each becomes `.claude/skills/<platform>/` and is registered in the scheduler with its own caps. Sponsor-register enrichment lands here too.

Phase 5 — Scale + tune (days 20–23)
- Auto-submit on high-confidence ATS applies, parallel browser workers, per-source conversion review, prune dead sources. Target: 100 touches/day (≈70 applies + 30 outreach).

---

## 4. Risks and mitigations
- Platform bans (LinkedIn, Wellfound): discovery-only defaults, hard caps, human pacing, persistent real profile, never headless for tier B.
- Gmail reputation: dedicated address, ≤60/day, personalized, no attachments on first follow-ups, stop on reply.
- Fabrication: fact-checker pass + master profile as the only source; screening answers always truthful (sponsorship needed = yes where true).
- CAPTCHA / bot checks: always routed to you; never bypassed.
- Supply: truly relevant FDE/AI remote-eligible roles may be 30–80 new/day globally; hitting 100 requires including full-stack/backend at AI-native companies and outreach touches.
- Duplicate applications across aggregators: canonical company+role key.

---

## 5. Inputs needed from you
1. Real metrics for the three Purplle systems (users affected, effort/time saved, engagement lift, price updates/day, requests auto-adjudicated).
2. Compensation floor (remote contractor USD/yr, EOR employee, onsite by country), notice period, earliest start.
3. Target countries for onsite/hybrid (ranked) and any you exclude.
4. Which email to use for the pipeline; whether to create a fresh one.
5. Whether Ignite / DevHive repos are public.
6. Gemini / OpenRouter keys placed in `.env` by you (never paste in chat). Ollama models you want available.
7. Confirm you have accounts on: Wellfound, YC Work at a Startup, Welcome to the Jungle, Naukri, Cutshort, Instahyre, Hirist, Peerlist, X, Threads, Reddit; and which Discord servers / Slack workspaces you are in.
8. Time windows when you can sit with me for the per-platform recording sessions (≈20–30 min each).
