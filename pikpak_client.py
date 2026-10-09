"""PikPak 离线下载对接：求片后自动把磁力加到 PikPak 云端下载。

用 refresh_token 换 access_token（自动续期），失败时返回 (False, 原因)。
"""
import json
import logging
import time
import urllib.request

from config import config

log = logging.getLogger(__name__)

_API = "https://api-drive.mypikpak.com"
_AUTH = "https://user.mypikpak.com"

# client_id 与 pikpak-cli 一致（OAuth 设备码流程用的那个）
_CLIENT_ID = "aal7sToC85zMXEpJ"

_token_cache = {"access_token": "", "expires_at": 0}


def _post(url: str, body: dict, token: str = "") -> dict:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}",
                      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def get_access_token() -> tuple[bool, str]:
    """返回 (成功, access_token或原因)。自动用 refresh_token 续期。"""
    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] - 300 > now:
        return True, _token_cache["access_token"]
    refresh = (config.PIKPAK_REFRESH_TOKEN or "").strip()
    if not refresh:
        return False, "PIKPAK_REFRESH_TOKEN 未配置"
    try:
        d = _post(f"{_AUTH}/v1/auth/token", {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": _CLIENT_ID,
        })
    except Exception as e:
        return False, f"token 刷新失败：{e}"
    at = d.get("access_token", "")
    if not at:
        return False, f"token 刷新失败：{str(d)[:120]}"
    _token_cache["access_token"] = at
    _token_cache["expires_at"] = now + int(d.get("expires_in", 3600))
    # refresh_token 可能会轮换，写回配置（仅内存，重启后从 .env 读）
    if d.get("refresh_token"):
        config.PIKPAK_REFRESH_TOKEN = d["refresh_token"]
    return True, at


def add_offline_task(magnet: str, name: str) -> tuple[bool, str]:
    """把磁力加到 PikPak 离线下载。返回 (成功, task_id或原因)。"""
    ok, token = get_access_token()
    if not ok:
        return False, token
    folder = (config.PIKPAK_FOLDER_ID or "").strip()
    try:
        d = _post(f"{_API}/drive/v1/files", {
            "kind": "drive#file",
            "name": name,
            "uploadType": "UPLOAD_TYPE_URL",
            "url": {"url": magnet},
            "parent_id": folder,
        }, token)
    except Exception as e:
        return False, f"添加离线任务失败：{e}"
    task = d.get("task", {})
    task_id = task.get("id", "")
    if not task_id:
        return False, f"添加离线任务失败：{str(d)[:150]}"
    return True, task_id


def get_task(task_id: str) -> tuple[bool, dict | str]:
    """查离线任务状态。返回 (成功, task dict或原因)。"""
    ok, token = get_access_token()
    if not ok:
        return False, token
    try:
        d = _get(f"{_API}/drive/v1/tasks/{task_id}", token)
    except Exception as e:
        return False, f"查任务状态失败：{e}"
    if d.get("error"):
        return False, d.get("error_description", str(d)[:120])
    return True, d


def task_progress_summary(task: dict) -> str:
    """把 PikPak 任务转成一行进度描述。"""
    phase = task.get("phase", "")
    name = task.get("file_name", "") or task.get("name", "")
    if phase == "PHASE_TYPE_COMPLETE":
        return f"✅ {name} 下载完成"
    if phase == "PHASE_TYPE_ERROR":
        return f"❌ {name} 失败：{task.get('message', '')[:60]}"
    prog = task.get("progress", 0)
    try:
        pct = float(prog)
    except (TypeError, ValueError):
        pct = 0
    bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
    return f"[{bar}] {pct:.0f}% {name}"
