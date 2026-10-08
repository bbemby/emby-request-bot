"""磁力选择性下载：aria2 取元数据 → 纯 Python 解析 torrent → 只下载最大的视频文件。

推荐下载工具：aria2（轻量、快、支持磁力 + select-file）。
安装：apt install aria2 / yum install aria2 / brew install aria2
"""
import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from config import config

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m2ts", ".ts", ".mpg"}

# ---------- 下载进度：rid -> {phase, total, downloaded, speed, file} ----------
# phase: metadata / downloading / uploading
_progress: dict[int, dict] = {}


def get_progress(rid: int) -> dict | None:
    return _progress.get(rid)


def set_phase(rid: int, phase: str):
    p = _progress.get(rid)
    if p:
        p["phase"] = phase
    else:
        _progress[rid] = {"phase": phase, "total": 0, "downloaded": 0, "speed": 0, "file": ""}


def clear_progress(rid: int):
    _progress.pop(rid, None)


def _measure_downloaded(dest_dir: str) -> int:
    """统计下载目录里视频文件的已下载字节数。"""
    total = 0
    if not os.path.isdir(dest_dir):
        return 0
    for root, _, files in os.walk(dest_dir):
        for fn in files:
            if fn.endswith(".aria2"):
                continue
            if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    return total


# ---------- 纯 Python bencode 解析 ----------
def _bdecode(data: bytes):
    def parse(i):
        c = data[i:i + 1]
        if c == b"i":
            j = data.index(b"e", i)
            return int(data[i + 1:j]), j + 1
        if c == b"l":
            lst, i = [], i + 1
            while data[i:i + 1] != b"e":
                v, i = parse(i)
                lst.append(v)
            return lst, i + 1
        if c == b"d":
            dct, i = {}, i + 1
            while data[i:i + 1] != b"e":
                k, i = parse(i)
                v, i = parse(i)
                dct[k] = v
            return dct, i + 1
        if c.isdigit():
            j = data.index(b":", i)
            n = int(data[i:j])
            return data[j + 1:j + 1 + n], j + 1 + n
        raise ValueError(f"bencode parse error at {i}")
    v, _ = parse(0)
    return v


def _pick_largest_video(torrent_path: str) -> tuple[int, str, int] | None:
    """返回 (aria2的1-based文件序号, 文件名, 大小)。"""
    with open(torrent_path, "rb") as f:
        info = _bdecode(f.read())[b"info"]
    files = info.get(b"files")
    candidates = []
    if files:  # 多文件
        for idx, fl in enumerate(files):
            path = "/".join(p.decode("utf-8", "ignore") for p in fl[b"path"])
            size = int(fl[b"length"])
            ext = os.path.splitext(path)[1].lower()
            if ext in VIDEO_EXTS and size > 50 * 1024 * 1024:  # >50MB 才算正片
                candidates.append((idx + 1, path, size))
    else:  # 单文件
        name = info[b"name"].decode("utf-8", "ignore")
        size = int(info[b"length"])
        if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
            candidates.append((1, name, size))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0]


def _is_magnet(s: str) -> bool:
    return s.strip().lower().startswith("magnet:?")


async def _run_aria2(*args: str, timeout: int = 300) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        config.ARIA2_BIN, *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode, (out + err).decode("utf-8", "ignore")[-500:]
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"


async def download_largest_video(magnet: str, dest_dir: str,
                                 rid: int | None = None) -> tuple[str | None, str]:
    """下载磁力中最大的视频文件。返回 (本地文件路径, 说明)。

    rid 不为 None 时记录下载进度，可用 get_progress(rid) 查询；
    任务被取消时会自动 kill aria2。
    """
    magnet = magnet.strip()
    if not _is_magnet(magnet):
        return None, "不是有效的磁力链接"
    os.makedirs(dest_dir, exist_ok=True)
    if rid is not None:
        _progress[rid] = {"phase": "metadata", "total": 0, "downloaded": 0,
                          "speed": 0, "file": ""}

    # 1) 取元数据
    meta_dir = tempfile.mkdtemp(prefix="meta_")
    try:
        rc, log_tail = await _run_aria2(
            "--bt-metadata-only=true", "--bt-save-metadata=true",
            f"--dir={meta_dir}", "--seed-time=0",
            "--bt-tracker-connect-timeout=20",
            magnet, timeout=180,
        )
        torrents = [f for f in os.listdir(meta_dir) if f.endswith(".torrent")]
        if not torrents:
            return None, f"获取种子元数据失败（{log_tail[-120:]}）"
        pick = _pick_largest_video(os.path.join(meta_dir, torrents[0]))
    finally:
        shutil.rmtree(meta_dir, ignore_errors=True)

    if not pick:
        return None, "磁力里没找到大于 50MB 的视频文件"
    file_idx, file_name, file_size = pick
    log.info("selected file #%d %s (%.1fGB)", file_idx, file_name, file_size / 1e9)
    if rid is not None:
        _progress[rid] = {"phase": "downloading", "total": file_size,
                          "downloaded": 0, "speed": 0, "file": file_name}

    # 2) 只下载选中的文件（轮询进度）
    proc = await asyncio.create_subprocess_exec(
        config.ARIA2_BIN,
        f"--select-file={file_idx}", f"--dir={dest_dir}",
        "--seed-time=0", "--bt-enable-lpd=true",
        "--max-connection-per-server=8", "--split=8",
        magnet,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        last_bytes, last_time = 0, time.time()
        while proc.returncode is None:
            await asyncio.sleep(5)
            downloaded = _measure_downloaded(dest_dir)
            now = time.time()
            dt = max(now - last_time, 0.1)
            speed = max(0.0, (downloaded - last_bytes) / dt)
            last_bytes, last_time = downloaded, now
            if rid is not None:
                _progress[rid].update({"downloaded": downloaded, "speed": speed})
        await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except Exception:
            pass
        raise
    if proc.returncode != 0:
        return None, "aria2 下载异常退出"

    # 找下载好的文件（aria2 保持原目录结构）
    base = os.path.basename(file_name)
    found = None
    for root, _, files in os.walk(dest_dir):
        if base in files:
            found = os.path.join(root, base)
            break
    if not found:
        cands = []
        for root, _, files in os.walk(dest_dir):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    p = os.path.join(root, fn)
                    try:
                        cands.append((os.path.getsize(p), p))
                    except OSError:
                        pass
        if cands:
            cands.sort(reverse=True)
            found = cands[0][1]
    if not found:
        return None, "下载完成但找不到视频文件"
    return found, f"{file_name} ({file_size / 1e9:.1f}GB)"
