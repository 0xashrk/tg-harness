import unittest

from tg_harness.cli import live_title
from tg_harness.policy import (
    PolicyError,
    chat_by_id,
    event_settings,
    is_configured_chat,
    is_marked_user_id,
    is_private_user_entity,
    make_event_chat,
    refuse_card,
    refuse_event_entity,
    refuse_live_title,
    refuse_pull,
    refuse_send,
    refuse_watch,
    resolve_chat,
    role_of,
)

CFG = {
    "chats": [
        {"id": 111, "title": "Example group", "mode": "report"},
        {"id": 222, "title": "Example friend", "mode": "secretary"},
        {"id": 333, "title": "Example group", "mode": "report"},
    ]
}


class RoleTests(unittest.TestCase):
    def test_env_wins(self):
        self.assertEqual(role_of({"role": "secretary"}, {"TG_HARNESS_ROLE": "reporter"}), "reporter")

    def test_missing_role(self):
        with self.assertRaises(PolicyError):
            role_of({}, {})

    def test_watch_reporter_blocked(self):
        self.assertEqual(refuse_watch("reporter"), "watch requires TG_HARNESS_ROLE=secretary")
        self.assertIsNone(refuse_watch("secretary"))


class ResolveTests(unittest.TestCase):
    def test_by_title(self):
        chat = resolve_chat(CFG, "Example friend")
        self.assertEqual(int(chat["id"]), 222)

    def test_by_id(self):
        chat = resolve_chat(CFG, "111")
        self.assertEqual(chat["title"], "Example group")

    def test_unknown(self):
        with self.assertRaises(PolicyError):
            resolve_chat(CFG, "999")
        with self.assertRaises(PolicyError):
            resolve_chat(CFG, "not-a-chat")

    def test_duplicate_title(self):
        with self.assertRaises(PolicyError):
            resolve_chat(CFG, "Example group")

    def test_no_raw_id_fallback(self):
        with self.assertRaises(PolicyError):
            chat_by_id(CFG, 999)


class PullSendTests(unittest.TestCase):
    def test_reporter_cannot_send(self):
        friend = resolve_chat(CFG, "Example friend")
        self.assertEqual(refuse_send("reporter", friend), "reporter cannot send")

    def test_reporter_cannot_pull_secretary(self):
        friend = resolve_chat(CFG, "Example friend")
        self.assertIn("mode=report", refuse_pull("reporter", friend) or "")

    def test_secretary_cannot_pull_report(self):
        group = chat_by_id(CFG, 111)
        self.assertIn("mode=secretary", refuse_pull("secretary", group) or "")

    def test_secretary_cannot_send_report(self):
        group = chat_by_id(CFG, 111)
        self.assertIn("read-only", refuse_send("secretary", group) or "")

    def test_secretary_can_send_friend(self):
        friend = resolve_chat(CFG, "Example friend")
        self.assertIsNone(refuse_send("secretary", friend))
        self.assertIsNone(refuse_pull("secretary", friend))

    def test_title_mismatch(self):
        friend = resolve_chat(CFG, "Example friend")
        self.assertIsNotNone(refuse_live_title(friend, "Someone else"))
        self.assertIsNone(refuse_live_title(friend, "example friend"))


class LiveNameTests(unittest.TestCase):
    def test_name_not_id(self):
        class User:
            id = 99
            first_name = "Ada"
            last_name = "Lovelace"

        class IdOnly:
            id = 99

        self.assertEqual(live_title(User()), "Ada Lovelace")
        self.assertEqual(live_title(IdOnly()), "")
        self.assertEqual(live_title(None), "")



EVENT_CFG = {
    "event": {"enabled": True, "name": "DemoCon"},
    "chats": [
        {"id": 111, "title": "Example group", "mode": "report"},
        {"id": 222, "title": "Example friend", "mode": "secretary"},
    ],
}


class EventSettingsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(event_settings({}), {"enabled": False, "name": ""})
        self.assertEqual(event_settings({"event": {}}), {"enabled": False, "name": ""})

    def test_enabled_name(self):
        self.assertEqual(
            event_settings({"event": {"enabled": True, "name": " DemoCon "}}),
            {"enabled": True, "name": "DemoCon"},
        )


