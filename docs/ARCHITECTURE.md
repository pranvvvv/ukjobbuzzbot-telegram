# Architecture

```mermaid
flowchart TD
    TG[Telegram] <--> BOT["bot/ · aiogram 3 long polling<br/>AllowlistMiddleware · SQLStorage (FSM)"]
    BOT --> H[handlers: start · onboarding · questions · jobs · menu · fallback]
    H --> SVC[services/]
    SCHED["scheduler/ · 1-min tick (APScheduler 3.11)"] --> DEL[bot/delivery.py · rate-limited Sender]
    H --> DEL
    DEL --> SVC
    SVC --> PROF[profile.py → resume/ extract · schema · profile]
    SVC --> SEARCH[search.py · orchestrator]
    SEARCH --> Q[queries.py] --> LLM
    SEARCH --> SRC[sources/ registry · http (quota, cache, retries)]
    SEARCH --> M[matching/ dedupe · filters · scam · scoring · links]
    M --> LLM["llm/ LLMClient (Claude · Gemini · OpenAI · Groq · Offline)"]
    M --> EMB[llm/embeddings · fastembed bge-small]
    SVC --> DB[(db/ SQLite WAL · SQLAlchemy 2.1 · Alembic)]
```

## Layering rules
- `bot/` knows Telegram; `services/` doesn't. A web UI could replace `bot/` without touching use-cases.
- `services/` reaches storage through repositories/sessions and providers through protocols only (`LLMClient`, `Embedder`, `JobSource`).
- `container.py` is the single composition root; tests build it with fakes (scripted LLM, hashing embedder, stub sources, fake Telegram session).
- Every user-visible string lives in `bot/formatting.py` or `bot/questions_ui.py`; every dynamic value is HTML-escaped after truncation.

## Data flow of one search
`SearchService.run` (per-user lock) → `build_queries` (AI or rules) → sources in parallel (each isolated, quota-checked, cached) → `dedupe` → persist jobs (upsert by fingerprint) → drop jobs this user has already been scored on → `apply_filters` → `scam.assess` → `stage1` (top K) → `stage2` (AI rubric in batches, budget-metered) → link health → store matches → select the best unsent matches → `Delivery` sends cards and a summary. Every run is recorded in `runs` with counts, errors, tokens and estimated cost.

## Tables
`users` · `profiles` (versioned; drafts inactive until confirmed) · `preferences` (prefs + feedback weights) · `jobs` (fingerprint unique) · `matches` (unique per user+job: the "never twice" invariant) · `feedback` · `runs` · `api_usage` (quota) · `fsm_state` · `http_cache` · `link_cache`.

## Reliability map
| Failure | Behaviour |
|---|---|
| Laptop asleep at digest time | DB-driven due check; one coalesced digest on wake |
| Network down at startup | Telegram calls retried with capped backoff until online |
| One source down / over quota | Recorded in the run, others continue; summary says results are partial |
| All sources down | Honest message; digest retried in 30 min (×3) |
| AI down / bad JSON / budget spent | Validated repair → batch falls back to stage-1 "basic match" scores |
| Duplicate bot instance | File lock (same machine) + 409 preflight + conflict watchdog (anywhere) |
| User blocked the bot | Digest disabled; re-enabled by `/start` |
| Telegram flood limits | Per-chat 1 msg/s, global 25/s, `RetryAfter` honoured |
