"""Process entry point for the local long-polling bot."""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from pc_power_bot.config import Config, ConfigError
from pc_power_bot.handler import PowerBot
from pc_power_bot.telegram import TelegramApi, TelegramError


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
        config = Config.from_environment(base_dir=Path(__file__).resolve().parent)
    except ConfigError as exc:
        logging.error("Invalid bot configuration: %s", exc)
        raise SystemExit(2) from exc

    if args.check:
        logging.info("Configuration check passed; Telegram and power actions were not started")
        return

    api = TelegramApi(config.token)
    bot = PowerBot(config, api)
    bot.restore()
    offset: int | None = None
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


if __name__ == "__main__":
    main()
