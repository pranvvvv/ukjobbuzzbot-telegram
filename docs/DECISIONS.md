# Architecture Decision Records

Short ADRs: context, decision, consequences. Newest last. Status is *Accepted* unless noted.

---

## ADR-001: Python 3.13, managed by uv
**Context.** The spec said 3.12. By Nov 2026, 3.12 only gets security fixes and python.org no longer ships Windows installers for it; 3.13 has wheels for every dependency we use.
**Decision.** Target 3.13 (`.python-version`), support `>=3.12,<3.16`, and test 3.12, 3.13 and 3.14 in CI. `uv` installs the interpreter, so nobody has to install Python by hand.
**Consequences.** One-command setup on a fresh Windows machine. We can use PEP 695 generics (`def f[T]`).

## ADR-002: aiogram 3 with long polling
**Context.** The bot runs on a laptop with no public URL.
**Decision.** Use long polling. On startup we call `deleteWebhook`, because `getUpdates` and webhooks are mutually exclusive.
**Consequences.** No port forwarding or TLS certificates. Webhook mode can be added later as a config flag for an always-on host.

## ADR-003: SQLite in WAL mode via SQLAlchemy 2.1 async; Postgres-ready
**Decision.** Use `aiosqlite` with the pragmas `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON` and `busy_timeout=5000` on every connection. Store every timestamp as aware UTC via a `UTCDateTime` type decorator (SQLite drops tzinfo). Store enums as strings with CHECK constraints. Name constraints by convention so Alembic diffs stay stable.
**Consequences.** Zero setup. Switching to Postgres is a `DATABASE_URL` change, and the FSM upsert already has a Postgres branch.

## ADR-004: Scheduling is driven by the DB, not by APScheduler's persistent job store
**Context.** APScheduler 4 is still alpha. The 3.x `SQLAlchemyJobStore` pickles callables, runs sync DB I/O inside the event loop, and lives outside Alembic. Catching up via `misfire_grace_time` is awkward after the laptop has been off for days.
**Decision.** `users.next_digest_at` (UTC) is the source of truth. An in-memory APScheduler 3.11 tick runs every minute and processes due users. *(Implemented in M7.)*
**Consequences.** Catch-up is coalesced by construction: one digest, then the next time is computed from now. The schema already has `next_digest_at` and `last_digest_at`.

## ADR-005: Custom SQL storage for aiogram's FSM
**Context.** aiogram ships Memory, Redis and Mongo storages only. The spec requires a restart mid-conversation to resume.
**Decision.** `SQLStorage(BaseStorage)` over a `fsm_state` table. It uses a dialect-aware upsert and deletes the row when the conversation ends.
**Consequences.** No Redis dependency. Covered by a test that rebuilds engine, factory and storage over the same file ("restart").

## ADR-006: Separate `LLMClient` and `Embedder`; local embeddings by default
**Context.** Groq and Anthropic offer no embedding API.
**Decision.** `LLMClient.complete_json(schema, prompt)` only. Embeddings sit behind their own `Embedder` protocol, with a local fastembed model by default (M5). A deterministic `FakeLLM` and `HashEmbedder` serve tests.
**Consequences.** Stage-1 matching is free, offline and private. Provider choice affects only extraction and scoring.

## ADR-007: Official SDKs; Groq through the OpenAI SDK
**Decision.** Use `google-genai` (not the legacy `google-generativeai`), `anthropic` and `openai`. Groq runs through `openai` with Groq's OpenAI-compatible base URL. Every adapter gets a timeout, retries and token accounting. *(M2.)*

## ADR-008: Strict-mode-compatible schemas plus a validated repair loop
**Decision.** In every LLM output schema, all fields are required, "optional" is written `X | None`, and `extra="forbid"`. On validation failure we retry up to 2 times, feeding the error back.
**Consequences.** One schema works with OpenAI and Groq strict JSON schema, Gemini `response_schema` and Anthropic tool use.

## ADR-009: The LLM extracts, code computes
**Decision.** The LLM returns normalized facts (dates as `YYYY-MM`, raw salary strings). Python computes years of experience, overlaps, salary ranges, "posted N days ago" and seniority bands.
**Consequences.** Deterministic, unit-testable numbers, and the LLM never invents arithmetic.

## ADR-010: Data outside the repo and outside OneDrive; single-instance lock
**Context.** The project started in a OneDrive folder. Syncing a live SQLite WAL database risks corruption.
**Decision.** `DATA_DIR` defaults to the OS app-data folder (`%LOCALAPPDATA%\JobPilot` on Windows). `doctor` fails if the data dir is cloud-synced and warns if the repo is. A file lock in `DATA_DIR` prevents two local instances.

## ADR-011: Detecting duplicate pollers
**Context.** aiogram's polling loop catches every exception, including `TelegramConflictError` (409), and retries forever, so two instances would silently fight.
**Decision.** (1) A preflight `getUpdates(timeout=0)` before polling; it doesn't acknowledge updates. (2) `ConflictWatchdog`, a logging handler on `aiogram.dispatcher`, stops polling after 3 conflicts in 2 minutes. Either path exits with code 4 and a clear message.

## ADR-012: Startup survives being offline; auth errors don't retry
**Decision.** Startup Telegram calls retry `TelegramNetworkError` and `TelegramServerError` forever, with capped exponential backoff and jitter (autostart at login often runs before Wi-Fi connects). `TelegramUnauthorizedError` becomes a `ConfigError` (exit 2) immediately.

## ADR-013: Handlers are built by router factories
**Context.** An aiogram `Router` can be attached to only one dispatcher, so module-level routers break tests and any rebuild.
**Decision.** Each handler module exposes `build_router()`. Every command's visibility comes from one registry (`bot/commands.py`); `setMyCommands` and `/help` show only commands whose milestone has shipped.

## ADR-014: An offline mode is a first-class path
**Decision.** With no AI key (or the AI down), resumes are parsed by rules (contacts, skills vocabulary, date ranges, degrees) and jobs get stage-1 "basic match" scores with their own calibrated threshold (`BASIC_MATCH_THRESHOLD`, set from the eval).
**Consequences.** The bot is useful from minute one, and AI outages degrade quality instead of availability.

## ADR-015: Cost guard via a usage meter in a context variable
**Decision.** Each run gets a `UsageMeter` whose token cap is min(per-run cap, what today's USD budget still buys at the model's price). Adapters check it before every call and raise `LLMBudgetExceededError`, which the scorer treats like "AI unavailable".
**Consequences.** Spend can't run away, even with a bug in a loop; `/stats` shows the estimated cost.

## ADR-016: Callback data carries IDs, never free text
**Context.** aiogram reserves `:` as the callback field separator; company names and times can contain it.
**Decision.** Buttons carry option indexes, match IDs and fixed codes only; times are encoded `HHMM`. Company names are looked up from the match.

## ADR-017: Test the SDK boundary for SDKs with their own HTTP stack
**Context.** anthropic 2.x and openai 3.x ship `httpx2`, which respx can't intercept.
**Decision.** Adapter tests stub the SDK method and assert the exact request (schema, system prompt, model). Gemini (plain httpx) is tested with an injected mock transport.

## ADR-018: A matching quality gate in CI
**Decision.** `tests/unit/test_evals.py` runs the labeled eval offline and fails if pairwise accuracy drops below 0.9 or Spearman ρ below 0.7 for any persona.
