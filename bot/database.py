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
            """
        )
        await db.commit()


def _db() -> aiosqlite.Connection:
    assert DB_PATH is not None, "Call init_db() first"
    return aiosqlite.connect(DB_PATH)


# ── subscriptions ──────────────────────────────────────────────────

async def add_subscription(chat_id: int, chat_title: str, sub_type: str, sub_key: str, sub_label: str) -> bool:
    """Returns True if added, False if already existed."""
    async with _db() as db:
        try:
            await db.execute(
                "INSERT INTO subscriptions (chat_id, chat_title, sub_type, sub_key, sub_label) VALUES (?, ?, ?, ?, ?)",
                (chat_id, chat_title, sub_type, sub_key, sub_label),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_subscription(chat_id: int, sub_type: str, sub_key: str) -> bool:
    async with _db() as db:
        cursor = await db.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND sub_type = ? AND sub_key = ?",
            (chat_id, sub_type, sub_key),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_subscriptions(chat_id: int, sub_type: str | None = None) -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        if sub_type:
            rows = await db.execute_fetchall(
                "SELECT * FROM subscriptions WHERE chat_id = ? AND sub_type = ? ORDER BY sub_label",
                (chat_id, sub_type),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM subscriptions WHERE chat_id = ? ORDER BY sub_type, sub_label",
                (chat_id,),
            )
        return [dict(r) for r in rows]


async def get_all_chat_ids_for(sub_type: str, sub_key: str) -> list[int]:
    async with _db() as db:
        rows = await db.execute_fetchall(
            "SELECT chat_id FROM subscriptions WHERE sub_type = ? AND sub_key = ?",
            (sub_type, sub_key),
        )
        return [r[0] for r in rows]


async def get_all_subscriptions() -> list[dict]:
    async with _db() as db:
        db.row_factory = aiosqlite.Row
        rows = await db.execute_fetchall("SELECT * FROM subscriptions ORDER BY chat_id")
        return [dict(r) for r in rows]


# ── seen demos ─────────────────────────────────────────────────────

async def mark_seen(chat_id: int, demo_id: str) -> None:
    async with _db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO seen_demos (chat_id, demo_id) VALUES (?, ?)",
            (chat_id, demo_id),
        )
        await db.commit()


async def is_seen(chat_id: int, demo_id: str) -> bool:
    async with _db() as db:
        row = await db.execute_fetchall(
            "SELECT 1 FROM seen_demos WHERE chat_id = ? AND demo_id = ?",
            (chat_id, demo_id),
        )
        return len(row) > 0


async def cleanup_old_seen(days: int = 90) -> None:
    async with _db() as db:
        await db.execute(
            "DELETE FROM seen_demos WHERE sent_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        await db.commit()
