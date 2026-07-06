import asyncio
import html
import logging
import os
import re
import secrets
import sqlite3
import string
import time
from dataclasses import dataclass
from typing import Any

import aiohttp
from dotenv import load_dotenv


ALLOWED_UPDATES = [
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "callback_query",
    "pre_checkout_query",
]

SPAM_WINDOW_SECONDS = 5
SPAM_MAX_MESSAGES = 5
SPAM_CLEANUP_SECONDS = 60
TEMP_NOTICE_SECONDS = 5
STATUS_NOTICE_SECONDS = 15
TEST_SPAM_MAX_MESSAGES = 30
TEST_SPAM_DELAY_SECONDS = 0.5

STAR_KEY_PRICE = 25
STAR_KEY_PAYLOAD = "star_key_access"
ADMIN_CODEWORD = "Админ"

SWEAR_PATTERNS = [
    r"^бл[яиа](?:д|т|$)",
    r"^су[кч]",
    r"^х[уy][йеёяию]",
    r"^п[иеe]зд",
    r"^(?:[её]|за[её]|по[её]|на[её]|вы[её]|от[её]|до[её]|раз[её])[б6]",
    r"^муд[ао]",
    r"^пид[ао]",
]

SWEAR_WARNING = (
    "🚨 <b>Культурная тревога</b>\n\n"
    "Система заметила мат и мягко просит сбавить обороты.\n"
    "Пиши красиво — так убедительнее 😇"
)

BUSY_REPLY = (
    "⏳ <b>{name} сейчас занят и ответит позже.</b>\n"
    "Можете оставить сообщение — я передам его в очередь ожидания."
)

PUBLIC_INFO_TEXT = (
    "✨ <b>Chat Manager</b>\n\n"
    "Личный помощник для Telegram Business: автоответ <b>«я занят»</b>, "
    "анти-мат, анти-спам, чистка сообщений.\n\n"
    "🔑 Для подключения нужен одноразовый код приглашения."
)


HELP_TEXT = """✨ <b>Chat Manager — инструкция</b>

<b>🏠 Личка с ботом</b>
<code>/settings</code> — главная панель
<code>/id</code> — узнать Telegram ID
<code>/invite</code> — создать код доступа
<code>/users</code> — клиенты, только главный админ
<code>/deluser ID</code> — удалить клиента
<code>/spamtest 10 текст</code> — тест сообщений себе

<b>💬 Business-чат</b>
<code>.status</code> — статус чата
<code>.busy</code> — автоответ «я занят» тут
<code>.skip</code> — исключить чат из общего режима
<code>.mat</code> — анти-мат тут
<code>.mute</code> / <code>.mute 10m</code> — мут навсегда или на время
<code>.unmute</code> — снять мут
<code>.clean 50</code> — удалить последние сообщения
<code>.on</code> / <code>.off</code> — тестовый режим для <code>.spam</code>
<code>.spam 10 текст</code> — до 30 отдельных сообщений только в тестовом чате

<b>🚀 Быстрый старт</b>
1. Открой <code>/settings</code>.
2. Подключи бота в Telegram Business.
3. В нужном чате напиши <code>.status</code>.

<b>Важно</b>: для удаления сообщений нужны права Telegram Business на удаление."""


@dataclass
class Config:
    token: str
    admin_user_id: int | None
    db_path: str
    log_level: str


