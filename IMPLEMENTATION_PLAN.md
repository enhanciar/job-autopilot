# Job Autopilot reliability implementation and handoff

Last updated: 2026-09-07 (batch 19). **Deployed locally on 2026-09-07 21:14 IST: migration v3 applied, API and durable worker restarted from current code.** Remaining items are listed per work package below; live platform reliability is still unproven.

## Start here (Claude or another coding agent)

User requested a complete audit, then a plan and implementation of all fixes. User subsequently requested a detailed, continuously updated plan so Claude can continue if Codex usage runs out. Continue implementation from this document; do not repeat the initial audit or assume all checked-in changes have been verified live.

Repository/workspace: `/Users/pushkarmishra/Desktop/job-autopilot`. It is **not currently a Git repository**. Initial source backup: `/tmp/job-autopilot-before-hardening.tgz`. Do not restore over current work. Temporary edit scripts `/tmp/harden1.py` through `/tmp/harden8.py` record edits but MUST NOT be rerun: replacements are not idempotent. The code files, not those scripts, are authoritative.

### Authorization and live-state boundaries

- Implement and test code. Preserve existing DB records, candidate profile, credentials, cookies and browser sessions.
- No live applications/messages were authorized as part of development. Do not test by sending applications, email, invitations, DMs or public replies.
- Batch 19 replaced the old API process (started 12:00, pre-hardening code) with the current code via `./start.sh` after a SQLite backup (`data/backups/autopilot-20260907-211454.db`). The durable worker is running; the queue was empty and the scheduler disabled when it started. `assist.py` was not running.
- Do not delete/copy Chrome profiles. Main Chrome and automation Chrome are separate, intentionally.
- Tests use `AUTOPILOT_DATA_DIR` pointing at a temporary directory. NEVER run recovery tests against `data/autopilot.db`.
- The new separate worker can send already approved work when started. Do not casually start it against the real DB during development. Validate with mocks/temporary DB first.
- Historical submitted statuses are NOT independently verified and must not be automatically changed or replayed.

### Immediate continuation checklist

1. Read this file, `backend/core/worker.py`, `runner.py`, `browser.py`, `submissions.py`, `messages.py`, and the test suite.
2. Run `.venv/bin/python -m pytest -q`; latest pass after batch 19: **58 tests passed**.
3. Run `npm --prefix frontend run build` and `npm --prefix frontend run lint` after frontend changes. Earlier build passed; lint had four RunSystem warnings, no errors.
4. Finish the explicit remaining work below, adding realistic regression tests as each issue is fixed.
5. After every coherent batch update the status table and append to the change log, recording exact checks and remaining limitations.
6. Finish documentation/launcher/operations support before a coordinated deployment. Do not claim live platform reliability based on offline mocks.

## Architecture and observed baseline

Python 3.13, FastAPI, SQLAlchemy/SQLite, synchronous Playwright using installed Chrome, LLM providers Claude CLI/Gemini/OpenRouter/Ollama, Jinja resume PDFs. React 19/TypeScript/Vite/Tailwind dashboard. Source facts in `data/master_profile.yaml`; regex screening answers in `data/answers.yaml`. Credentials in `.env` and `data/google_*.json` (do not expose).

Original flow: collectors -> normalize/dedupe/eligibility -> score -> tailor/fact-check/PDF -> review -> platform or ATS worker -> outreach -> Google Sheets.

Read-only baseline: 147 applications: 34 submitted, 3 replied, 100 needs_human, 9 failed, 1 approved. 38 orphaned runs after restarts. 23 common errors explicitly required Workday employer login; 13 lacked confirmation; 11 routed HN posts to forms. Four bounced emails. No duplicate application rows at audit time. Three needs-human applications had failed fact-checks.

Profile diagnosis: shared has persistent cookies for several platforms; main Chrome's last-used directory was Default. Cookie values were not read. Login mismatch confirmed in code: assist used shared, ATS workers used ats_0/1/2. Logs confirmed shared-profile ownership collisions. Do not assert every platform logout was reproduced: it was not.

## Status legend

- IMPLEMENTED: code exists; validation may still be limited to offline checks.
- PARTIAL: code changes landed but explicit gaps remain.
- TODO: not implemented.
- LIVE CHECK: requires observed browser/provider behavior; no sends unless separately authorized.

