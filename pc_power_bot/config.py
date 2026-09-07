"""Environment-backed configuration with fail-closed safety defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when a required or safety-sensitive setting is invalid."""


@dataclass(frozen=True)
class Config:
    token: str
    allowed_user_ids: frozenset[int]
    state_path: Path
    dry_run: bool
    poll_timeout: int = 25

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None, base_dir: Path | None = None) -> "Config":
        values = dict(os.environ if env is None else env)
        dotenv = (base_dir or Path.cwd()) / ".env"
        if dotenv.is_file():
            for key, value in _read_dotenv(dotenv).items():
                values.setdefault(key, value)

        token = values.get("BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("BOT_TOKEN is required")

        raw_ids = values.get("ALLOWED_USER_IDS", "")
        ids = _parse_user_ids(raw_ids)
        if len(ids) != 2:
            raise ConfigError("ALLOWED_USER_IDS must contain exactly two unique numeric IDs")

        dry_value = values.get("DRY_RUN", "1").strip()
        if dry_value not in {"0", "1"}:
            raise ConfigError("DRY_RUN must be 0 or 1")

        try:
            poll_timeout = int(values.get("POLL_TIMEOUT", "25"))
        except ValueError as exc:
            raise ConfigError("POLL_TIMEOUT must be an integer") from exc
        if not 1 <= poll_timeout <= 50:
            raise ConfigError("POLL_TIMEOUT must be between 1 and 50")

        state_raw = values.get("STATE_PATH", "state.json").strip() or "state.json"
        state_path = Path(state_raw)
        if not state_path.is_absolute():
            state_path = (base_dir or Path.cwd()) / state_path
        return cls(token, frozenset(ids), state_path, dry_value == "1", poll_timeout)


def _parse_user_ids(raw: str) -> set[int]:
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    if len(parts) != 2 or len(set(parts)) != 2:
        raise ConfigError("ALLOWED_USER_IDS must contain exactly two unique numeric IDs")
    if any(not re.fullmatch(r"[0-9]+", item) for item in parts):
        raise ConfigError("Telegram user IDs must be decimal numbers")
    ids = {int(item) for item in parts}
    if any(item <= 0 for item in ids):
        raise ConfigError("Telegram user IDs must be positive")
    return ids


def _read_dotenv(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result
