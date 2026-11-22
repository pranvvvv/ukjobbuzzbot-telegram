# JobPilot: Implementation Plan v1 (for review)

Prepared 21 Nov 2026 in response to the build prompt, Section 16. No code yet. Every external fact below was checked against live docs or PyPI today; anything only reported by a third party is marked *(3rd-party)*.

---

## 0. Summary: what I'd change in the spec

| # | Spec says | I propose | Why |
|---|-----------|-----------|-----|
| 1 | `LLMClient.embed(texts)` on every provider | Split into `LLMClient` (JSON completions) and a separate `Embedder`. Default: **local embeddings** (fastembed, `BAAI/bge-small-en-v1.5`, CPU/ONNX) | Groq lists no embedding models and Anthropic has none of its own (it points to Voyage AI). Local embeddings also make stage 1 free, offline and private: the resume never leaves the laptop for stage 1. |
| 2 | APScheduler with a **SQLAlchemy job store**, `misfire_grace_time` | **The DB is the source of truth** (`users.next_digest_at_utc`, `last_digest_at`). APScheduler 3.11 runs an in-memory 1-minute "dispatch due digests" tick plus housekeeping jobs. | APScheduler 4 is still alpha (4.0.0a6, Apr 2025), so 3.11 is the choice. 3.x's SQLAlchemyJobStore pickles callables, uses sync DB I/O inside the event loop and lives outside Alembic. Catch-up via `misfire_grace_time` is awkward after a laptop has been off for 3 days. With "due if `next_digest_at <= now`", catch-up is coalesced by construction: one run, then the next time is computed from now. |
| 3 | FSM "states persisted in the DB" | **Custom aiogram `BaseStorage` on SQLAlchemy** (`fsm_state` table) | aiogram ships only Memory, Redis and Mongo storages. A SQL storage is about 60 lines (`get/set_state`, `get/set_data`, `close`) and makes a good portfolio piece. |
| 4 | Project in `OneDrive\Desktop\JobPilot, an AI Job-Finder…` | Keep the **repo outside OneDrive** (e.g. `C:\dev\jobpilot`). Keep the **data dir** in `%LOCALAPPDATA%\JobPilot` (via `platformdirs`), configurable with `DATA_DIR`. | OneDrive syncing a live SQLite WAL database risks lock errors and corruption. Syncing `.venv` and `.git` is slow and creates conflict copies. The folder name's commas and spaces also make scripts fragile. |
| 5 | "Experience computed from dates" | **The LLM extracts, code computes.** The LLM returns normalized `YYYY-MM` dates; Python computes years, merges overlaps and separates internships. The same applies to salary parsing, "posted N days ago" and seniority from "X+ years" regexes. | Deterministic, testable and never hallucinated. |
| 6 | Link health HEAD/GET on every job | Skip link checks for **ATS jobs** (being in the live board feed already proves the job is live). For aggregators, use a streamed `GET` (many servers reject `HEAD`). Drop only on **404/410**; treat 403/405/429/timeout as "unverified, keep". Cache results for 24 h. | Fewer false drops, less traffic, no bot-wall bans. |
| 7 | Gemini as a likely provider | If the key is a **free-tier Gemini key**, flag it: Google marks free-tier content as "used to improve our products" (paid tier: not used). | This conflicts with rule 8's intent. See Q2. |
| 8 | Single-instance detection via Telegram 409 | Use **both** a file lock in the data dir at startup (`filelock`) **and** handling of aiogram's `TelegramConflictError` | The 409 only arrives after polling starts. By then a second scheduler could already have sent a duplicate digest. |
| 9 | CI and setup scripts in M8/M9 | **CI from M1** (ruff + mypy + pytest on GitHub Actions). A minimal `setup.ps1`/`setup.sh` in M1, polished in M8. | Recruiters look at commit history. Green CI from the first commit is the signal. |
| 10 | No visible end-to-end output until M6 | **Walking skeleton at M4**: `/search` sends a plain, unscored top-10 list. | We see real data in Telegram early and catch normalization bugs before scoring hides them. |
| 11 | Optional fields in schemas | **Strict-mode-compatible schemas everywhere**: every field is required, "optional" is written `X \| None`, and `extra="forbid"` | One schema then works with OpenAI strict `json_schema`, Groq strict (which requires every property in `required` and `additionalProperties: false`), Gemini `response_schema` and Anthropic tool-use. Pydantic validation and up to 2 repair retries still run on top. |
| 12 | `tzdata` not mentioned | Add `tzdata` as a dependency | Windows has no system IANA tz database, so `zoneinfo("Asia/Kolkata")` fails without it. |

