# Architecture

## Trust boundaries
| Component | Runs | Holds | Can |
|---|---|---|---|
| OpenClaw + ChatGPT | Docker (`cap_drop: ALL`, no-new-privileges, 4 GB, no Docker socket, no home folder) | nothing secret | ask the broker over a Unix socket |
| Privacy broker (`broker/server.py`) | host, systemd user service | Gmail tokens, Notion key, placeholder mappings | read and filter, draft, add to calendar, archive/trash, edit Notion |
| Local model | host, Ollama | nothing | classify and redact text |
| Watchdog / Sunday cleanup / Notion backup | host, systemd timers | — | alert on Telegram, call the broker, back up Notion |

## Reads (filtered)
`privacy.filter_email` / `privacy.filter_page`:
1. **Drop**: 2FA codes and password resets never leave the host (only counted).
2. **Sensitive**: sender lists (banks, health, immigration, tax, registrar) + keyword regexes (subject and body,
   footers stripped) + the local model's judgment. Sensitive mail is shown to the cloud model as sender only.
3. **Redact**: regexes (cards, accounts, phones, SSN, passports, IDs, addresses) + PII the local model finds
   become stable placeholders like `[PHONE_1]`. The mapping is stored on the host for 30 days.
4. **Summarize/bucket**: the local model gives each email a bucket, one-line summary and events.

Results are cached per message / per Notion `last_edited_time`; the broker pre-filters new mail every
5 minutes and changed Notion pages every 30 minutes, at lower priority than your own requests.

## Writes (pass-through)
The cloud model writes; the broker puts real values back (`rehydrate`) just before saving:
- Gmail drafts (never sent; `ia-send` refuses), calendar events (only from pending suggestions the user named),
  Notion create/append/set/move/archive (edits need `--confirmed`, which the skill only passes after the user's yes).
- Edits that would remove or change a placeholder are refused (locked lines).
- Every Notion change is logged with an undo snapshot. Archive and cleanup batches are logged and undoable.

## Mail archive and cleanup
- `ia archive` removes only the INBOX (and UNREAD) labels from whole conversations the user named.
  More than 5 emails or a group needs a confirmation round trip. 50 per call and 300 per day max.
- `ia cleanup` works only on the Spam / Promotions / Social / Updates / Forums areas, excludes starred and
  important mail, and runs in two steps: preview (plan id, counts, top senders; spam shows domains only),
  then `--confirm <plan_id>` carries out exactly that plan. Trash is recoverable for 30 days.

## Known gaps
- The broker cannot tell whether a request came from the user or from a prompt-injected model.
  Mitigations: content is labelled untrusted, dangerous actions don't exist (no send, no permanent delete),
  bulk actions need a preview round trip, rate limits, logs and undo.
- Calendar uses the `calendar.app.created` scope, so the agent can only touch calendars it created.
