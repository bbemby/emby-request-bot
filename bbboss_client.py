"""bb_boss 用户系统对接。

- 查用户：GET /user/user_info?tg=<tg_id>&token=...
  返回 {code: 200, data: {tg, iv(子弹), name(Emby用户名, None=未绑定), embyid, lv, cr, ex}}
  lv: a=白名单 b=正常 c=已禁用 d=未注册
- 扣子弹：POST /user/update_credit?token=... {"tg": str, "credit": 负数}
  余额不足返回 code 400。
"""
import json
import logging
import urllib.parse
import urllib.request

from config import config

log = logging.getLogger(__name__)


def _api(path: str, params: dict | None = None, method: str = "GET",
         data: dict | None = None, timeout: int = 10) -> dict:
    base = (config.BBOSS_API_URL or "").rstrip("/")
    if not base or not config.BBOSS_API_TOKEN:
        raise RuntimeError("BBOSS_API_URL/TOKEN 未配置")
    qs = {"token": config.BBOSS_API_TOKEN}
    qs.update(params or {})
    url = f"{base}{path}?{urllib.parse.urlencode(qs)}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_user(tg_id: int) -> tuple[str, dict | None]:
    """查 bb_boss 用户。

    返回 (状态, 用户数据)：
      ok       用户存在，数据在第二个元素
      no_user  用户不存在（code 404）
      api_error 接口异常/未配置（调用方应按系统繁忙处理）
    """
    try:
        r = _api("/user/user_info", {"tg": str(tg_id)})
    except Exception as e:
        log.warning("bbboss get_user failed: %s", e)
        return "api_error", None
    if r.get("code") == 200 and isinstance(r.get("data"), dict):
        return "ok", r["data"]
    if r.get("code") == 404:
        return "no_user", None
    log.warning("bbboss get_user unexpected: %s", str(r)[:200])
    return "api_error", None


def deduct_bullets(tg_id: int, amount: int) -> tuple[bool, int | str]:
    """扣子弹。amount 为正数。返回 (成功, 新余额 | 错误信息)。"""
    try:
        r = _api("/user/update_credit", method="POST",
                 data={"tg": str(tg_id), "credit": -int(amount)})
    except Exception as e:
        log.warning("bbboss deduct failed: %s", e)
        return False, "接口异常"
    if r.get("code") == 200:
        return True, int(r["data"].get("iv", 0))
    return False, str(r.get("message") or "扣费失败")


def is_whitelist(user: dict) -> bool:
    return (user or {}).get("lv") == "a"


def has_emby_account(user: dict | None) -> bool:
    return bool(user and user.get("name"))
