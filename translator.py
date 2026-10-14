"""翻译：日文简介/标题 → 中文。

主用 Google 免费 gtx 接口；该接口在某些机房 IP 会被 Google 挡掉
（返回 Sorry 页面），此时自动降级到 MyMemory 免费接口。
失败时原样返回（不抛异常，保证 bot 不崩）。
"""
import html
import json
import logging
import urllib.parse
import urllib.request

from config import config

log = logging.getLogger(__name__)

_GTX = "https://translate.googleapis.com/translate_a/single"


def _google(text: str, src: str, target: str) -> str:
    q = urllib.parse.quote(text[:1500])  # 接口对超长文本不友好，截断
    url = f"{_GTX}?client=gtx&sl={src}&tl={target}&dt=t&q={q}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # data[0] 是 [[译文, 原文, ...], ...] 结构；被挡时返回的是 HTML，会在这里抛错
    parts = [seg[0] for seg in data[0] if seg and seg[0]]
    out = html.unescape("".join(parts)).strip()
    if not out:
        raise ValueError("empty translation")
    return out


def _mymemory(text: str, src: str, target: str) -> str:
    q = urllib.parse.quote(text[:500])  # 免费额度小，截短一点
    url = f"https://api.mymemory.translated.net/get?q={q}&langpair={src}|{target}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = (data.get("responseData") or {}).get("translatedText", "").strip()
    if not out or data.get("responseStatus") != 200:
        raise ValueError(f"mymemory failed: {data.get('responseDetails')}")
    return html.unescape(out)


def translate(text: str, src: str = "auto") -> str:
    """翻译文本，失败时原样返回（不抛异常，保证 bot 不崩）。"""
    text = (text or "").strip()
    if not text:
        return ""
    target = config.TRANS_TARGET or "zh-CN"
    for name, fn in (("google", _google), ("mymemory", _mymemory)):
        try:
            out = fn(text, src, target)
            if out and out != text:
                return out
        except Exception as e:
            log.warning("translate via %s failed: %s", name, e)
    return text
