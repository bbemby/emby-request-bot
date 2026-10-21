"""SQLite 求片记录。"""
import contextlib
import datetime
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

# pending: 等管理员处理 / downloading: 下载中（旧 aria2 流程，保留兼容）
# uploading: 上传Drive中（旧流程）/ uploaded: 已传Drive待入库（旧流程）
# pikpak_queued: 已加入 PikPak 离线 / pikpak_done: PikPak 下载完成
# fulfilled: 已入库 / cancelled: 已取消 / failed: 失败
ACTIVE_STATUSES = ("pending", "downloading", "uploading", "uploaded",
                   "pikpak_queued", "pikpak_done")


def _migrate(c):
    cols = {r[1] for r in c.execute("PRAGMA table_info(requests)")}
    if "pikpak_task_id" not in cols:
        c.execute("ALTER TABLE requests ADD COLUMN pikpak_task_id TEXT DEFAULT ''")


@contextlib.contextmanager
def _conn():
    """打开一个 SQLite 连接，用完一定关闭。

    原先直接返回连接、靠 `with sqlite3.Connection` 只 commit 不 close，
    长期运行会累积连接直到 Too many open files；同时开 WAL +
    busy_timeout，缓解并发写 database is locked。
    """
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


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
    """找该番号未完结的求片（去重用）。状态集合与 ACTIVE_STATUSES 保持一致，
    别再手写列表（曾经漏过 uploading，导致锁内复查也拦不住）。"""
    placeholders = ", ".join("?" * len(ACTIVE_STATUSES))
    with _conn() as c:
        row = c.execute(
            f"SELECT * FROM requests WHERE code = ? AND status IN ({placeholders})"
            " ORDER BY id LIMIT 1",
            (code.upper(), *ACTIVE_STATUSES),
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


def try_fulfill(rid: int) -> bool:
    """原子地将未完结求片标为已入库。

    只有当行仍处于未完结状态时才更新，返回 True 表示本次调用实际发生了
    状态变更（调用方据此决定是否发通知，避免并发检查导致重复通知）。
    """
    placeholders = ", ".join("?" * len(ACTIVE_STATUSES))
    with _conn() as c:
        cur = c.execute(
            f"UPDATE requests SET status = 'fulfilled', updated_at = ?"
            f" WHERE id = ? AND status IN ({placeholders})",
            (time.time(), rid, *ACTIVE_STATUSES),
        )
        return cur.rowcount > 0


def _day_start() -> float:
    return time.mktime(datetime.date.today().timetuple())


def count_user_today(user_id: int) -> int:
    """用户今天求了几部（含已取消，防刷）。"""
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM requests WHERE user_id = ? AND created_at >= ?",
            (user_id, _day_start())).fetchone()
        return row[0] if row else 0


def get_user_requests(user_id: int, limit: int = 10, offset: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM requests WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)).fetchall()
        return [dict(r) for r in rows]


def count_user_requests(user_id: int) -> int:
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM requests WHERE user_id = ?",
            (user_id,)).fetchone()
        return row[0] if row else 0


def get_stats() -> dict:
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        by_status = {r[0]: r[1] for r in c.execute(
            "SELECT status, COUNT(*) FROM requests GROUP BY status")}
        today = c.execute(
            "SELECT COUNT(*) FROM requests WHERE created_at >= ?",
            (_day_start(),)).fetchone()[0]
        week = c.execute(
            "SELECT COUNT(*) FROM requests WHERE created_at >= ?",
            (_day_start() - 6 * 86400,)).fetchone()[0]
        top = c.execute(
            "SELECT code, COUNT(*) as n FROM requests "
            "GROUP BY code ORDER BY n DESC LIMIT 5").fetchall()
        return {"total": total, "by_status": by_status, "today": today,
                "week": week, "top": [(r[0], r[1]) for r in top]}
