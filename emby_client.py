"""Emby 入库检查。"""
import json
import logging
import re
import urllib.parse
import urllib.request

from config import config

log = logging.getLogger(__name__)

# 已知需要补数字前缀的番号系列：请求 GANA-2535，库里是 200GANA-2535
# 200 系是业余番号的通用编目惯例（不只 GANA），见 _code_variants 的通用规则；
# 这里只放不符合 200 惯例的特殊映射（如 MIUM -> 300MIUM）。
_PREFIX_MAP = {"MIUM": "300MIUM"}


def _code_variants(code: str) -> list[str]:
    """生成番号的搜索变体。

    Emby 的 SearchTerm 不是子串匹配：搜 GANA-2535 找不到 200GANA-2535，
    所以把去前缀 / 补前缀的变体都试一遍。
    """
    code = code.strip().upper()
    variants = [code]
    # 去数字前缀：200GANA-2535 -> GANA-2535
    m = re.match(r"^(\d+)([A-Z]+-\d+)$", code)
    if m and m.group(2) not in variants:
        variants.append(m.group(2))
    # 补数字前缀：GANA-2535 -> 200GANA-2535
    m = re.match(r"^([A-Z]+)-(\d+)$", code)
    if m:
        full = _PREFIX_MAP.get(m.group(1), f"200{m.group(1)}")
        full = f"{full}-{m.group(2)}"
        if full not in variants:
            variants.append(full)
    return variants


def is_in_library(code: str) -> tuple[bool, str]:
    """查番号是否已在 Emby 库。返回 (是否在库, 匹配到的标题)。"""
    code = code.strip().upper()
    if not code or not config.EMBY_URL or not config.EMBY_API_KEY:
        return False, ""
    for variant in _code_variants(code):
        try:
            params = urllib.parse.urlencode({
                "api_key": config.EMBY_API_KEY,
                "SearchTerm": variant,
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
            # 标题包含任一变体才算命中，避免误杀
            for it in items:
                name = str(it.get("Name") or "")
                if variant in name.upper() or code in name.upper():
                    return True, name
        except Exception as e:
            log.warning("emby check failed for %s: %s", variant, e)
    return False, ""
