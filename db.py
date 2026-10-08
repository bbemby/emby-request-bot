"""SQLite 求片记录。"""
import logging
import sqlite3
import time
from pathlib import Path

from config import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    title_cn TEXT DEFAULT '',
    cover_file_id TEXT DEFAULT '',
    user_id INTEGER NOT NULL,
    username TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    magnet TEXT DEFAULT '',
    drive_path TEXT DEFAULT '',
    created_at REAL DEFAULT 0,
    updated_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_code ON requests(code);
CREATE INDEX IF NOT EXISTS idx_status ON requests(status);
"""

# pending: 等管理员处理 / downloading: 下载中 / uploading: 上传Drive中
# uploaded: 已传Drive待入库 / fulfilled: 已入库 / cancelled: 已取消 / failed: 失败
ACTIVE_STATUSES = ("pending", "downloading", "uploading", "uploaded")


def _conn():
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.executescript(SCHEMA)


def add_request(code: str, title_cn: str, user_id: int, username: str) -> int:
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO requests (code, title_cn, user_id, username, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (code.upper(), title_cn, user_id, username, now, now),
        )
        return cur.lastrowid


def get_request(rid: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        return dict(row) if row else None


def find_active_by_code(code: str) -> dict | None:
    """找该番号未完结的求片（去重用）。"""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM requests WHERE code = ? AND status IN ('pending','downloading','uploaded')"
            " ORDER BY id LIMIT 1",
            (code.upper(),),
        ).fetchone()
        return dict(row) if row else None


def list_by_status(statuses: tuple = ACTIVE_STATUSES) -> list[dict]:
    placeholders = ",".join("?" * len(statuses))
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM requests WHERE status IN ({placeholders}) ORDER BY created_at",
            statuses,
        ).fetchall()
        return [dict(r) for r in rows]


def update_status(rid: int, status: str, **fields):
    fields["status"] = status
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE requests SET {sets} WHERE id = ?", (*fields.values(), rid))


def set_cover_file_id(rid: int, file_id: str):
    with _conn() as c:
        c.execute("UPDATE requests SET cover_file_id = ? WHERE id = ?", (file_id, rid))