## Work packages and acceptance criteria

### A. Truthful answers and submission evidence — PARTIAL

Implemented:
- `forms.CTX` is now a thread-local compatibility mapping; platform app_info populates country too.
- Added `submissions.claim`: SQLite immediate transaction, status/fact-check gate, exclusive claiming.
- Fact-check must be strict true and have no violations before newly tailored applications queue.
- Added Pydantic TailoredResume/FactCheck validators in `validation.py`; validates employer/skill categories.
- Stored profile hash and prompt version on new application answers.
- Fixed empty-string multi-step confirmation bug; tightened generic, Hirist, YC, Wellfound and LinkedIn success checks.
- Added stop/warning checks before ATS final submit and recipe final advance.
- Minimum experience checks compare N with profile years; sensitive declarations no longer blindly use generic yes.
- Native selects/buttons/dropdowns/radios increasingly share answer_question.
- Added required-control validation, removed some arbitrary first-option fallbacks; checkbox recipes interpret yes/no instead of blindly checking.
- Resume renderer checks exactly one page and extractable candidate name.

Remaining:
- Audit every factual answer path. Country-specific questions should respect explicit country in the question, not blindly current job country. Unknown authorization must stay unanswered. Tech-specific years must not default to total career years. Salary answers need currency/period/country and numeric control awareness.
- Several platform-specific flows still hardcode personal facts (Cutshort years/year/notice/company, Naukri profile drawer). Replace with structured profile facts.
- `simple_only` re-fill must never call the LLM or undo country-aware answers.
- Validate generated screening answers with the same fact-check gate; add per-claim provenance if practical.
- Contradictory factchecks now fail the claim gate. Remaining: stale profile hash enforcement and complete legacy/manual entrypoint audit.
- Generic `_label_for` and iframe selectors require fixture tests. Required radio-group validator should check group state correctly.
- Some confirmation checks remain broad (`already applied` against entire pages). Require job/form-specific context and store evidence rather than generic button text.
- Preserve unverified submission state; retry requires reconciliation rather than one-click blind resubmit. APIs and UI need an explicit confirmation note flow.
- Check all final clicks, including platform questionnaires and assisted code entry, for cancellation.

Acceptance: concurrent mixed-country fixture; explicit-country overrides; experience/tool-years/salary tests; negative success fixtures; failed factcheck never sends; real final-click mocks prove cancellation prevents send.

### B. Persistent browser sessions — PARTIAL

Implemented:
- `browser.open_context` acquires cross-process `browser-owner.lock`, connects to one local CDP endpoint, or launches dedicated Chrome on `127.0.0.1:9223` using existing `data/profiles/shared`.
- Externally launched Chrome survives disconnect, preserving sessions and human tabs.
- ATS default concurrency reduced to one authenticated workflow; platform and login paths open new tabs.
- assist.py/login_all.py/open_manual.py use the central open_context function.
- refresh_chrome_profile.sh is now non-destructive guidance; no cookie copying/deletion.
- Removed generic-word logged-in fallback; login wait checks cancellation.
- BaseSkill challenge waits while holding ownership; resets pacing at run boundary.

Remaining:
- Test manager reuse, lock contention, launch failures, and configured CDP behavior with mocks/local harmless page. No live job applications.
- Add session health API/UI: active profile, browser ownership, endpoint availability, last positive check, actionable expired/busy states.
- LOGIN_MARKERS still contain stale/weak selectors and duplicate X entry; fix with platform-specific positive + negative signals.
- Login completion should set run back to running. Login timeout should not be reported as done.
- `profile_dir` comments and shared_profile option are stale: manager always uses shared; remove misleading unsupported configuration.
- login_all.py/open_manual.py wait for all shared tabs to close; adapt to only tabs they created and cancellation. assist.py must use its own tab and not navigate a user's existing task tab.
- Persist explicit human handoff/ownership after timeout so the next run doesn't crowd an unresolved form. Avoid browser tab accumulation.
- Externally started Chrome may still require occasional genuine site authentication. Workday tenants may need separate employer accounts.

Acceptance: login and apply resolve identical profile; restart preserves session on a harmless local cookie test; contention queues cleanly; no session deletion; test platform logout later without sends.

### C. Durable queue and recovery — PARTIAL

