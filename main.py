"""Запуск бота: python main.py (настройки в файле .env)."""
import asyncio
import logging
import os
from pathlib import Path

from bot.telegram_bot import App

ROOT = Path(__file__).resolve().parent


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def main():
    load_env(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("Укажите BOT_TOKEN в файле .env (токен от @BotFather)")
    os.chdir(ROOT)
    app = App(token=token,
              source=os.environ.get("SCENARIO", "scenario.xlsx"),
              db_path=os.environ.get("DB_PATH", "data/bot.db"),
              lead_chat=os.environ.get("LEAD_CHAT_ID", ""),
              proxy=os.environ.get("PROXY") or os.environ.get("HTTPS_PROXY", ""))
    asyncio.run(app.run())


if __name__ == "__main__":
    main()
