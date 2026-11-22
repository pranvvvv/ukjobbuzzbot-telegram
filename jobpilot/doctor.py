"""`python -m jobpilot doctor`: a green/red checklist of everything JobPilot needs.

Checks are read-only: doctor never migrates the DB or burns job-source quota unless
asked (`--sources`). Keys for features that aren't built yet show as "later", not red,
so a correct setup is all green.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from jobpilot.core.config import LLMProvider, Settings, is_under_sync_folder, load_settings
from jobpilot.core.errors import ConfigError
from jobpilot.core.lock import lock_is_free
from jobpilot.db.migrate import current_revision, head_revision

MIN_PYTHON = (3, 12)


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    LATER = "later"  # optional extra, not set
    SKIP = "skip"  # could not run because an earlier check failed


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str


LLM_PING: dict[LLMProvider, tuple[str, Callable[[str], dict[str, str]]]] = {
    LLMProvider.GEMINI: (
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
        lambda key: {"x-goog-api-key": key},
    ),
    LLMProvider.OPENAI: (
        "https://api.openai.com/v1/models",
        lambda key: {"Authorization": f"Bearer {key}"},
    ),
    LLMProvider.GROQ: (
        "https://api.groq.com/openai/v1/models",
        lambda key: {"Authorization": f"Bearer {key}"},
    ),
    LLMProvider.ANTHROPIC: (
        "https://api.anthropic.com/v1/models?limit=1",
        lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"},
    ),
}


def check_python() -> CheckResult:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info[:2] >= MIN_PYTHON:
        return CheckResult("Python", Status.OK, version)
    return CheckResult("Python", Status.FAIL, f"{version}; JobPilot needs 3.12 or newer")


def check_env_file(cwd: Path) -> CheckResult:
    if (cwd / ".env").is_file():
        return CheckResult(".env file", Status.OK, str(cwd / ".env"))
    return CheckResult(
        ".env file",
        Status.WARN,
        "no .env here: copy .env.example to .env (or set environment variables)",
    )


def check_repo_location(cwd: Path) -> CheckResult:
    if is_under_sync_folder(cwd):
        return CheckResult(
            "Project folder",
            Status.WARN,
            "inside a cloud-synced folder (OneDrive?). Move it to e.g. C:\\dev\\jobpilot "
            "so .venv and .git aren't synced",
        )
    return CheckResult("Project folder", Status.OK, str(cwd))


def check_data_dir(settings: Settings) -> CheckResult:
    data_dir = settings.data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".doctor-", delete=True) as fh:
            fh.write(b"ok")
    except OSError as exc:
        return CheckResult("Data folder", Status.FAIL, f"{data_dir} is not writable: {exc}")
    if is_under_sync_folder(data_dir):
        return CheckResult(
            "Data folder",
            Status.FAIL,
            f"{data_dir} is cloud-synced; a synced SQLite database can corrupt. "
            "Set DATA_DIR to a local folder",
        )
    return CheckResult("Data folder", Status.OK, str(data_dir))


def check_timezone(settings: Settings) -> CheckResult:
    tz = ZoneInfo(settings.default_timezone)
    now = datetime.now(tz).strftime("%H:%M %Z")
    return CheckResult("Timezone", Status.OK, f"{settings.default_timezone} (now {now})")


def check_allowlist(settings: Settings) -> CheckResult:
    ids = sorted(settings.allowed_user_ids)
    return CheckResult(
        "Allowed users", Status.OK, f"{len(ids)} user(s): {', '.join(map(str, ids))}"
    )


async def check_database(settings: Settings) -> CheckResult:
    url = settings.resolved_database_url
    db_file = settings.data_dir / "jobpilot.db"
    if url.startswith("sqlite") and not db_file.exists():
        return CheckResult("Database", Status.OK, "not created yet (`run` or `migrate` creates it)")
    if url.startswith("sqlite") and not os.access(db_file, os.W_OK):
        return CheckResult("Database", Status.FAIL, f"{db_file} is read-only")
    try:
        current = await current_revision(url)
    except Exception as exc:
        return CheckResult("Database", Status.FAIL, f"can't open: {exc}")
    head = head_revision(url)
    if current == head:
        return CheckResult("Database", Status.OK, f"schema up to date (rev {head})")
    return CheckResult(
        "Database", Status.WARN, f"schema at {current or 'empty'}, latest {head}: run `migrate`"
    )


async def check_telegram(settings: Settings, client: httpx.AsyncClient) -> list[CheckResult]:
    base = f"https://api.telegram.org/bot{settings.telegram_bot_token.get_secret_value()}"
    try:
        me = await client.get(f"{base}/getMe")
    except httpx.HTTPError as exc:
        return [
            CheckResult(
                "Telegram token", Status.FAIL, f"can't reach Telegram: {type(exc).__name__}"
            )
        ]
    if me.status_code == 401:
        return [
            CheckResult("Telegram token", Status.FAIL, "rejected by Telegram (revoked or mistyped)")
        ]
    if me.status_code != 200:
        return [CheckResult("Telegram token", Status.FAIL, f"unexpected HTTP {me.status_code}")]
    bot = me.json().get("result", {})
    results = [
        CheckResult("Telegram token", Status.OK, f"@{bot.get('username')} (id {bot.get('id')})")
    ]
    try:
        hook = (await client.get(f"{base}/getWebhookInfo")).json().get("result", {})
    except (httpx.HTTPError, ValueError):
        return results
    if hook.get("url"):
        results.append(
            CheckResult("Telegram webhook", Status.WARN, "a webhook is set; `run` will remove it")
        )
    return results


async def check_llm(settings: Settings, client: httpx.AsyncClient) -> CheckResult:
    name = f"LLM key ({settings.llm_provider.value})"
    if settings.llm_provider is LLMProvider.FAKE or settings.llm_api_key is None:
        return CheckResult(
            name,
            Status.WARN,
            "no AI key: rule-based resume parsing and basic scoring (set LLM_API_KEY to upgrade)",
        )
    url, headers = LLM_PING[settings.llm_provider]
    try:
        resp = await client.get(url, headers=headers(settings.llm_api_key.get_secret_value()))
    except httpx.HTTPError as exc:
        return CheckResult(name, Status.FAIL, f"can't reach provider: {type(exc).__name__}")
    if resp.status_code == 200:
        return CheckResult(name, Status.OK, "key accepted (no tokens used)")
    if resp.status_code in (400, 401, 403):  # Gemini answers a bad key with 400
        return CheckResult(
            name,
            Status.FAIL,
            f"key rejected (HTTP {resp.status_code}). Check LLM_API_KEY, or a proxy/firewall",
        )
    if resp.status_code == 429:
        return CheckResult(name, Status.WARN, "rate-limited right now (key looks valid)")
    return CheckResult(name, Status.FAIL, f"unexpected HTTP {resp.status_code}")


async def check_sources(
    settings: Settings, client: httpx.AsyncClient, *, live: bool
) -> list[CheckResult]:
    results: list[CheckResult] = []
    if settings.adzuna_app_id is None or settings.adzuna_app_key is None:
        results.append(
            CheckResult("Adzuna keys", Status.LATER, "optional: free key adds Adzuna India jobs")
        )
    elif not live:
        results.append(CheckResult("Adzuna keys", Status.OK, "set (use --sources for a live test)"))
    else:
        try:
            resp = await client.get(
                "https://api.adzuna.com/v1/api/jobs/in/search/1",
                params={
                    "app_id": settings.adzuna_app_id.get_secret_value(),
                    "app_key": settings.adzuna_app_key.get_secret_value(),
                    "results_per_page": 1,
                },
            )
            ok = resp.status_code == 200
            results.append(
                CheckResult(
                    "Adzuna keys",
                    Status.OK if ok else Status.FAIL,
                    "live call OK (1 request used)" if ok else f"HTTP {resp.status_code}",
                )
            )
        except httpx.HTTPError as exc:
            results.append(CheckResult("Adzuna keys", Status.FAIL, type(exc).__name__))
    if settings.rapidapi_key is None:
        results.append(
            CheckResult("JSearch key", Status.LATER, "optional: adds Google-for-Jobs results")
        )
    else:
        results.append(CheckResult("JSearch key", Status.OK, "set (rationed: tiny free quota)"))
    return results


def check_matching(settings: Settings) -> list[CheckResult]:
    from jobpilot.sources.boards import load_companies  # noqa: PLC0415

    results = []
    try:
        companies = load_companies(settings.companies_file)
        boards = {a: sum(c.ats == a for c in companies) for a in ("greenhouse", "lever", "ashby")}
        results.append(
            CheckResult(
                "Company boards",
                Status.OK if companies else Status.WARN,
                f"{len(companies)} companies ({', '.join(f'{k} {v}' for k, v in boards.items())})",
            )
        )
    except (OSError, ValueError, KeyError) as exc:
        results.append(CheckResult("Company boards", Status.FAIL, f"companies.yaml: {exc}"))
    if settings.use_local_embeddings:
        cached = any((settings.cache_dir / "models").glob("**/*.onnx"))
        results.append(
            CheckResult(
                "Embedding model",
                Status.OK,
                "downloaded" if cached else "downloads once (~70 MB) on the first search",
            )
        )
    else:
        results.append(CheckResult("Embedding model", Status.OK, "off (lexical similarity)"))
    return results


def check_instance(settings: Settings) -> CheckResult:
    if lock_is_free(settings.lock_path):
        return CheckResult("Running instance", Status.OK, "none (free to start)")
    return CheckResult("Running instance", Status.WARN, "JobPilot is already running")


async def run_checks(
    *,
    cwd: Path | None = None,
    live_sources: bool = False,
    settings_loader: Callable[[], Settings] = load_settings,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> list[CheckResult]:
    cwd = cwd or Path.cwd()
    results = [check_python(), check_env_file(cwd), check_repo_location(cwd)]
    try:
        settings = settings_loader()
    except ConfigError as exc:
        results.append(CheckResult("Configuration", Status.FAIL, "; ".join(exc.problems)))
        results.append(CheckResult("Remaining checks", Status.SKIP, "fix configuration first"))
        return results
    results.append(CheckResult("Configuration", Status.OK, "all required keys present"))
    results += [check_allowlist(settings), check_data_dir(settings), check_timezone(settings)]
    results.append(await check_database(settings))

    make_client = client_factory or (
        lambda: httpx.AsyncClient(timeout=settings.http_timeout_seconds)
    )
    async with make_client() as client:
        results += await check_telegram(settings, client)
        results.append(await check_llm(settings, client))
        results += await check_sources(settings, client, live=live_sources)
    results += check_matching(settings)
    results.append(check_instance(settings))
    return results


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status is Status.FAIL for r in results) else 0