Implemented:
- New `worker.py`: separate local process, singleton lock, persistent queue claim/dispatch, conservative crash recovery.
- Run columns spec/checkpoints/cancel_requested; additive schema_migrations version 1 and unique application(job_id) index.
- API trigger routes enqueue persisted specs. Web startup no longer marks other runs orphaned or starts scheduling.
- Scheduler enqueues; worker owns scheduling.
- Run stats persisted; stop flag stored in DB; partial/interrupted outcomes; explicit retry endpoint.
- Stage checkpoints persisted; retry copies successful non-apply checkpoints. Interrupted submissions/sends require human reconciliation.

Remaining:
- Ownership recovery implemented in batch 9 with regression coverage. Remaining: heartbeat/lease visibility and explicit reconciliation for unowned legacy work.
- Add worker heartbeat/status to distinguish queued-without-worker from running. Add launcher/service configuration; not yet deployed.
- Test dispatch/run_one, restart during send, singleton contention, stop while waiting for lock, partial-stage failure, retry checkpoint behavior.
- ATS checkpoint labels now include source. Remaining: integration test that later ATS sources execute and retries preserve completed collection stages.
- Interrupted/paused stage must not checkpoint as completed or continue downstream stages incorrectly.
- Stop handling for running LLM subprocess/network calls remains coarse. Implement cancellation hooks and bounded retries/backoff for safe reads only.
- Legacy start_in_thread remains for compatibility; shell helpers still contain old IDs/log polling/auto-approval. Replace with supported durable CLI wrappers; never autoapprove all.
- Centralize run parameter validation (limits, workers, batch, source list, modes, boolean types) so malformed params are rejected before queueing.
- Run deduplication now compares kind/name/params; retry inserts checkpoints atomically without overwriting an existing active run. Regression test passes.

Acceptance: queued work survives web restart; only one worker claims; no duplicate submission on crash; stop honored before side effects; no orphan mutation at web startup.

### D. Discovery, eligibility and scoring — PARTIAL

Implemented:
- Strict location/sponsorship rules replacing unconditional remote/international eligibility.
- Negated sponsorship detection; removed generic sponsor assumptions in JapanDev/Relocate.me descriptions.
- New canonical posting URL identity preserves seniority; existing URLs refreshed with longer descriptions/reset scores.
- Missing descriptions remain retryable, not permanently scored 55.
- LinkedIn hydration resets score and recomputes country/eligibility.
- Added general HTTP hydration stage for short employer descriptions.
- Batch score output validated and matched by returned job number; missing/malformed batches fail retryably.
- Source-scoped score/prepare/full run and ATS handoffs.
- HN/Reddit postings with email are routed to email method for new applications.
- WWR registered and displayed; X registry key corrected.

Remaining:
- General hydration currently may read generic page chrome as a JD. Prefer JSON-LD JobPosting/employer-specific selectors; test challenge/login/closed-job pages and link routing.
- Upsert only recomputes eligibility on longer description; update for changed location/sponsor/closed state regardless of text length. Existing tracking aliases can still duplicate canonical URLs. Add canonical_url column and careful non-destructive backfill/reconciliation.
- Cross-source duplicate company/role handling needs explicit identity strategy; preserve distinct requisitions and seniority.
- Strict country rules may over-filter new legitimate roles; uncertain needs_verification should be separate from definitely ineligible. Don't infer sponsorship from generic relocation-only text.
- Some collectors still catch/log errors without incrementing failed; ensure partial success is accurate.
- Existing HN applications are not migrated. Provide reviewable reconciliation report and explicit repair command, not silent historical rewrites.
- Queue-for-apply currently triggers daily for a source, not exactly selected job; should target job ID and return a clear reason for below-threshold jobs.
- Email-method application lifecycle is not yet integrated with outreach approval/send completion.

Acceptance: realistic country/negation fixtures; description enrichment unlocks scoring; reordered scores map correctly; same job updates, different requisitions don't merge; platform selection stays scoped end to end.

### E. Outreach approvals, scheduling and evidence — PARTIAL

