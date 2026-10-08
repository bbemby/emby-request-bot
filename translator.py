"""Google 翻译（免费 gtx 接口）：日文简介/标题 → 中文。"""
import html
import json
import logging
import urllib.parse
import urllib.request

from config import config

log = logging.getLogger(__name__)

_ENDPOINT = "https://translate.googleapis.com/translate_a/single"


def translate(text: str, src: str = "auto") -> str:
    """翻译文本，失败时原样返回（不抛异常，保证 bot 不崩）。"""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        q = urllib.parse.quote(text[:1500])  # 接口对超长文本不友好，截断
        url = (
            f"{_ENDPOINT}?client=gtx&sl={src}&tl={config.TRANS_TARGET}"
            f"&dt=t&q={q}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # data[0] 是 [[译文, 原文, ...], ...] 结构
        parts = [seg[0] for seg in data[0] if seg and seg[0]]
        return html.unescape("".join(parts)).strip() or text
    except Exception as e:
        log.warning("translate failed: %s", e)
        return text
