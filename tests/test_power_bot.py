from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from pc_power_bot.config import Config, ConfigError
from pc_power_bot.domain import ScheduledTask, parse_clock, parse_duration
from pc_power_bot.handler import PowerBot
from pc_power_bot.power import PowerController
from pc_power_bot.state import StateStore, TaskScheduler


class FakeApi:
    def __init__(self) -> None:
        self.acks: list[str] = []
        self.messages: list[tuple[int, str, object]] = []

    def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        self.acks.append(callback_id)

    def send_message(self, chat_id: int, text: str, buttons=None) -> None:
        self.messages.append((chat_id, text, buttons))


class PowerBotTests(unittest.TestCase):
    user_id = 1_111_111_111

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000.0
        self.api = FakeApi()
        self.config = Config(
            "test-token",
            frozenset({self.user_id, 2_222_222_222}),
            Path(self.temp.name) / "state.json",
            True,
        )
        self.bot = PowerBot(self.config, self.api, now=lambda: self.now)

    def tearDown(self) -> None:
        self.bot.scheduler.cancel()
        self.temp.cleanup()

    def message(self, user_id: int = user_id, chat_type: str = "private", text: str = "/start") -> dict:
        return {
            "message": {
                "from": {"id": user_id},
                "chat": {"id": user_id, "type": chat_type},
                "text": text,
            }
        }

    def callback(self, data: str, user_id: int = user_id, chat_type: str = "private") -> dict:
        return {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": user_id},
                "message": {"chat": {"id": user_id, "type": chat_type}},
                "data": data,
            }
        }

    def pending_token(self) -> str:
        return self.bot.pending[self.user_id].token

    def confirm_pending(self) -> None:
        self.bot.handle_update(self.callback(f"confirm:{self.pending_token()}"))

    def test_duration_and_clock_parsing(self) -> None:
        self.assertEqual(parse_duration("30 мин"), 1800)
        self.assertEqual(parse_duration("1h"), 3600)
        current = datetime(2026, 8, 29, 22, 0, tzinfo=timezone.utc)
        deadline = parse_clock("23:30", current)
        self.assertEqual(datetime.fromtimestamp(deadline, timezone.utc).hour, 23)
        self.assertIsNone(parse_duration("0m"))
        self.assertIsNone(parse_duration("8 days"))

    def test_allowlist_requires_exactly_two_distinct_numeric_ids(self) -> None:
        config = Config.from_environment(
            {"BOT_TOKEN": "token", "ALLOWED_USER_IDS": "2147483648,9223372036854775807"},
            Path(self.temp.name),
        )
        self.assertEqual(config.allowed_user_ids, frozenset({2147483648, 9223372036854775807}))
        for invalid in ("1,1", "1,2,2", "first,2"):
            with self.assertRaises(ConfigError):
                Config.from_environment({"BOT_TOKEN": "token", "ALLOWED_USER_IDS": invalid}, Path(self.temp.name))

    def test_group_and_unknown_updates_are_rejected(self) -> None:
        self.bot.handle_update(self.message(chat_type="group"))
        self.bot.handle_update(self.message(user_id=3_333_333_333))
        self.assertEqual(self.api.messages, [])

    def test_callback_is_acknowledged_but_rejected_user_gets_no_message(self) -> None:
        self.bot.handle_update(self.callback("action:status", user_id=3_333_333_333))
        self.assertEqual(self.api.acks, ["callback-1"])
        self.assertEqual(self.api.messages, [])

    def test_no_power_action_happens_before_confirmation(self) -> None:
        self.bot.handle_update(self.callback("immediate:shutdown"))
        self.assertIsNone(self.bot.scheduler.status())
        self.assertIn(self.user_id, self.bot.pending)

    def test_immediate_action_gets_a_fresh_15_second_grace_period(self) -> None:
        self.bot.handle_update(self.callback("immediate:sleep"))
        token = self.pending_token()
        self.now += 60
        self.bot.handle_update(self.callback(f"confirm:{token}"))
        task = self.bot.scheduler.status()
        self.assertIsNotNone(task)
        self.assertEqual(task.action, "sleep")
        self.assertEqual(task.deadline, self.now + 15)
        self.assertEqual(
            self.api.messages[-1][1],
            "Запланировано: сон через 15 секунд после подтверждения. "
            "Тестовый режим: сон/выключение не будет выполнено.",
        )

    def test_stale_confirmation_cannot_confirm_a_new_proposal(self) -> None:
        self.bot.handle_update(self.callback("timer:duration:sleep:1800"))
        stale_token = self.pending_token()
        self.bot.handle_update(self.callback("timer:duration:shutdown:3600"))
        fresh_token = self.pending_token()
        self.bot.handle_update(self.callback(f"confirm:{stale_token}"))
        self.assertIsNone(self.bot.scheduler.status())
        self.bot.handle_update(self.callback(f"confirm:{fresh_token}"))
        self.assertEqual(self.bot.scheduler.status().action, "shutdown")

    def test_expired_timer_proposal_is_not_scheduled(self) -> None:
        self.bot.handle_update(self.callback("timer:duration:sleep:1800"))
        token = self.pending_token()
        self.now += 1800
        self.bot.handle_update(self.callback(f"confirm:{token}"))
        self.assertIsNone(self.bot.scheduler.status())
        self.assertIn("истекло", self.api.messages[-1][1])

    def test_cancellation_removes_an_active_task(self) -> None:
        self.bot.handle_update(self.callback("timer:duration:sleep:1800"))
        self.confirm_pending()
        self.assertIsNotNone(self.bot.scheduler.status())
        self.bot.handle_update(self.callback("action:cancel"))
        self.assertIsNone(self.bot.scheduler.status())

    def test_replacement_requires_the_same_proposal_token(self) -> None:
        self.bot.handle_update(self.callback("timer:duration:sleep:1800"))
        self.confirm_pending()
        self.bot.handle_update(self.callback("timer:duration:shutdown:3600"))
        token = self.pending_token()
        self.bot.handle_update(self.callback(f"confirm:{token}"))
        self.assertEqual(self.bot.scheduler.status().action, "sleep")
        self.bot.handle_update(self.callback(f"replace:{token}"))
        self.assertEqual(self.bot.scheduler.status().action, "shutdown")

    def test_cancel_command_leaves_clock_input_mode(self) -> None:
        self.bot.handle_update(self.callback("timer:clock:sleep"))
        self.assertIn(self.user_id, self.bot.awaiting_time)
        self.bot.handle_update(self.message(text="/cancel"))
        self.assertNotIn(self.user_id, self.bot.awaiting_time)

    def test_dry_run_never_calls_subprocess(self) -> None:
        calls: list[object] = []

        def runner(*args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)

        self.assertFalse(PowerController(dry_run=True, runner=runner).execute("shutdown"))
        self.assertEqual(calls, [])

    def test_live_shutdown_uses_the_fixed_non_forced_command(self) -> None:
        calls: list[tuple[object, dict]] = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0)

        self.assertTrue(PowerController(dry_run=False, runner=runner).execute("shutdown"))
        command, options = calls[0]
        self.assertTrue(command[0].lower().endswith("system32\\shutdown.exe"))
        self.assertEqual(command[1:], ["/s", "/t", "0"])
        self.assertFalse(options["shell"])

    def test_stale_deadline_is_dropped_on_restore(self) -> None:
        path = Path(self.temp.name) / "stale.json"
        store = StateStore(path)
        store.save(ScheduledTask("shutdown", self.now - 1, self.user_id))
        scheduler = TaskScheduler(store, lambda action: self.fail("stale task executed"), now=lambda: self.now)
        self.assertIsNone(scheduler.restore())
        self.assertFalse(path.exists())

    def test_failed_replacement_save_keeps_the_existing_timer(self) -> None:
        first = ScheduledTask("sleep", self.now + 1800, self.user_id)
        self.bot.scheduler.schedule(first)
        with patch.object(self.bot.scheduler.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.bot.scheduler.schedule(ScheduledTask("shutdown", self.now + 3600, self.user_id))
        self.assertEqual(self.bot.scheduler.status(), first)


if __name__ == "__main__":
    unittest.main()