Implemented:
- `messages.claim` requires approval/due date and claims atomically.
- LinkedInPeople message creates reviewable drafts before sending; legacy LinkedIn connect/dm execution delegates to that module.
- X DM/reply drafts require review; X reply processes approved rows then drafts candidates.
- Message evidence checks target message/tweet elements rather than full-page composer text.
- Gmail respects scheduled_for, discovered-address checks, recipient suppression/dedup, atomic caps/claims; ambiguous sends become submission_unverified.
- Gmail no longer interprets an email snippet as application interview/rejection proof; stops queued followups on replies.
- Day 4/day 9 followup draft service added; explicit review required.
- Null job IDs excluded from outreach drafting exclusion query.

Remaining:
- Test all outbound paths with fake providers/pages: zero sends from pending_review, dry-run causes zero sends, cancellation, recipient duplicates and reply suppression.
- LinkedIn/X caps currently can count draft-only attempts; move reservation immediately before authorized send.
- LinkedIn approval must cover exact note content; attaching/re-drafting a contact currently can alter already approved note. Reset approval on text changes.
- LinkedIn no-note fallback should require a clearly approved policy; don't silently send modified approved content.
- X verification should exclude the original hiring tweet and prove current user's new reply/DM; exact text alone isn't enough.
- Gmail payload/attachment/thread metadata preparation now runs before claiming. Remaining: mocked metadata/send failure tests and clearer per-item preflight errors.
- Followup step 3 should require step 2 sent and no pending followup, not simply existence; dedup by normalized recipient across Contact rows.
- Missing API/UI actions for gmail_send and gmail_followups; add services controls.
- Stop/reconcile submission_unverified sending state in API/UI without blind reapproval.
- Contact matching should not overwrite unrelated existing email contact when choosing LinkedIn person. Avoid network calls while holding write transaction.
- Confirm provider message IDs stored for reconciliation; schema currently only thread_id.

Acceptance: approval-only sends with exact frozen text, correct recipient, confirmed provider evidence, no repeats, due-only followups, stop on any reply/bounce.

### F. Dashboard/API and local access — PARTIAL

Implemented:
- artifactUrl helper maps data/artifacts paths to /artifacts.
- API latest-events option and recent activity fixes.
- Completed full-run log drain added; queued counts as live in RunSystem.
- UI auto-apply copy now explains global review policy.
- Job queue action reenters score/prepare instead of stranding queued status.
- Application/outreach status validation and factcheck approval gate.
- Local-host/peer/origin write protection; SPA fallback and API health route fixed.
- Atomic config save and partial configuration validation.

Remaining:
- RunSystem async polling can append stale results after selection changes or duplicate concurrent pulls; implement abort/generation guard and pagination controller.
- Add worker health, retry action, queued/interrupted/partial/sending/unverified badges and empty/error states across pages.
- Runs.tsx nests buttons (invalid markup); fix. Lint warnings in RunSystem remain.
- Application/outreach pagination currently fixed-size. Add real next/prev.
- Add note/evidence UI for manual submitted status; current dropdown has no input for required evidence.
- Backend status transitions need a centralized transition map, not only target allowlists.
- Config validation must validate nested LLM dictionaries, filters arrays, browser options and schedule job params/times completely.
- API sort fields/pagination bounds need allowlists and bounds, and trigger params need strict schemas.
- Add service-account/OAuth readiness rather than 'implemented' only from skill/collector registry.
- Main middleware is localhost-only, no remote deployment auth; document local-only support. No credential values in API responses/logs.

Acceptance: APIs reject invalid transitions/params; manual queue creates exactly selected app; current logs complete, no stale poll races; artifact links resolve; health and SPA routes work.

### G. Operations, dependencies, docs, tests — PARTIAL

Implemented:
- Declared Google integration dependencies, pypdf, pytest.
- Installed pytest into existing venv (no other application dependency upgrade in that install).
- 11 initial offline regression tests, isolated DB and profile copies.
- Initial source archive preserved under /tmp.

