"""Minimal Telegram Bot API client using only the Python standard library."""

from __future__ import annotations

import json
import urllib.request
from typing import Any


class TelegramError(RuntimeError):
    """Raised when the Bot API request fails."""


class TelegramApi:
    def __init__(self, token: str, request_timeout: int = 40):
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._request_timeout = request_timeout

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, timeout + 15)
        return result if isinstance(result, list) else []

    def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        self._call("answerCallbackQuery", payload)

    def send_message(self, chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if buttons:
            payload["reply_markup"] = self._reply_markup(buttons)
        self._call("sendMessage", payload)

    def edit_message(
        self, chat_id: int, message_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": self._reply_markup(buttons),
        }
        self._call("editMessageText", payload)

    @staticmethod
    def _reply_markup(buttons: list[list[tuple[str, str]]] | None) -> dict[str, list[list[dict[str, str]]]]:
        return {
            "inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in buttons or []
            ]
        }

    def _call(self, method: str, payload: dict[str, Any], timeout: int | None = None) -> Any:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self._request_timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TelegramError("Telegram API request failed") from exc
        if not isinstance(decoded, dict) or not decoded.get("ok"):
            description = decoded.get("description") if isinstance(decoded, dict) else None
            detail = f": {description}" if isinstance(description, str) else ""
            raise TelegramError(f"Telegram API returned an error{detail}")
        return decoded.get("result")
