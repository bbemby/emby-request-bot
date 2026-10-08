"""javdb-cli 的异步封装。

注意：javdb 二进制会在 stdin 上阻塞，必须关闭 stdin（DEVNULL）再跑，
search 默认 zone 会过滤部分番号，所以统一加 --zone all。
"""
import asyncio
import json
import logging
from typing import Any, Optional

from config import config

log = logging.getLogger(__name__)


async def _run(*args: str, timeout: int = 60) -> Optional[Any]:
    """跑一条 javdb 命令，返回解析后的 JSON，失败返回 None。"""
    cmd = [config.JAVDB_BIN, *args, "--json"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,  # 关键：防止二进制阻塞在 stdin
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode != 0:
            log.warning("javdb %s failed: %s", args[:2], stderr.decode()[:200])
            return None
        return json.loads(stdout.decode())
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError) as e:
        log.warning("javdb %s error: %s", args[:2], e)
        return None


async def search_movie(code: str) -> Optional[dict]:
    """按番号精确搜索，返回第一条结果；搜不到时尝试模糊搜索。"""
    code = code.strip().upper()
    # 1) 先按番号 code 维度查（最准）
    data = await _run("code", code, "--limit", "5")
    movies = (data or {}).get("movies") or []
    for m in movies:
        if (m.get("number") or "").upper() == code:
            return m
    if movies:
        return movies[0]
    # 2) 退化为关键词搜索
    data = await _run("search", code, "--zone", "all", "--limit", "5")
    movies = (data or {}).get("movies") or []
    for m in movies:
        if (m.get("number") or "").upper() == code:
            return m
    return movies[0] if movies else None


async def get_detail(movie_id: str) -> Optional[dict]:
    """取影片详情（含演员/片商/标签/简介/封面）。"""
    return await _run("detail", movie_id, "-i")


async def get_magnets(movie_id: str, top_n: int = 5) -> list[dict]:
    """取磁力：中文字幕优先，其次按体积从大到小，取 top_n。size 单位是 MB。"""
    data = await _run("magnets", movie_id, "-i")
    magnets = (data or {}).get("magnets") or []
    # 排序：cnsub=True 置顶，其次按 size 降序
    magnets.sort(key=lambda m: (bool(m.get("cnsub")), m.get("size") or 0), reverse=True)
    return magnets[:top_n]


async def reverse_search(image_path: str, limit: int = 3) -> list[dict]:
    """以图搜番：返回候选列表，每项含 code / similarity / movie_id。

    后端是 AVScan，和 javdb-cli 内置以图搜番同一套。
    注意：javdb 部分候选失败时会以非 0 退出，但 stdout 仍是有效 JSON，
    所以这里不走 _run 的严格错误处理，直接解析 stdout。
    """
    cmd = [config.JAVDB_BIN, "search", image_path, "--image",
           "--limit", str(limit), "--json"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), 120)
        data = json.loads(stdout.decode())
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError) as e:
        log.warning("reverse search error: %s", e)
        return []
    matches = (data or {}).get("matches") or []
    out = []
    for m in matches:
        cand = m.get("candidate") or {}
        code = cand.get("video_code") or ""
        if not code:
            continue
        out.append({
            "code": code,
            "similarity": float(cand.get("best_similarity") or 0),
            "movie_id": m.get("movie_id") or "",
        })
    return out


def magnet_link(m: dict) -> str:
    """从 hash 拼出标准 magnet 链接。"""
    h = m.get("hash") or ""
    name = m.get("name") or ""
    import urllib.parse

    dn = f"&dn={urllib.parse.quote(name)}" if name else ""
    return f"magnet:?xt=urn:btih:{h}{dn}" if h else ""


def format_size(mb: Any) -> str:
    """MB → 人性化显示。"""
    try:
        mb = float(mb or 0)
    except (TypeError, ValueError):
        return "未知"
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb:.0f} MB"
