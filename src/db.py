"""Слой доступа к Postgres (psycopg3 + пул соединений)."""
from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import DATABASE_URL

# Этапы воронки
STAGE_STARTED = "started"      # нажал /start, получил приветствие
STAGE_ANSWERED = "answered"    # ответил (имя + город)
STAGE_CONTACTED = "contacted"  # админ взял в работу / ответил
STAGE_CLOSED = "closed"        # закрыт

STAGE_RU = {
    STAGE_STARTED: "Начал диалог",
    STAGE_ANSWERED: "Оставил заявку",
    STAGE_CONTACTED: "В работе",
    STAGE_CLOSED: "Закрыт",
}

# Пул создаётся лениво (чтобы модуль импортировался без живой БД — например, для smoke-тестов)
_pool: ConnectionPool | None = None


def _pg() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL не задан в .env")
        _pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=5, kwargs={"row_factory": dict_row})
    return _pool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _exec(sql: str, params: tuple = ()) -> None:
    with _pg().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)


def _one(sql: str, params: tuple = ()):
    with _pg().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _all(sql: str, params: tuple = ()):
    with _pg().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def init() -> None:
    ddl = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id      BIGINT PRIMARY KEY,
            username     TEXT,
            first_name   TEXT,
            last_name    TEXT,
            contact_name TEXT,
            city         TEXT,
            phone        TEXT,
            raw_answer   TEXT,
            stage        TEXT DEFAULT 'started',
            assigned_to  TEXT,
            created_at   TEXT,
            updated_at   TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS lead_messages (
            group_message_id BIGINT PRIMARY KEY,
            user_id          BIGINT
        )""",
        """CREATE TABLE IF NOT EXISTS admins (
            user_id  BIGINT PRIMARY KEY,
            username TEXT,
            added_at TEXT
        )""",
    ]
    for stmt in ddl:
        _exec(stmt)


# ---------- users ----------

def upsert_start(user_id: int, username, first_name, last_name) -> bool:
    """Создаёт/обновляет пользователя. Возвращает True, если это новый пользователь."""
    is_new = _one("SELECT 1 AS e FROM users WHERE user_id=%s", (user_id,)) is None
    _exec(
        """INSERT INTO users (user_id, username, first_name, last_name, stage, created_at, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (user_id) DO UPDATE SET
               username   = EXCLUDED.username,
               first_name = EXCLUDED.first_name,
               last_name  = EXCLUDED.last_name,
               updated_at = EXCLUDED.updated_at""",
        (user_id, username, first_name, last_name, STAGE_STARTED, _now(), _now()),
    )
    return is_new


def save_answer(user_id: int, raw_answer: str, contact_name, city) -> None:
    _exec(
        """UPDATE users SET raw_answer=%s, contact_name=%s, city=%s, stage=%s, updated_at=%s
           WHERE user_id=%s""",
        (raw_answer, contact_name, city, STAGE_ANSWERED, _now(), user_id),
    )


def save_phone(user_id: int, phone: str) -> None:
    _exec("UPDATE users SET phone=%s, updated_at=%s WHERE user_id=%s", (phone, _now(), user_id))


def set_stage(user_id: int, stage: str, assigned_to=None) -> None:
    if assigned_to is not None:
        _exec(
            "UPDATE users SET stage=%s, assigned_to=%s, updated_at=%s WHERE user_id=%s",
            (stage, assigned_to, _now(), user_id),
        )
    else:
        _exec("UPDATE users SET stage=%s, updated_at=%s WHERE user_id=%s", (stage, _now(), user_id))


def get_user(user_id: int):
    return _one("SELECT * FROM users WHERE user_id=%s", (user_id,))


def all_users():
    return _all("SELECT * FROM users ORDER BY created_at DESC")


def broadcast_targets():
    """Все, кто хоть раз писал — для рассылки."""
    return [r["user_id"] for r in _all("SELECT user_id FROM users")]


def wipe() -> int:
    """Полная очистка базы клиентов (лиды + связки сообщений). Админы сохраняются."""
    n = _one("SELECT count(*) AS n FROM users")["n"]
    _exec("TRUNCATE users, lead_messages")
    return n


# ---------- связка сообщений группы с лидом (для reply) ----------

def link_message(group_message_id: int, user_id: int) -> None:
    _exec(
        """INSERT INTO lead_messages (group_message_id, user_id) VALUES (%s,%s)
           ON CONFLICT (group_message_id) DO UPDATE SET user_id = EXCLUDED.user_id""",
        (group_message_id, user_id),
    )


def user_by_message(group_message_id: int):
    row = _one("SELECT user_id FROM lead_messages WHERE group_message_id=%s", (group_message_id,))
    return row["user_id"] if row else None


# ---------- admins ----------

def add_admin(user_id: int, username) -> None:
    _exec(
        """INSERT INTO admins (user_id, username, added_at) VALUES (%s,%s,%s)
           ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username""",
        (user_id, username, _now()),
    )


def db_admin_ids() -> set[int]:
    return {r["user_id"] for r in _all("SELECT user_id FROM admins")}
