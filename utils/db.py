import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


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
                active_generation INTEGER NOT NULL DEFAULT 0,
                size TEXT NOT NULL DEFAULT 'large'
            )
            """
        )
        # Миграции для уже существующей таблицы: добавляем колонку size при отсутствии
        cur.execute("PRAGMA table_info(users)")
        cols = {row[1] for row in cur.fetchall()}
        if "size" not in cols:
            try:
                cur.execute("ALTER TABLE users ADD COLUMN size TEXT NOT NULL DEFAULT 'large'")
            except Exception:
                pass
        # Accounts table for Sora cookies farm
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cookies_json TEXT NOT NULL,
                account_key TEXT,
                active_generations INTEGER NOT NULL DEFAULT 0,
                daily_generations INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                last_used_date TEXT,
                disabled INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # Basic migrations: add columns if missing
        cur.execute("PRAGMA table_info(accounts)")
        acols = {row[1] for row in cur.fetchall()}
        if "account_key" not in acols:
            try:
                cur.execute("ALTER TABLE accounts ADD COLUMN account_key TEXT")
            except Exception:
                pass
        if "disabled" not in acols:
            try:
                cur.execute("ALTER TABLE accounts ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass
        if "last_used_at" not in acols:
            try:
                cur.execute("ALTER TABLE accounts ADD COLUMN last_used_at TEXT")
            except Exception:
                pass
        if "last_used_date" not in acols:
            try:
                cur.execute("ALTER TABLE accounts ADD COLUMN last_used_date TEXT")
            except Exception:
                pass

        # Ensure a partial unique index for non-null account_key values
        try:
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_account_key_unique ON accounts(account_key) WHERE account_key IS NOT NULL"
            )
        except Exception:
            pass

        # Jobs table for persistent generation queue
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS generation_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                prompt TEXT NOT NULL,
                orientation TEXT,
                frames INTEGER NOT NULL,
                size TEXT NOT NULL,
                image BLOB,
                status TEXT NOT NULL,
                progress REAL,
                result_url TEXT,
                error_message TEXT,
                wait_message_id INTEGER,
                task_id TEXT,
                account_id INTEGER,
                poll_interval REAL NOT NULL DEFAULT 3.0,
                timeout_sec REAL NOT NULL DEFAULT 900.0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_event TEXT
            )
            """
        )

        # Basic indices to speed up lookups
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_jobs_status ON generation_jobs(status)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_jobs_user ON generation_jobs(user_id)"
        )

        conn.commit()
    finally:
        conn.close()


def _connect_rw() -> sqlite3.Connection:
    """Open a read-write connection with WAL-friendly settings."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    return conn


def add_account_row(cookies_json: str, account_key: Optional[str] = None) -> int:
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO accounts (cookies_json, account_key, active_generations, daily_generations, last_used_at, last_used_date, disabled) "
            "VALUES (?, ?, 0, 0, NULL, NULL, 0)",
            (cookies_json, account_key),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def get_account_id_by_key(account_key: str) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM accounts WHERE account_key = ?", (account_key,))
        row = cur.fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def list_all_accounts_minimal() -> list[tuple[int, str, Optional[str]]]:
    """Return list of (id, cookies_json, account_key)."""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, cookies_json, account_key FROM accounts")
        out = []
        for row in cur.fetchall():
            out.append((int(row[0]), str(row[1]), (str(row[2]) if row[2] is not None else None)))
        return out
    finally:
        conn.close()


def list_accounts_counts(daily_limit: int = 100, concurrency_limit: int = 5) -> tuple[int, int, int]:
    """Return (total, available_daily, available_slots) counts without modifying state.

    - available_daily: accounts with ``daily_generations < daily_limit`` and not disabled
    - available_slots: accounts with ``daily_generations < daily_limit`` and ``active_generations < concurrency_limit`` and not disabled
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM accounts")
        total = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM accounts WHERE disabled=0 AND daily_generations < ?",
            (int(daily_limit),),
        )
        available_daily = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM accounts WHERE disabled=0 AND daily_generations < ? AND active_generations < ?",
            (int(daily_limit), int(concurrency_limit)),
        )
        available_slots = int(cur.fetchone()[0])
        return total, available_daily, available_slots
    finally:
        conn.close()


def reset_daily_where_needed(today_str: str) -> None:
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE accounts SET daily_generations=0, last_used_date=? WHERE last_used_date IS NOT NULL AND last_used_date <> ?",
            (today_str, today_str),
        )
        conn.commit()
    finally:
        conn.close()


