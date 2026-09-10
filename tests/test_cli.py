import argparse
import unittest
from unittest.mock import patch

from pathlib import Path
import tempfile

from tg_harness.cli import cmd_pull, format_event_section, write_event_section
from tg_harness.policy import event_settings, resolve_chat


class PullRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reporter_pull_fails_closed_without_watch(self):
        args = argparse.Namespace(
            chat="Example group",
            hours=24,
            limit=1000,
            timezone=None,
            out=None,
            keep_empty=False,
        )
        chat = {"id": 111, "title": "Example group", "mode": "report"}

        with (
            patch("tg_harness.cli.require_role", return_value="reporter"),
            patch("tg_harness.cli.require_chat", return_value=chat),
            patch("tg_harness.cli.watch_up", return_value=False),
            patch("tg_harness.cli.client_from") as client_from,
        ):
            with self.assertRaises(SystemExit):
                await cmd_pull({}, args)

        client_from.assert_not_called()



class EventCliTests(unittest.TestCase):
    def test_format_event_section(self):
        text = format_event_section(enabled=True, name='Meet "Up"')
        self.assertIn("[event]", text)
        self.assertIn("enabled = true", text)
        self.assertIn("Meet", text)

    def test_write_event_preserves_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'timezone = "UTC"\n\n[[chats]]\nid = 1\ntitle = "Sam"\nmode = "secretary"\n',
                encoding="utf-8",
            )
            write_event_section(path, enabled=True, name="DemoCon")
            raw = path.read_text(encoding="utf-8")
            self.assertIn('timezone = "UTC"', raw)
            self.assertIn("[[chats]]", raw)
            self.assertIn("[event]", raw)
            self.assertIn("enabled = true", raw)
            self.assertIn('name = "DemoCon"', raw)
            # round-trip off keeps chats
            write_event_section(path, enabled=False, name="DemoCon")
            raw2 = path.read_text(encoding="utf-8")
            self.assertIn("[[chats]]", raw2)
            self.assertIn("enabled = false", raw2)
            # tomllib round-trip via resolve when enabled again
            write_event_section(path, enabled=True, name="DemoCon")
            import tomllib
            cfg = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event_settings(cfg)["name"], "DemoCon")
            stranger = resolve_chat(cfg, "424242")
            self.assertTrue(stranger.get("event"))

    def test_write_event_brackets_inside_name(self):
        """Brackets in the event name must not break section matching on toggle."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'timezone = "UTC"\n\n[[chats]]\nid = 1\ntitle = "Sam"\nmode = "secretary"\n',
                encoding="utf-8",
            )
            write_event_section(path, enabled=True, name="Demo [London]")
            raw = path.read_text(encoding="utf-8")
            self.assertEqual(raw.count("[event]"), 1)
            self.assertIn('name = "Demo [London]"', raw)
            write_event_section(path, enabled=False, name="Demo [London]")
            raw2 = path.read_text(encoding="utf-8")
            self.assertEqual(raw2.count("[event]"), 1)
            self.assertIn("enabled = false", raw2)
            import tomllib
            cfg = tomllib.loads(raw2)
            self.assertFalse(event_settings(cfg)["enabled"])
            self.assertEqual(event_settings(cfg)["name"], "Demo [London]")

    def test_write_event_backslash_round_trip(self):
        """Callable re.sub must not double-interpret backslashes in the name."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                'timezone = "UTC"\n\n[event]\nenabled = false\nname = ""\n\n'
                '[[chats]]\nid = 1\ntitle = "Sam"\nmode = "secretary"\n',
                encoding="utf-8",
            )
            write_event_section(path, enabled=True, name="Demo\\London")
            raw = path.read_text(encoding="utf-8")
            self.assertIn('name = "Demo\\\\London"', raw)
            import tomllib
            cfg = tomllib.loads(raw)
            self.assertEqual(event_settings(cfg)["name"], "Demo\\London")
            # Toggle off then on again — must still round-trip
            write_event_section(path, enabled=False, name="Demo\\London")
            write_event_section(path, enabled=True, name="Demo\\London")
            cfg2 = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event_settings(cfg2)["name"], "Demo\\London")
            # Literal backslash-n must stay two chars, not become a newline
            write_event_section(path, enabled=True, name="line\\nbreak")
            cfg3 = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(event_settings(cfg3)["name"], "line\\nbreak")


class EventEntityGuardTests(unittest.IsolatedAsyncioTestCase):
    """Direct + watch-style paths must refuse non-user entities for event chats."""

    async def test_direct_pull_rejects_channel_entity(self):
        from tg_harness.cli import pull_chat

        class Channel:
            id = 999
            title = "Report Group"

        class FakeClient:
            async def get_entity(self, cid):
                return Channel()

        chat = {"id": 999, "title": "", "mode": "secretary", "event": True}
        with patch("tg_harness.cli.die", side_effect=SystemExit("blocked")) as die_mock:
            with self.assertRaises(SystemExit):
                await pull_chat(FakeClient(), {}, chat, 24, 10, "UTC", False)
        die_mock.assert_called()
        self.assertIn("private users", die_mock.call_args[0][0])

    async def test_direct_send_rejects_channel_entity(self):
        from tg_harness.cli import guard_entity

        class Channel:
            id = 999
            title = "Report Group"

        chat = {"id": 999, "title": "", "mode": "secretary", "event": True}
        with patch("tg_harness.cli.die", side_effect=SystemExit("blocked")) as die_mock:
            with self.assertRaises(SystemExit):
                guard_entity(chat, Channel())
        self.assertIn("private users", die_mock.call_args[0][0])

    async def test_direct_allows_user_entity(self):
        from tg_harness.cli import guard_entity

        class User:
            id = 999
            first_name = "Ada"
            last_name = None

        chat = {"id": 999, "title": "", "mode": "secretary", "event": True}
        guard_entity(chat, User())  # must not raise

    def test_watch_send_refuse_event_entity(self):
        # Mirrors the watch socket send path checks (both direct and watch).
        from tg_harness.policy import make_event_chat, refuse_event_entity

        class Channel:
            pass

        class User:
            pass

        chat = make_event_chat(999)
        self.assertIsNotNone(refuse_event_entity(chat, Channel()))
        self.assertIsNone(refuse_event_entity(chat, User()))


if __name__ == "__main__":
    unittest.main()
