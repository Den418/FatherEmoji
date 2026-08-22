import sqlite3

import config


def _db():
    return sqlite3.connect(config.DB_PATH)


def init_db():
    with _db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                joined_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id  INTEGER PRIMARY KEY,
                added_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS channels (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id    TEXT UNIQUE,
                chat_url   TEXT,
                chat_title TEXT
            );
            CREATE TABLE IF NOT EXISTS packs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                name       TEXT,
                title      TEXT,
                UNIQUE(user_id, name)
            );
        """)
        con.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (config.SUPER_ADMIN,))


def db_add_user(uid: int):
    with _db() as con:
        con.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))


def db_all_users() -> list[int]:
    with _db() as con:
        return [r[0] for r in con.execute("SELECT user_id FROM users")]


def db_user_count() -> int:
    with _db() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def db_get_admins() -> list[int]:
    with _db() as con:
        return [r[0] for r in con.execute("SELECT user_id FROM admins")]


def db_add_admin(uid: int):
    with _db() as con:
        con.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))


def db_remove_admin(uid: int) -> bool:
    if uid == config.SUPER_ADMIN:
        return False
    with _db() as con:
        con.execute("DELETE FROM admins WHERE user_id = ?", (uid,))
    return True


def is_admin(uid: int) -> bool:
    return uid in db_get_admins()


def db_get_channels() -> list[tuple]:
    with _db() as con:
        return con.execute("SELECT id, chat_id, chat_url, chat_title FROM channels").fetchall()


def db_add_channel(chat_id: str, chat_url: str, chat_title: str):
    with _db() as con:
        con.execute(
            "INSERT OR IGNORE INTO channels (chat_id, chat_url, chat_title) VALUES (?,?,?)",
            (chat_id, chat_url, chat_title),
        )


def db_remove_channel(row_id: int):
    with _db() as con:
        con.execute("DELETE FROM channels WHERE id = ?", (row_id,))


def db_add_pack(uid: int, name: str, title: str):
    with _db() as con:
        con.execute("INSERT OR IGNORE INTO packs (user_id, name, title) VALUES (?,?,?)", (uid, name, title))


def db_get_packs(uid: int) -> list[tuple]:
    with _db() as con:
        return con.execute("SELECT id, name, title FROM packs WHERE user_id = ?", (uid,)).fetchall()


def db_get_pack_by_id(pack_id: int) -> tuple[str, str] | None:
    with _db() as con:
        res = con.execute("SELECT name, title FROM packs WHERE id = ?", (pack_id,)).fetchone()
        return res if res else None


def db_get_pack_title(uid: int, name: str) -> str | None:
    with _db() as con:
        res = con.execute("SELECT title FROM packs WHERE user_id = ? AND name = ?", (uid, name)).fetchone()
        return res[0] if res else None


def db_update_pack_title(uid: int, name: str, title: str):
    with _db() as con:
        con.execute("UPDATE packs SET title = ? WHERE user_id = ? AND name = ?", (title, uid, name))


def db_remove_pack(uid: int, name: str):
    with _db() as con:
        con.execute("DELETE FROM packs WHERE user_id = ? AND name = ?", (uid, name))


def db_user_known(uid: int) -> bool:
    with _db() as con:
        return con.execute("SELECT 1 FROM users WHERE user_id = ?", (uid,)).fetchone() is not None