Remaining:
- Dependency lock generated (`uv.lock`); README documents frozen install. Remaining: move pytest to dev group if desired and validate a fresh frozen environment.
- SQLite backup/restore implemented in `backend/core/ops.py`, with temporary restore/overwrite-protection regression coverage. Remaining: optional secure profile/config backup and coordinated production backup.
- Retention report + explicit cleanup of unreferenced old artifacts/cache; never delete active artifacts/credentials/profiles.
- Non-destructive reconciliation and retention reports implemented in `backend/core/ops.py`; improve factcheck report to include missing or contradictory checks.
- README now explains separate worker, persistent Chrome, maintenance and offline checks. Remaining: audit older capability descriptions and update launch configuration.
- Expand offline tests to integration cases listed above; frontend build/lint after final edits. Do not rely only on syntax tests.
- One-page resume check added but no render fixture verification yet; validate real base resume locally (no network/sends). Existing PDFs may be >1 page; report, don't delete.
- Rollout: backup real DB with sqlite backup, additive migration, coordinate old assist completion, restart backend, start worker only when queued tasks have been reviewed. Do not auto-send by starting against live queue during coding.

## Current checks

- Initial Python AST parse: passed before final batches.
- Latest pytest after batch 10: **15 passed** (50 deprecation warnings), isolated temporary database.
- Latest frontend build after batch 10: passed; bundle-size warning.
- Latest frontend lint after batch 10: zero errors, four RunSystem React warnings.
- No live send tests, no live login reuse reproduction, no production worker start, no real DB migration performed intentionally.

## Change log

- Batch 1: thread-local country context, minimum-experience answers, factcheck gate, recipe/confirmation corrections, shared answer resolver.
- Batch 2: persistent shared browser manager/lock, non-destructive login helper integration, registry corrections.
- Batch 3: separate durable worker and additive run schema, queued APIs, checkpoint/cancellation primitives.
- Batch 4: atomic cap/application claims, stricter confirmation, hydration score reset, source scope.
- Batch 5: eligibility/identity/refresh changes, artifact links/events/UI wording, status/config API gates.
- Batch 6: outbound approval/claim helper, LinkedIn/X draft-first behavior and scoped evidence.
- Batch 7: Gmail scheduling/suppression/followup drafts, config validation, resume check, dependencies and LLM logging.
- Batch 8: typed AI outputs/profile version metadata, HTTP hydration, safer retries/recovery, required-control checks and cache write atomicity.

## Suggested prompt to resume in Claude

> Read IMPLEMENTATION_PLAN.md in /Users/pushkarmishra/Desktop/job-autopilot. Continue implementing the audit fixes from the actual current files. The plan distinguishes implemented and remaining work; do not assume partial items are done. First run isolated offline tests, then finish the highest-risk remaining correctness/worker/session issues. Preserve live DB and browser sessions; no real applications/messages during testing. Update this plan after every coherent batch with changes, checks, remaining limitations, and deployment state. Finish with a precise handoff instead of claiming untested live reliability.


### Batch 9 handoff update
- Added nullable `claim_run_id` ownership to applications/outreach and additive migration version 2.
- ATS/platform/Gmail/LinkedIn/X claims pass their run ID. Recovery only marks operations belonging to interrupted durable runs; legacy/unowned work remains untouched for manual reconciliation.
- Added regression tests for ownership isolation, claimed-application recovery, and backup/restore integrity/overwrite refusal.
- Generated `uv.lock`; added SQLite maintenance CLI and updated README startup/session guidance.
- Corrected an indentation error introduced during batch 8; the 11 existing tests subsequently passed.
- Still **not deployed**: old API/assist were not restarted, new worker was not launched, live DB migration was not run intentionally. No external applications/messages were sent for testing.
- Migration v2 must run before old processes are restarted with changed models. Coordinate shutdown/backup first; inspect queued work before starting the durable worker.
- Recovery ownership is now addressed; leases/heartbeats, paused-worker queue behavior, and ambiguous-send reconciliation remain outstanding. Null ownership is deliberately never auto-recovered.


### Batch 10 handoff update
- Queue deduplication compares full specs, so collect/apply requests with different parameters are distinct.
- Retry checkpoints are stored as part of new-run creation; retries that resolve to an existing active run no longer overwrite its progress.
- Gmail prepares attachments and performs read-only thread metadata fetch before acquiring the send claim, avoiding stranded `sending` rows on preparation failure.
- Validation: 15 offline tests passed; frontend production build passed; lint has zero errors/four existing React warnings. No live send/login tests or rollout performed.
- Next work: finish browser handoff/session-health and legacy helpers, then remaining form/recipe fact correctness, Gmail lifecycle tests, API/UI reconciliation and launch integration. Full itemized remaining work above is authoritative; this implementation is still partial.