def acquire_account_for_generation(
    today_str: str,
    now_iso: str,
    daily_limit: int = 100,
    concurrency_limit: int = 5,
) -> Optional[dict]:
    """Atomically pick the best account and increment active_generations.

    Returns dict {id, cookies_json, active_generations, daily_generations} if acquired, else None.
    """
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        # Start an immediate transaction to avoid race on update
        cur.execute("BEGIN IMMEDIATE")

        # Reset daily counters when date changed
        cur.execute(
            "UPDATE accounts SET daily_generations=0, last_used_date=? WHERE last_used_date IS NOT NULL AND last_used_date <> ?",
            (today_str, today_str),
        )

        # Pick candidate
        cur.execute(
            """
            SELECT id, cookies_json, active_generations, daily_generations
            FROM accounts
            WHERE disabled=0 AND daily_generations < ? AND active_generations < ?
            ORDER BY active_generations ASC, daily_generations ASC, last_used_at ASC NULLS FIRST, id ASC
            LIMIT 1
            """,
            (int(daily_limit), int(concurrency_limit)),
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return None
        acc_id, cookies_json, active_gens, daily_gens = int(row[0]), str(row[1]), int(row[2]), int(row[3])

        # Increment active and update last_used markers (check guard again in WHERE)
        cur.execute(
            "UPDATE accounts SET active_generations = active_generations + 1, last_used_at = ?, last_used_date = ? "
            "WHERE id = ? AND disabled=0 AND daily_generations < ? AND active_generations < ?",
            (now_iso, today_str, acc_id, int(daily_limit), int(concurrency_limit)),
        )
        if cur.rowcount != 1:
            # Someone raced; give up this time
            conn.commit()
            return None
        conn.commit()
        return {
            "id": acc_id,
            "cookies_json": cookies_json,
            "active_generations": active_gens + 1,
            "daily_generations": daily_gens,
        }
    finally:
        conn.close()


def increment_daily_generation(acc_id: int, today_str: str, now_iso: str) -> None:
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE accounts SET daily_generations = daily_generations + 1, last_used_at = ?, last_used_date = ? WHERE id = ?",
            (now_iso, today_str, acc_id),
        )
        conn.commit()
    finally:
        conn.close()


def decrement_active_generation(acc_id: int) -> None:
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE accounts SET active_generations = CASE WHEN active_generations > 0 THEN active_generations - 1 ELSE 0 END WHERE id = ?",
            (acc_id,),
        )
        conn.commit()
    finally:
        conn.close()


def set_daily_generations(acc_id: int, value: int, today_str: Optional[str] = None, now_iso: Optional[str] = None) -> None:
    """Force-set ``daily_generations`` for an account.

    Useful when the server returns a daily-limit error so local counter stays in sync.
    """
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        if today_str and now_iso:
            cur.execute(
                "UPDATE accounts SET daily_generations = ?, last_used_at = ?, last_used_date = ? WHERE id = ?",
                (int(value), now_iso, today_str, acc_id),
            )
        else:
            cur.execute(
                "UPDATE accounts SET daily_generations = ? WHERE id = ?",
                (int(value), acc_id),
            )
        conn.commit()
    finally:
        conn.close()


def add_user_if_not_exists(user_id: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        if cur.fetchone() is None:
            cur.execute(
                "INSERT INTO users (user_id, is_vertical, duration_sec, active_generation, size) VALUES (?, 1, 10, 0, 'large')",
                (user_id,),
            )
            conn.commit()
    finally:
        conn.close()


def get_user_settings(user_id: int) -> Tuple[int, int, int, str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT is_vertical, duration_sec, active_generation, size FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            add_user_if_not_exists(user_id)
            return 1, 10, 0, 'large'
        return int(row[0]), int(row[1]), int(row[2]), str(row[3])
    finally:
        conn.close()


def update_orientation(user_id: int, is_vertical: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_vertical = ? WHERE user_id = ?", (1 if is_vertical else 0, user_id))
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO users (user_id, is_vertical, duration_sec, active_generation, size) VALUES (?, ?, 10, 0, 'large')",
                (user_id, 1 if is_vertical else 0),
            )
        conn.commit()
    finally:
        conn.close()


def update_duration(user_id: int, duration_sec: int) -> None:
    if duration_sec not in (5, 10, 15):
        raise ValueError("duration_sec must be 5, 10, or 15")
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET duration_sec = ? WHERE user_id = ?", (duration_sec, user_id))
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO users (user_id, is_vertical, duration_sec, active_generation, size) VALUES (?, 1, ?, 0, 'large')",
                (user_id, duration_sec),
            )
        conn.commit()
    finally:
        conn.close()


def update_size(user_id: int, size: str) -> None:
    size_norm = (size or '').lower()
    if size_norm not in ("small", "large"):
        raise ValueError("size must be 'small' or 'large'")
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET size = ? WHERE user_id = ?", (size_norm, user_id))
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO users (user_id, is_vertical, duration_sec, active_generation, size) VALUES (?, 1, 10, 0, ?)",
                (user_id, size_norm),
            )
        conn.commit()
    finally:
        conn.close()


