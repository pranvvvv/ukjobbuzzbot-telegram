# Setup guide

Time needed: about 10 minutes. Nothing is installed on your phone: Telegram is the interface, and your laptop runs the agent.

> **Before you start (Windows):** keep the project **outside OneDrive** (e.g. `C:\dev\jobpilot`). OneDrive syncing `.venv`, `.git` and the database causes lock errors and conflict copies. JobPilot keeps its data in `%LOCALAPPDATA%\JobPilot` automatically.

## 1. Create your bot with @BotFather
1. In Telegram, open **@BotFather** and send `/newbot`.
2. Choose a display name (e.g. *JobPilot*) and a username ending in `bot` (e.g. `pranav_jobpilot_bot`).
3. Copy the token it gives you (`123456789:AA...`). This is `TELEGRAM_BOT_TOKEN`. Treat it like a password.

<!-- screenshot: botfather-newbot.png -->

## 2. Get your Telegram user ID
Easiest: send your new bot `/start` in Telegram, then run
```text
uv run python -m jobpilot whoami --save
```
It lists who has messaged the bot and writes your ID into `.env` as `ALLOWED_USER_IDS` (the setup script does this for you). It needs only the bot token and doesn't consume the bot's messages.

Alternative: message **@userinfobot**, which replies with your numeric ID.

## 3. Get an AI key (recommended: Claude)
JobPilot works **without** a key (rule-based resume parsing and "basic match" scoring), but a key makes it much smarter: accurate profile extraction, the 100-point rubric, reasons, gaps and pitches.

Set `LLM_PROVIDER` and paste the key into `LLM_API_KEY`:

| Provider | Where to get a key | Default model | Note |
|---|---|---|---|
| `anthropic` (default) | console.anthropic.com → API Keys | `claude-haiku-5-5` | About $0.009 per full search; set `LLM_MODEL=claude-sonnet-5-5` for higher quality |
| `gemini` | Google AI Studio → *Get API key* | `gemini-2.5-flash-lite` | On the **free tier, Google may use your content to improve its products** |
| `groq` | console.groq.com → API Keys | `openai/gpt-oss-120b` | |
| `openai` | platform.openai.com → API keys | `gpt-5-mini` | |

The daily AI spend is capped by `LLM_DAILY_BUDGET_USD` (default $0.25). When it's reached, JobPilot switches to basic scoring until tomorrow.

## 4. Optional job-source keys (more jobs)
- **Adzuna** (recommended for India): register at developer.adzuna.com → `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`. Your limits are shown in your Adzuna dashboard. Without it, JobPilot still searches the 21 company boards plus the remote boards.
- **JSearch** (RapidAPI): subscribe to the free Basic plan → `RAPIDAPI_KEY`. The free quota is very small; JobPilot rations it.

## 5. Install and check
**Windows (PowerShell):**
```powershell
cd C:\dev\jobpilot
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```
**Linux / macOS:**
```bash
./setup.sh
```
The script installs `uv` (if needed), Python 3.13 and all dependencies, creates `.env` from `.env.example`, opens it for you to fill in, then runs **doctor**:

```text
uv run python -m jobpilot doctor            # read-only checklist
uv run python -m jobpilot doctor --sources  # also 1 live call per job source
```
`PASS` = good, `OPTIONAL` = an extra you can add later, `WARN` = works but worth fixing, `FAIL` = must fix.

<!-- screenshot: doctor-all-green.png -->

## 6. Run
```text
uv run python -m jobpilot run
```
Open your bot in Telegram and send `/start`. Anyone not in `ALLOWED_USER_IDS` is ignored silently.

## 7. Start automatically at login (Windows)
```powershell
powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1     # remove: .\uninstall-autostart.ps1
```
It runs hidden, restarts if it crashes, and sends a missed digest once when the laptop wakes. Logs: `%LOCALAPPDATA%\JobPilot\logs\jobpilot.log`.

## 8. Optional
- **Company boards:** edit `jobpilot/data/companies.yaml` (or point `COMPANIES_FILE` at your own copy), then check every board with `uv run python -m jobpilot companies`.
- **Matching quality:** `uv run python -m jobpilot eval --local-embeddings --ai` writes `evals/RESULTS.md`.
- **Always-on hosting:** `docker compose up -d` on any small VPS (your data lives in the `jobpilot-data` volume). Run it in only one place at a time: Telegram allows one poller per bot.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `TELEGRAM_BOT_TOKEN was rejected` | Re-copy the token from @BotFather (`/token`), or revoke and regenerate it |
| `Another process is already polling this bot token` | Stop the other copy (another terminal, Docker, a second laptop) |
| `JobPilot is already running on this computer` | It's already running; check Task Manager for `python` |
| `Data folder ... is cloud-synced` | Set `DATA_DIR` in `.env` to a local folder |
| `key rejected (HTTP 400/401/403)` | Check `LLM_API_KEY` and `LLM_PROVIDER` match; a corporate proxy can also cause this |
| Cards say "basic match" | No AI key, AI down, or today's budget used; add a key or raise `LLM_DAILY_BUDGET_USD` |
| "No strong matches" every day | Loosen `/preferences` (more cities, relocation, job types) or lower `MATCH_THRESHOLD` |
| A company board shows HTTP 404 in `companies` | The company changed ATS; fix or disable its line in `companies.yaml` |
