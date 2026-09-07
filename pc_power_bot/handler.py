"""Private-chat Telegram UI and confirmation-gated power workflow."""

from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime
from typing import Any, Callable

from .config import Config
from .domain import Proposal, ScheduledTask, parse_clock, parse_duration
from .power import PowerController
from .state import StateStore, TaskScheduler
from .telegram import TelegramError

LOGGER = logging.getLogger(__name__)
ButtonRows = list[list[tuple[str, str]]]


class PowerBot:
    def __init__(self, config: Config, api: Any, now: Callable[[], float] = time.time):
        self.config = config
        self.api = api
        self.now = now
        self.pending: dict[int, Proposal] = {}
        self.awaiting_time: dict[int, tuple[str, int | None]] = {}
        controller = PowerController(config.dry_run)
        self.scheduler = TaskScheduler(StateStore(config.state_path), controller.execute, now=now)

    def restore(self) -> ScheduledTask | None:
        return self.scheduler.restore()

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _handle_message(self, message: dict[str, Any]) -> None:
        if not self._authorized_message(message):
            return
        chat_id = message["chat"].get("id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return
        text = message.get("text", "").strip()
        command = text.casefold().split(maxsplit=1)[0] if text else ""
        if command in {"/start", "/menu"}:
            self.awaiting_time.pop(chat_id, None)
            self._send_main(chat_id)
        elif command == "/cancel":
            self._cancel(chat_id)
        elif chat_id in self.awaiting_time and text:
            self._handle_clock_input(chat_id, text)
        elif command == "/timer":
            self._handle_timer_command(chat_id, text)
        elif command == "/at":
            self._handle_clock_command(chat_id, text)
        elif command == "/status":
            self._send_status(chat_id)
        elif command in {"/extra", "/advanced"}:
            self._send_extra(chat_id)

    def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id", ""))
        if callback_id:
            try:
                self.api.answer_callback_query(callback_id)
            except Exception:
                LOGGER.warning("Could not acknowledge callback")
        message = callback.get("message") or {}
        if not self._authorized_callback(callback, message):
            return
        chat_id = message["chat"].get("id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return
        message_id = message.get("message_id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            message_id = None
        data = callback.get("data", "")
        if not isinstance(data, str):
            return
        if data == "menu:main":
            self._send_main(chat_id, message_id)
        elif data == "menu:timer":
            self._send_timer_actions(chat_id, message_id)
        elif data.startswith("timer:action:"):
            self._send_timer_options(chat_id, data.removeprefix("timer:action:"), message_id)
        elif data.startswith("timer:duration:"):
            self._duration_proposal(chat_id, data.removeprefix("timer:duration:"), message_id)
        elif data.startswith("timer:clock:"):
            self._clock_prompt(chat_id, data.removeprefix("timer:clock:"), message_id)
        elif data == "menu:extra":
            self._send_extra(chat_id, message_id)
        elif data.startswith("immediate:"):
            self._immediate_proposal(chat_id, data.removeprefix("immediate:"), message_id)
        elif data == "action:status":
            self._send_status(chat_id, message_id)
        elif data == "action:cancel":
            self._cancel(chat_id, message_id)
        elif data.startswith("confirm:"):
            self._confirm_pending(chat_id, data.removeprefix("confirm:"), message_id)
        elif data.startswith("replace:"):
            self._confirm_pending(chat_id, data.removeprefix("replace:"), message_id, replacement_confirmed=True)
        elif data.startswith("cancel:"):
            self._cancel_pending(chat_id, data.removeprefix("cancel:"), message_id)

    def _handle_timer_command(self, chat_id: int, text: str) -> None:
        parts = text.split()
        if len(parts) == 3 and parts[1].casefold() in {"sleep", "shutdown"}:
            seconds = parse_duration(parts[2])
            if seconds:
                self._show_proposal(chat_id, parts[1].casefold(), self.now() + seconds, "timer")
                return
        self._send_timer_actions(chat_id)

    def _handle_clock_command(self, chat_id: int, text: str) -> None:
        parts = text.split()
        if len(parts) == 3 and parts[1].casefold() in {"sleep", "shutdown"}:
            deadline = parse_clock(parts[2], datetime.fromtimestamp(self.now()).astimezone())
            if deadline:
                self._show_proposal(chat_id, parts[1].casefold(), deadline, "timer")
                return
        self._send_timer_actions(chat_id)

    def _handle_clock_input(self, chat_id: int, text: str) -> None:
        action, message_id = self.awaiting_time.pop(chat_id)
        deadline = parse_clock(text, datetime.fromtimestamp(self.now()).astimezone())
        if deadline is None:
            self._respond(chat_id, "Формат времени: HH:MM. Попробуйте ещё раз.", message_id=message_id)
            self.awaiting_time[chat_id] = (action, message_id)
            return
        self._show_proposal(chat_id, action, deadline, "timer", message_id)

    def _duration_proposal(self, chat_id: int, value: str, message_id: int | None = None) -> None:
        try:
            action, raw_seconds = value.split(":", 1)
            seconds = int(raw_seconds)
        except ValueError:
            return
        if action not in {"sleep", "shutdown"} or not 0 < seconds <= 7 * 24 * 3600:
            return
        self._show_proposal(chat_id, action, self.now() + seconds, "timer", message_id)

    def _clock_prompt(self, chat_id: int, action: str, message_id: int | None = None) -> None:
        if action not in {"sleep", "shutdown"}:
            return
        self.awaiting_time[chat_id] = (action, message_id)
        self._respond(chat_id, "Отправьте время в формате HH:MM по времени этого ПК.", message_id=message_id)

    def _immediate_proposal(self, chat_id: int, action: str, message_id: int | None = None) -> None:
        if action in {"sleep", "shutdown"}:
            self._show_proposal(chat_id, action, 0.0, "immediate", message_id)

    def _show_proposal(
        self, chat_id: int, action: str, deadline: float, reason: str, message_id: int | None = None
    ) -> None:
        proposal = Proposal(action, deadline, chat_id, reason, secrets.token_urlsafe(6))
        self.pending[chat_id] = proposal
        self.awaiting_time.pop(chat_id, None)
        current = self.scheduler.status()
        if current is not None:
            text = f"Уже запланировано: {self._describe(current)}. Заменить на {self._describe(proposal)}?"
            buttons = self._proposal_buttons(proposal, replacement=True)
        else:
            text = f"Подтвердить: {self._describe(proposal)}?"
            buttons = self._proposal_buttons(proposal)
        self._respond(chat_id, text, buttons, message_id)

    def _confirm_pending(
        self, chat_id: int, token: str, message_id: int | None = None, replacement_confirmed: bool = False
    ) -> None:
        proposal = self.pending.get(chat_id)
        if proposal is None or not secrets.compare_digest(proposal.token, token):
            return
        if proposal.reason != "immediate" and proposal.deadline <= self.now():
            self.pending.pop(chat_id, None)
            self._respond(chat_id, "Время подтверждения истекло. Выберите таймер заново.", self._main_buttons(), message_id)
            return
        current = self.scheduler.status()
        if current is not None and not replacement_confirmed:
            self._respond(
                chat_id,
                f"Уже запланировано: {self._describe(current)}. Подтвердите замену.",
                self._proposal_buttons(proposal, replacement=True),
                message_id,
            )
            return
        deadline = self.now() + 15 if proposal.reason == "immediate" else proposal.deadline
        task = ScheduledTask(proposal.action, deadline, proposal.owner_id, proposal.reason)
        try:
            self.scheduler.schedule(task)
        except (OSError, ValueError):
            LOGGER.warning("Could not save power timer")
            self._respond(chat_id, "Не удалось сохранить таймер. Попробуйте ещё раз.", self._main_buttons(), message_id)
            return
        self.pending.pop(chat_id, None)
        text = f"Запланировано: {self._describe(task)}."
        if self.config.dry_run:
            text += " Тестовый режим: сон/выключение не будет выполнено."
        self._respond(chat_id, text, self._main_buttons(), message_id)

    def _cancel_pending(self, chat_id: int, token: str, message_id: int | None = None) -> None:
        proposal = self.pending.get(chat_id)
        if proposal is None or not secrets.compare_digest(proposal.token, token):
            return
        self.pending.pop(chat_id, None)
        self.awaiting_time.pop(chat_id, None)
        self._respond(chat_id, "Отменено.", self._main_buttons(), message_id)

    def _cancel(self, chat_id: int, message_id: int | None = None) -> None:
        self.pending.pop(chat_id, None)
        self.awaiting_time.pop(chat_id, None)
        cancelled = self.scheduler.cancel()
        self._respond(
            chat_id, "Активных задач нет." if cancelled is None else "Таймер отменён.", self._main_buttons(), message_id
        )

    def _send_status(self, chat_id: int, message_id: int | None = None) -> None:
        task = self.scheduler.status()
        text = "Активных задач нет." if task is None else f"Запланировано: {self._describe(task)}."
        self._respond(chat_id, text, self._main_buttons(), message_id)

    def _respond(
        self, chat_id: int, text: str, buttons: ButtonRows | None = None, message_id: int | None = None
    ) -> None:
        if message_id is not None:
            try:
                self.api.edit_message(chat_id, message_id, text, buttons)
                return
            except TelegramError as exc:
                if "message is not modified" in str(exc).casefold():
                    return
                LOGGER.warning("Could not edit control panel: %s", exc)
        self.api.send_message(chat_id, text, buttons)

    def _authorized_message(self, message: dict[str, Any]) -> bool:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        return chat.get("type") == "private" and sender.get("id") in self.config.allowed_user_ids

    def _authorized_callback(self, callback: dict[str, Any], message: dict[str, Any]) -> bool:
        chat = message.get("chat") or {}
        sender = callback.get("from") or {}
        return chat.get("type") == "private" and sender.get("id") in self.config.allowed_user_ids

    @staticmethod
    def _describe(task: ScheduledTask | Proposal) -> str:
        action = "сон" if task.action == "sleep" else "выключение"
        if task.reason == "immediate":
            return f"{action} через 15 секунд после подтверждения"
        when = datetime.fromtimestamp(task.deadline).astimezone().strftime("%d.%m %H:%M:%S")
        return f"{action} в {when}"

    @staticmethod
    def _proposal_buttons(proposal: Proposal, replacement: bool = False) -> ButtonRows:
        confirm = "replace" if replacement else "confirm"
        label = "Заменить" if replacement else "Подтвердить"
        return [[(label, f"{confirm}:{proposal.token}"), ("Отмена", f"cancel:{proposal.token}")]]

    @staticmethod
    def _main_buttons() -> ButtonRows:
        return [[("⏱ Таймер", "menu:timer"), ("Статус", "action:status")], [("Дополнительно", "menu:extra"), ("Отмена", "action:cancel")]]

    def _send_main(self, chat_id: int, message_id: int | None = None) -> None:
        self._respond(chat_id, "Выберите действие.", self._main_buttons(), message_id)

    def _send_timer_actions(self, chat_id: int, message_id: int | None = None) -> None:
        buttons = [[("Сон", "timer:action:sleep"), ("Выключение", "timer:action:shutdown")], [("Назад", "menu:main")]]
        self._respond(chat_id, "Что запланировать?", buttons, message_id)

    def _send_timer_options(self, chat_id: int, action: str, message_id: int | None = None) -> None:
        if action not in {"sleep", "shutdown"}:
            return
        buttons = [
            [("30 минут", f"timer:duration:{action}:1800"), ("1 час", f"timer:duration:{action}:3600")],
            [("2 часа", f"timer:duration:{action}:7200"), ("Время HH:MM", f"timer:clock:{action}")],
            [("Назад", "menu:timer")],
        ]
        self._respond(chat_id, "Выберите задержку или время.", buttons, message_id)

    def _send_extra(self, chat_id: int, message_id: int | None = None) -> None:
        buttons = [[("Сон сейчас", "immediate:sleep"), ("Выключить сейчас", "immediate:shutdown")], [("Назад", "menu:main")]]
        self._respond(chat_id, "Осторожно: действие начнётся через 15 секунд после подтверждения.", buttons, message_id)
