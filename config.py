"""配置：全部从环境变量读取。"""
import os


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


class Config:
    # Telegram
    BOT_TOKEN: str = _get("BOT_TOKEN")
    ADMIN_IDS: set[int] = {
        int(x) for x in _get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
    }
    FEEDBACK_GROUP_ID: int = int(_get("FEEDBACK_GROUP_ID", "0") or 0)

    # Emby
    EMBY_URL: str = _get("EMBY_URL").rstrip("/")
    EMBY_API_KEY: str = _get("EMBY_API_KEY")

    # 下载
    ARIA2_BIN: str = _get("ARIA2_BIN", "aria2c")
    JAVDB_BIN: str = _get("JAVDB_BIN", os.path.expanduser("~/bin/javdb"))
    DOWNLOAD_DIR: str = _get("DOWNLOAD_DIR", os.path.expanduser("~/downloads"))

    # Google Drive（rclone）
    RCLONE_BIN: str = _get("RCLONE_BIN", "rclone")
    RCLONE_REMOTE: str = _get("RCLONE_REMOTE", "gdrive")  # rclone remote 名
    RCLONE_DEST: str = _get("RCLONE_DEST", "emby-requests")  # 目标文件夹

    # 调度
    CHECK_INTERVAL_MIN: int = int(_get("CHECK_INTERVAL_MIN", "30") or 30)

    # 每人每天求片上限（0=不限）
    DAILY_LIMIT: int = int(_get("DAILY_LIMIT", "5") or 5)

    # Emby webhook 接收端口（Emby装webhook插件后POST到 http://服务器:端口/emby-webhook）
    WEBHOOK_PORT: int = int(_get("WEBHOOK_PORT", "8080") or 8080)
    WEBHOOK_TOKEN: str = _get("WEBHOOK_TOKEN", "")  # 可选，?token=xxx 校验

    # 求片后自动下载（中文优先的最优磁力），默认开
    AUTO_DOWNLOAD: bool = _get("AUTO_DOWNLOAD", "true").lower() not in ("0", "false", "no")

    # 磁力元数据获取超时（秒），超时换下一个磁力重试
    META_TIMEOUT: int = int(_get("META_TIMEOUT", "120") or 120)

    # 搜番相关（复用 javdb-ss 逻辑）
    MAGNET_COUNT: int = int(_get("MAGNET_COUNT", "3") or 3)
    RATE_LIMIT_SECONDS: int = int(_get("RATE_LIMIT_SECONDS", "3") or 3)
    CACHE_TTL: int = int(_get("CACHE_TTL", "3600") or 3600)
    TRANS_TARGET: str = _get("TRANS_TARGET", "zh-CN")

    DB_PATH: str = _get("DB_PATH", os.path.expanduser("~/.emby-request-bot.db"))


config = Config()
