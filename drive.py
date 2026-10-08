"""Google Drive 上传：rclone。

安装：curl https://rclone.org/install.sh | sudo bash
配置：rclone config  按向导添加 Google Drive（需要浏览器做一次 OAuth）
"""
import asyncio
import logging
import os

from config import config

log = logging.getLogger(__name__)


async def upload_to_drive(local_path: str, dest_folder: str | None = None) -> tuple[bool, str]:
    """上传文件到 Drive。返回 (成功, 说明/远端路径)。"""
    if not os.path.isfile(local_path):
        return False, "本地文件不存在"
    remote = config.RCLONE_REMOTE
    folder = (dest_folder or config.RCLONE_DEST).strip("/")

    # 先确认 remote 配好了
    proc = await asyncio.create_subprocess_exec(
        config.RCLONE_BIN, "lsd", f"{remote}:",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await asyncio.wait_for(proc.communicate(), 30)
    if proc.returncode != 0:
        return False, f"rclone remote [{remote}] 未配置，先跑 rclone config"

    dest = f"{remote}:{folder}/" if folder else f"{remote}:/"
    proc = await asyncio.create_subprocess_exec(
        config.RCLONE_BIN, "move", local_path, dest,
        "--progress=false", "--stats=30s",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 7200)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "上传超时"
    if proc.returncode != 0:
        return False, f"上传失败：{err.decode()[-200:]}"
    name = os.path.basename(local_path)
    return True, f"{dest}{name}"