### Batch 11–12 update (continuation)
- Paused stages no longer checkpoint as complete or run downstream stages. Single-platform chains stop on failed prerequisites; all-platform chains only process successful discovery sources.
- Successful login restores running status. Login timeout remains paused rather than being reported done. Recovery also accounts for unfinished paused durable runs.
- Browser lock/launch waits now accept cancellation; profile helper consistently returns shared; duplicate X login marker removed and platform aliases resolve correctly.
- Worker defers browser tasks while a durable run is paused for human attention; offline work may continue. Explicit Stop or Retry releases this reservation; retry marks its original run superseded and never reapproves applications.
- Added read-only `/api/runs/health` (worker lock presence, queued count, browser handoff IDs, shared-profile/busy status). No heartbeat timestamp or endpoint reachability probe yet.
- Login/manual helpers use their own tabs and wait only for those tabs. Assist uses its own page, resets question context, removes a broad success marker, and stops printing verification code values. Assist still needs a complete approval/claim audit.
- Platform API reports login support and service credential readiness; UI consumption remains outstanding.
- Checks: **22 offline tests pass**; backend syntax compilation passes. No live browser test, migration, worker launch, or outbound send performed.
- Remaining related work: surface worker/handoff health in UI; fix paused-run retry race with concurrent retries; handle lock cancellation as stopped rather than failed; ensure stage checkpoint identity includes successful-source scope on retry; browser endpoint/profile identity verification; full legacy helper replacement.

### Batches 13–18 update
- Recipe version 2 fingerprints observed controls/questions/options before reuse, relearns mismatched steps, writes atomically, confines advance actions to their form scope, rejects ambiguous controls, uses observed question wording, and no longer clicks the first autocomplete option by default. Custom LLM confirmation text cannot establish success. Remaining: full DOM fixtures, typed schemas and metrics, final-action classification, selector escaping, and site-specific success evidence.
- Cancelled runs finish stopped even if cancellation raises; full-run shared-stage checkpoint labels now include successful-source scope.
- Queue request validation is centralized in `run_params.py` and used by enqueue (API/scheduler/CLI). Rejects unknown params, invalid modes, string booleans, limits/workers/source lists. Fullrun no longer coerces the string "false" to true. Remaining: required parameter validation and persisted-spec revalidation before dispatch.
- Selecting Queue on one job creates `prepare_selected` for that exact job ID, with hydration/score/prepare scope; existing applications return a review-record conflict. No source-wide preparation.
- Added atomic user application transitions, prevents resubmission paths from terminal states, rejects contradictory factcheck approval, requires evidence for manual submitted and uncertain retry. Outreach changes are atomic and empty messages cannot be approved.
- Added dashboard ApplicationActions with explicit evidence entry and inline errors; worker/queue/handoff status, retry controls and login-profile explanation; Runs no longer nests buttons. Platform API/UI resolves login aliases and distinguishes login support/credential presence.
- Added real pagination to Applications, Review and Outreach; bounded API pagination/sort inputs.
- Replaced usePoll and full-run event polling with response generation guards and sequential polling; switching run/filter no longer appends stale responses. Completed logs continue fetching paginated tail/retry transient errors.
- Added isolated headless Chrome dashboard smoke tests with mocked APIs (all nonlocal requests blocked), testing evidence gating, handoff health, retry and nested-button regression. Tests do not open automation profiles.
- Latest completed checks before dashboard smoke run: 26 backend tests pass, frontend production build passes, lint zero errors/one component-export warning. Dashboard test run currently pending; update after results.
- No live database migration/worker start/send/login test. UI dist rebuilt; old API process still needs coordinated rollout and cannot serve new API fields until restarted.


### Batch 19 update (Claude, 2026-09-07 evening)
Verified first: 36 tests passed, frontend build/lint clean, live DB unmigrated (no schema_migrations table), API process from 12:00 running pre-hardening code, no worker, no Codex process editing files.