---

## 1. Milestone plan

At the end of each milestone: tests green in CI, a short demo (Telegram screenshots + `pytest` summary), then I stop for review.

### M1: Skeleton (≈1 day)
- `uv` project, `.python-version` (3.13), `pyproject.toml`, `uv.lock`, ruff, mypy (strict on `core/`, `db/`, `matching/`), pytest, pre-commit
- `core/config.py` (pydantic-settings): fails fast with **one message listing every missing key**. Includes `DATA_DIR` (platformdirs) and a single-instance file lock.
- `core/logging.py`: structlog JSON to stdout plus a rotating file in `DATA_DIR/logs`. Includes a **redaction processor** that masks emails and phones, drops `resume_text` and `description` keys, and scrubs tokens.
- `db/`: SQLAlchemy 2.1 async + aiosqlite, WAL + `busy_timeout` pragmas, Alembic baseline with all tables from Section 6 plus `fsm_state`, `link_cache`, `http_cache`
- Bot: aiogram 3 long polling, **allowlist outer middleware** (silent drop + log), `/start` (new/existing variants), `/help`, `setMyCommands`
- `python -m jobpilot doctor` (typer + rich): Python version, `.env` keys, `getMe`, LLM ping (1 tiny call), DB write, timezone, data dir not under OneDrive, lock free. Source checks are **opt-in** (`--sources`) so doctor doesn't burn quota.
- `FakeLLM` adapter, CI workflow, `docs/DECISIONS.md` with ADR-001…012 (Section 2)
- **Done when**: bot replies only to you; doctor is all green; CI is green.