class Database:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS owners (
                user_id INTEGER PRIMARY KEY,
                display_name TEXT NOT NULL,
                username TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS owner_settings (
                owner_user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (owner_user_id, key)
            );

            CREATE TABLE IF NOT EXISTS invite_codes (
                code TEXT PRIMARY KEY,
                created_by INTEGER,
                used_by INTEGER,
                created_at INTEGER NOT NULL,
                used_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS business_connections (
                business_connection_id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                is_enabled INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS muted_chats (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                mute_until INTEGER,
                PRIMARY KEY (business_connection_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS swear_watch_chats (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (business_connection_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS busy_chats (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (business_connection_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS busy_excluded_chats (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (business_connection_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS test_chats (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (business_connection_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS busy_state (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                skipped_count INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (business_connection_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS managed_chats (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                owner_user_id INTEGER,
                display_name TEXT,
                last_seen_at INTEGER NOT NULL,
                PRIMARY KEY (business_connection_id, chat_id)
            );

            CREATE TABLE IF NOT EXISTS seen_messages (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                from_user_id INTEGER,
                text TEXT,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (business_connection_id, chat_id, message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_seen_messages_chat
            ON seen_messages (business_connection_id, chat_id, message_id DESC);

            CREATE TABLE IF NOT EXISTS spam_events (
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                from_user_id INTEGER,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_spam_events
            ON spam_events (business_connection_id, chat_id, from_user_id, created_at);
            """
        )
        self.migrate_schema()
        self.conn.commit()

    def migrate_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(managed_chats)").fetchall()
        }
        if "owner_user_id" not in columns:
            self.conn.execute("ALTER TABLE managed_chats ADD COLUMN owner_user_id INTEGER")
        muted_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(muted_chats)").fetchall()
        }
        if "mute_until" not in muted_columns:
            self.conn.execute("ALTER TABLE muted_chats ADD COLUMN mute_until INTEGER")

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_owner_setting(self, owner_user_id: int, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO owner_settings(owner_user_id, key, value) VALUES(?, ?, ?) "
            "ON CONFLICT(owner_user_id, key) DO UPDATE SET value = excluded.value",
            (owner_user_id, key, value),
        )
        self.conn.commit()

    def get_owner_setting(self, owner_user_id: int | None, key: str, default: str = "") -> str:
        if owner_user_id is None:
            return self.get_setting(key, default)
        row = self.conn.execute(
            "SELECT value FROM owner_settings WHERE owner_user_id = ? AND key = ?",
            (owner_user_id, key),
        ).fetchone()
        return row["value"] if row else default

    def create_invite_code(self, code: str, created_by: int | None) -> None:
        self.conn.execute(
            "INSERT INTO invite_codes(code, created_by, created_at) VALUES(?, ?, ?)",
            (code, created_by, int(time.time())),
        )
        self.conn.commit()

    def redeem_invite_code(self, code: str, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT used_by FROM invite_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if row is None or row["used_by"] is not None:
            return False
        cursor = self.conn.execute(
            "UPDATE invite_codes SET used_by = ?, used_at = ? WHERE code = ? AND used_by IS NULL",
            (user_id, int(time.time()), code),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def add_owner(self, user_id: int, display_name: str, username: str | None) -> None:
        self.conn.execute(
            "INSERT INTO owners(user_id, display_name, username, created_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET display_name = excluded.display_name, username = excluded.username",
            (user_id, display_name, username, int(time.time())),
        )
        self.conn.commit()

    def get_owner(self, user_id: int | None) -> sqlite3.Row | None:
        if user_id is None:
            return None
        return self.conn.execute("SELECT * FROM owners WHERE user_id = ?", (user_id,)).fetchone()

    def owner_name(self, user_id: int | None, default: str = "Владелец") -> str:
        owner = self.get_owner(user_id)
        return str(owner["display_name"]) if owner else default

    def list_owners(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT
                o.user_id,
                o.display_name,
                o.username,
                o.created_at,
                COUNT(DISTINCT bc.business_connection_id) AS connections_count,
                COUNT(DISTINCT mc.chat_id) AS chats_count
            FROM owners o
            LEFT JOIN business_connections bc
              ON bc.owner_user_id = o.user_id
             AND bc.is_enabled = 1
            LEFT JOIN managed_chats mc
              ON mc.owner_user_id = o.user_id
            GROUP BY o.user_id, o.display_name, o.username, o.created_at
            ORDER BY o.created_at DESC
            """
        ).fetchall()

    def delete_owner(self, user_id: int) -> bool:
        cursor = self.conn.execute("DELETE FROM owners WHERE user_id = ?", (user_id,))
        self.conn.execute("DELETE FROM owner_settings WHERE owner_user_id = ?", (user_id,))
        self.conn.execute(
            "UPDATE business_connections SET is_enabled = 0, updated_at = ? WHERE owner_user_id = ?",
            (int(time.time()), user_id),
        )
        self.conn.execute(
            "UPDATE managed_chats SET owner_user_id = NULL WHERE owner_user_id = ?",
            (user_id,),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def upsert_business_connection(
        self,
        business_connection_id: str,
        owner_user_id: int,
        is_enabled: bool,
    ) -> None:
        self.conn.execute(
            "INSERT INTO business_connections VALUES(?, ?, ?, ?) "
            "ON CONFLICT(business_connection_id) DO UPDATE SET "
            "owner_user_id = excluded.owner_user_id, is_enabled = excluded.is_enabled, updated_at = excluded.updated_at",
            (business_connection_id, owner_user_id, 1 if is_enabled else 0, int(time.time())),
        )
        self.conn.commit()

    def owner_for_business_connection(self, business_connection_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT owner_user_id FROM business_connections WHERE business_connection_id = ? AND is_enabled = 1",
            (business_connection_id,),
        ).fetchone()
        return int(row["owner_user_id"]) if row else None

    def add_mute(self, business_connection_id: str, chat_id: int, mute_until: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO muted_chats(business_connection_id, chat_id, created_at, mute_until) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET "
            "created_at = excluded.created_at, mute_until = excluded.mute_until",
            (business_connection_id, chat_id, int(time.time()), mute_until),
        )
        self.conn.commit()

    def remove_mute(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "DELETE FROM muted_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        )
        self.conn.commit()

    def is_muted(self, business_connection_id: str, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT mute_until FROM muted_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        ).fetchone()
        if row is None:
            return False
        mute_until = row["mute_until"]
        if mute_until is not None and int(mute_until) <= int(time.time()):
            self.remove_mute(business_connection_id, chat_id)
            return False
        return True

    def mute_until(self, business_connection_id: str, chat_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT mute_until FROM muted_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        ).fetchone()
        if row is None:
            return None
        return int(row["mute_until"]) if row["mute_until"] is not None else None

    def add_swear_watch(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO swear_watch_chats VALUES(?, ?, ?)",
            (business_connection_id, chat_id, int(time.time())),
        )
        self.conn.commit()

    def remove_swear_watch(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "DELETE FROM swear_watch_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        )
        self.conn.commit()

    def is_swear_watch_enabled(self, business_connection_id: str, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM swear_watch_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        ).fetchone()
        return row is not None

    def add_busy(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO busy_chats VALUES(?, ?, ?)",
            (business_connection_id, chat_id, int(time.time())),
        )
        self.conn.execute(
            "DELETE FROM busy_state WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        )
        self.conn.commit()

    def busy_started_at(self, business_connection_id: str, chat_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT created_at FROM busy_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        ).fetchone()
        return int(row["created_at"]) if row else None

    def remove_busy(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "DELETE FROM busy_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        )
        self.conn.execute(
            "DELETE FROM busy_state WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        )
        self.conn.commit()

    def is_busy_enabled(self, business_connection_id: str, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM busy_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        ).fetchone()
        return row is not None

    def add_busy_exclusion(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO busy_excluded_chats VALUES(?, ?, ?)",
            (business_connection_id, chat_id, int(time.time())),
        )
        self.conn.execute(
            "DELETE FROM busy_state WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        )
        self.conn.commit()

    def remove_busy_exclusion(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "DELETE FROM busy_excluded_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        )
        self.conn.commit()

    def is_busy_excluded(self, business_connection_id: str, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM busy_excluded_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        ).fetchone()
        return row is not None

    def add_test_chat(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO test_chats VALUES(?, ?, ?)",
            (business_connection_id, chat_id, int(time.time())),
        )
        self.conn.commit()

    def remove_test_chat(self, business_connection_id: str, chat_id: int) -> None:
        self.conn.execute(
            "DELETE FROM test_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        )
        self.conn.commit()

    def is_test_chat(self, business_connection_id: str, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM test_chats WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        ).fetchone()
        return row is not None

    def should_send_busy_reply(self, business_connection_id: str, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT skipped_count FROM busy_state WHERE business_connection_id = ? AND chat_id = ?",
            (business_connection_id, chat_id),
        ).fetchone()
        now = int(time.time())

        if row is None or int(row["skipped_count"]) >= 3:
            self.conn.execute(
                "INSERT INTO busy_state VALUES(?, ?, ?, ?) "
                "ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET "
                "skipped_count = excluded.skipped_count, updated_at = excluded.updated_at",
                (business_connection_id, chat_id, 0, now),
            )
            self.conn.commit()
            return True

        self.conn.execute(
            "UPDATE busy_state SET skipped_count = skipped_count + 1, updated_at = ? "
            "WHERE business_connection_id = ? AND chat_id = ?",
            (now, business_connection_id, chat_id),
        )
        self.conn.commit()
        return False

    def upsert_managed_chat(
        self,
        business_connection_id: str,
        chat_id: int,
        owner_user_id: int | None,
        display_name: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO managed_chats(business_connection_id, chat_id, owner_user_id, display_name, last_seen_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(business_connection_id, chat_id) DO UPDATE SET "
            "owner_user_id = excluded.owner_user_id, "
            "display_name = excluded.display_name, last_seen_at = excluded.last_seen_at",
            (business_connection_id, chat_id, owner_user_id, display_name, int(time.time())),
        )
        self.conn.commit()

    def count_managed_chats(self, owner_user_id: int | None = None) -> int:
        if owner_user_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS count FROM managed_chats").fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS count FROM managed_chats WHERE owner_user_id = ?",
                (owner_user_id,),
            ).fetchone()
        return int(row["count"])

    def count_seen_messages(self, owner_user_id: int | None = None) -> int:
        if owner_user_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS count FROM seen_messages").fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM seen_messages sm
                JOIN managed_chats mc
                  ON mc.business_connection_id = sm.business_connection_id
                 AND mc.chat_id = sm.chat_id
                WHERE mc.owner_user_id = ?
                """,
                (owner_user_id,),
            ).fetchone()
        return int(row["count"])

    def count_muted_chats(self, owner_user_id: int | None = None) -> int:
        if owner_user_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS count FROM muted_chats").fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM muted_chats m
                JOIN managed_chats mc
                  ON mc.business_connection_id = m.business_connection_id
                 AND mc.chat_id = m.chat_id
                WHERE mc.owner_user_id = ?
                """,
                (owner_user_id,),
            ).fetchone()
        return int(row["count"])

    def count_swear_watch_chats(self, owner_user_id: int | None = None) -> int:
        if owner_user_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS count FROM swear_watch_chats").fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM swear_watch_chats s
                JOIN managed_chats mc
                  ON mc.business_connection_id = s.business_connection_id
                 AND mc.chat_id = s.chat_id
                WHERE mc.owner_user_id = ?
                """,
                (owner_user_id,),
            ).fetchone()
        return int(row["count"])

    def count_busy_chats(self, owner_user_id: int | None = None) -> int:
        if owner_user_id is None:
            row = self.conn.execute("SELECT COUNT(*) AS count FROM busy_chats").fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM busy_chats b
                JOIN managed_chats mc
                  ON mc.business_connection_id = b.business_connection_id
                 AND mc.chat_id = b.chat_id
                WHERE mc.owner_user_id = ?
                """,
                (owner_user_id,),
            ).fetchone()
        return int(row["count"])

    def add_seen_message(
        self,
        business_connection_id: str,
        chat_id: int,
        message_id: int,
        from_user_id: int | None,
        text: str | None,
    ) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_messages VALUES(?, ?, ?, ?, ?, ?)",
            (
                business_connection_id,
                chat_id,
                message_id,
                from_user_id,
                text,
                int(time.time()),
            ),
        )
        self.conn.commit()


    def is_spam_message(
        self,
        business_connection_id: str,
        chat_id: int,
        from_user_id: int | None,
    ) -> bool:
        now = time.time()
        window_started_at = now - SPAM_WINDOW_SECONDS

        self.conn.execute(
            "DELETE FROM spam_events WHERE created_at < ?",
            (now - SPAM_CLEANUP_SECONDS,),
        )
        self.conn.execute(
            "INSERT INTO spam_events VALUES(?, ?, ?, ?)",
            (business_connection_id, chat_id, from_user_id, now),
        )

        if from_user_id is None:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count FROM spam_events
                WHERE business_connection_id = ?
                  AND chat_id = ?
                  AND from_user_id IS NULL
                  AND created_at >= ?
                """,
                (business_connection_id, chat_id, window_started_at),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS count FROM spam_events
                WHERE business_connection_id = ?
                  AND chat_id = ?
                  AND from_user_id = ?
                  AND created_at >= ?
                """,
                (business_connection_id, chat_id, from_user_id, window_started_at),
            ).fetchone()

        self.conn.commit()
        return int(row["count"]) > SPAM_MAX_MESSAGES

    def last_message_ids(self, business_connection_id: str, chat_id: int, limit: int) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT message_id FROM seen_messages
            WHERE business_connection_id = ? AND chat_id = ?
            ORDER BY message_id DESC
            LIMIT ?
            """,
            (business_connection_id, chat_id, limit),
        ).fetchall()
        return [int(row["message_id"]) for row in rows]

    def remove_seen_messages(
        self,
        business_connection_id: str,
        chat_id: int,
        message_ids: list[int],
    ) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" for _ in message_ids)
        self.conn.execute(
            f"""
            DELETE FROM seen_messages
            WHERE business_connection_id = ?
              AND chat_id = ?
              AND message_id IN ({placeholders})
            """,
            (business_connection_id, chat_id, *message_ids),
        )
        self.conn.commit()


class TelegramAPI:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "TelegramAPI":
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=70))
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.session:
            await self.session.close()

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        if not self.session:
            raise RuntimeError("TelegramAPI session is not open")
        async with self.session.post(f"{self.base_url}/{method}", json=payload or {}) as resp:
            data = await resp.json(content_type=None)
            if not data.get("ok"):
                raise RuntimeError(f"{method} failed: {data}")
            return data.get("result")

    async def try_call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            return await self.call(method, payload)
        except Exception as exc:
            logging.warning("%s", exc)
            return None

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": 50,
            "allowed_updates": ALLOWED_UPDATES,
        }
        if offset is not None:
            payload["offset"] = offset
        return await self.call("getUpdates", payload)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        business_connection_id: str | None = None,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.try_call("sendMessage", payload)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.try_call("editMessageText", payload)

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return await self.try_call("answerCallbackQuery", payload)

    async def set_my_commands(self) -> Any:
        # Админские команды (/invite, /users, /deluser) сюда намеренно не входят —
        # они скрыты из меню и доступны только главному админу после кодового слова.
        return await self.try_call(
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "Открыть Chat Manager"},
                    {"command": "help", "description": "Короткая инструкция"},
                    {"command": "id", "description": "Узнать свой Telegram ID"},
                    {"command": "settings", "description": "Главная панель"},
                    {"command": "spamtest", "description": "Тест сообщений себе"},
                ]
            },
        )

    async def send_invoice(
        self,
        chat_id: int,
        title: str,
        description: str,
        payload: str,
        currency: str,
        prices: list[dict[str, Any]],
    ) -> Any:
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "title": title,
            "description": description,
            "payload": payload,
            "provider_token": "",
            "currency": currency,
            "prices": prices,
        }
        return await self.try_call("sendInvoice", body)

    async def answer_pre_checkout_query(
        self,
        pre_checkout_query_id: str,
        ok: bool = True,
        error_message: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
        if error_message:
            payload["error_message"] = error_message
        return await self.try_call("answerPreCheckoutQuery", payload)

    async def delete_business_messages(
        self,
        business_connection_id: str,
        message_ids: list[int],
    ) -> None:
        for chunk_start in range(0, len(message_ids), 100):
            chunk = message_ids[chunk_start : chunk_start + 100]
            await self.try_call(
                "deleteBusinessMessages",
                {
                    "business_connection_id": business_connection_id,
                    "message_ids": chunk,
                },
            )


class HelperBot:
    def __init__(self, api: TelegramAPI, db: Database, config: Config) -> None:
        self.api = api
        self.db = db
        self.config = config

    def is_superadmin(self, user_id: int | None) -> bool:
        return self.config.admin_user_id is not None and user_id == self.config.admin_user_id

    def is_admin(self, user_id: int | None) -> bool:
        return self.is_superadmin(user_id) or self.db.get_owner(user_id) is not None

    def is_admin_unlocked(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return self.db.get_setting(f"admin_unlocked:{user_id}") == "1"

    def make_invite_code(self) -> str:
        alphabet = string.ascii_uppercase + string.digits
        return "-".join(
            "".join(secrets.choice(alphabet) for _ in range(4))
            for _ in range(3)
        )

    def busy_reply_text(self, owner_user_id: int | None) -> str:
        owner_name = self.db.owner_name(owner_user_id, "Владелец")
        return f"{owner_name} сейчас занят и ответит позже."

    def parse_duration_seconds(self, value: str) -> int | None:
        match = re.fullmatch(r"(\d+)([smhd])", value.strip().lower())
        if not match:
            return None
        amount = int(match.group(1))
        unit = match.group(2)
        multipliers = {
            "s": 1,
            "m": 60,
            "h": 60 * 60,
            "d": 24 * 60 * 60,
        }
        seconds = amount * multipliers[unit]
        return seconds if 1 <= seconds <= 30 * 24 * 60 * 60 else None

    def format_duration(self, seconds: int) -> str:
        if seconds >= 24 * 60 * 60:
            value = seconds // (24 * 60 * 60)
            return f"{value} д."
        if seconds >= 60 * 60:
            value = seconds // (60 * 60)
            return f"{value} ч."
        if seconds >= 60:
            value = seconds // 60
            return f"{value} мин."
        return f"{seconds} сек."

    def users_text(self) -> str:
        owners = self.db.list_owners()
        if not owners:
            return (
                "<b>Клиенты</b>\n\n"
                "Пока нет зарегистрированных клиентов.\n\n"
                "Чтобы выдать доступ, напиши <code>/invite</code>."
            )

        lines = ["<b>Клиенты</b>\n"]
        for owner in owners:
            username = f"@{owner['username']}" if owner["username"] else "без username"
            lines.append(
                f"<b>{html.escape(str(owner['display_name']))}</b>\n"
                f"ID: <code>{owner['user_id']}</code>\n"
                f"Username: {html.escape(username)}\n"
                f"Business-подключений: <b>{owner['connections_count']}</b>\n"
                f"Чатов видел: <b>{owner['chats_count']}</b>\n"
                f"Удалить: <code>/deluser {owner['user_id']}</code>\n"
            )
        return "\n".join(lines)

    def users_menu_text(self) -> str:
        owners_count = len(self.db.list_owners())
        return (
            "<b>Клиенты</b>\n\n"
            f"Всего клиентов: <b>{owners_count}</b>\n\n"
            "Нажми на клиента, чтобы открыть карточку со статистикой и управлением."
        )

    def users_keyboard(self) -> dict[str, Any]:
        owners = self.db.list_owners()
        keyboard = []
        for owner in owners:
            username = f" @{owner['username']}" if owner["username"] else ""
            keyboard.append(
                [
                    {
                        "text": f"{owner['display_name']}{username}",
                        "callback_data": f"admin:user:{owner['user_id']}",
                    }
                ]
            )
        keyboard.append([{"text": "Создать код доступа", "callback_data": "admin:invite"}])
        keyboard.append([{"text": "Обновить список", "callback_data": "admin:users"}])
        return {"inline_keyboard": keyboard}

    def user_detail_text(self, user_id: int) -> str:
        owner = self.db.get_owner(user_id)
        if owner is None:
            return "<b>Клиент не найден</b>"
        username = f"@{owner['username']}" if owner["username"] else "без username"
        busy_all = self.db.get_owner_setting(user_id, "busy_all", "0") == "1"
        swear_all = self.db.get_owner_setting(user_id, "swear_all", "0") == "1"
        return (
            "<b>Карточка клиента</b>\n\n"
            f"Имя: <b>{html.escape(str(owner['display_name']))}</b>\n"
            f"ID: <code>{owner['user_id']}</code>\n"
            f"Username: {html.escape(username)}\n\n"
            "<b>Статистика</b>\n"
            f"Business-подключений: <b>{self.owner_connections_count(user_id)}</b>\n"
            f"Чатов видел: <b>{self.db.count_managed_chats(user_id)}</b>\n"
            f"Сообщений видел: <b>{self.db.count_seen_messages(user_id)}</b>\n"
            f"Замученных чатов: <b>{self.db.count_muted_chats(user_id)}</b>\n"
            f"Чатов с анти-матом отдельно: <b>{self.db.count_swear_watch_chats(user_id)}</b>\n"
            f"Чатов с локальным режимом занят: <b>{self.db.count_busy_chats(user_id)}</b>\n\n"
            "<b>Глобальные режимы</b>\n"
            f"Анти-мат во всех чатах: <b>{html.escape(self.enabled_label(swear_all))}</b>\n"
            f"Автоответ 'я занят': <b>{html.escape(self.enabled_label(busy_all))}</b>"
        )

    def user_detail_keyboard(self, user_id: int) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Удалить клиента", "callback_data": f"admin:delete:{user_id}"}],
                [{"text": "Назад к списку", "callback_data": "admin:users"}],
            ]
        }

    def delete_confirm_text(self, user_id: int) -> str:
        owner = self.db.get_owner(user_id)
        if owner is None:
            return "<b>Клиент не найден</b>"
        return (
            "<b>Удалить клиента?</b>\n\n"
            f"Клиент: <b>{html.escape(str(owner['display_name']))}</b>\n"
            f"ID: <code>{owner['user_id']}</code>\n\n"
            "После удаления его Business-подключения отключатся, а настройки удалятся."
        )

    def delete_confirm_keyboard(self, user_id: int) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Да, удалить", "callback_data": f"admin:confirm_delete:{user_id}"}],
                [{"text": "Отмена", "callback_data": f"admin:user:{user_id}"}],
            ]
        }

    def owner_connections_count(self, user_id: int) -> int:
        rows = self.db.list_owners()
        for owner in rows:
            if int(owner["user_id"]) == user_id:
                return int(owner["connections_count"])
        return 0

    def enabled_label(self, value: bool) -> str:
        return "вкл" if value else "выкл"

    def contains_swear(self, text: str) -> bool:
        words = re.findall(r"[a-zа-яё0-9*]+", text.lower())
        for word in words:
            normalized = word.replace("x", "х").replace("*", "")
            if any(re.search(pattern, normalized) for pattern in SWEAR_PATTERNS):
                return True
        return False

    def chat_display_name(self, chat: dict[str, Any]) -> str:
        parts = [chat.get("first_name"), chat.get("last_name")]
        name = " ".join(part for part in parts if part)
        return name or chat.get("username") or chat.get("title") or str(chat.get("id"))

    async def send_business_message_and_remember(
        self,
        chat_id: int,
        text: str,
        business_connection_id: str,
        parse_mode: str | None = None,
    ) -> Any:
        sent = await self.api.send_message(
            chat_id,
            text,
            business_connection_id=business_connection_id,
            parse_mode=parse_mode,
        )
        if isinstance(sent, dict) and sent.get("message_id") is not None:
            self.db.add_seen_message(
                business_connection_id=business_connection_id,
                chat_id=int(chat_id),
                message_id=int(sent["message_id"]),
                from_user_id=None,
                text=text[:1000] if text else None,
            )
        return sent

    async def send_test_spam_messages(
        self,
        chat_id: int,
        text: str,
        business_connection_id: str,
        count: int,
    ) -> None:
        for _ in range(count):
            await self.send_business_message_and_remember(chat_id, text, business_connection_id)
            await asyncio.sleep(TEST_SPAM_DELAY_SECONDS)

    async def send_private_spamtest_messages(self, chat_id: int, text: str, count: int) -> None:
        for _ in range(count):
            await self.api.send_message(chat_id, text)
            await asyncio.sleep(0.15)

    async def send_temporary_business_message(
        self,
        chat_id: int,
        text: str,
        business_connection_id: str,
        parse_mode: str | None = None,
        delay_seconds: int = TEMP_NOTICE_SECONDS,
    ) -> None:
        sent = await self.api.send_message(chat_id, text, business_connection_id, parse_mode=parse_mode)
        if not isinstance(sent, dict) or sent.get("message_id") is None:
            return

        asyncio.create_task(
            self.delete_business_message_later(
                business_connection_id,
                int(sent["message_id"]),
                delay_seconds,
            )
        )

    async def delete_business_message_later(
        self,
        business_connection_id: str,
        message_id: int,
        delay_seconds: int,
    ) -> None:
        await asyncio.sleep(delay_seconds)
        await self.api.delete_business_messages(business_connection_id, [message_id])

    def settings_keyboard(self, owner_user_id: int | None) -> dict[str, Any]:
        swear_all = self.db.get_owner_setting(owner_user_id, "swear_all", "0") == "1"
        busy_all = self.db.get_owner_setting(owner_user_id, "busy_all", "0") == "1"
        keyboard = [
            [
                {
                    "text": f"Анти-мат во всех чатах: {self.enabled_label(swear_all)}",
                    "callback_data": "toggle:swear_all",
                }
            ],
            [
                {
                    "text": f"Автоответ 'я занят': {self.enabled_label(busy_all)}",
                    "callback_data": "toggle:busy_all",
                }
            ],
            [
                {"text": "Статистика", "callback_data": "view:stats"},
                {"text": "Что умеет", "callback_data": "view:help"},
            ],
        ]
        if self.is_superadmin(owner_user_id) and self.is_admin_unlocked(owner_user_id):
            keyboard.append([{"text": "Клиенты", "callback_data": "admin:users"}])
        keyboard.append([{"text": "Обновить", "callback_data": "panel:main"}])
        return {"inline_keyboard": keyboard}

    def settings_text(self, owner_user_id: int | None) -> str:
        swear_all = self.db.get_owner_setting(owner_user_id, "swear_all", "0") == "1"
        busy_all = self.db.get_owner_setting(owner_user_id, "busy_all", "0") == "1"
        owner_name = self.db.owner_name(owner_user_id, "Владелец")
        return (
            "✨ <b>Chat Manager</b>\n"
            "Управление личными чатами Telegram Business\n\n"
            f"👤 Профиль: <b>{html.escape(owner_name)}</b>\n\n"
            "<b>🌐 Глобальные режимы</b>\n"
            f"🧼 Анти-мат: <b>{html.escape(self.enabled_label(swear_all))}</b>\n"
            f"⏳ Автоответ «я занят»: <b>{html.escape(self.enabled_label(busy_all))}</b>\n\n"
            "<b>💬 Команды в конкретном чате</b>\n"
            "<code>.status</code> · <code>.busy</code> · <code>.mat</code> · <code>.skip</code> · <code>.clean 50</code>\n\n"
            "<i>Нажимай кнопки ниже — изменения применяются сразу.</i>"
        )

    def stats_text(self, owner_user_id: int | None) -> str:
        owner_name = self.db.owner_name(owner_user_id, "Владелец")
        return (
            "<b>Статистика Chat Manager</b>\n\n"
            f"Профиль: <b>{html.escape(owner_name)}</b>\n\n"
            f"Подключенных чатов видел: <b>{self.db.count_managed_chats(owner_user_id)}</b>\n"
            f"Сообщений видел: <b>{self.db.count_seen_messages(owner_user_id)}</b>\n"
            f"Замученных чатов: <b>{self.db.count_muted_chats(owner_user_id)}</b>\n"
            f"Чатов с анти-матом отдельно: <b>{self.db.count_swear_watch_chats(owner_user_id)}</b>\n"
            f"Чатов с локальным режимом занят: <b>{self.db.count_busy_chats(owner_user_id)}</b>\n\n"
            "Считается только то, что бот успел увидеть после подключения."
        )

    def help_panel_text(self) -> str:
        return (
            "<b>Что умеет Chat Manager</b>\n\n"
            "<b>Личка с ботом</b>\n"
            "<code>/id</code> - узнать свой ID\n"
            "<code>/settings</code> - открыть панель\n\n"
            "<b>Business-чат с человеком</b>\n"
            "<code>.status</code> - красиво показать статус чата\n"
            "<code>.busy</code> - переключить автоответ только тут\n"
            "<code>.skip</code> - не применять общий режим занят к этому чату\n"
            "<code>.mat</code> - переключить анти-мат только тут\n"
            "<code>.mute</code> - мут навсегда\n"
            "<code>.mute 10m</code> - мут на время: s/m/h/d\n"
            "<code>.unmute</code> - снять мут\n"
            "<code>.clean 50</code> - удалить последние 50 сообщений\n"
            "<code>.on</code> - разрешить тестовый спам в этом чате\n"
            "<code>.off</code> - запретить тестовый спам\n"
            "<code>.spam 10 текст</code> - отдельные сообщения только в тестовом чате, максимум 30\n"
            "\n<b>Автоматически</b>\n"
            "Анти-спам удаляет лишние сообщения, если собеседник пишет больше 5 сообщений за 5 секунд."
        )

    async def send_settings_panel(self, chat_id: int, owner_user_id: int | None) -> None:
        await self.api.send_message(
            chat_id,
            self.settings_text(owner_user_id),
            parse_mode="HTML",
            reply_markup=self.settings_keyboard(owner_user_id),
        )

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "message" in update:
            await self.handle_normal_message(update["message"])
        elif "callback_query" in update:
            await self.handle_callback_query(update["callback_query"])
        elif "business_message" in update:
            await self.handle_business_message(update["business_message"])
        elif "edited_business_message" in update:
            await self.handle_business_message(update["edited_business_message"])
        elif "deleted_business_messages" in update:
            await self.handle_deleted_business_messages(update["deleted_business_messages"])
        elif "business_connection" in update:
            await self.handle_business_connection(update["business_connection"])
        elif "pre_checkout_query" in update:
            await self.handle_pre_checkout_query(update["pre_checkout_query"])

    async def handle_pre_checkout_query(self, pre_checkout_query: dict[str, Any]) -> None:
        query_id = pre_checkout_query.get("id")
        if not query_id:
            return
        if pre_checkout_query.get("invoice_payload") != STAR_KEY_PAYLOAD:
            await self.api.answer_pre_checkout_query(query_id, ok=False, error_message="Неизвестный товар.")
            return
        await self.api.answer_pre_checkout_query(query_id, ok=True)

    async def handle_deleted_business_messages(self, deleted: dict[str, Any]) -> None:
        business_connection_id = deleted.get("business_connection_id")
        chat_id = deleted.get("chat", {}).get("id")
        message_ids = [int(message_id) for message_id in deleted.get("message_ids", [])]
        if not business_connection_id or chat_id is None or not message_ids:
            return
        self.db.remove_seen_messages(str(business_connection_id), int(chat_id), message_ids)

    async def handle_business_connection(self, connection: dict[str, Any]) -> None:
        business_connection_id = connection.get("id")
        user = connection.get("user", {})
        owner_user_id = user.get("id")
        is_enabled = bool(connection.get("is_enabled", True))

        if not business_connection_id or owner_user_id is None:
            logging.info("Business connection without owner: %s", connection)
            return

        if self.db.get_owner(int(owner_user_id)) is None and not self.is_superadmin(int(owner_user_id)):
            logging.warning("Business connection ignored: user_id=%s is not registered", owner_user_id)
            return

        if self.is_superadmin(int(owner_user_id)) and self.db.get_owner(int(owner_user_id)) is None:
            display_name = user.get("first_name") or user.get("username") or "Админ"
            self.db.add_owner(int(owner_user_id), display_name, user.get("username"))

        self.db.upsert_business_connection(
            str(business_connection_id),
            int(owner_user_id),
            is_enabled,
        )
        logging.info("Business connection saved: %s owner=%s enabled=%s", business_connection_id, owner_user_id, is_enabled)

    async def handle_normal_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        from_user = message.get("from", {})
        text = message.get("text", "")
        chat_id = chat.get("id")
        user_id = from_user.get("id")
        username = from_user.get("username")
        successful_payment = message.get("successful_payment")

        if successful_payment is not None:
            await self.handle_successful_payment(chat_id, user_id, successful_payment)
            return

        if user_id is not None and self.is_superadmin(user_id) and text.strip().lower() == ADMIN_CODEWORD.lower():
            self.db.set_setting(f"admin_unlocked:{user_id}", "1")
            await self.api.send_message(
                chat_id,
                "<b>Админ-панель разблокирована</b>\n\n"
                "Доступны команды:\n"
                "<code>/invite</code> — создать код доступа\n"
                "<code>/users</code> — список клиентов\n"
                "<code>/deluser ID</code> — удалить клиента",
                parse_mode="HTML",
            )
            return

        if text == "/id":
            await self.api.send_message(chat_id, f"Твой Telegram ID: {user_id}")
        elif text.startswith("/spamtest"):
            if not self.is_admin(user_id):
                await self.api.send_message(chat_id, PUBLIC_INFO_TEXT, parse_mode="HTML")
                return
            parts = text.split(maxsplit=2)
            if len(parts) < 3 or not parts[1].isdigit():
                await self.api.send_message(
                    chat_id,
                    "Формат:\n<code>/spamtest 10 текст</code>\n\n"
                    "Отправляет отдельные сообщения только сюда, в личку с ботом.",
                    parse_mode="HTML",
                )
                return
            count = max(1, min(int(parts[1]), 20))
            test_text = parts[2].strip()
            await self.api.send_message(chat_id, f"Тест запущен: сообщений: {count}")
            asyncio.create_task(self.send_private_spamtest_messages(chat_id, test_text, count))
        elif text == "/users":
            if not self.is_superadmin(user_id) or not self.is_admin_unlocked(user_id):
                await self.api.send_message(chat_id, "Команда недоступна.")
                return
            await self.api.send_message(
                chat_id,
                self.users_menu_text(),
                parse_mode="HTML",
                reply_markup=self.users_keyboard(),
            )
        elif text.startswith("/deluser"):
            if not self.is_superadmin(user_id) or not self.is_admin_unlocked(user_id):
                await self.api.send_message(chat_id, "Команда недоступна.")
                return
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit():
                await self.api.send_message(
                    chat_id,
                    "Напиши так:\n<code>/deluser 123456789</code>\n\n"
                    "ID можно посмотреть командой <code>/users</code>.",
                    parse_mode="HTML",
                )
                return
            target_user_id = int(parts[1])
            if target_user_id == self.config.admin_user_id:
                await self.api.send_message(chat_id, "Главного админа удалить нельзя.")
                return
            owner = self.db.get_owner(target_user_id)
            if owner is None:
                await self.api.send_message(chat_id, "Такого клиента нет в списке.")
                return
            deleted = self.db.delete_owner(target_user_id)
            if deleted:
                await self.api.send_message(
                    chat_id,
                    f"Клиент <b>{html.escape(str(owner['display_name']))}</b> удален.\n\n"
                    "Его Business-подключения отключены, настройки удалены.",
                    parse_mode="HTML",
                )
            else:
                await self.api.send_message(chat_id, "Не получилось удалить клиента.")
        elif text.startswith("/invite"):
            if not self.is_superadmin(user_id) or not self.is_admin_unlocked(user_id):
                await self.api.send_message(chat_id, "Команда недоступна.")
                return
            code = self.make_invite_code()
            self.db.create_invite_code(code, user_id)
            await self.api.send_message(
                chat_id,
                "<b>Код доступа создан</b>\n\n"
                f"<code>{html.escape(code)}</code>\n\n"
                "<b>Что отправить клиенту:</b>\n"
                "1. Откройте этого бота и нажмите /start.\n"
                "2. Отправьте код выше.\n"
                "3. Напишите имя для автоответов.\n"
                "4. Подключите бота в Telegram Business.",
                parse_mode="HTML",
            )
        elif text == "/settings":
            if not self.is_admin(user_id):
                await self.api.send_message(chat_id, PUBLIC_INFO_TEXT, parse_mode="HTML")
                return
            await self.send_settings_panel(chat_id, user_id)
        elif text in {"/start", "/help"}:
            if not self.is_admin(user_id):
                await self.api.send_message(
                    chat_id,
                    "<b>Добро пожаловать в Chat Manager</b>\n\n"
                    "Это помощник для Telegram Business.\n\n"
                    "Выбери, как получить доступ:",
                    parse_mode="HTML",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "🔑 У меня есть код приглашения", "callback_data": "reg:code"}],
                            [{"text": f"⭐ Купить ключ за {STAR_KEY_PRICE}", "callback_data": "reg:buy"}],
                        ]
                    },
                )
                return
            if text == "/start":
                await self.api.send_message(
                    chat_id,
                    "<b>Chat Manager готов</b>\n\n"
                    "Открой главную панель, чтобы включить автоответы или анти-мат.\n\n"
                    "Если бот еще не подключен к Telegram Business, подключи его в настройках Telegram Business.",
                    parse_mode="HTML",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "Открыть панель", "callback_data": "panel:main"}],
                        ]
                    },
                )
            else:
                await self.api.send_message(chat_id, HELP_TEXT, parse_mode="HTML")
        elif user_id is not None and self.db.get_setting(f"register_state:{user_id}") == "awaiting_code":
            code = text.strip().upper()
            if self.db.redeem_invite_code(code, user_id):
                self.db.set_setting(f"register_state:{user_id}", "awaiting_name")
                await self.api.send_message(
                    chat_id,
                    "<b>Код принят</b>\n\n"
                    "Теперь напиши имя, которое будет в автоответах.\n\n"
                    "Например: <code>Артем</code>",
                    parse_mode="HTML",
                )
            else:
                await self.api.send_message(
                    chat_id,
                    "Код не найден или уже использован.\n\n"
                    "Проверь код и отправь его еще раз. Если не получается, попроси новый код.",
                )
        elif user_id is not None and self.db.get_setting(f"register_state:{user_id}") == "awaiting_name":
            display_name = text.strip()[:50]
            if len(display_name) < 2:
                await self.api.send_message(chat_id, "Имя слишком короткое. Напиши нормальное имя, например: Иван.")
                return
            self.db.add_owner(user_id, display_name, username)
            self.db.set_setting(f"register_state:{user_id}", "done")
            await self.api.send_message(
                chat_id,
                "<b>Профиль готов</b>\n\n"
                f"Автоответ будет от имени: <b>{html.escape(display_name)}</b>\n\n"
                "<b>Что дальше:</b>\n"
                "1. Подключи этого бота в Telegram Business.\n"
                "2. Дай права на чтение, ответы и удаление сообщений.\n"
                "3. Открой /settings и включи нужные функции.",
                parse_mode="HTML",
            )
            await self.send_settings_panel(chat_id, user_id)

    async def handle_successful_payment(
        self,
        chat_id: int | None,
        user_id: int | None,
        payment: dict[str, Any],
    ) -> None:
        if chat_id is None or user_id is None:
            return
        if payment.get("invoice_payload") != STAR_KEY_PAYLOAD:
            return
        self.db.set_setting(f"register_state:{user_id}", "awaiting_name")
        await self.api.send_message(
            chat_id,
            "<b>Оплата получена ⭐</b>\n\n"
            "Ключ доступа активирован.\n\n"
            "Теперь напиши имя, которое будет в автоответах.\n\n"
            "Например: <code>Артем</code>",
            parse_mode="HTML",
        )

    async def handle_callback_query(self, query: dict[str, Any]) -> None:
        query_id = query.get("id")
        from_user_id = query.get("from", {}).get("id")
        message = query.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")
        data = query.get("data", "")

        # Сразу убираем "часики" на inline-кнопке.
        # Долгие действия ниже запускаются отдельно, чтобы кнопки не лагали.
        if query_id:
            await self.api.answer_callback_query(query_id)

        if data == "reg:code":
            self.db.set_setting(f"register_state:{from_user_id}", "awaiting_code")
            if chat_id is not None:
                await self.api.send_message(chat_id, "Отправь одноразовый код приглашения.")
            return

        if data == "reg:buy":
            if chat_id is not None:
                await self.api.send_invoice(
                    chat_id,
                    title="Ключ доступа Chat Manager",
                    description="Разовая покупка доступа к боту Chat Manager.",
                    payload=STAR_KEY_PAYLOAD,
                    currency="XTR",
                    prices=[{"label": "Ключ доступа", "amount": STAR_KEY_PRICE}],
                )
            return

        if not self.is_admin(from_user_id):
            if query_id:
                await self.api.answer_callback_query(query_id, "Это кнопки владельца бота.")
            return

        if data.startswith("admin:"):
            if not self.is_superadmin(from_user_id) or not self.is_admin_unlocked(from_user_id):
                if query_id:
                    await self.api.answer_callback_query(query_id, "Это только для главного админа.")
                return

            if data == "admin:users":
                text = self.users_menu_text()
                keyboard = self.users_keyboard()
                notice = ""
            elif data == "admin:invite":
                code = self.make_invite_code()
                self.db.create_invite_code(code, from_user_id)
                text = (
                    "<b>Код доступа создан</b>\n\n"
                    f"<code>{html.escape(code)}</code>\n\n"
                    "<b>Что отправить клиенту:</b>\n"
                    "1. Откройте этого бота и нажмите /start.\n"
                    "2. Отправьте код выше.\n"
                    "3. Напишите имя для автоответов.\n"
                    "4. Подключите бота в Telegram Business."
                )
                keyboard = {"inline_keyboard": [[{"text": "Назад к клиентам", "callback_data": "admin:users"}]]}
                notice = "Код создан"
            elif data.startswith("admin:user:"):
                target_user_id = int(data.rsplit(":", 1)[1])
                text = self.user_detail_text(target_user_id)
                keyboard = self.user_detail_keyboard(target_user_id)
                notice = ""
            elif data.startswith("admin:delete:"):
                target_user_id = int(data.rsplit(":", 1)[1])
                text = self.delete_confirm_text(target_user_id)
                keyboard = self.delete_confirm_keyboard(target_user_id)
                notice = ""
            elif data.startswith("admin:confirm_delete:"):
                target_user_id = int(data.rsplit(":", 1)[1])
                if target_user_id == self.config.admin_user_id:
                    text = self.user_detail_text(target_user_id)
                    keyboard = self.user_detail_keyboard(target_user_id)
                    notice = "Главного админа удалить нельзя"
                else:
                    owner = self.db.get_owner(target_user_id)
                    if owner is None:
                        text = self.users_menu_text()
                        keyboard = self.users_keyboard()
                        notice = "Клиент уже удален"
                    else:
                        self.db.delete_owner(target_user_id)
                        text = (
                            "<b>Клиент удален</b>\n\n"
                            f"Клиент: <b>{html.escape(str(owner['display_name']))}</b>\n"
                            f"ID: <code>{owner['user_id']}</code>\n\n"
                            "Business-подключения отключены, настройки удалены."
                        )
                        keyboard = {"inline_keyboard": [[{"text": "Назад к клиентам", "callback_data": "admin:users"}]]}
                        notice = "Удалено"
            else:
                text = self.users_menu_text()
                keyboard = self.users_keyboard()
                notice = ""
        elif data == "toggle:swear_all":
            current = self.db.get_owner_setting(from_user_id, "swear_all", "0") == "1"
            self.db.set_owner_setting(from_user_id, "swear_all", "0" if current else "1")
            text = self.settings_text(from_user_id)
            keyboard = self.settings_keyboard(from_user_id)
            notice = "Переключено"
        elif data == "toggle:busy_all":
            current = self.db.get_owner_setting(from_user_id, "busy_all", "0") == "1"
            if current:
                self.db.set_owner_setting(from_user_id, "busy_all", "0")
            else:
                now = str(int(time.time()))
                self.db.set_owner_setting(from_user_id, "busy_all", "1")
                self.db.set_owner_setting(from_user_id, "busy_all_started_at", now)
            text = self.settings_text(from_user_id)
            keyboard = self.settings_keyboard(from_user_id)
            notice = "Переключено"
        elif data == "view:stats":
            text = self.stats_text(from_user_id)
            keyboard = {"inline_keyboard": [[{"text": "Назад", "callback_data": "panel:main"}]]}
            notice = ""
        elif data == "view:help":
            text = self.help_panel_text()
            keyboard = {"inline_keyboard": [[{"text": "Назад", "callback_data": "panel:main"}]]}
            notice = ""
        else:
            text = self.settings_text(from_user_id)
            keyboard = self.settings_keyboard(from_user_id)
            notice = ""

        if chat_id is not None and message_id is not None:
            await self.api.edit_message_text(chat_id, message_id, text, "HTML", keyboard)
        # callback_query уже подтверждён в начале обработчика.

    async def handle_business_message(self, message: dict[str, Any]) -> None:
        business_connection_id = message.get("business_connection_id")
        chat = message.get("chat", {})
        from_user = message.get("from", {})
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        from_user_id = from_user.get("id")
        text = message.get("text") or message.get("caption") or ""

        if not business_connection_id or chat_id is None or message_id is None:
            logging.debug("Skipped unsupported business message: %s", message)
            return

        owner_user_id = self.db.owner_for_business_connection(str(business_connection_id))
        if owner_user_id is None:
            if self.is_superadmin(from_user_id):
                owner_user_id = from_user_id
            elif self.config.admin_user_id is not None:
                # На старых установках business_connection мог не сохраниться.
                # Привязываем неизвестное подключение к главному админу.
                owner_user_id = self.config.admin_user_id
            else:
                logging.warning("Skipped business message from unregistered connection=%s", business_connection_id)
                return
            self.db.upsert_business_connection(str(business_connection_id), int(owner_user_id), True)

        self.db.upsert_managed_chat(
            business_connection_id=business_connection_id,
            chat_id=int(chat_id),
            owner_user_id=owner_user_id,
            display_name=self.chat_display_name(chat),
        )

        self.db.add_seen_message(
            business_connection_id=business_connection_id,
            chat_id=int(chat_id),
            message_id=int(message_id),
            from_user_id=from_user_id,
            text=text[:1000] if text else None,
        )

        is_owner_message = from_user_id == owner_user_id
        if text.startswith(".") and is_owner_message:
            await self.handle_business_command(
                business_connection_id=business_connection_id,
                chat_id=int(chat_id),
                message_id=int(message_id),
                from_user_id=from_user_id,
                owner_user_id=owner_user_id,
                text=text.strip(),
            )
            return

        if not is_owner_message and self.db.is_spam_message(
            business_connection_id,
            int(chat_id),
            from_user_id,
        ):
            await self.api.delete_business_messages(business_connection_id, [int(message_id)])
            return

        if not is_owner_message and self.db.is_muted(business_connection_id, int(chat_id)):
            await self.api.delete_business_messages(business_connection_id, [int(message_id)])
            return

        swear_all = self.db.get_owner_setting(owner_user_id, "swear_all", "0") == "1"
        swear_watch = swear_all or self.db.is_swear_watch_enabled(business_connection_id, int(chat_id))
        if not is_owner_message and swear_watch and text and self.contains_swear(text):
            await self.send_business_message_and_remember(chat_id, SWEAR_WARNING, business_connection_id, parse_mode="HTML")

        busy_all = self.db.get_owner_setting(owner_user_id, "busy_all", "0") == "1"
        busy_local = self.db.is_busy_enabled(business_connection_id, int(chat_id))
        busy_excluded = self.db.is_busy_excluded(business_connection_id, int(chat_id))
        busy_enabled = busy_local or (busy_all and not busy_excluded)
        if not is_owner_message and busy_enabled and text:
            if self.db.should_send_busy_reply(business_connection_id, int(chat_id)):
                await self.send_business_message_and_remember(chat_id, self.busy_reply_text(owner_user_id), business_connection_id, parse_mode="HTML")

    async def handle_business_command(
        self,
        business_connection_id: str,
        chat_id: int,
        message_id: int,
        from_user_id: int | None,
        owner_user_id: int | None,
        text: str,
    ) -> None:
        if from_user_id != owner_user_id:
            logging.warning("Ignored business command from non-admin user_id=%s: %s", from_user_id, text)
            return

        parts = text.split()
        command = parts[0].lower()

        if command == ".mute":
            mute_until = None
            if len(parts) > 1:
                duration_seconds = self.parse_duration_seconds(parts[1])
                if duration_seconds is None:
                    await self.api.delete_business_messages(business_connection_id, [message_id])
                    await self.send_temporary_business_message(
                        chat_id,
                        "Формат времени: <code>.mute 10m</code>, <code>.mute 2h</code>, <code>.mute 1d</code>.\n"
                        "Без времени: <code>.mute</code> навсегда.",
                        business_connection_id,
                        parse_mode="HTML",
                    )
                    return
                mute_until = int(time.time()) + duration_seconds
            self.db.add_mute(business_connection_id, chat_id, mute_until)
            await self.api.delete_business_messages(business_connection_id, [message_id])
            if mute_until is None:
                reply = "Ок, этот чат замучен навсегда."
            else:
                reply = f"Ок, этот чат замучен на {self.format_duration(mute_until - int(time.time()))}."
            await self.send_temporary_business_message(chat_id, reply, business_connection_id)
        elif command == ".unmute":
            self.db.remove_mute(business_connection_id, chat_id)
            await self.api.delete_business_messages(business_connection_id, [message_id])
            await self.send_temporary_business_message(chat_id, "Ок, этот чат размучен.", business_connection_id)
        elif command == ".status":
            muted = self.db.is_muted(business_connection_id, chat_id)
            mute_until = self.db.mute_until(business_connection_id, chat_id) if muted else None
            if muted and mute_until is not None:
                mute_label = f"вкл, осталось {self.format_duration(max(1, mute_until - int(time.time())))}"
            else:
                mute_label = self.enabled_label(muted)
            swear_all = self.db.get_owner_setting(owner_user_id, "swear_all", "0") == "1"
            swear_watch = self.db.is_swear_watch_enabled(business_connection_id, chat_id)
            busy_all = self.db.get_owner_setting(owner_user_id, "busy_all", "0") == "1"
            busy = self.db.is_busy_enabled(business_connection_id, chat_id)
            busy_excluded = self.db.is_busy_excluded(business_connection_id, chat_id)
            test_chat = self.db.is_test_chat(business_connection_id, chat_id)
            chat_swear = swear_all or swear_watch
            chat_busy = busy or (busy_all and not busy_excluded)
            owner_name = self.db.owner_name(owner_user_id, "Владелец")
            status = (
                "📌 <b>Статус чата</b>\n\n"
                f"👤 Владелец: <b>{html.escape(owner_name)}</b>\n"
                f"🔇 Мут: <b>{html.escape(mute_label)}</b>\n"
                f"🧼 Анти-мат: <b>{html.escape(self.enabled_label(chat_swear))}</b>\n"
                f"⏳ Я занят: <b>{html.escape(self.enabled_label(chat_busy))}</b>\n"
                f"⭐ Исключение: <b>{html.escape(self.enabled_label(busy_excluded))}</b>\n"
                f"🧪 Тестовый режим: <b>{html.escape(self.enabled_label(test_chat))}</b>\n\n"
                "🌐 <b>Глобально</b>\n"
                f"🧼 Анти-мат везде: <b>{html.escape(self.enabled_label(swear_all))}</b>\n"
                f"⏳ Я занят везде: <b>{html.escape(self.enabled_label(busy_all))}</b>"
            )
            await self.api.delete_business_messages(business_connection_id, [message_id])
            await self.send_temporary_business_message(
                chat_id,
                status,
                business_connection_id,
                parse_mode="HTML",
                delay_seconds=STATUS_NOTICE_SECONDS,
            )
        elif command == ".mat":
            if self.db.is_swear_watch_enabled(business_connection_id, chat_id):
                self.db.remove_swear_watch(business_connection_id, chat_id)
                reply = "Анти-мат выключен для этого чата."
            else:
                self.db.add_swear_watch(business_connection_id, chat_id)
                reply = "Анти-мат включен для этого чата."
            await self.api.delete_business_messages(business_connection_id, [message_id])
            await self.send_temporary_business_message(chat_id, reply, business_connection_id)
        elif command == ".busy":
            if self.db.is_busy_enabled(business_connection_id, chat_id):
                self.db.remove_busy(business_connection_id, chat_id)
                reply = "Режим 'я занят' выключен для этого чата."
            else:
                self.db.add_busy(business_connection_id, chat_id)
                reply = "Режим 'я занят' включен для этого чата."
            await self.api.delete_business_messages(business_connection_id, [message_id])
            await self.send_temporary_business_message(chat_id, reply, business_connection_id)
        elif command == ".skip":
            if self.db.is_busy_excluded(business_connection_id, chat_id):
                self.db.remove_busy_exclusion(business_connection_id, chat_id)
                reply = "Ок, этот чат снова подчиняется общему режиму 'я занят'."
            else:
                self.db.add_busy_exclusion(business_connection_id, chat_id)
                reply = "Ок, этот чат исключен из общего режима 'я занят'. Можно спокойно переписываться."
            await self.api.delete_business_messages(business_connection_id, [message_id])
            await self.send_temporary_business_message(chat_id, reply, business_connection_id)
        elif command == ".on":
            self.db.add_test_chat(business_connection_id, chat_id)
            await self.api.delete_business_messages(business_connection_id, [message_id])
            await self.send_temporary_business_message(
                chat_id,
                "🧪 <b>Тестовый режим включен</b>\nТеперь тут работает <code>.spam 10 текст</code> до 30 сообщений.",
                business_connection_id,
                parse_mode="HTML",
            )
        elif command == ".off":
            self.db.remove_test_chat(business_connection_id, chat_id)
            await self.api.delete_business_messages(business_connection_id, [message_id])
            await self.send_temporary_business_message(
                chat_id,
                "🧪 <b>Тестовый режим выключен</b>\n<code>.spam</code> в этом чате больше не работает.",
                business_connection_id,
                parse_mode="HTML",
            )
        elif command == ".clean":
            limit = 50
            if len(parts) > 1 and parts[1].isdigit():
                limit = max(1, min(int(parts[1]), 500))
            ids = self.db.last_message_ids(business_connection_id, chat_id, limit)
            await self.api.delete_business_messages(business_connection_id, ids)
            self.db.remove_seen_messages(business_connection_id, chat_id, ids)
            await self.send_temporary_business_message(
                chat_id,
                f"Удаляю последние сообщения: {len(ids)}",
                business_connection_id,
            )
        elif command in {".spam", ".testspam"}:
            await self.api.delete_business_messages(business_connection_id, [message_id])
            if not self.db.is_test_chat(business_connection_id, chat_id):
                await self.send_temporary_business_message(
                    chat_id,
                    "🧪 <code>.spam</code> работает только в тестовом чате. Сначала включи <code>.on</code>.",
                    business_connection_id,
                    parse_mode="HTML",
                )
                return
            if len(parts) < 3 or not parts[1].isdigit():
                await self.send_temporary_business_message(
                    chat_id,
                    "Формат: <code>.spam 10 текст</code>\nЛимит: до 30 отдельных сообщений.",
                    business_connection_id,
                    parse_mode="HTML",
                )
                return
            repeat_count = max(1, min(int(parts[1]), TEST_SPAM_MAX_MESSAGES))
            test_text = " ".join(parts[2:]).strip()[:500]
            if not test_text:
                await self.send_temporary_business_message(
                    chat_id,
                    "Текст для <code>.spam</code> пустой.",
                    business_connection_id,
                    parse_mode="HTML",
                )
                return
            await self.send_temporary_business_message(
                chat_id,
                f"🧪 Тест запущен: <code>{repeat_count}</code> сообщений.",
                business_connection_id,
                parse_mode="HTML",
            )
            asyncio.create_task(self.send_test_spam_messages(chat_id, test_text, business_connection_id, repeat_count))
        else:
            await self.send_temporary_business_message(
                chat_id,
                "Не понял команду. Напиши /help боту в личку.",
                business_connection_id,
            )


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip() or "8810713323:AAG2f-SpxRooWTarnGrpfPDx0KwfaoXZrXE"
    if not token:
        raise RuntimeError("BOT_TOKEN is empty. Create .env from .env.example and paste your bot token.")

    raw_admin_id = os.getenv("ADMIN_USER_ID", "").strip() or "1400515994"
    admin_user_id = int(raw_admin_id) if raw_admin_id else None
    return Config(
        token=token,
        admin_user_id=admin_user_id,
        db_path=os.getenv("DB_PATH", "helper_bot.sqlite3"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if config.admin_user_id is None:
        logging.warning("ADMIN_USER_ID is empty. Business commands will be ignored.")

    db = Database(config.db_path)

    async with TelegramAPI(config.token) as api:
        await api.try_call("deleteWebhook", {"drop_pending_updates": False})
        await api.set_my_commands()
        bot = HelperBot(api, db, config)
        offset: int | None = None
        logging.info("Bot started")

        while True:
            try:
                updates = await api.get_updates(offset)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    await bot.handle_update(update)
            except Exception:
                logging.exception("Polling error")
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
