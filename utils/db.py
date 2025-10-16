import os
import sqlite3
from typing import Tuple


DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "database.db")


def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_vertical INTEGER NOT NULL DEFAULT 1,
                duration_sec INTEGER NOT NULL DEFAULT 10,
                size TEXT NOT NULL DEFAULT 'large'
            )
            """
        )
        cur.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in cur.fetchall()}
        if "size" not in cols:
            try:
                cur.execute("ALTER TABLE users ADD COLUMN size TEXT NOT NULL DEFAULT 'large'")
            except Exception:
                pass

        conn.commit()
    finally:
        conn.close()


def _connect_rw() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn

def _ensure_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))

def add_user_if_not_exists(user_id: int) -> None:
    conn = _connect_rw()
    try:
        _ensure_user(conn, user_id)
    finally:
        conn.close()


def get_user_settings(user_id: int) -> Tuple[int, int, int, str]:
    conn = _connect_rw()
    try:
        _ensure_user(conn, user_id)
        cur = conn.cursor()
        cur.execute(
            "SELECT is_vertical, duration_sec, size FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return 1, 10, 'large'
        return int(row[0]), int(row[1]), int(row[2])
    finally:
        conn.close()


def update_orientation(user_id: int, is_vertical: int) -> None:
    conn = _connect_rw()
    try:
        _ensure_user(conn, user_id)
        conn.execute(
            "UPDATE users SET is_vertical = ? WHERE user_id = ?",
            (1 if is_vertical else 0, user_id),
        )
    finally:
        conn.close()


def update_duration(user_id: int, duration_sec: int) -> None:
    if duration_sec not in (5, 10, 15):
        raise ValueError("duration_sec must be 5, 10, or 15")
    conn = _connect_rw()
    try:
        _ensure_user(conn, user_id)
        conn.execute(
            "UPDATE users SET duration_sec = ? WHERE user_id = ?",
            (duration_sec, user_id),
        )
    finally:
        conn.close()


def update_size(user_id: int, size: str) -> None:
    size_norm = (size or '').lower()
    if size_norm not in ("small", "large"):
        raise ValueError("size must be 'small' or 'large'")
    conn = _connect_rw()
    try:
        _ensure_user(conn, user_id)
        conn.execute(
            "UPDATE users SET size = ? WHERE user_id = ?",
            (size_norm, user_id),
        )
    finally:
        conn.close()

