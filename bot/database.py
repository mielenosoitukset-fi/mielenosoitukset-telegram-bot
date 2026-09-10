from __future__ import annotations

import aiosqlite
from pathlib import Path

DB_PATH: Path | None = None


async def init_db(db_path: str) -> None:
    global DB_PATH
    DB_PATH = Path(db_path)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                chat_title  TEXT,
                sub_type    TEXT NOT NULL CHECK(sub_type IN ('city', 'org', 'chain')),
                sub_key     TEXT NOT NULL,
                sub_label   TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now')),
                UNIQUE(chat_id, sub_type, sub_key)
            );
            CREATE TABLE IF NOT EXISTS seen_demos (
                chat_id    INTEGER NOT NULL,
                demo_id    TEXT NOT NULL,
                sent_at    TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (chat_id, demo_id)
            );
            CREATE TABLE IF NOT EXISTS admins (
                chat_id INTEGER PRIMARY KEY,
                added_at TEXT DEFAULT (datetime('now'))
            );
            """
        )

        # Migration: group_links -> entity_links
        cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='group_links'")
        if await cursor.fetchone():
            await db.execute("ALTER TABLE group_links RENAME TO entity_links")

        # Create entity_links if not exists (fresh install)
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS entity_links (
                user_chat_id   INTEGER NOT NULL,
                entity_chat_id INTEGER NOT NULL,
                entity_type    TEXT DEFAULT 'group',
                created_at     TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (user_chat_id, entity_chat_id)
            );
            """
        )

        # Bring an old-schema entity_links table up to the current columns.
        cols = [row[1] for row in await (await db.execute("PRAGMA table_info(entity_links)")).fetchall()]
        if "entity_type" not in cols:
            await db.execute("ALTER TABLE entity_links ADD COLUMN entity_type TEXT DEFAULT 'group'")
        if "group_chat_id" in cols and "entity_chat_id" not in cols:
            await db.execute("ALTER TABLE entity_links RENAME COLUMN group_chat_id TO entity_chat_id")

        await db.commit()


# ── context manager ────────────────────────────────────────────────

class _DB:
    async def __aenter__(self):
        self._db = await aiosqlite.connect(DB_PATH)
        return self._db

    async def __aexit__(self, *args):
        await self._db.close()


def _db():
    return _DB()


# ── subscriptions ──────────────────────────────────────────────────

async def add_subscription(chat_id: int, chat_title: str, sub_type: str, sub_key: str, sub_label: str) -> bool:
    """Add a subscription. Returns True if added, False if already exists (toggled off)."""
    async with _db() as db:
        existing = await db.execute_fetchall(
            "SELECT id FROM subscriptions WHERE chat_id = ? AND sub_type = ? AND sub_key = ?",
            (chat_id, sub_type, sub_key),
        )
        if existing:
            await db.execute(
                "DELETE FROM subscriptions WHERE chat_id = ? AND sub_type = ? AND sub_key = ?",
                (chat_id, sub_type, sub_key),
            )
            await db.commit()
            return False

        await db.execute(
            "INSERT INTO subscriptions (chat_id, chat_title, sub_type, sub_key, sub_label) VALUES (?, ?, ?, ?, ?)",
            (chat_id, chat_title, sub_type, sub_key, sub_label),
        )
        await db.commit()
        return True


async def remove_subscription(chat_id: int, sub_type: str, sub_key: str) -> None:
    async with _db() as db:
        await db.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND sub_type = ? AND sub_key = ?",
            (chat_id, sub_type, sub_key),
        )
        await db.commit()


async def get_subscriptions(chat_id: int, sub_type: str | None = None) -> list[dict]:
    async with _db() as db:
        if sub_type:
            rows = await db.execute_fetchall(
                "SELECT sub_type, sub_key, sub_label, chat_title FROM subscriptions WHERE chat_id = ? AND sub_type = ?",
                (chat_id, sub_type),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT sub_type, sub_key, sub_label, chat_title FROM subscriptions WHERE chat_id = ?",
                (chat_id,),
            )
        return [{"sub_type": r[0], "sub_key": r[1], "sub_label": r[2], "chat_title": r[3]} for r in rows]


async def get_all_subscriptions() -> list[dict]:
    async with _db() as db:
        rows = await db.execute_fetchall(
            "SELECT chat_id, sub_type, sub_key, sub_label FROM subscriptions"
        )
        return [{"chat_id": r[0], "sub_type": r[1], "sub_key": r[2], "sub_label": r[3]} for r in rows]


# ── seen demos ─────────────────────────────────────────────────────

async def is_seen(chat_id: int, demo_id: str) -> bool:
    async with _db() as db:
        rows = await db.execute_fetchall(
            "SELECT 1 FROM seen_demos WHERE chat_id = ? AND demo_id = ?",
            (chat_id, demo_id),
        )
        return len(rows) > 0


async def mark_seen(chat_id: int, demo_id: str) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_demos (chat_id, demo_id) VALUES (?, ?)",
            (chat_id, demo_id),
        )
        await db.commit()


async def prune_seen(days: int = 30) -> None:
    async with _db() as db:
        await db.execute(
            "DELETE FROM seen_demos WHERE sent_at < datetime(?, '-' || ? || ' days')",
            (days,),
        )
        await db.commit()


# ── admins ─────────────────────────────────────────────────────────

async def add_admin(chat_id: int) -> None:
    async with _db() as db:
        await db.execute("INSERT OR IGNORE INTO admins (chat_id) VALUES (?)", (chat_id,))
        await db.commit()


async def get_admins() -> list[int]:
    async with _db() as db:
        rows = await db.execute_fetchall("SELECT chat_id FROM admins")
        return [r[0] for r in rows]


async def is_bootstrapped() -> bool:
    async with _db() as db:
        rows = await db.execute_fetchall("SELECT 1 FROM admins LIMIT 1")
        return len(rows) > 0


# ── entity links (DM <-> group/channel pairing) ───────────────────

async def link_entity(user_chat_id: int, entity_chat_id: int, entity_type: str, entity_title: str) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO entity_links (user_chat_id, entity_chat_id, entity_type) VALUES (?, ?, ?)",
            (user_chat_id, entity_chat_id, entity_type),
        )
        await db.execute(
            "UPDATE subscriptions SET chat_title = ? WHERE chat_id = ?",
            (entity_title, entity_chat_id),
        )
        await db.commit()


async def get_linked_entities(user_chat_id: int) -> list[dict]:
    async with _db() as db:
        rows = await db.execute_fetchall(
            "SELECT entity_chat_id, entity_type FROM entity_links WHERE user_chat_id = ?",
            (user_chat_id,),
        )
        return [{"chat_id": r[0], "type": r[1]} for r in rows]


async def get_linked_users(entity_chat_id: int) -> list[int]:
    async with _db() as db:
        rows = await db.execute_fetchall(
            "SELECT user_chat_id FROM entity_links WHERE entity_chat_id = ?",
            (entity_chat_id,),
        )
        return [r[0] for r in rows]