Implemented:
- **C/B ops**: worker heartbeat (`data/worker.heartbeat.json`, written every loop and at run start; `heartbeat_status()`), `/api/runs/health` now reports heartbeat age/freshness, current run id and running run ids; WorkerHealth shows them. `InterruptedError` from a cancelled browser wait finishes the run as *stopped*, not failed. `start.sh` (backup → additive migration → API → worker, idempotent) and `stop.sh`; README rewritten around them.
- **A answers**: technology-specific "years with X" questions answer only from an explicit `skill_years` profile entry, otherwise stay unanswered (generic "years of professional experience" still answered); the hard-coded `years.*(python|…)` answer-bank row was removed. A country named in the question overrides the job's country for authorization/sponsorship (`forms.question_country`). Numeric controls get `forms.numeric_answer`: salary amounts chosen by currency/country (USD/EUR/INR, monthly ÷12), unknown jurisdiction → unanswered; other numeric fields accept exactly one number. LLM-generated screening answers pass the same FactCheck gate as the resume (one repair attempt) or are left to the human. `simple_only` verified never to reach the LLM.
- **D discovery**: `hydration.extract_posting` prefers schema.org JobPosting JSON-LD, detects closed postings (`validThrough` past or closure phrases → job `expired`) and challenge/login pages; falls back to main/article text with chrome removed. `normalize.upsert` re-evaluates sponsor flag and eligibility when location, remote scope or sponsorship text change (not only when the description grows), and bumps `updated_at`. Collector source failures now bump `failed`.
- **E outreach**: `messages.set_body` resets an approved message to pending_review when its text changes (used when LinkedIn re-drafts a note for a matched person). LinkedIn DM and X DM daily caps are reserved immediately before an approved send, never for drafts. `Outreach.provider_message_id` (migration v3) stores the Gmail message id. Outreach page has Send approved emails / Check replies / Draft follow-ups buttons (queue `gmail_send`, `gmail_poll`, `gmail_followups`).
- **F config**: `config.validate` checks nested `llm.routes/models/task_models`, `filters.*` lists, `schedule.enabled/jobs` (name, kind, every_minutes 1–10080, booleans, params, duplicates), `business_hours`, `browser`.
- **Multi-user profile (new request)**: `/api/profile` GET/PUT (YAML, validated by `profile.validate_profile`, atomic write with backup to `data/backups`), `/api/profile/answers` PUT (regex-validated), `/api/profile/resume` POST (PDF/DOCX/TXT ≤10 MB stored under `data/uploads`, text read locally, LLM `profile_extract` task on Sonnet → `normalise_draft`; returns a DRAFT only). New **Profile** page (upload → extract → review YAML → save; answer-bank editor). Sidebar shows the profile's name. Hard-coded personal facts replaced with profile lookups: resume filename/skill labels/"most recent previous employer" rule (resume.py), tailor and score prompts, Greenhouse city/school/degree, Cutshort company/city/salary, Naukri profile-drawer chips, YC fallback message, and answer-bank rows (salary, employer, school, location, graduation years, gender now use `{path}` placeholders; `profile.get` supports list indices). Remaining literals in answers.yaml: salutation "Mr", monthly EUR "6,500", "May", keyword list — editable in the UI.

Checks: **58 backend tests pass** (17 new in `tests/test_batch19.py`), frontend build passes, lint zero warnings. Deployed: `./stop.sh && ./start.sh`; `/api/health`, `/api/runs/health` (heartbeat fresh), `/api/profile` verified live; schema_migrations = 1,2,3. No live sends, logins or applications performed.

Still remaining (unchanged unless listed): A — Cutshort/Naukri still use the profile's first education/experience entries heuristically; per-claim provenance; broad "already applied" confirmation checks; unverified-retry reconciliation UI. B — session health probe of the CDP endpoint, LOGIN_MARKERS positive+negative signals, tab accumulation. C — lease/heartbeat per run (only worker-level heartbeat exists), paused-run retry race, LLM subprocess cancellation. D — canonical_url column/backfill, cross-source identity, needs_verification vs ineligible split, HN reconciliation report, email-method lifecycle. E — X reply verification excluding the original tweet, Contact matching overwrite guard, per-item Gmail preflight errors. F — transition map for outreach, service-account readiness. G — fresh frozen-env validation, resume render fixture, retention cleanup command. Recipe first-run "step 1 would not advance" (Workday account wall) still unresolved; LinkedIn external-apply link capture still failing.


