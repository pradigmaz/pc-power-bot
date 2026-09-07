from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import bot as bot_entry

from pc_power_bot.config import Config, ConfigError
from pc_power_bot.domain import ScheduledTask, parse_clock, parse_duration
from pc_power_bot.handler import PowerBot
from pc_power_bot.power import PowerController
from pc_power_bot.state import StateStore, TaskScheduler
from pc_power_bot.telegram import TelegramApi, TelegramError


class FakeApi:
    def __init__(self) -> None:
        self.acks: list[str] = []
        self.messages: list[tuple[int, str, object]] = []
        self.edits: list[tuple[int, int, str, object]] = []
        self.edit_error: Exception | None = None

    def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        self.acks.append(callback_id)

    def send_message(self, chat_id: int, text: str, buttons=None) -> None:
        self.messages.append((chat_id, text, buttons))

    def edit_message(self, chat_id: int, message_id: int, text: str, buttons=None) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append((chat_id, message_id, text, buttons))


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

    def callback(
        self, data: str, user_id: int = user_id, chat_type: str = "private", message_id: int | None = None
    ) -> dict:
        message: dict[str, object] = {"chat": {"id": user_id, "type": chat_type}}
        if message_id is not None:
            message["message_id"] = message_id
        return {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": user_id},
                "message": message,
                "data": data,
            }
        }

    def pending_token(self) -> str:
        return self.bot.pending[self.user_id].token

    def confirm_pending(self) -> None:
        self.bot.handle_update(self.callback(f"confirm:{self.pending_token()}"))

    def test_duration_and_clock_parsing(self) -> None:
        self.assertEqual(parse_duration("30 мин"), 1800)
        self.assertEqual(parse_duration("90 минут"), 5400)
        self.assertEqual(parse_duration("1h"), 3600)
        current = datetime(2026, 8, 29, 22, 0, tzinfo=timezone.utc)
        deadline = parse_clock("23:30", current)
        self.assertEqual(datetime.fromtimestamp(deadline, timezone.utc).hour, 23)
        tomorrow = datetime.fromtimestamp(parse_clock("21:30", current), timezone.utc)
        self.assertEqual((tomorrow.day, tomorrow.hour), (30, 21))
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

    def test_main_menu_names_timer_cancellation(self) -> None:
        self.bot.handle_update(self.message())

        labels = [label for row in self.api.messages[-1][2] for label, _ in row]

        self.assertEqual(
            labels,
            ["⏱ Таймер", "📋 Статус", "ℹ️ Инструкция", "⚙️ Дополнительно", "✖️ Отменить таймер"],
        )

    def test_instruction_is_shown_in_the_existing_control_panel(self) -> None:
        panel_id = 77
        self.bot.handle_update(self.callback("menu:help", message_id=panel_id))

        _, edited_message_id, text, buttons = self.api.edits[-1]

        self.assertEqual(self.api.messages, [])
        self.assertEqual(edited_message_id, panel_id)
        self.assertIn("VPN включён", text)
        self.assertIn("ℹ️ Инструкция", [label for row in buttons for label, _ in row])

    def test_no_power_action_happens_before_confirmation(self) -> None:
        self.bot.handle_update(self.callback("immediate:shutdown"))
        self.assertIsNone(self.bot.scheduler.status())
        self.assertIn(self.user_id, self.bot.pending)

    def test_callbacks_edit_the_existing_control_panel(self) -> None:
        panel_id = 77
        self.bot.handle_update(self.callback("menu:extra", message_id=panel_id))
        self.bot.handle_update(self.callback("immediate:sleep", message_id=panel_id))
        self.bot.handle_update(self.callback(f"confirm:{self.pending_token()}", message_id=panel_id))

        self.assertEqual(self.api.messages, [])
        self.assertEqual([edit[1] for edit in self.api.edits], [panel_id, panel_id, panel_id])
        self.assertIn("Запланировано", self.api.edits[-1][2])

    def test_clock_input_edits_the_existing_control_panel(self) -> None:
        panel_id = 77
        self.bot.handle_update(self.callback("timer:action:sleep", message_id=panel_id))
        self.bot.handle_update(self.callback("timer:clock:sleep", message_id=panel_id))
        self.assertIn("прошедшее время будет завтра", self.api.edits[-1][2])
        self.bot.handle_update(self.message(text="23:30"))

        self.assertEqual(self.api.messages, [])
        self.assertEqual([edit[1] for edit in self.api.edits], [panel_id, panel_id, panel_id])
        self.assertIn("Подтвердить", self.api.edits[-1][2])

    def test_timer_clearly_separates_after_and_exact_time(self) -> None:
        panel_id = 77
        self.bot.handle_update(self.callback("timer:action:sleep", message_id=panel_id))

        _, _, text, buttons = self.api.edits[-1]
        self.assertEqual(text, "Когда перевести ПК в сон?")
        self.assertEqual(
            [label for row in buttons for label, _ in row],
            ["⏱ Через время", "🕒 В точное время", "◀️ Назад"],
        )

    def test_relative_timer_keeps_its_duration_after_confirmation(self) -> None:
        self.bot.handle_update(self.callback("timer:duration:sleep:5400"))
        self.confirm_pending()

        task = self.bot.scheduler.status()
        self.assertIsNotNone(task)
        self.assertEqual(
            self.api.messages[-1][1],
            "Запланировано: сон через 90 минут. Тестовый режим: сон/выключение не будет выполнено.",
        )
        self.bot.handle_update(self.callback("action:status"))
        self.assertIn("сон в ", self.api.messages[-1][1])

    def test_custom_delay_edits_the_control_panel(self) -> None:
        panel_id = 77
        self.bot.handle_update(self.callback("timer:action:shutdown", message_id=panel_id))
        self.bot.handle_update(self.callback("timer:mode:delay:shutdown", message_id=panel_id))
        self.bot.handle_update(self.callback("timer:delay-input:shutdown", message_id=panel_id))
        self.bot.handle_update(self.message(text="скоро"))
        self.assertIn(self.user_id, self.bot.awaiting_duration)
        self.assertIn("Напишите задержку", self.api.edits[-1][2])
        self.bot.handle_update(self.message(text="90 минут"))

        proposal = self.bot.pending[self.user_id]
        self.assertEqual((proposal.action, proposal.deadline), ("shutdown", self.now + 5400))
        self.assertEqual(self.api.edits[-1][2], "Подтвердить: выключение через 90 минут?")
        self.assertEqual(self.api.messages, [])
        self.assertEqual([edit[1] for edit in self.api.edits], [panel_id, panel_id, panel_id, panel_id, panel_id])

    def test_panel_edit_uses_telegram_edit_message_text(self) -> None:
        api = TelegramApi("test-token")
        with patch.object(api, "_call") as call:
            api.edit_message(self.user_id, 77, "Панель", [[("Назад", "menu:main")]])

        call.assert_called_once_with(
            "editMessageText",
            {
                "chat_id": self.user_id,
                "message_id": 77,
                "text": "Панель",
                "reply_markup": {"inline_keyboard": [[{"text": "Назад", "callback_data": "menu:main"}]]},
            },
        )

    def test_unchanged_panel_does_not_create_a_fallback_message(self) -> None:
        self.api.edit_error = TelegramError("Bad Request: message is not modified")
        self.bot.handle_update(self.callback("action:status", message_id=77))

        self.assertEqual(self.api.edits, [])
        self.assertEqual(self.api.messages, [])

    def test_telegram_error_keeps_api_description(self) -> None:
        class FailedResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"ok": false, "description": "Bad Request: message is not modified"}'

        with patch("pc_power_bot.telegram.urllib.request.urlopen", return_value=FailedResponse()):
            with self.assertRaisesRegex(TelegramError, "message is not modified"):
                TelegramApi("test-token")._call("editMessageText", {})

    def test_source_change_restarts_the_current_python_process(self) -> None:
        restarted: list[tuple[str, list[str]]] = []
        snapshot = (("bot.py", 1),)
        updated_snapshot = (("bot.py", 2),)

        with patch.object(bot_entry, "_runtime_source_snapshot", return_value=updated_snapshot):
            with patch.object(bot_entry.sys, "argv", ["bot.py"]):
                restarted_now = bot_entry._restart_if_sources_changed(
                    Path(self.temp.name), snapshot, lambda executable, args: restarted.append((executable, args)), lambda _: None
                )

        self.assertTrue(restarted_now)
        self.assertEqual(restarted, [(bot_entry.sys.executable, [bot_entry.sys.executable, "bot.py"])])

    def test_source_change_waits_for_a_stable_file_set(self) -> None:
        snapshot = (("bot.py", 1),)
        with patch.object(
            bot_entry, "_runtime_source_snapshot", side_effect=[(("bot.py", 2),), (("bot.py", 3),)]
        ):
            restarted_now = bot_entry._restart_if_sources_changed(
                Path(self.temp.name), snapshot, lambda *_: self.fail("restart must wait"), lambda _: None
            )

        self.assertFalse(restarted_now)

    def test_auto_update_uses_main_fast_forward_only(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            stdout = "main\n" if command[-3:] == ["rev-parse", "--abbrev-ref", "HEAD"] else None
            return subprocess.CompletedProcess(command, 0, stdout=stdout)

        self.assertTrue(bot_entry._auto_update_repository(Path(self.temp.name), runner))
        self.assertEqual(
            [command[3:] for command, _ in calls],
            [
                ["rev-parse", "--abbrev-ref", "HEAD"],
                ["diff", "--quiet"],
                ["diff", "--cached", "--quiet"],
                ["pull", "--ff-only", "--quiet", "origin", "main"],
            ],
        )
        self.assertEqual(calls[0][1]["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(calls[-1][1]["stdout"], subprocess.DEVNULL)

    def test_auto_update_skips_a_dirty_checkout(self) -> None:
        calls: list[list[str]] = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[-2:] == ["diff", "--quiet"]:
                return subprocess.CompletedProcess(command, 1)
            return subprocess.CompletedProcess(command, 0, stdout="main\n")

        self.assertFalse(bot_entry._auto_update_repository(Path(self.temp.name), runner))
        self.assertEqual([command[3:] for command in calls], [["rev-parse", "--abbrev-ref", "HEAD"], ["diff", "--quiet"]])

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

    def test_second_user_cannot_replace_an_existing_timer(self) -> None:
        self.bot.handle_update(self.callback("timer:duration:sleep:1800"))
        self.confirm_pending()
        second_user = 2_222_222_222

        self.bot.handle_update(self.callback("timer:duration:shutdown:3600", user_id=second_user))

        task = self.bot.scheduler.status()
        self.assertIsNotNone(task)
        self.assertEqual(task.action, "sleep")
        self.assertNotIn(second_user, self.bot.pending)
        chat_id, text, _ = self.api.messages[-1]
        self.assertEqual(chat_id, second_user)
        self.assertIn("Уже запланировано: сон", text)
        self.assertIn("Новый таймер не создан.", text)

    def test_legacy_replace_callback_cannot_replace_an_existing_timer(self) -> None:
        self.bot.handle_update(self.callback("timer:duration:shutdown:3600"))
        token = self.pending_token()
        existing = ScheduledTask("sleep", self.now + 1800, self.user_id)
        self.bot.scheduler.schedule(existing)

        self.bot.handle_update(self.callback(f"replace:{token}"))

        self.assertEqual(self.bot.scheduler.status(), existing)
        self.assertNotIn(self.user_id, self.bot.pending)
        self.assertIn("Новый таймер не создан.", self.api.messages[-1][1])

    def test_cancel_command_leaves_time_and_delay_input_modes(self) -> None:
        self.bot.handle_update(self.callback("timer:clock:sleep"))
        self.assertIn(self.user_id, self.bot.awaiting_time)
        self.bot.handle_update(self.message(text="/cancel"))
        self.assertNotIn(self.user_id, self.bot.awaiting_time)
        self.bot.handle_update(self.callback("timer:delay-input:sleep"))
        self.assertIn(self.user_id, self.bot.awaiting_duration)
        self.bot.handle_update(self.message(text="/cancel"))
        self.assertNotIn(self.user_id, self.bot.awaiting_duration)

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
