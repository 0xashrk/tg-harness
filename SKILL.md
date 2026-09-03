---
name: Telegram Telethon harness
description: >-
  Use this when an agent needs Telegram user I/O via tg-harness: login, pull,
  send, or the secretary NewMessage watcher that webhooks a Grok routine.
  Reporter never sends. Secretary never posts in report chats
---
# Telegram Telethon harness

Checkout: `tg-harness`. 

```bash
export TG_HARNESS_ROLE=reporter    # or secretary
python -m tg_harness.cli status
python -m tg_harness.cli pull "Chat" --hours 24
python -m tg_harness.cli send "Chat" --text "..."          # secretary role only
python -m tg_harness.cli send "Chat" --reply-to <id> --text "..."
python -m tg_harness.cli watch                             # secretary role only
TG_HARNESS_ROLE=secretary python -m tg_harness.cli restore # one-shot WARP+watch heal
```

Keep `watch` running (long-running: `scripts/supervise.sh`; one-shot heal when SOCKS/watch die: `bash /abs/path/scripts/restore-pipeline.sh` or `TG_HARNESS_ROLE=secretary python -m tg_harness.cli restore`. Use absolute paths so Auto-review can bind the command; if binding fails, escalate or inline the script body. Never start a dummy SOCKS / `local_socks5`. Never open a second Telethon client.) `watch.sock` is request/response Unix IPC, not a WebSocket. The Telegram pipe is Telethon. `pull` / `send` / `status` go through the socket on the same client. Do **not** kill watch to send. Do not open a second client.

`watch` listens on `mode=secretary` only, queues `out/secretary-queue.jsonl`, POSTs the webhook, and on startup backfills inbound after the last outgoing. Report chats (`mode=report`) are ignored and send-blocked.

The CLI enforces this: `TG_HARNESS_ROLE=reporter` cannot `send` or pull secretary chats. `secretary` cannot pull or send report chats. Names/ids must exist in `config.toml` (no raw-id fallback). Telegram’s live title must match the config title.

WARP proxy mode on `127.0.0.1:40000` if MTProto is blocked. Do not change the default route.

Forks use their own `api_id` / `api_hash`. Do not ship session, `.env`, webhook key, or `watch.sock`.

## Roles

Set `TG_HARNESS_ROLE` in the agent process, not in the shared `.env`.

- Reporter: `AGENTS_REPORTER.md` — `TG_HARNESS_ROLE=reporter`, scheduled `pull`, write the brief. `send` is a hard error.
- Secretary: `AGENTS_SECRETARY.md` — `TG_HARNESS_ROLE=secretary`, woken by the webhook, `pull`/`send` through the live watcher.

## Reply targeting

User named it → last inbound in a 1:1 → last @user in a small group → else ask.

## Quoting

Do not quote every message. In a 1:1, just send. Only `--reply-to` when they asked about a specific older message or the group thread would be ambiguous without it.
