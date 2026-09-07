"""Small JSON state store and one-task in-process scheduler."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from .domain import ALLOWED_ACTIONS, ScheduledTask

LOGGER = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self, now: float | None = None) -> ScheduledTask | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            task = ScheduledTask(
                action=data["action"],
                deadline=float(data["deadline"]),
                owner_id=int(data["owner_id"]),
                reason=data.get("reason", "timer"),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        if task.action not in ALLOWED_ACTIONS or task.deadline <= (now or time.time()):
            self.clear()
            return None
        return task

    def save(self, task: ScheduledTask) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "action": task.action,
            "deadline": task.deadline,
            "owner_id": task.owner_id,
            "reason": task.reason,
        }
        fd, temp_name = tempfile.mkstemp(prefix="power-state-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            LOGGER.warning("Could not clear local task state")


class TaskScheduler:
    """Persist and execute at most one future task."""

    def __init__(self, store: StateStore, execute: Callable[[str], None], now: Callable[[], float] = time.time):
        self.store = store
        self.execute = execute
        self.now = now
        self._lock = threading.RLock()
        self._task: ScheduledTask | None = None
        self._timer: threading.Timer | None = None

    def restore(self) -> ScheduledTask | None:
        with self._lock:
            self._cancel_timer()
            self._task = None
            task = self.store.load(self.now())
            if task is not None:
                self._task = task
                self._arm(task)
            return task

    def schedule(self, task: ScheduledTask) -> None:
        if task.action not in ALLOWED_ACTIONS or task.deadline <= self.now():
            raise ValueError("Task must contain a future allowed action")
        with self._lock:
            self.store.save(task)
            self._cancel_timer()
            self._task = task
            self._arm(task)

    def cancel(self) -> ScheduledTask | None:
        with self._lock:
            previous = self._task
            self._cancel_timer()
            self._task = None
            self.store.clear()
            return previous

    def status(self) -> ScheduledTask | None:
        with self._lock:
            return self._task

    def _arm(self, task: ScheduledTask) -> None:
        # ponytail: execution is intentionally an in-process ceiling; restart recovery
        # clears expired persisted tasks instead of executing stale remote commands.
        delay = max(0.0, task.deadline - self.now())
        self._timer = threading.Timer(delay, self._run, args=(task,))
        self._timer.daemon = True
        self._timer.start()

    def _run(self, expected: ScheduledTask) -> None:
        with self._lock:
            if self._task != expected:
                return
            self._task = None
            self._timer = None
            self.store.clear()
        self.execute(expected.action)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
