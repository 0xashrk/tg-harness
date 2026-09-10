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
python -m tg_harness.cli card show "Chat"                  # secretary role only
python -m tg_harness.cli card write "Chat" --relationship friend --voice "…"
python -m tg_harness.cli watch                             # secretary role only
python -m tg_harness.cli event on --name "Meetup"          # wake on stranger private 1:1s
python -m tg_harness.cli event off
```

Keep `watch` running (supervise it with `scripts/supervise.sh` or it dies overnight and the secretary never wakes). `watch.sock` is request/response Unix IPC, not a WebSocket. The Telegram pipe is Telethon. `pull` / `send` / `status` go through the socket on the same client. Do **not** kill watch to send. Do not open a second client.

`watch` listens on `mode=secretary` chats, and when `[event] enabled = true` also on unknown private 1:1 DMs. It queues `out/secretary-queue.jsonl`, POSTs the webhook, and on startup backfills inbound after the last outgoing (allowlisted only). Report chats (`mode=report`) are ignored and send-blocked. See Event mode in `AGENTS_SECRETARY.md`.

The CLI enforces this: `TG_HARNESS_ROLE=reporter` cannot `send` or pull secretary chats. `secretary` cannot pull or send report chats. Names/ids must exist in `config.toml` (no raw-id fallback) unless event mode is on and the id is an unknown private chat. Telegram’s live title must match the config title (skipped for ephemeral event chats).

WARP proxy mode on `127.0.0.1:40000` if MTProto is blocked. Do not change the default route.

Forks use their own `api_id` / `api_hash`. Do not ship session, `.env`, webhook key, or `watch.sock`.

## Relationship cards

Secretary-only durable notes per allowlisted chat: `out/cards/<chat_id>.md` (gitignored). Schema in `cards/README.md`. On webhook wake: load card if present, then pull recent history at the usual depth (~48h) — cards do not replace that pull.

## Roles

Set `TG_HARNESS_ROLE` in the agent process, not in the shared `.env`.

- Reporter: `AGENTS_REPORTER.md` — `TG_HARNESS_ROLE=reporter`, scheduled `pull`, write the brief. `send` is a hard error.
- Secretary: `AGENTS_SECRETARY.md` — `TG_HARNESS_ROLE=secretary`, woken by the webhook, `pull`/`send` through the live watcher.

## Reply targeting

User named it → last inbound in a 1:1 → last @user in a small group → else ask.

## Quoting

Do not quote every message. In a 1:1, just send. Only `--reply-to` when they asked about a specific older message or the group thread would be ambiguous without it.
