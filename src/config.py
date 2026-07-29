"""Конфигурация из .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

# .env лежит в корне проекта (на уровень выше src/)
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _ids(raw: str) -> set[int]:
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or "0")
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", ""))
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
SPECIALIST_NAME = os.getenv("SPECIALIST_NAME", "Дмитрий").strip()

# Строка подключения к Postgres. На Railway переменная приходит как DATABASE_URL.
DATABASE_URL = (os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL") or "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан в .env")
if not ADMIN_CHAT_ID:
    raise RuntimeError("ADMIN_CHAT_ID не задан в .env")