class EventResolveTests(unittest.TestCase):
    def test_make_event_chat(self):
        chat = make_event_chat(999, title="Ada")
        self.assertEqual(chat["mode"], "secretary")
        self.assertTrue(chat["event"])
        self.assertEqual(int(chat["id"]), 999)

    def test_is_configured(self):
        self.assertTrue(is_configured_chat(CFG, 111))
        self.assertFalse(is_configured_chat(CFG, 999))

    def test_resolve_event_stranger(self):
        chat = resolve_chat(EVENT_CFG, "999")
        self.assertTrue(chat.get("event"))
        self.assertEqual(int(chat["id"]), 999)
        self.assertEqual(chat["mode"], "secretary")
        self.assertEqual(int(chat_by_id(EVENT_CFG, 999)["id"]), 999)

    def test_event_off_still_unknown(self):
        cfg = {"event": {"enabled": False, "name": "x"}, "chats": EVENT_CFG["chats"]}
        with self.assertRaises(PolicyError):
            resolve_chat(cfg, "999")
        with self.assertRaises(PolicyError):
            chat_by_id(cfg, 999)

    def test_report_id_fail_closed_with_event_on(self):
        chat = resolve_chat(EVENT_CFG, "111")
        self.assertEqual(chat["mode"], "report")
        self.assertFalse(chat.get("event"))
        self.assertIn("mode=secretary", refuse_pull("secretary", chat) or "")
        self.assertIn("read-only", refuse_send("secretary", chat) or "")

    def test_configured_secretary_unchanged(self):
        chat = resolve_chat(EVENT_CFG, "222")
        self.assertFalse(chat.get("event"))
        self.assertIsNone(refuse_pull("secretary", chat))
        self.assertIsNone(refuse_send("secretary", chat))

    def test_title_resolve_never_event(self):
        with self.assertRaises(PolicyError):
            resolve_chat(EVENT_CFG, "Stranger Name")


class EventPrivateUserTests(unittest.TestCase):
    def test_reject_negative_group_id(self):
        # Report group id=12345; resolve('-12345') must not become an event secretary chat.
        with self.assertRaises(PolicyError) as cm:
            resolve_chat(EVENT_CFG, "-12345")
        self.assertIn("private user", str(cm.exception))
        with self.assertRaises(PolicyError):
            chat_by_id(EVENT_CFG, -12345)
        with self.assertRaises(PolicyError):
            resolve_chat(EVENT_CFG, "-10012345")

    def test_marked_user_id_helper(self):
        self.assertTrue(is_marked_user_id(999))
        self.assertFalse(is_marked_user_id(0))
        self.assertFalse(is_marked_user_id(-12345))

    def test_refuse_event_entity_requires_user(self):
        chat = make_event_chat(999)

        class User:
            pass

        class Channel:
            pass

        self.assertTrue(is_private_user_entity(User()))
        self.assertFalse(is_private_user_entity(Channel()))
        self.assertIsNone(refuse_event_entity(chat, User()))
        self.assertIn("private users", refuse_event_entity(chat, Channel()) or "")
        self.assertIn("private users", refuse_event_entity(chat, None) or "")
        # Non-event chats are unaffected (title guard covers them elsewhere).
        configured = {"id": 222, "title": "Example friend", "mode": "secretary"}
        self.assertIsNone(refuse_event_entity(configured, Channel()))



class EventRefuseTests(unittest.TestCase):
    def test_live_title_skipped(self):
        chat = make_event_chat(999, title="")
        self.assertIsNone(refuse_live_title(chat, "Anyone"))
        self.assertIsNone(refuse_live_title(chat, None))

    def test_card_blocked(self):
        chat = make_event_chat(999)
        self.assertIn("event", refuse_card("secretary", chat) or "")

    def test_pull_send_allowed(self):
        chat = make_event_chat(999, title="Ada")
        self.assertIsNone(refuse_pull("secretary", chat))
        self.assertIsNone(refuse_send("secretary", chat))
        self.assertIn("mode=report", refuse_pull("reporter", chat) or "")


if __name__ == "__main__":
    unittest.main()