### Batch 20–21 update (Claude, 2026-09-08)
- **Versioning split (blocker for everything below).** `profile.fingerprint()` now covers only `identity/positioning/skills/experience/education/projects`; the answer bank, preferences, declarations and capabilities are versioned separately by `answers_fingerprint()` and stored on each application as `answers_hash`. Editing a screening answer no longer marks every prepared document stale. `ops relink-documents` re-stamped 109 live applications whose stored overlay still matches the profile; 1 was listed for regeneration.
- **Queue repairs** in `backend/core/ops.py`, all report-only unless `--apply`, all documented in `--help`: `route-email` (HN/Reddit postings with an address become email applications), `link-apply-urls` (reads the real apply link out of the post text; 7 recovered), `recheck-answers` (applications stalled pre-submit whose questions now resolve go back to review; never touches anything that reached an employer), `clear-stuck --statuses` (deletes stalled applications; a job whose submit may have landed is marked `applied` so it can never be re-applied to, everything else `skipped` so the next run does not rebuild the same failures).
- **Hacker News collector** now extracts `apply_url` and an email from the comment body instead of pointing at the discussion thread.
- **Capabilities.** `profile.capabilities()` answers "have you worked with X" from the skills/stacks already listed plus an explicit `capabilities:` map (booleans, including deliberate No). Anything unlisted stays unanswered and reaches the human. Seeded with claims the profile already supports.
- **Question inbox + chat (new).** `questions` and `chat_turns` tables (migration v4). The ATS worker records every unanswered field; `questions.record` groups rewordings by a normalised fingerprint, drops stray labels (single words, the company name, form chrome), and keeps options/companies/times-seen. `/api/questions`, `/api/questions/chat`, `/api/questions/{id}/skip`, `/api/questions/backfill` (accepts a backup DB path). The chat sends one question plus the user's own words to the model, which only classifies and phrases: `capabilities` for technology questions, `declarations` for consent/legal wording, otherwise an answer-bank rule inserted first (specific beats generic). Ambiguous replies produce a follow-up and store nothing. New **Questions** page with the conversation, option chips, skip, and a sidebar count. An explicit declaration for a question now beats every generic rule (`profile.declaration_key`).
- Live queue: cleared at the user's explicit request (94 stalled, then 16 reviewable) with backups at `data/backups/before-clear-*.db`; 32 questions recovered from the pre-clear backup so the inbox has real content. Applications now: 34 submitted, 3 replied, 0 open.
- Checks: **67 tests pass** (11 new), frontend build and lint clean, dashboard smoke tests extended with `/api/questions` and `/api/profile` mocks after a null-shape crash in the sidebar. Deployed via `./start.sh`; chat verified live end to end against Claude CLI (capability, free-text and ambiguous replies).
- Remaining: label extraction still yields stray labels on some forms (filtered at the inbox, not at the source); questions are not yet re-attached to the applications they came from, so answering one does not automatically requeue that application (`ops recheck-answers` does it in bulk); no bulk "answer these 5 similar questions at once" flow.


### Batch 22 (Claude, 2026-09-08): first live apply run, and a payment funnel
- **We Work Remotely routes "Apply" through a paid subscription.** Its own 3-step flow ends on a checkout: $29.95 billed
  immediately, monthly for a 12-month commitment, auto-renewing, with Apple Pay preselected. The worker reached the final
  consent checkbox ("I agree to the Terms & Conditions and the renewal terms above") and stopped only because ticking a
  legal agreement requires an explicit declaration. That was one checkbox away from a payment control.
- Added `forms.payment_wall(page)`: two or more money/plan/billing phrases, or a payment control next to a price, or a
  recurring-charge pattern. Checked before the form is touched AND before every recipe step, since the funnel appears at
  step 2. The application stops as needs_human with the phrase that triggered it; the recipe is marked stale. Regression
  test covers the real WWR text plus salary-mentioning application pages that must NOT trip it.
- Run 226 stopped by hand; the in-flight application was reconciled to needs_human (nothing was submitted).
- Also fixed in this batch: CDP attach failed with "Browser context management is not supported" when the automation
  Chrome was alive with no window; `open_context` now requests a blank tab first. Resume upload handles drag-and-drop
  zones via `expect_file_chooser` and verifies the file actually attached rather than assuming. Recipe-step unanswered
  questions now reach the Questions inbox.
- **Open question for the user: whether to keep We Work Remotely at all.** Its listings are real, but its apply path is a
  paywall, so the collector produces jobs that cannot be applied to through it. Applying on the employer's own site works.