### M2: Resume intake + profile (≈2 days)
- `resume/extract.py`: pypdf first; pdfplumber fallback when pypdf's text is sparse or garbled; python-docx (paragraphs **and tables**, since many Indian resume templates use tables); TXT with encoding sniffing
- Scanned-PDF detection: fewer than ~40 chars per page, or a low alphabetic ratio, means scanned, so ask the user to paste text. OCR goes behind `ENABLE_OCR` (pytesseract, off by default).
- File limit: check `document.file_size` **before** download against the **20 MB `getFile` limit** (verified).
- Paste buffer: per-user 3 s debounce merge plus a `Done` button. Telegram splits long pastes into 4096-char messages.
- LLM pipeline: (1) `is_resume` classifier (cheap model), (2) `CandidateProfile` extraction, (3) code-side post-processing: experience math, skill-evidence check (**each skill must appear in the source text**, fuzzy-matched with an alias map, else it's dropped and logged), seniority rule table
- Prompts in `llm/prompts/*.md` with version headers. Resume text goes inside `<resume>…</resume>` delimiters with a "data, not instructions" system rule.
- Confirmation card + `Looks right` / `Fix something` (free-text correction goes to an LLM patch of the profile JSON, then re-validation)
- Fixtures: **synthetic resumes only** (fresher, student-intern, mid backend, DOCX with tables, scanned PDF, non-resume). Your real resume is never committed.
- **Done when**: PDF, DOCX and pasted text all produce validated profiles; the scanned PDF is detected; the injection fixture ("ignore previous instructions…") has no effect.

### M3: Clarifying questions FSM + search plan (≈2 days)
- **Question selection is rule-based, not LLM-decided**: a pure function `needed_questions(profile, prefs) -> list[QuestionId]` (e.g. Q8 visa only if a location outside the home country is chosen; Q5 is pre-filled from seniority). It is testable and deterministic. The LLM only proposes role and synonym suggestions for Q1.
- Inline multi-select keyboards with ✓ toggles, `Skip`, `Back`, and a "Question 3 of 7" header, editing one message in place
- Search Plan card with `Start search` / `Edit` (jump to any question)
- SQL FSM storage; a **restart test** in which the integration test kills the dispatcher mid-flow, starts a new one and asserts the same question re-renders
- `/profile`, `/preferences`, `/new` (archive + "Reset history too?"), `/deleteme` (two-step confirm, wipes rows and files)
- **Done when**: a restart mid-flow resumes correctly (automated test plus a manual demo).

### M4: Sources + quota manager (≈2–3 days)
- `JobSource` protocol plus registry (adding a source = one file + one line in the registry)
- **Adzuna** (`/v1/api/jobs/in/search/{page}`), **Greenhouse**, **Lever** (global + EU hosts), **Ashby** (`includeCompensation=true`)
- `companies.yaml` plus `python -m jobpilot companies validate`, which hits each board once and reports dead tokens
- LLM query generation (role synonyms × locations × seniority), capped per run
- Quota manager over `api_usage` (per minute, day, week and month windows from config), per-host `aiolimiter`, tenacity backoff, 6 h response cache, partial-failure reporting
- Walking skeleton: `/search` sends a plain top-10 list
- Recorded fixtures for every source (respx), plus a fault-injection test in which one source raises and the run still completes
- **Done when**: a query returns normalized jobs, and a failing source doesn't break the run.

### M5: Matching pipeline + eval (≈3 days)
- normalize → dedupe (exact fingerprint + rapidfuzz `token_set_ratio` on title+company within the same city; link-priority tie-break) → hard filters → scam heuristics → stage 1 (embedding cosine + weighted skill overlap) → top-K=30 → stage 2 LLM rubric in batches of 5 jobs per call → threshold ≥60 → rank by total, then recency
- Job descriptions inside `<job id=…>` delimiters. Schema includes `job_id` so batched outputs map back safely. Unknown IDs are rejected.
- Fallback: if the LLM is down or the budget is hit, use stage-1-only scores and a "basic match" label
- `evals/`: 30 labeled pairs, `python -m jobpilot eval` → precision@10, Spearman score↔label correlation, confusion table. The result is written to `evals/RESULTS.md` and the README.
- **Done when**: the eval runs and precision@10 is reported.

### M6: Cards, buttons, feedback (≈2 days)
- HTML formatter: escape everything, truncate on plain text **before** markup is applied (so never mid-tag), stay under 4096 chars. Property tests (hypothesis-style fuzz) on emoji and Unicode.
- Buttons: `Apply` (URL), `Save`, `Not interested` → reason picker, `More like this`, and (proposed) `Applied ✓`. callback_data stays within 64 bytes: `m:{match_id}:{action}`.
- Global send queue: 1 msg/s per chat, ≤25/s global, `TelegramRetryAfter` sleep-and-retry, `TelegramForbiddenError` → disable digest
- Feedback weights in `preferences.weights` (explainable: a "why this changed" line in `/preferences`). "Company" dismissals propose an exclusion and ask for confirmation.
- `/saved`, `/history` (paginated)
- **Done when**: a scripted test shows feedback changing the next run's ranking.

### M7: Scheduler + digest (≈1–2 days)
- 1-min tick → due users → per-user `asyncio.Lock` (shared with `/search`) → digest of **new, unseen, ≥ threshold** jobs, or the "No strong new matches today; N jobs checked" message
- Catch-up on startup is coalesced. `/pause`, `/resume`, `/settime` (time picker), `/stats` (runs, per-source quota left, tokens, ₹/$ cost)
- Tests use `time-machine` to simulate "laptop off for 3 days" and assert exactly one digest.
- **Done when**: the digest arrives on time and the laptop-off catch-up works.

### M8: JSearch, cost guard, packaging (≈2 days)
- JSearch with a strict monthly budget from config (the free plan is tiny, see Section 3)
- Cost guard: per-run and per-day token caps, price table in `config/pricing.yaml` (not hardcoded)
- Dockerfile + compose, `setup.ps1`/`setup.sh` polish, `install-autostart.ps1` / `uninstall-autostart.ps1` (Task Scheduler: at logon, restart on failure ×3, "run as soon as possible after a missed start")
- Optional remote sources depending on market (Q15)
- **Done when**: a fresh Windows machine is set up in under 10 minutes using the README only (I'll write the script; you time it).

### M9: Polish
- README (pitch, demo GIF from a **synthetic persona**, Mermaid architecture diagram, "How matching works", metrics, 5-step setup, roadmap), `ARCHITECTURE.md`, `SETUP.md`, CI and coverage badges
- Optional PyInstaller build (expect around 150–250 MB because of onnxruntime; stays a stretch goal)

---

## 2. Architecture decisions (ADRs I'll record)

| ADR | Decision |
|-----|----------|
| 001 | Python **3.13** managed by uv; CI matrix 3.12 / 3.13 / 3.14 |
| 002 | aiogram 3 long polling; webhooks are a future config flag |
| 003 | SQLite (WAL, `busy_timeout=5000`, `foreign_keys=ON`) via SQLAlchemy 2.1 async; Postgres-ready through the URL |
| 004 | DB-driven scheduling on APScheduler 3.11 (Section 0 #2) |
| 005 | Custom SQL FSM storage |
| 006 | `LLMClient` and `Embedder` split; local fastembed by default |
| 007 | Official SDKs: `google-genai` (not the legacy `google-generativeai`), `anthropic`, `openai`. **Groq through the `openai` SDK** with Groq's OpenAI-compatible base URL (one fewer dependency, same code path). Every adapter: timeout, tenacity retries, token accounting. |
| 008 | Strict-compatible Pydantic schemas plus repair loop (2 retries with the validation error fed back) |
| 009 | "LLM extracts, code computes" for dates, money, durations and seniority |
| 010 | Data dir outside repo and OneDrive; single-instance file lock |
| 011 | Link-health policy (Section 0 #6) |
| 012 | Rate-limited Telegram send queue; callback_data scheme |

Layering stays exactly as in Section 5 of the spec. `services/` depends only on interfaces (`JobSource`, `LLMClient`, `Embedder`, `Repo`), so a web UI can replace `bot/` later.

---

## 3. Verified external facts (21 Nov 2026) and spec corrections

| Item | Finding | Impact |
|------|---------|--------|
| Telegram Bot API | Current version 10.3 (24 Aug 2026). `getFile` downloads **up to 20 MB**; uploads up to 50 MB. | Pre-download size check |
| Telegram rate limits | Avoid **>1 msg/s per chat**; about **30 msg/s** global without paid broadcasts | Send queue |
| Polling vs webhook | `getUpdates` and webhooks are mutually exclusive | Call `deleteWebhook` on start |
| aiogram | 3.33.0 (19 Nov 2026), Python ≥3.11, <3.16. FSM storages: Memory, Redis, PyMongo (Motor version deprecated); no SQL storage | Custom storage |
| APScheduler | Stable 3.11.3; 4.0 still alpha (a6, Apr 2025) | Use 3.11 |
| Adzuna | `app_id` + `app_key` query params on every call; limits are "shown in your account". **Personal job search is a permitted use**; commercial use is a 14-day trial, then a licence. **Attribution required** on ad listings. *(3rd-party: free tier ≈25/min, 250/day, 1,000/week, 2,500/month; descriptions are snippets; links go through Adzuna redirects; many salaries are model-predicted.)* | Quotas in config, set from your dashboard. Cards show "via Adzuna". Predicted salaries are never used for the salary filter. The SaaS roadmap item needs an Adzuna licence (noted in README). |
| JSearch | Free plan is a hard cap with no overage. *(3rd-party: about 100 req/month direct from OpenWeb Ninja; the RapidAPI Basic figure is shown on your dashboard.)* | ≤ a few calls/day, budget from config, first search + weekly refresh only |
| Greenhouse | `GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`, no auth. The list returns `updated_at` and `absolute_url`; **`first_published` only on the single-job endpoint** | Freshness from `updated_at` (edits can make old jobs look new). Fetch the detail endpoint for top-K only to get the true `first_published`. |
| Lever | `api.lever.co/v0/postings/{site}?mode=json` (EU: `api.eu.lever.co`). Fields include `hostedUrl`, `applyUrl`, `categories`, `workplaceType`, `salaryRange`. The docs only rate-limit **application POSTs** (2/s); they give no crawl guidance for GETs. | The spec's "Lever asks for slow crawling" isn't in the current docs. We stay polite anyway (≤1 req/s per host). |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true`. **`isRemote` is true for Hybrid too**, so `workplaceType` (OnSite/Remote/Hybrid) is the field to use | Map from `workplaceType` |
| Remotive | Must link back and credit Remotive; no redistribution to other job sites; **24 h delay** | Fine for personal use |
| Remote OK | Must credit and link back with a followed (not nofollow) link; no logo | Fine |
| Arbeitnow | No key; mostly Europe + UK endpoint | Skip if the market is India |
| Jooble | Free key via a request form, POST `jooble.org/api/{key}`, 60+ countries incl. India. *(3rd-party claim: small lifetime quota per key.)* | Optional later source |
| Gemini | Free tier: content **used to improve Google's products**; paid: not. Paid per 1M tokens: 2.5 Flash-Lite $0.10 in / $0.40 out; 3.1 Flash-Lite $0.25 / $1.50; 2.5 Flash $0.30 / $2.50. 2.0 Flash shut down 1 Jun 2026. | Q2 |
| Groq | Production models include `openai/gpt-oss-120b`/`20b` and Llama 3.x. **Strict** structured outputs only on gpt-oss and a couple of others; no streaming or tool use with structured outputs. No embedding models listed. | Use `gpt-oss-120b` for Groq; embeddings stay local |
| Anthropic | No embedding model of its own (recommends Voyage AI) | Embeddings stay local |

**Cost estimate (stage 2)**: 30 jobs ÷ 5 per call = 6 calls ≈ 45k input + 9k output tokens. On Gemini 2.5 Flash-Lite that's about **$0.008 per full search**, and on 2.5 Flash about $0.036. Daily digests score only new jobs, so typically under **₹30/month** on paid Flash-Lite.

---

## 4. Library choices (latest stable on PyPI today)

**Runtime**

| Package | Version | Role |
|---|---|---|
| Python | 3.13 (uv-managed) | |
| aiogram | 3.33.0 | Telegram |
| SQLAlchemy[asyncio] | 2.1.9 | ORM |
| aiosqlite | 0.22.1 | SQLite driver |
| alembic | 1.20.1 | Migrations |
| APScheduler | 3.11.3 | Ticks / housekeeping |
| pydantic / pydantic-settings | 2.14.4 / 2.16.3 | Schemas, config |
| httpx | 0.28.1 | HTTP client |
| tenacity | 9.2.1 | Retries |
| aiolimiter | 1.3.0 | Per-host rate limits |
| structlog | 26.1.0 | Logs |
| rapidfuzz | 3.14.6 | Fuzzy dedupe, skill aliases |
| pypdf | 7.1.1 | PDF (BSD; avoiding PyMuPDF because of AGPL) |
| pdfplumber | 0.11.11 | PDF fallback |
| python-docx | 1.2.0 | DOCX |
| beautifulsoup4 | 4.15.0 | HTML → text (stdlib parser, no compile step) |
| fastembed | 0.9.1 (onnxruntime 1.32, cp313 wheels) | Local embeddings |
| numpy | 2.5.5 | Cosine similarity |
| google-genai | 3.6.1 | Gemini adapter |
| anthropic | 2.12.0 | Claude adapter |
| openai | 3.57.0 | OpenAI + Groq adapters |
| PyYAML | 6.0.4 | companies/pricing/aliases YAML |
| typer / rich | 0.29.2 / 15.0.0 | CLI + doctor checklist |
| platformdirs | 4.14.8 | Data dir |
| filelock | 4.2.12 | Single instance |
| tzdata | 2026.6 | Windows timezones |
| pytesseract | 0.3.13 (optional extra `ocr`) | OCR flag |

Not adding: `python-dateutil` (4.0 is a brand-new major; stdlib `fromisoformat` plus LLM-normalized `YYYY-MM` covers our needs), LangChain or LiteLLM (a thin owned abstraction is the better portfolio story and has fewer moving parts), Redis.

**Dev**: ruff 0.17.6 · mypy 2.4.1 · pytest 9.2.0 · pytest-asyncio 1.5.0 · pytest-cov 7.1.0 · respx 0.23.1 (httpx mocking; I'll record fixtures with a small `scripts/record_fixture.py` rather than vcrpy) · time-machine 3.6.0 · pre-commit 4.7.0 · types-PyYAML.

All pinned through `uv.lock`. `pyproject` uses `>=` lower bounds at these versions.

---

## 5. Edge-case checklist (spec Section 10)

| Case | Handling |
|---|---|
| Laptop asleep/off at digest time | DB-driven due check on startup + every minute; one coalesced catch-up |
| Network loss | tenacity backoff on every call; aiogram polling auto-reconnects; failures logged to `runs.errors_json` |
| Restart mid-onboarding | SQL FSM storage; the current question re-renders on the next message or `/start` |
| Two searches at once | Per-user `asyncio.Lock` held by search and digest; the second gets "a search is already running" |
| LLM down / quota hit | Stage-1-only ranking, "basic match" label, retried next tick |
| Bad LLM JSON | 2 repair retries with the validation error fed back → skip that job/batch, count it in the run |
| All sources fail | Honest message + automatic retry in 30 min (bounded) |
| Timezones | Store UTC; render with `zoneinfo` + `tzdata`; user tz default Asia/Kolkata |
| Unicode / emoji | NFC normalize; HTML-escape; length checks on UTF-16 code units (what Telegram counts) |
| Huge result sets | Per-source page caps + per-run job cap before scoring |
| Missing secrets | pydantic-settings validation → one consolidated error, exit code 2 |
| Duplicate instances | File lock + `TelegramConflictError` → clear message, exit |
| Cost | Per-run and per-day token caps; cost in `runs`; shown in `/stats` |
| User blocks bot | `TelegramForbiddenError` → `digest_enabled=False` |
| Callback from old message after `/new` | Match lookup by ID; stale profile version → "This card is from an older profile" |
| Job removed from board after sending | `Apply` still works (URL button); `/saved` re-checks when opened |

---

## 6. Risks

1. **India coverage from free sources is thin.** Adzuna India plus ATS boards of India-hiring companies is the realistic base; Naukri and LinkedIn are off-limits by rule. The quality of `companies.yaml` will drive results more than anything else. Mitigation: seed 30–50 verified boards, plus the `companies validate` command.
2. **Adzuna snippets** limit scoring depth for Adzuna jobs. Mitigation: score what's there, mark confidence lower, rank ATS jobs (full text) fairly via calibration in the eval.
3. **JSearch's free quota** is too small to matter daily. It's a bonus source, not a pillar.
4. **Eval labels** need your judgment (30 pairs, about 30 min of your time).
5. **Free-tier LLM privacy** (Q2).
6. **First run downloads the embedding model** (~70–130 MB, once). Doctor pre-fetches it.

---

## 7. Questions (each has a default; reply "defaults" or override by number)

1. **Primary market?** Default: India (Hyderabad, Bengaluru, remote-India) **plus** remote-global roles open to India.
2. **LLM provider and tier?** If your only key is a free-tier Gemini key, Google may use the content to improve its products. Options: (a) free Gemini during development with synthetic resumes only, paid Gemini Flash-Lite (pennies) for your real resume; (b) Groq `gpt-oss-120b`; (c) Claude or OpenAI. Default: (a). Which key do you actually have?
3. **Local embeddings** (one-time ~100 MB model, CPU, nothing leaves the laptop)? Default: yes.
4. **Move the repo out of OneDrive** to e.g. `C:\dev\jobpilot`? The data dir goes to `%LOCALAPPDATA%\JobPilot` regardless. Default: yes, move it.
5. **Keep original resume files?** Default: store only extracted text + SHA-256 of the file; discard the original (less PII on disk). The spec says files go in `data/`; say if you want them kept.
6. **Python 3.13** instead of 3.12? Default: yes (CI still tests 3.12).
7. **GitHub**: public repo `jobpilot`, MIT licence, GitHub Actions? Default: yes. Your GitHub username?
8. **Eval set**: I build 30 pairs (5 synthetic personas × 6 recorded jobs), pre-label them with rationales, and you correct them. May I also run (never commit) an eval on your real resume locally? Default: yes to both.
9. **companies.yaml seed**: I'll seed 30–50 India-hiring software/AI companies on Greenhouse, Lever or Ashby, each verified by script. Any must-include companies or sectors (fintech, AI startups, product MNCs)?
10. **LLM budget caps**: default $0.10 per day and $2 per month hard cap.
11. **Adzuna attribution**: a text "via Adzuna" link on Adzuna cards (their pixel-size logo rules don't map to Telegram). OK? Default: yes.
12. **Add an `Applied ✓` button** (sets `status=applied`, feeds `/stats` and the future tracker)? The spec has the status but no button. Default: yes.
13. **OCR**: off by default; enabling it needs a manual Tesseract install on Windows. Default: off.
14. **Wake the laptop from sleep for digests** via Task Scheduler? Default: no (battery); catch up at logon instead.
15. **Optional sources in M8**: Remotive (24 h delay) + Remote OK if you choose remote-global; skip Arbeitnow (Europe); Jooble later. Default: as stated.
16. **Digest defaults**: 10 jobs, 09:00 IST, threshold 60, freshness 14 days. Default: as stated.
17. **Demo GIF persona**: a synthetic "fresher backend/AI engineer" resume, not yours. Default: synthetic.

---

### Sources
- Telegram Bot API: https://core.telegram.org/bots/api · FAQ (limits): https://core.telegram.org/bots/faq
- aiogram FSM storages: https://docs.aiogram.dev/en/latest/dispatcher/finite_state_machine/storages.html
- Adzuna overview and terms: https://developer.adzuna.com/overview · https://developer.adzuna.com/docs/terms_of_service
- Greenhouse Job Board API: https://docs.greenhouse.io/job-board.html
- Lever Postings API: https://github.com/lever/postings-api
- Ashby posting API: https://developers.ashbyhq.com/docs/public-job-posting-api
- Remotive API: https://remotive.com/remote-jobs/api · Remote OK API: https://remoteok.com/api · Arbeitnow: https://www.arbeitnow.com/blog/job-board-api
- Gemini pricing: https://ai.google.dev/gemini-api/docs/pricing · Groq models and structured outputs: https://console.groq.com/docs/models · https://console.groq.com/docs/structured-outputs
- Anthropic embeddings note: https://platform.claude.com/docs/en/build-with-claude/embeddings
- 3rd-party quota reports: https://jobspipe.dev/blog/free-job-api · https://prospeo.io/s/openweb-ninja-pricing-reviews-pros-and-cons
- Package versions: PyPI JSON API, queried 21 Nov 2026
