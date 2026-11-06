<p align="center">
  <img src="docs/banner.svg" width="100%" alt="dingding: a hooded keeper with a glowing staff guards the gate between your tower and the cloud city. Plain letters pass with secrets blacked out; sealed scrolls stay behind.">
</p>

# dingding

A privacy-first personal assistant for **Gmail, Google Calendar and Notion**, chatted with over Telegram.
A strong cloud model (ChatGPT through [OpenClaw](https://openclaw.ai)) does the thinking and writing,
but it **never holds your keys and only sees filtered data**: a local model on your own machine decides
what is sensitive and hides private values before anything leaves it.

```
Telegram ──▶ OpenClaw + ChatGPT (Docker, no keys) ──unix socket──▶ privacy broker (host, holds all keys)
                                                                      │  ① rules (regex)   ② local LLM (Ollama)
                                                                      ▼
                                                     Gmail · Calendar · Notion
```

## What it does
- **Briefs** at 5 AM / noon / 6 PM: action items, emails to reply to, events you might like, heads-ups,
  and a 🔒 PRIVATE list (sensitive emails show only the sender).
- **Replies as drafts**: "reply 2 say Tuesday works" puts a draft in Gmail. **It cannot send email**; you tap Send.
- **Calendar**: suggests events from email; adds them only when you say "add 1 3".
- **Archive / cleanup**: "archive the LinkedIn one and both newsletters", "empty my spam",
  "trash promotions older than 2 weeks". A weekly Sunday cleanup is included. Everything is undoable.
- **Notion**: clean notes and recipes, database rows, careful edits (preview + yes), undo, nightly backup.

## How the privacy works
| Layer | Where | Job |
|---|---|---|
| ① Rules | host | Drop 2FA/password-reset emails entirely; flag banks, health, immigration, tax, grades; find account numbers, phones, IDs, addresses |
| ② Local model | host (Ollama) | Sort each email (action / reply / event / heads-up / skip), judge sensitivity, catch PII the rules missed |
| ③ Cloud model | container | Sees only summaries and redacted text: `Call me at [PHONE_1]` |
| ④ Broker | host | Holds all keys. Filters reads; writes go straight through with placeholders put back (`[PHONE_1]` → real number in your draft). Lines with hidden values are locked against edits |
| ⑤ You | Telegram | Approve calendar adds, edits to existing Notion pages, bulk cleanups |

Placeholder mappings stay on the host (0700) and expire after 30 days. See [docs/architecture.md](docs/architecture.md).

## Requirements
- Linux machine that can stay on (tested on a ThinkPad, Ryzen 5 PRO 4650U, 22 GB RAM, no GPU)
- Docker, Python 3.13 with [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com)
- Local model: `hf.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:UD-Q2_K_XL` (~12 GB RAM, ~12–25 s per email on CPU).
  With less RAM use `qwen3:8b` (set `PRIVACY_MODEL`); it is less accurate at finding PII.
- A ChatGPT subscription (OpenClaw's Codex login), a Telegram bot, a Google Cloud OAuth client, optionally a Notion integration

## Setup
1. **Code and Python**
   ```bash
   git clone https://github.com/triakshunn/dingding ~/dingding && cd ~/dingding
   uv sync
   ollama pull hf.co/unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF:UD-Q2_K_XL
   ```
2. **Google**: create an OAuth *Desktop* client in Google Cloud (Gmail + Calendar APIs enabled, your
   accounts added as test users) and save it as `~/dingding/credentials.json`. Then, per account:
   ```bash
   mkdir -p data && cp config.example.json data/config.json   # edit timezone, calendar account, name
   uv run auth.py personal
   uv run auth.py school --calendar        # the account whose calendar gets suggestions
   ```
   Create a calendar for the agent (e.g. "AI Suggestions") in that account and put its id in `data/config.json`.
3. **Notion (optional)**: create an internal integration, share your pages with it, and save the key:
   ```bash
   mkdir -p -m 700 ~/.local/share/privacy-broker
   (umask 077; read -rs -p "Notion key: " k && echo "$k" > ~/.local/share/privacy-broker/notion_token)
   ```
4. **Broker** (host service):
   ```bash
   cp broker/deploy/privacy-broker.service ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now privacy-broker
   ```
5. **OpenClaw** in Docker:
   ```bash
   cd docker && mkdir -p -m 700 broker-io state && docker compose up -d --build
   docker compose exec -it openclaw openclaw onboard          # Telegram bot + ChatGPT login (device code)
   mkdir -p state/workspace/skills && cp -r ../skills/* state/workspace/skills/
   ```
   Edit the `<!-- EDIT -->` parts of `state/workspace/skills/inbox/SKILL.md` (your name, timezone,
   accounts, interests) and `.../notion/SKILL.md`. Apply the privacy settings (no OpenClaw memory/dreaming/heartbeat):
   ```bash
   docker compose exec -T openclaw openclaw config patch --stdin < ../ops/openclaw-privacy.patch.json5
   ```
   Then create the brief cron jobs in OpenClaw (e.g. "check my email and write the morning brief", 0 5 * * *).
6. **Ops (optional)**: watchdog (Telegram alerts) and the Sunday cleanup. Set `TELEGRAM_CHAT_ID` in both
   `.service` files, then:
   ```bash
   cp ops/deploy/*.service ops/deploy/*.timer ~/.config/systemd/user/ && systemctl --user daemon-reload
   systemctl --user enable --now watchdog.timer mail-cleanup.timer
   ```

## Tests
```bash
uv run --with pytest pytest -q privacy/tests                  # filter rules, model mocked
uv run python -m unittest discover -s broker/tests            # broker end to end, fake Google/Notion
```
The broker tests check, among other things, that no secret value from the fake mailbox ever reaches the container.

## Commands the agent uses (inside the container)
`ia new · ia search · ia read · ia suggest-event · ia suggest-reply · ia pending · ia cal-add · ia draft ·
ia recent · ia archive · ia unarchive · ia cleanup · ia mail-log · ia undo-mail` and
`nt search · nt read · nt dbs · nt query · nt create · nt append · nt set · nt move · nt archive · nt log · nt undo`.
`ia-send` exists only to refuse.

## Limits and honest caveats
- A personal project, not audited. The filter reduces what the cloud sees; it is not a guarantee.
  Emails it marks sensitive are never sent, but a missed phone number in a normal email could be.
- The local model is slow on CPU; the broker pre-filters new mail in the background every 5 minutes.
- Gmail tokens on the host can technically send mail (the compose scope); the code never does, and the
  container never gets the tokens.
- Built against OpenClaw 2026.10.6.

## License
MIT. See [LICENSE](LICENSE).