def set_active_generation(user_id: int, flag: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE users SET active_generation = ? WHERE user_id = ?", (1 if flag else 0, user_id))
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO users (user_id, is_vertical, duration_sec, active_generation, size) VALUES (?, 1, 10, ?, 'large')",
                (user_id, 1 if flag else 0),
            )
        conn.commit()
    finally:
        conn.close()


def get_account_credentials(acc_id: int) -> Optional[Dict[str, str]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, cookies_json FROM accounts WHERE id = ?", (acc_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": str(row[0]), "cookies_json": str(row[1])}
    finally:
        conn.close()


def _row_to_job(row: Tuple[Any, ...]) -> Dict[str, Any]:
    keys = [
        "id",
        "user_id",
        "chat_id",
        "prompt",
        "orientation",
        "frames",
        "size",
        "image",
        "status",
        "progress",
        "result_url",
        "error_message",
        "wait_message_id",
        "task_id",
        "account_id",
        "poll_interval",
        "timeout_sec",
        "created_at",
        "updated_at",
        "last_event",
    ]
    data = dict(zip(keys, row))
    if data.get("image") is not None and not isinstance(data["image"], (bytes, bytearray)):
        data["image"] = bytes(data["image"])
    for int_key in ("id", "user_id", "chat_id", "frames", "wait_message_id", "account_id"):
        if data.get(int_key) is not None:
            try:
                data[int_key] = int(data[int_key])
            except Exception:
                pass
    for float_key in ("progress", "poll_interval", "timeout_sec"):
        if data.get(float_key) is not None:
            try:
                data[float_key] = float(data[float_key])
            except Exception:
                pass
    return data


def enqueue_generation_job(
    user_id: int,
    chat_id: int,
    prompt: str,
    orientation: Optional[str],
    frames: int,
    size: str,
    image_bytes: Optional[bytes],
    wait_message_id: Optional[int],
    poll_interval: float,
    timeout_sec: float,
) -> int:
    now = datetime.utcnow().isoformat()
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO generation_jobs (
                user_id, chat_id, prompt, orientation, frames, size, image, status, progress,
                result_url, error_message, wait_message_id, task_id, account_id,
                poll_interval, timeout_sec, created_at, updated_at, last_event
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, ?, NULL, NULL, ?, ?, ?, ?, NULL)
            """,
            (
                int(user_id),
                int(chat_id),
                prompt,
                orientation,
                int(frames),
                size,
                sqlite3.Binary(image_bytes) if image_bytes is not None else None,
                wait_message_id,
                float(poll_interval),
                float(timeout_sec),
                now,
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def fetch_next_queued_job() -> Optional[Dict[str, Any]]:
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "SELECT id, user_id, chat_id, prompt, orientation, frames, size, image, status, progress, result_url, error_message, "
            "wait_message_id, task_id, account_id, poll_interval, timeout_sec, created_at, updated_at, last_event "
            "FROM generation_jobs WHERE status = 'queued' ORDER BY id ASC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return None
        job = _row_to_job(row)
        now = datetime.utcnow().isoformat()
        cur.execute(
            "UPDATE generation_jobs SET status = 'running', updated_at = ? WHERE id = ?",
            (now, job["id"]),
        )
        conn.commit()
        job["status"] = "running"
        job["updated_at"] = now
        return job
    finally:
        conn.close()


def list_jobs_by_status(statuses: List[str]) -> List[Dict[str, Any]]:
    if not statuses:
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in statuses)
        cur.execute(
            f"SELECT id, user_id, chat_id, prompt, orientation, frames, size, image, status, progress, result_url, error_message, "
            f"wait_message_id, task_id, account_id, poll_interval, timeout_sec, created_at, updated_at, last_event "
            f"FROM generation_jobs WHERE status IN ({placeholders}) ORDER BY id ASC",
            tuple(statuses),
        )
        rows = cur.fetchall()
        return [_row_to_job(row) for row in rows]
    finally:
        conn.close()


def update_job(job_id: int, **fields: Any) -> None:
    if not fields:
        fields = {}
    now = datetime.utcnow().isoformat()
    fields["updated_at"] = now
    columns = []
    values: List[Any] = []
    for key, value in fields.items():
        if key == "image" and value is not None and not isinstance(value, sqlite3.Binary):
            value = sqlite3.Binary(value)
        columns.append(f"{key} = ?")
        values.append(value)
    values.append(job_id)
    conn = _connect_rw()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE generation_jobs SET {', '.join(columns)} WHERE id = ?",
            tuple(values),
        )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, user_id, chat_id, prompt, orientation, frames, size, image, status, progress, result_url, error_message, "
            "wait_message_id, task_id, account_id, poll_interval, timeout_sec, created_at, updated_at, last_event FROM generation_jobs WHERE id = ?",
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return _row_to_job(row)
    finally:
        conn.close()
