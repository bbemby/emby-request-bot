"""Emby 入库检查。"""
import json
import logging
import urllib.parse
import urllib.request

from config import config

log = logging.getLogger(__name__)


def is_in_library(code: str) -> tuple[bool, str]:
    """查番号是否已在 Emby 库。返回 (是否在库, 匹配到的标题)。"""
    code = code.strip().upper()
    if not code or not config.EMBY_URL or not config.EMBY_API_KEY:
        return False, ""
    try:
        params = urllib.parse.urlencode({
            "api_key": config.EMBY_API_KEY,
            "SearchTerm": code,
            "Recursive": "true",
            "IncludeItemTypes": "Movie",
            "Fields": "BasicSyncInfo",
            "limit": 5,
        })
        url = f"{config.EMBY_URL}/emby/Items?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("Items") or []
        # 标题或原名包含番号才算命中，避免误杀
        for it in items:
            name = str(it.get("Name") or "")
            if code in name.upper():
                return True, name
        return False, ""
    except Exception as e:
        log.warning("emby check failed for %s: %s", code, e)
        return False, ""
