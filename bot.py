"""Process entry point for the local long-polling bot."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from pc_power_bot.config import Config, ConfigError
from pc_power_bot.handler import PowerBot
from pc_power_bot.telegram import TelegramApi, TelegramError

AUTO_UPDATE_INTERVAL_SECONDS = 60 * 60
GIT_TIMEOUT_SECONDS = 30


def _run_git(project_root: Path, arguments: list[str], runner=subprocess.run, capture_output: bool = False):
    return runner(
        ["git", "-C", str(project_root), *arguments],
        check=False,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def _auto_update_repository(project_root: Path, runner=subprocess.run) -> bool:
    """Fast-forward the clean main checkout without prompting or forcing."""
    try:
        branch = _run_git(project_root, ["rev-parse", "--abbrev-ref", "HEAD"], runner, capture_output=True)
        if branch.returncode != 0 or branch.stdout.strip() != "main":
            return False
        for arguments in (
            ["diff", "--quiet"],
            ["diff", "--cached", "--quiet"],
            ["pull", "--ff-only", "--quiet", "origin", "main"],
        ):
            if _run_git(project_root, arguments, runner).returncode != 0:
                return False
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _runtime_source_snapshot(project_root: Path) -> tuple[tuple[str, int], ...] | None:
    sources = [project_root / "bot.py", *sorted((project_root / "pc_power_bot").glob("*.py"))]
    try:
        return tuple((str(source), source.stat().st_mtime_ns) for source in sources)
    except OSError:
        return None


def _restart_if_sources_changed(
    project_root: Path,
    source_snapshot: tuple[tuple[str, int], ...] | None,
    execute=os.execv,
    sleep=time.sleep,
) -> bool:
    current_snapshot = _runtime_source_snapshot(project_root)
    if source_snapshot is None or current_snapshot is None or current_snapshot == source_snapshot:
        return False
    sleep(1)
    if _runtime_source_snapshot(project_root) != current_snapshot:
        return False
    logging.info("Python source update detected; restarting the bot process")
    execute(sys.executable, [sys.executable, *sys.argv])
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Telegram PC power bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration without connecting to Telegram or changing PC power state.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        project_root = Path(__file__).resolve().parent
        config = Config.from_environment(base_dir=project_root)
    except ConfigError as exc:
        logging.error("Invalid bot configuration: %s", exc)
        raise SystemExit(2) from exc

    if args.check:
        mode = "disabled (DRY_RUN=1)" if config.dry_run else "enabled (DRY_RUN=0)"
        logging.info("Configuration check passed; Telegram and power actions were not started. Power actions are %s.", mode)
        return

    api = TelegramApi(config.token)
    bot = PowerBot(config, api)
    bot.restore()
    offset: int | None = None
    source_snapshot = _runtime_source_snapshot(project_root)
    next_update_at = 0.0
    while True:
        try:
            for update in api.get_updates(offset, config.poll_timeout):
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                bot.handle_update(update)
        except TelegramError:
            logging.warning("Telegram polling failed; retrying")
            time.sleep(5)
        except KeyboardInterrupt:
            logging.info("Bot stopped")
            return
        if time.monotonic() >= next_update_at:
            _auto_update_repository(project_root)
            next_update_at = time.monotonic() + AUTO_UPDATE_INTERVAL_SECONDS
        if _restart_if_sources_changed(project_root, source_snapshot):
            return


if __name__ == "__main__":
    main()
