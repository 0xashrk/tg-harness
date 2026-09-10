"""Fail-closed chat and role rules. No Telegram I/O here."""

from __future__ import annotations

import os

ROLES = ("reporter", "secretary")
MODES = ("report", "secretary")


class PolicyError(Exception):
    pass


def role_of(cfg: dict, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    raw = (env.get("TG_HARNESS_ROLE") or str(cfg.get("role") or "")).strip().lower()
    if raw not in ROLES:
        raise PolicyError("set TG_HARNESS_ROLE to reporter or secretary")
    return raw


def event_settings(cfg: dict) -> dict:
    """Return ``{enabled: bool, name: str}`` from ``[event]`` (or empty)."""
    raw = cfg.get("event") or {}
    if not isinstance(raw, dict):
        raw = {}
    enabled = bool(raw.get("enabled"))
    name = str(raw.get("name") or "").strip()
    return {"enabled": enabled, "name": name}


def make_event_chat(chat_id: int, title: str = "") -> dict:
    """Ephemeral secretary chat for an event-mode stranger DM."""
    return {
        "id": int(chat_id),
        "title": title or "",
        "mode": "secretary",
        "event": True,
    }


def is_configured_chat(cfg: dict, chat_id: int) -> bool:
    want = int(chat_id)
    for chat in cfg.get("chats") or []:
        cid = chat.get("id")
        if cid is not None and int(cid) == want:
            return True
    return False


def _configured_hits(cfg: dict, *, numeric: int | None = None, title_key: str | None = None) -> list[dict]:
    chats = cfg.get("chats") or []
    if numeric is not None:
        return [c for c in chats if c.get("id") is not None and int(c["id"]) == numeric]
    assert title_key is not None
    return [c for c in chats if (c.get("title") or "").casefold() == title_key.casefold()]


def _maybe_event_chat(cfg: dict, numeric: int | None, key: str) -> dict:
    """If event mode is on and ``numeric`` is an unknown *user* id, synthesize a chat.

    Configured rows (including ``mode=report``) never take this path — callers
    only invoke us when there were zero config hits, so report chats stay
    fail-closed via normal resolve + refuse_*.

    Rejects marked non-user ids (groups/channels use negative / ``-100…`` forms).
    Callers must still verify the live Telethon entity is a private ``User``
    before pull/send — positivity alone is not enough.
    """
    if numeric is None:
        raise PolicyError(f"chat {key!r} is not in config.toml")
    settings = event_settings(cfg)
    if not settings["enabled"]:
        raise PolicyError(f"chat {key!r} is not in config.toml")
    if not is_marked_user_id(numeric):
        raise PolicyError(f"event mode only allows private user ids, not {key!r}")
    return make_event_chat(numeric)


def is_marked_user_id(chat_id: int) -> bool:
    """True when the bare integer is not a Telegram group/channel marker.

    User ids are positive. Basic groups and ``-100…`` channel forms are negative.
    """
    return int(chat_id) > 0


def is_private_user_entity(entity) -> bool:
    """True when ``entity`` is a Telethon private user (not Chat/Channel)."""
    return type(entity).__name__ == "User"


def refuse_event_entity(chat: dict, entity) -> str | None:
    """For event chats, require a resolved private user before read/send."""
    if not chat.get("event"):
        return None
    if entity is None or not is_private_user_entity(entity):
        kind = type(entity).__name__ if entity is not None else "None"
        return f"event mode only allows private users, got {kind}"
    return None


def chat_by_id(cfg: dict, chat_id: int) -> dict:
    hits = _configured_hits(cfg, numeric=int(chat_id))
    if hits:
        return _one(hits, str(chat_id))
    return _maybe_event_chat(cfg, int(chat_id), str(chat_id))


def resolve_chat(cfg: dict, raw: str) -> dict:
    key = str(raw or "").strip()
    if not key:
        raise PolicyError("chat is required")
    numeric = _int(key)
    if numeric is not None:
        hits = _configured_hits(cfg, numeric=numeric)
    else:
        hits = _configured_hits(cfg, title_key=key)
    if not hits:
        return _maybe_event_chat(cfg, numeric, key)
    chat = _one(hits, key)
    if numeric is not None and int(chat["id"]) != numeric:
        raise PolicyError("resolved id does not match")
    if numeric is None and (chat.get("title") or "").casefold() != key.casefold():
        raise PolicyError("resolved title does not match")
    return chat


def refuse_pull(role: str, chat: dict) -> str | None:
    mode = chat.get("mode")
    if role == "reporter" and mode != "report":
        return "reporter can only pull mode=report chats"
    if role == "secretary" and mode != "secretary":
        return "secretary can only pull mode=secretary chats"
    if mode not in MODES:
        return f"invalid mode {mode!r}"
    return None


def refuse_send(role: str, chat: dict | None) -> str | None:
    if role == "reporter":
        return "reporter cannot send"
    if role != "secretary":
        return "send requires TG_HARNESS_ROLE=secretary"
    if chat is None:
        return "chat is not in config.toml"
    mode = chat.get("mode")
    if mode == "report":
        return "refusing send: report chat is read-only"
    if mode != "secretary":
        return "send only to mode=secretary chats"
    return None


def refuse_watch(role: str) -> str | None:
    if role != "secretary":
        return "watch requires TG_HARNESS_ROLE=secretary"
    return None


def refuse_card(role: str, chat: dict | None) -> str | None:
    if role == "reporter":
        return "reporter cannot use cards"
    if role != "secretary":
        return "card requires TG_HARNESS_ROLE=secretary"
    if chat is None:
        return "chat is not in config.toml"
    if chat.get("event"):
        return "cards not available for event chats"
    if chat.get("mode") != "secretary":
        return "cards only for mode=secretary chats"
    return None


def refuse_live_title(chat: dict, live_title: str | None) -> str | None:
    if chat.get("event"):
        return None
    expected = (chat.get("title") or "").strip()
    if not expected:
        return "config row missing title"
    got = (live_title or "").strip()
    if expected.casefold() != got.casefold():
        return f"telegram title {got!r} != config title {expected!r}"
    return None


def echo_chat(chat: dict) -> dict:
    out = {"id": int(chat["id"]), "title": chat.get("title"), "mode": chat.get("mode")}
    if chat.get("event"):
        out["event"] = True
    return out


def _int(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _one(hits: list[dict], key: str) -> dict:
    uniq: list[dict] = []
    seen: set[int] = set()
    for chat in hits:
        cid = chat.get("id")
        if cid is None:
            continue
        i = int(cid)
        if i in seen:
            continue
        seen.add(i)
        uniq.append(chat)
    if not uniq:
        raise PolicyError(f"chat {key!r} is not in config.toml")
    if len(uniq) > 1:
        raise PolicyError(f"chat {key!r} matches more than one config row")
    chat = uniq[0]
    mode = chat.get("mode")
    if mode not in MODES:
        raise PolicyError(f"chat {key!r} has invalid mode {mode!r}")
    return chat
