#!/usr/bin/env python3
"""login | pull | send | chats | watch | restore — one live Telethon client when watch is up."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def load_dotenv(path) -> None:
    try:
        from dotenv import load_dotenv as _load
    except ImportError:
        return
    _load(path)

from tg_harness.policy import (
    PolicyError,
    chat_by_id,
    echo_chat,
    refuse_live_title,
    refuse_pull,
    refuse_send,
    refuse_watch,
    resolve_chat,
    role_of,
)

ROOT = Path(__file__).resolve().parent.parent
SOCK = ROOT / "watch.sock"
PID = ROOT / "watch.pid"


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def load_config(path: Path) -> dict:
    if not path.is_file():
        die(f"missing {path}. Copy config.example.toml to config.toml")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def proxy_from(cfg: dict):
    host = cfg.get("proxy_host") or os.getenv("TELEGRAM_PROXY_HOST", "127.0.0.1")
    port = int(cfg.get("proxy_port") or os.getenv("TELEGRAM_PROXY_PORT", "40000"))
    return ("socks5", host, port)


def client_from(cfg: dict):
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    load_dotenv(ROOT / ".env")
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        die("set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env (see .env.example)")
    string_path = ROOT / "user.session.string"
    if string_path.is_file():
        return TelegramClient(
            StringSession(string_path.read_text().strip()),
            int(api_id),
            api_hash,
            proxy=proxy_from(cfg),
        )
    session = Path(cfg.get("session_path") or "user.session")
    if not session.is_absolute():
        session = ROOT / session
    return TelegramClient(
        str(session.with_suffix("")),
        int(api_id),
        api_hash,
        proxy=proxy_from(cfg),
    )


def require_role(cfg: dict) -> str:
    try:
        return role_of(cfg)
    except PolicyError as exc:
        die(str(exc))


def require_chat(cfg: dict, raw: str) -> dict:
    try:
        chat = resolve_chat(cfg, raw)
    except PolicyError as exc:
        die(str(exc))
    print(
        "resolved id={id} title={title!r} mode={mode}".format(**echo_chat(chat)),
        file=sys.stderr,
    )
    return chat


def live_title(entity) -> str:
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    name = " ".join(
        p
        for p in [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
        if p
    )
    if name:
        return name
    username = getattr(entity, "username", None)
    return str(username) if username else ""


def guard_entity(chat: dict, entity) -> None:
    err = refuse_live_title(chat, live_title(entity))
    if err:
        die(err)


def watch_up() -> bool:
    if not SOCK.exists():
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1.5)
        s.connect(str(SOCK))
        s.close()
        return True
    except OSError:
        try:
            SOCK.unlink()
        except OSError:
            pass
        return False


def call_watch(payload: dict, timeout: float = 90) -> dict:
    raw = json.dumps(payload).encode() + b"\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(SOCK))
    s.sendall(raw)
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    if not buf:
        die("watch socket returned empty")
    out = json.loads(buf.decode())
    if out.get("error"):
        die(out["error"])
    return out


async def sender_bits(msg) -> tuple[str, str]:
    sender_id, sender_name = "", ""
    try:
        sender = await msg.get_sender()
        sender_id = str(getattr(sender, "id", "") or "")
        sender_name = live_title(sender)
    except Exception:
        sender_id = str(getattr(getattr(msg, "from_id", None), "user_id", "") or "")
        sender_name = live_title(getattr(msg, "sender", None))
    if msg.out and not sender_name:
        sender_name = "me"
    return sender_id, sender_name


def reply_id(msg) -> int | None:
    reply = getattr(msg, "reply_to", None)
    return getattr(reply, "reply_to_msg_id", None) if reply is not None else None


def write_pull_files(out: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    chat_id = out["chat_id"]
    json_path = out_dir / f"{chat_id}.json"
    txt_path = out_dir / f"{chat_id}.txt"
    json_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    lines = [
        f"title={out['title']} count={out['count']} "
        f"window={out['window_start']} -> {out['window_end']}"
    ]
    for m in out["messages"]:
        stamp = datetime.fromisoformat(m["date"]).strftime("%Y-%m-%d %H:%M")
        who = "me" if m["is_outgoing"] else (m["sender_name"] or out.get("title") or "them")
        text = " ".join((m["text"] or "").split())
        if len(text) > 400:
            text = text[:400] + "…"
        extra = f" [reply_to={m['reply_to_msg_id']}]" if m["reply_to_msg_id"] else ""
        lines.append(f"{stamp} {who}: {text}{extra}")
    txt_path.write_text("\n".join(lines) + "\n")
    return json_path, txt_path


async def pull_chat(
    client,
    cfg: dict,
    chat: dict,
    hours: int,
    limit: int,
    tz_name: str,
    keep_empty: bool,
) -> dict:
    tz = ZoneInfo(tz_name)
    end = datetime.now(tz)
    start = end - timedelta(hours=hours)
    chat_id = int(chat["id"])
    entity = None
    last_err = None
    for cid in (chat_id, int(f"-100{abs(chat_id)}") if abs(chat_id) < 10**12 else None):
        if cid is None:
            continue
        try:
            entity = await client.get_entity(cid)
            break
        except Exception as exc:
            last_err = exc
    if entity is None:
        raise last_err or RuntimeError(f"could not resolve chat {chat_id}")
    guard_entity(chat, entity)
    live = live_title(entity)
    raw = await client.get_messages(entity, limit=limit, offset_date=end)
    messages = []
    for msg in raw:
        if not msg.date:
            continue
        when = msg.date.astimezone(tz)
        if when < start or when > end:
            continue
        text = (msg.text or "").strip()
        if not text and not keep_empty:
            continue
        sender_id, sender_name = await sender_bits(msg)
        if not sender_name and not msg.out:
            sender_name = live
        messages.append(
            {
                "id": msg.id,
                "date": when.isoformat(),
                "sender_id": sender_id,
                "sender_name": sender_name,
                "is_outgoing": bool(msg.out),
                "text": text,
                "reply_to_msg_id": reply_id(msg),
            }
        )
    messages.sort(key=lambda m: m["date"])
    return {
        "chat_id": chat_id,
        "title": live or chat.get("title"),
        "mode": chat.get("mode"),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "count": len(messages),
        "messages": messages,
    }


async def cmd_login(cfg: dict, args: argparse.Namespace) -> None:
    from telethon.errors import SessionPasswordNeededError

    if watch_up():
        die("watch is running; stop it before login")
    phone = args.phone or os.getenv("TELEGRAM_PHONE")
    if not phone:
        die("pass --phone or set TELEGRAM_PHONE")
    client = client_from(cfg)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"already authorized as {me.id}")
        await client.disconnect()
        return
    await client.send_code_request(phone)
    code = args.code
    if not code:
        code = input("Telegram login code: ").strip()
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        pw = args.password or input("2FA cloud password: ").strip()
        await client.sign_in(password=pw)
    me = await client.get_me()
    print(f"authorized as {me.id}")
    await client.disconnect()


async def cmd_status(cfg: dict, args: argparse.Namespace) -> None:
    role = require_role(cfg)
    if watch_up():
        print(json.dumps(call_watch({"op": "status", "role": role})))
        return
    client = client_from(cfg)
    await client.connect()
    ok = await client.is_user_authorized()
    print(json.dumps({"authorized": bool(ok), "via": "direct", "role": role}))
    await client.disconnect()


async def cmd_chats(cfg: dict, args: argparse.Namespace) -> None:
    role = require_role(cfg)
    if role == "reporter":
        rows = [
            {"id": int(c["id"]), "title": c.get("title"), "mode": c.get("mode")}
            for c in (cfg.get("chats") or [])
            if c.get("mode") == "report"
        ]
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if watch_up():
        print(json.dumps(call_watch({"op": "chats", "role": role}).get("chats"), ensure_ascii=False, indent=2))
        return
    client = client_from(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        die("not authorized. run: python -m tg_harness.cli login --phone +1...")
    rows = []
    for dialog in await client.get_dialogs():
        entity = dialog.entity
        rows.append(
            {
                "id": entity.id,
                "title": live_title(entity),
                "username": getattr(entity, "username", None),
                "kind": "user" if type(entity).__name__ == "User" else type(entity).__name__,
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    await client.disconnect()


async def cmd_pull(cfg: dict, args: argparse.Namespace) -> None:
    role = require_role(cfg)
    chat = require_chat(cfg, args.chat)
    err = refuse_pull(role, chat)
    if err:
        die(err)
    tz_name = args.timezone or cfg.get("timezone") or "UTC"
    out_dir = Path(args.out or ROOT / "out")
    if watch_up():
        out = call_watch(
            {
                "op": "pull",
                "role": role,
                "chat_id": int(chat["id"]),
                "hours": args.hours,
                "limit": args.limit,
                "timezone": tz_name,
                "keep_empty": bool(args.keep_empty),
            }
        )
        json_path, txt_path = write_pull_files(out, out_dir)
        print(json.dumps({"json": str(json_path), "txt": str(txt_path), "count": out["count"], "via": "watch", **echo_chat(chat)}))
        return
    if role == "reporter":
        die("watch is down; reporter pull requires the supervised watch")
    client = client_from(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        die("not authorized. run login first")
    out = await pull_chat(client, cfg, chat, args.hours, args.limit, tz_name, bool(args.keep_empty))
    json_path, txt_path = write_pull_files(out, out_dir)
    print(json.dumps({"json": str(json_path), "txt": str(txt_path), "count": out["count"], "via": "direct", **echo_chat(chat)}))
    await client.disconnect()


async def cmd_send(cfg: dict, args: argparse.Namespace) -> None:
    role = require_role(cfg)
    chat = require_chat(cfg, args.chat)
    err = refuse_send(role, chat)
    if err:
        die(err)
    text = args.text
    if args.file:
        text = Path(args.file).read_text().strip()
    if not text:
        die("pass --text or --file")
    if watch_up():
        req = {"op": "send", "role": role, "chat_id": int(chat["id"]), "text": text}
        if args.reply_to:
            req["reply_to"] = args.reply_to
        print(json.dumps({**call_watch(req), **echo_chat(chat)}))
        return
    if not args.direct:
        die("watch is down; pass --direct to send without watch")
    client = client_from(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        die("not authorized. run login first")
    entity = await client.get_entity(int(chat["id"]))
    guard_entity(chat, entity)
    msg = await client.send_message(entity, text, reply_to=args.reply_to)
    print(json.dumps({"sent_id": msg.id, "reply_to": args.reply_to, "via": "direct", **echo_chat(chat)}))
    await client.disconnect()


def queued_ids() -> set[int]:
    q = ROOT / "out" / "secretary-queue.jsonl"
    ids: set[int] = set()
    if not q.is_file():
        return ids
    for line in q.read_text().splitlines():
        if not line.strip():
            continue
        try:
            ids.add(int(json.loads(line)["message_id"]))
        except Exception:
            continue
    return ids


def post_webhook(url: str | None, key: str | None, payload: dict) -> str:
    if not url:
        return "skip"
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return "ok"
    except Exception as exc:
        return type(exc).__name__


async def cmd_watch(cfg: dict, args: argparse.Namespace) -> None:
    """Stay connected. pull/send go through watch.sock so we never drop the listener."""
    load_dotenv(ROOT / ".env")
    err = refuse_watch(require_role(cfg))
    if err:
        die(err)
    url = args.webhook or cfg.get("webhook_url") or os.getenv("SECRETARY_WEBHOOK_URL")
    key = os.getenv("SECRETARY_WEBHOOK_KEY")
    allow = {int(c["id"]): c for c in (cfg.get("chats") or []) if c.get("mode") == "secretary"}
    if not allow:
        die("no mode=secretary chats in config.toml")
    if watch_up():
        die("watch already running")

    client = client_from(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        die("not authorized. run login first")

    from telethon import events

    seen = queued_ids()

    async def emit(payload: dict, *, webhook: bool = True) -> None:
        mid = int(payload["message_id"])
        if mid in seen:
            return
        seen.add(mid)
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        with (out / "secretary-queue.jsonl").open("a") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        print("queued", payload.get("title") or payload.get("from"), mid, flush=True)
        if not webhook:
            return
        status = await asyncio.to_thread(post_webhook, url, key, payload)
        print("webhook_" + status, payload.get("title") or payload.get("from"), mid, flush=True)

    def match_secretary(raw_id: int, chat_id: int):
        for cid, meta in allow.items():
            if raw_id == cid or chat_id == cid or abs(chat_id) == cid:
                return cid, meta
        return None

    @client.on(events.NewMessage(incoming=True))
    async def on_new(event):
        chat_id = int(event.chat_id)
        raw_id = getattr(event.chat, "id", None) or chat_id
        hit = match_secretary(raw_id, chat_id)
        if hit is None or event.out:
            return
        cid, meta = hit
        from_name = live_title(await event.get_sender()) or live_title(event.chat) or meta.get("title")
        await emit(
            {
                "type": "secretary_inbound",
                "chat_id": cid,
                "title": live_title(event.chat) or meta.get("title"),
                "from": from_name,
                "message_id": event.id,
                "text": (event.raw_text or "")[:500],
                "reply_to_msg_id": getattr(getattr(event, "reply_to", None), "reply_to_msg_id", None),
            }
        )

    async def catch_up() -> None:
        tz_name = cfg.get("timezone") or "UTC"
        for cid, meta in allow.items():
            out = await pull_chat(client, cfg, meta, 2, 80, tz_name, False)
            last_out = 0
            for m in out["messages"]:
                if m["is_outgoing"]:
                    last_out = max(last_out, int(m["id"]))
            for m in out["messages"]:
                if m["is_outgoing"] or int(m["id"]) <= last_out:
                    continue
                await emit(
                    {
                        "type": "secretary_inbound",
                        "chat_id": cid,
                        "title": out.get("title") or meta.get("title"),
                        "from": m.get("sender_name") or out.get("title") or meta.get("title"),
                        "message_id": m["id"],
                        "text": (m["text"] or "")[:500],
                        "reply_to_msg_id": m.get("reply_to_msg_id"),
                    }
                )

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            req = json.loads(line.decode() or "{}")
            op = req.get("op")
            role = req.get("role")
            if op in {"chats", "pull", "send"} and role not in ("reporter", "secretary"):
                resp = {"error": "watch request missing role"}
            elif op == "status":
                ok = await client.is_user_authorized()
                resp = {"authorized": bool(ok), "via": "watch"}
            elif op == "chats":
                if role == "reporter":
                    resp = {
                        "chats": [
                            {"id": int(c["id"]), "title": c.get("title"), "mode": c.get("mode")}
                            for c in (cfg.get("chats") or [])
                            if c.get("mode") == "report"
                        ]
                    }
                else:
                    rows = []
                    for dialog in await client.get_dialogs():
                        entity = dialog.entity
                        rows.append(
                            {
                                "id": entity.id,
                                "title": live_title(entity),
                                "username": getattr(entity, "username", None),
                                "kind": "user" if type(entity).__name__ == "User" else type(entity).__name__,
                            }
                        )
                    resp = {"chats": rows}
            elif op == "pull":
                chat = chat_by_id(cfg, int(req["chat_id"]))
                err = refuse_pull(role, chat)
                if err:
                    resp = {"error": err}
                else:
                    resp = await pull_chat(
                        client,
                        cfg,
                        chat,
                        int(req.get("hours") or 24),
                        int(req.get("limit") or 1000),
                        req.get("timezone") or cfg.get("timezone") or "UTC",
                        bool(req.get("keep_empty")),
                    )
            elif op == "send":
                chat = chat_by_id(cfg, int(req["chat_id"]))
                err = refuse_send(role, chat)
                if err:
                    resp = {"error": err}
                else:
                    entity = await client.get_entity(int(chat["id"]))
                    live_err = refuse_live_title(chat, live_title(entity))
                    if live_err:
                        resp = {"error": live_err}
                    else:
                        msg = await client.send_message(entity, req["text"], reply_to=req.get("reply_to"))
                        resp = {"sent_id": msg.id, "reply_to": req.get("reply_to"), "via": "watch", **echo_chat(chat)}
            else:
                resp = {"error": f"unknown op {op}"}
        except PolicyError as exc:
            resp = {"error": str(exc)}
        except Exception as exc:
            resp = {"error": f"{type(exc).__name__}: {exc}"}
        try:
            writer.write((json.dumps(resp, default=str) + "\n").encode())
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    async def poll_missed() -> None:
        # NewMessage can go deaf while get_messages still works. Re-gap every 30s.
        while True:
            await asyncio.sleep(30)
            try:
                await client.catch_up()
            except Exception as exc:
                print("updates_catch_up", type(exc).__name__, flush=True)
            try:
                await catch_up()
            except Exception as exc:
                print("poll_fail", type(exc).__name__, flush=True)

    if SOCK.exists():
        SOCK.unlink()
    server = await asyncio.start_unix_server(handle, path=str(SOCK))
    SOCK.chmod(0o600)
    PID.write_text(str(os.getpid()))
    print("watching", list(allow), "webhook", bool(url), "sock", str(SOCK), flush=True)
    await catch_up()
    asyncio.create_task(poll_missed())
    async with server:
        await client.run_until_disconnected()


def cmd_restore(cfg: dict, args: argparse.Namespace) -> None:
    """One-shot heal: real WARP on :40000 + exactly one secretary watch.

    Does not open Telethon itself — execs scripts/restore-pipeline.sh with an
    absolute path so agent Auto-review can bind the command. Secretary only.
    """
    role = role_of()
    if role != "secretary":
        die("restore requires TG_HARNESS_ROLE=secretary (got %r)" % (role,))
    script = ROOT / "scripts" / "restore-pipeline.sh"
    if not script.is_file():
        die(f"missing {script}")
    # Absolute bash + absolute script: preferred Auto-review-safe invoke.
    os.chdir(ROOT)
    os.execvp("bash", ["bash", str(script.resolve())])



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tg", description="Thin Telegram I/O for agents")
    p.add_argument("--config", default=str(ROOT / "config.toml"))
    sub = p.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="phone + code + optional 2FA; writes session file")
    login.add_argument("--phone")
    login.add_argument("--code", help="avoid if you can; prefer typing it")
    login.add_argument("--password", help="2FA; prefer a prompt")

    sub.add_parser("status", help="is the session authorized?")
    sub.add_parser("chats", help="list dialogs (ids + titles)")

    pull = sub.add_parser("pull", help="write last N hours to out/<id>.json and .txt")
    pull.add_argument("chat", help="chat id or title from config.toml")
    pull.add_argument("--hours", type=int, default=24)
    pull.add_argument("--limit", type=int, default=1000)
    pull.add_argument("--timezone")
    pull.add_argument("--out")
    pull.add_argument("--keep-empty", action="store_true")

    send = sub.add_parser("send", help="send a reply (secretary role, secretary chats)")
    send.add_argument("chat", help="chat id or title from config.toml")
    send.add_argument("--text")
    send.add_argument("--file")
    send.add_argument("--reply-to", type=int, help="optional quote; omit in 1:1s unless a specific message needs it")
    send.add_argument("--direct", action="store_true", help="send without watch; human escape only")

    watch = sub.add_parser("watch", help="listen for secretary inbound and POST webhook")
    watch.add_argument("--webhook")

    sub.add_parser(
        "restore",
        help="heal WARP :40000 + one secretary watch (execs scripts/restore-pipeline.sh; secretary only)",
    )
    return p


def main() -> None:
    load_dotenv(ROOT / ".env")
    args = build_parser().parse_args()
    cfg = load_config(Path(args.config))
    if args.cmd == "restore":
        cmd_restore(cfg, args)
        return
    handler = {
        "login": cmd_login,
        "status": cmd_status,
        "chats": cmd_chats,
        "pull": cmd_pull,
        "send": cmd_send,
        "watch": cmd_watch,
    }[args.cmd]
    asyncio.run(handler(cfg, args))


if __name__ == "__main__":
    main()
