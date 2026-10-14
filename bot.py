"""Emby 求片 Bot 主逻辑。

用户端（私聊/群组）：
  /s <番号>     搜番：遮罩封面 + 中文简介 + 磁力
  /q <番号>     求片：登记并通知到反馈群
  私聊直接发番号  等同 /q
  私聊发图       以图搜番（仅私聊）

管理员端（仅私聊，需在 ADMIN_IDS）：
  /pending      查看未完结的求片
  直接发磁力链接  选择对应求片 → 下载最大视频 → 传 Drive
  /cancel <id>  取消求片
  /done <id>    手动标记已入库

定时任务：每 CHECK_INTERVAL_MIN 分钟检查求片是否已入库，
入库后在反馈群通知。
"""
import asyncio
import html
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

import db
import downloader  # 旧 aria2 流程，已停用（保留兼容）
import drive  # 旧 Drive 上传，已停用（保留兼容）
import pikpak_client
import emby_client
import bbboss_client
import javdb_client as jdb
from config import config
from translator import translate

try:
    from aiohttp import web
except ImportError:
    web = None

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("emby_request_bot")

CODE_RE = re.compile(r"^[A-Z0-9]+-[0-9]+$", re.I)
MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+", re.I)

STATUS_EMOJI = {
    "pending": "⏳", "downloading": "⬇️", "uploading": "📤",
    "uploaded": "📥", "fulfilled": "✅", "failed": "❌", "cancelled": "🚫",
    "pikpak_queued": "☁️", "pikpak_done": "✅",
}

_cache: dict[str, tuple[float, dict]] = {}
_last_query: dict[int, float] = {}
_last_photo_query: dict[int, float] = {}
# 待管理员认领的磁力：msg_id -> magnet
_pending_magnets: dict[int, str] = {}
# 进行中的下载任务：rid -> asyncio.Task（支持取消/覆盖）
_download_tasks: dict[int, asyncio.Task] = {}


def _esc(s) -> str:
    return html.escape(str(s or ""))


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ---------- 缓存 & 限流 ----------
def _cache_get(key: str):
    item = _cache.get(key)
    if item and time.time() - item[0] < config.CACHE_TTL:
        return item[1]
    _cache.pop(key, None)
    return None


def _cache_set(key: str, value: dict):
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.time(), value)


def _rate_limited(user_id: int) -> bool:
    now = time.time()
    if now - _last_query.get(user_id, 0) < config.RATE_LIMIT_SECONDS:
        return True
    _last_query[user_id] = now
    return False


# ---------- 搜番展示（复用 javdb-ss 逻辑） ----------
def build_caption(d: dict) -> str:
    number = _esc(d.get("number"))
    title_cn = _esc(translate(d.get("title") or "", src="ja"))
    score = d.get("score")
    date = _esc(d.get("release_date"))
    dur = d.get("duration")
    actors = ", ".join(_esc(a.get("name")) for a in (d.get("actors") or [])[:4])
    lines = [f"🎬 <b>{number}</b>"]
    if title_cn:
        lines.append(f"<i>{title_cn}</i>")
    meta = []
    if score:
        meta.append(f"⭐ {score}")
    if date:
        meta.append(f"📅 {date}")
    if dur:
        meta.append(f"⏱️ {dur}分钟")
    if meta:
        lines.append(" | ".join(meta))
    if actors:
        lines.append(f"👩 {actors}")
    return "\n".join(lines)[:1000]


def build_detail(d: dict, magnets: list[dict]) -> str:
    parts: list[str] = []
    summary = (d.get("summary") or "").strip()
    if summary:
        parts.append(f"📝 <b>简介</b>\n{_esc(translate(summary, src='ja'))}\n")
    else:
        title_cn = translate(d.get("title") or "", src="ja")
        if title_cn:
            parts.append(f"📝 <b>标题翻译</b>\n{_esc(title_cn)}\n")
    tags = [_esc(t.get("name")) for t in (d.get("tags") or []) if t.get("name")]
    if tags:
        parts.append(f"🏷️ {' · '.join(tags[:10])}")
    maker = _esc(d.get("maker_name"))
    if maker:
        parts.append(f"🏢 {maker}")
    if magnets:
        parts.append(f"\n🧲 <b>磁力 Top{len(magnets)}</b>（中字优先）")
        for i, m in enumerate(magnets, 1):
            name = _esc(m.get("name") or "未知")
            size = jdb.format_size(m.get("size"))
            flags = []
            if m.get("cnsub"):
                flags.append("中字")
            if m.get("hd"):
                flags.append("高清")
            flag = f" <i>[{'·'.join(flags)}]</i>" if flags else ""
            link = jdb.magnet_link(m)
            parts.append(f"\n{i}️⃣ <b>{name}</b>{flag}\n📦 {size}\n<code>{_esc(link)}</code>")
    else:
        parts.append("\n🧲 暂无磁力")
    parts.append(f'\n🔗 <a href="https://javdb.com/v/{_esc(d.get("id"))}">JavDB 详情页</a>')
    return "\n".join(parts)


async def fetch_movie(code: str) -> dict | None:
    key = code.strip().upper()
    hit = _cache_get(key)
    if hit:
        return hit
    movie = await jdb.search_movie(key)
    if not movie:
        return None
    detail = await jdb.get_detail(movie["id"]) or movie
    magnets = await jdb.get_magnets(movie["id"], config.MAGNET_COUNT)
    result = {"detail": detail, "magnets": magnets}
    _cache_set(key, result)
    return result


async def _download_cover(movie_number: str, cover_url: str) -> str | None:
    if not cover_url or not movie_number:
        return None
    try:
        def _fetch():
            tmpdir = tempfile.mkdtemp(prefix="jdbcover_")
            p1 = subprocess.Popen(
                [config.JAVDB_BIN, "assets", "list", movie_number],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            cover_line = ""
            for raw in p1.stdout:
                line = raw.decode().strip()
                if line.startswith("image\t") and "/covers/" in line:
                    cover_line = line
                    break
            p1.wait()
            if not cover_line:
                return None
            p2 = subprocess.run(
                [config.JAVDB_BIN, "assets", "download", "--dir", tmpdir],
                input=(cover_line + "\n").encode(),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=60,
            )
            if p2.returncode != 0:
                return None
            files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
            return files[0] if files else None
        return await asyncio.to_thread(_fetch)
    except Exception as e:
        log.warning("download cover failed: %s", e)
        return None


async def send_result(msg, code: str):
    thinking = await msg.reply_text(f"🔍 正在搜索 <code>{_esc(code)}</code>…", parse_mode=ParseMode.HTML)
    try:
        result = await fetch_movie(code)
    except Exception:
        log.exception("fetch failed")
        await thinking.edit_text("⚠️ 搜索出错，稍后再试。")
        return
    if not result:
        await thinking.edit_text(f"❌ 没找到 <code>{_esc(code)}</code>", parse_mode=ParseMode.HTML)
        return
    d, magnets = result["detail"], result["magnets"]
    await thinking.delete()
    cover = d.get("cover_url")
    cover_path = await _download_cover(d.get("number") or "", cover) if cover else None
    cover_dir = os.path.dirname(cover_path) if cover_path else None
    if cover_path:
        try:
            with open(cover_path, "rb") as f:
                await msg.reply_photo(photo=f, caption=build_caption(d),
                                      parse_mode=ParseMode.HTML, has_spoiler=True)
        except Exception as e:
            log.warning("send cover failed: %s", e)
        finally:
            if cover_dir:
                shutil.rmtree(cover_dir, ignore_errors=True)
    await msg.reply_text(build_detail(d, magnets)[:4000], parse_mode=ParseMode.HTML,
                         disable_web_page_preview=True)


def _find_request(arg: str) -> dict | None:
    """按 id 或番号找未完结求片。"""
    arg = arg.strip().upper()
    req = None
    if arg.isdigit():
        req = db.get_request(int(arg))
        if req and req["status"] not in db.ACTIVE_STATUSES:
            req = None
    if not req:
        req = db.find_active_by_code(arg)
    return req


# ---------- 求片通知文案 ----------
def _user_mention(username: str, user_id: int) -> str:
    name = _esc(username or str(user_id))
    return f'<a href="tg://user?id={user_id}"><i>{name}</i></a>'


async def send_report(app: Application, req: dict, title: str, title_emoji: str,
                      status_line: str, cover_path: str | None = None) -> str:
    """发送统一风格的报告（spoiler 封面 + caption）。
    格式参考：
        🎊 自动入库报告

        🎬 番号：SONE-248
        📽️ 资源：SONE-248 中文标题
        👤 求片人：Wyatt
        ✅ 状态：已存入媒体库，请刷新观看
    返回 cover 的 TG file_id（供复用）。
    """
    code = _esc(req["code"])
    title_cn = _esc(req.get("title_cn") or "")
    resource = f"{code} {title_cn}".strip()
    text = (
        f"{title_emoji} {title}\n\n"
        f"🎬 番号：{code}\n"
        f"📽️ 资源：{resource}\n"
        f"👤 求片人：{_user_mention(req['username'], req['user_id'])}\n"
        f"{status_line}"
    )
    # 1) 优先用缓存的 cover_file_id
    if req.get("cover_file_id"):
        try:
            await app.bot.send_photo(
                chat_id=config.FEEDBACK_GROUP_ID, photo=req["cover_file_id"],
                caption=text, parse_mode=ParseMode.HTML, has_spoiler=True)
            return req["cover_file_id"]
        except Exception as e:
            log.warning("send report cached photo failed: %s", e)
    # 2) 本地封面文件
    if cover_path and os.path.isfile(cover_path):
        try:
            with open(cover_path, "rb") as f:
                sent = await app.bot.send_photo(
                    chat_id=config.FEEDBACK_GROUP_ID, photo=f,
                    caption=text, parse_mode=ParseMode.HTML, has_spoiler=True)
            return sent.photo[-1].file_id if sent.photo else ""
        except Exception as e:
            log.warning("send report photo failed: %s", e)
    # 3) 无封面，纯文字
    try:
        await app.bot.send_message(
            chat_id=config.FEEDBACK_GROUP_ID, text=text,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        log.warning("send report text failed: %s", e)
    return ""


async def post_to_group(app: Application, text: str, cover_path: str | None = None,
                        caption: str = "") -> str:
    """发到反馈群。返回 cover 的 TG file_id（供复用）。"""
    if not config.FEEDBACK_GROUP_ID:
        log.warning("FEEDBACK_GROUP_ID 未配置，跳过群通知")
        return ""
    file_id = ""
    try:
        if cover_path and os.path.isfile(cover_path):
            with open(cover_path, "rb") as f:
                sent = await app.bot.send_photo(
                    chat_id=config.FEEDBACK_GROUP_ID, photo=f,
                    caption=caption[:1000], parse_mode=ParseMode.HTML,
                    has_spoiler=True)
            file_id = sent.photo[-1].file_id if sent.photo else ""
        else:
            await app.bot.send_message(chat_id=config.FEEDBACK_GROUP_ID, text=text,
                                       parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
            return ""
        # 封面发出后，再发文字详情
        if text:
            await app.bot.send_message(chat_id=config.FEEDBACK_GROUP_ID, text=text,
                                       parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
    except Exception as e:
        log.warning("post to group failed: %s", e)
    return file_id


async def handle_request(msg, code: str, user_id: int, username: str, app: Application):
    """处理一次求片。"""
    code = code.strip().upper()
    thinking = await msg.reply_text(f"📩 正在处理求片 <code>{_esc(code)}</code>…", parse_mode=ParseMode.HTML)

    # 1) 已在 Emby？
    in_lib, lib_name = emby_client.is_in_library(code)
    if in_lib:
        await thinking.edit_text(
            f"✅ <code>{_esc(code)}</code> 已经在库了！\n{_esc(lib_name)}",
            parse_mode=ParseMode.HTML)
        return

    # 2) 已有人求过？
    dup = db.find_active_by_code(code)
    if dup:
        rid = dup["id"]
        # PikPak 离线中 → 显示 PikPak 进度
        if dup["status"] == "pikpak_queued" and dup.get("pikpak_task_id"):
            import asyncio as _aio2
            ok, task = await _aio2.get_running_loop().run_in_executor(
                None, pikpak_client.get_task, dup["pikpak_task_id"])
            prog = pikpak_client.task_progress_summary(task) if ok else "查询中…"
            await thinking.edit_text(
                f"📮 <code>{_esc(code)}</code> 已在 PikPak 离线中（#{rid}）\n{prog}",
                parse_mode=ParseMode.HTML)
            return
        # 正在处理 → 简单提示
        if rid in _download_tasks:
            await thinking.edit_text(
                f"📮 <code>{_esc(code)}</code> 正在处理中（#{rid}）…",
                parse_mode=ParseMode.HTML)
            return
        # pending 但没有下载任务在跑 → 自动重新触发（比如重启后被重置的）
        if (dup["status"] == "pending" and config.AUTO_DOWNLOAD):
            result = await fetch_movie(code)
            magnets = (result or {}).get("magnets") or []
            if magnets:
                await thinking.edit_text(
                    f"📮 <code>{_esc(code)}</code> 已在队列（#{rid}），重新加入 PikPak…",
                    parse_mode=ParseMode.HTML)
                start_download(app, rid, magnets, auto=True)
                return
        await thinking.edit_text(
            f"📮 <code>{_esc(code)}</code> 已经有人求过了（#{rid}），排队中…",
            parse_mode=ParseMode.HTML)
        return

    # 2.5) bb_boss 门禁：Emby 账号 + 每日次数 + 子弹（bot 管理员跳过）
    rule = None  # (是否白名单, 每日上限, 单次费用, 身份名, 是否免费)
    if not is_admin(user_id):
        import asyncio as _aio_bb
        loop_bb = _aio_bb.get_running_loop()
        status, buser = await loop_bb.run_in_executor(
            None, bbboss_client.get_user, user_id)
        if status == "api_error":
            await thinking.edit_text(
                "⚠️ 用户系统暂时连不上，稍后再试。",
                parse_mode=ParseMode.HTML)
            return
        if status == "no_user" or not bbboss_client.has_emby_account(buser):
            await thinking.edit_text(
                "❌ 求片需要先绑定步兵营地 Emby 账号。\n"
                "请先去 @bb_boss 完成绑定，再来求片！",
                parse_mode=ParseMode.HTML)
            return
        wl = bbboss_client.is_whitelist(buser)
        daily = config.REQ_DAILY_WHITELIST if wl else config.REQ_DAILY_NORMAL
        cost = config.REQ_COST_WHITELIST if wl else config.REQ_COST_NORMAL
        label = "白名单" if wl else "普通用户"
        n = db.count_user_today(user_id)
        if n >= daily:
            await thinking.edit_text(
                f"🚫 你今天求片已达上限（{label} {daily}次/天），明天再来吧！",
                parse_mode=ParseMode.HTML)
            return
        free = n < config.REQ_FREE_DAILY
        if not free:
            bullets = int((buser or {}).get("iv") or 0)
            if bullets < cost:
                await thinking.edit_text(
                    f"🪙 子弹不足！本次求片需要 {cost} 子弹，你只有 {bullets}。\n"
                    "去 @bb_boss 签到补充子弹吧！",
                    parse_mode=ParseMode.HTML)
                return
        rule = (wl, daily, cost, label, free)

    # 3) 取影片信息
    result = await fetch_movie(code)
    if not result:
        await thinking.edit_text(f"❌ 没找到 <code>{_esc(code)}</code>，检查番号是否正确。",
                                 parse_mode=ParseMode.HTML)
        return
    d = result["detail"]
    title_cn = translate(d.get("title") or "", src="ja")

    # 3.5) 扣子弹（每天前 N 次免费；bot 管理员免扣）
    cost_info = ""
    if rule:
        _, _, cost, _, free = rule
        if free:
            cost_info = "（今日免费）"
        else:
            import asyncio as _aio_dd
            ok, res = await _aio_dd.get_running_loop().run_in_executor(
                None, bbboss_client.deduct_bullets, user_id, cost)
            if not ok:
                await thinking.edit_text(
                    f"⚠️ 扣费失败（{_esc(str(res))}），本次求片未受理，请稍后再试。",
                    parse_mode=ParseMode.HTML)
                return
            cost_info = f"（已扣 {cost} 子弹，余额 {res}）"

    # 4) 入库
    rid = db.add_request(code, title_cn, user_id, username)

    # 5) 封面（群通知只发一条：等 PikPak 结果出来由 worker 统一发）
    cover = d.get("cover_url")
    cover_path = await _download_cover(code, cover) if cover else None

    await thinking.edit_text(
        f"📩 求片 <code>{_esc(code)}</code> 已登记（#{rid}）{cost_info}，入库后会通知你！",
        parse_mode=ParseMode.HTML)

    # 6) 自动下载：依次尝试最优磁力（中字优先）；无磁力也进 worker，由它发唯一的群通知
    if config.AUTO_DOWNLOAD:
        start_download(app, rid, result["magnets"], auto=True, cover_path=cover_path)
    elif cover_path:
        shutil.rmtree(os.path.dirname(cover_path), ignore_errors=True)


# ---------- handlers：用户 ----------
def _request_rules_text() -> str:
    return (
        "📜 <b>求片规则</b>\n"
        f"· 每天前 {config.REQ_FREE_DAILY} 次免费\n"
        f"· 之后：普通用户每次 {config.REQ_COST_NORMAL} 子弹，白名单每次 {config.REQ_COST_WHITELIST} 子弹\n"
        f"· 每日上限：普通 {config.REQ_DAILY_NORMAL} 次，白名单 {config.REQ_DAILY_WHITELIST} 次\n"
        "· 求片前需绑定步兵营地 Emby 账号\n"
        "· 已在库 / 已有人求过的不扣子弹"
    )


async def _build_start_panel(user_id: int, first_name: str) -> tuple[str, InlineKeyboardMarkup]:
    """组装 /start 面板（参考 bb_boss 用户面板风格）。"""
    name = _esc(first_name or "")
    if is_admin(user_id):
        text = (
            f"▎欢迎来到求片面板！{name}\n\n"
            f"· 🆔 TG ID | <code>{user_id}</code>\n"
            f"· 📊 身份 | 👮 管理员（求片不限次、免子弹）\n\n"
            f"{_request_rules_text()}\n\n"
            "<code>/s 番号</code> — 搜番\n"
            "<code>/q 番号</code> — 求片（私聊直接发番号也行）\n"
            "<code>/my</code> — 我的求片 · 📷 私聊发图 — 以图搜番"
        )
    else:
        status, buser = await asyncio.get_running_loop().run_in_executor(
            None, bbboss_client.get_user, user_id)
        if status == "ok" and buser:
            emby_name = buser.get("name")
            emby_line = f"📺 Emby 账号 | <code>{_esc(emby_name)}</code>" if emby_name else \
                "📺 Emby 账号 | ❌ 未绑定（先去 @bb_boss 绑定）"
            bullets = int(buser.get("iv") or 0)
            ident = "白名单" if bbboss_client.is_whitelist(buser) else "普通用户"
        elif status == "no_user":
            emby_line = "📺 Emby 账号 | ❌ 未绑定（先去 @bb_boss 绑定）"
            bullets = 0
            ident = "普通用户"
        else:
            emby_line = "📺 Emby 账号 | ⚠️ 用户系统连不上"
            bullets = 0
            ident = "普通用户"
        free_left = max(0, config.REQ_FREE_DAILY - db.count_user_today(user_id))
        text = (
            f"▎欢迎来到求片面板！{name}\n\n"
            f"· 🆔 TG ID | <code>{user_id}</code>\n"
            f"· {emy_line}\n"
            f"· 🍒 子弹 | {bullets}\n"
            f"· 📊 身份 | {ident}\n"
            f"· 🆓 今日免费剩余 | {free_left} 次\n\n"
            f"{_request_rules_text()}\n\n"
            "<code>/s 番号</code> — 搜番\n"
            "<code>/q 番号</code> — 求片（私聊直接发番号也行）\n"
            "<code>/my</code> — 我的求片 · 📷 私聊发图 — 以图搜番"
        )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新面板", callback_data="qp:refresh"),
         InlineKeyboardButton("❌ 关闭", callback_data="qp:close")],
    ])
    return text, kb


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("请私聊我使用 /start 面板。")
        return
    user = update.effective_user
    text, kb = await _build_start_panel(user.id, user.first_name)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=kb)


async def panel_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """求片面板按钮：刷新 / 关闭。"""
    query = update.callback_query
    await query.answer()
    action = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    if action == "close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return
    if action == "refresh":
        user = query.from_user
        text, kb = await _build_start_panel(user.id, user.first_name)
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
        except Exception:
            pass


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    is_adm = is_admin(update.effective_user.id)
    text = ("🎬 <b>Emby 求片 Bot</b>\n\n"
            "<b>用户</b>\n"
            "/s <code>番号</code> — 搜番\n"
            "/q <code>番号</code> — 求片\n"
            "私聊发番号 / 发图 — 求片 / 以图搜番\n"
            "/my — 我的求片记录\n\n"
            + _request_rules_text() + "\n\n")
    if is_adm:
        text += ("<b>管理员</b>\n"
                 "/pending — 未完结求片\n"
                 "/progress — PikPak离线进度\n"
                 "/retry <code>id或番号</code> — 重试加入PikPak\n"
                 "私聊发磁力链接 — 选求片加入PikPak（可覆盖）\n"
                 "/cancel <code>id或番号</code> — 取消求片\n"
                 "/done <code>id</code> — 手动标记入库\n"
                 "/stats — 求片统计\n")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if _rate_limited(update.effective_user.id):
        await update.message.reply_text("⏳ 太快了，歇几秒再搜。")
        return
    if not ctx.args:
        await update.message.reply_text("用法：<code>/s 番号</code>", parse_mode=ParseMode.HTML)
        return
    await send_result(update.message, " ".join(ctx.args))


async def cmd_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("用法：<code>/q 番号</code>\n例：<code>/q MIDA-497</code>",
                                        parse_mode=ParseMode.HTML)
        return
    await handle_request(update.message, ctx.args[0], update.effective_user.id,
                         update.effective_user.username or update.effective_user.first_name,
                         ctx.application)


async def bare_code_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """私聊直接发番号 → 求片；群里忽略（避免误触）。"""
    if update.effective_chat.type != "private":
        return
    text = (update.message.text or "").strip()
    if not CODE_RE.match(text):
        return
    await handle_request(update.message, text, update.effective_user.id,
                         update.effective_user.username or update.effective_user.first_name,
                         ctx.application)


async def photo_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    user_id = update.effective_user.id
    now = time.time()
    if now - _last_photo_query.get(user_id, 0) < 10:
        await update.message.reply_text("⏳ 以图搜番冷却中，歇几秒再试。")
        return
    _last_photo_query[user_id] = now
    msg = update.effective_message
    thinking = await msg.reply_text("🔍 以图搜番中…")
    tmpdir = tempfile.mkdtemp(prefix="jdbimg_")
    img_path = os.path.join(tmpdir, "query.jpg")
    try:
        tg_file = await msg.photo[-1].get_file()
        await tg_file.download_to_drive(img_path)
        matches = await jdb.reverse_search(img_path)
    except Exception:
        log.exception("reverse search failed")
        matches = []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not matches:
        await thinking.edit_text("❌ 没识别出来，换张更清晰的图试试。")
        return
    top = matches[0]
    if top["similarity"] >= 75:
        await thinking.delete()
        await send_result(msg, top["code"])
        return
    kb = [[InlineKeyboardButton(f"🎬 {m['code']}（{m['similarity']:.0f}%）",
                               callback_data=f"pick:{m['code']}")] for m in matches[:3]]
    kb.append([InlineKeyboardButton("❌ 都不是", callback_data="pick:")])
    await thinking.edit_text("🔍 识别到以下候选，点一个查看详情：",
                             reply_markup=InlineKeyboardMarkup(kb))


async def pick_candidate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    if not code:
        await query.edit_message_text("已取消。")
        return
    await send_result(query.message, code)


async def inline_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = (update.inline_query.query or "").strip()
    if len(query) < 3 or _rate_limited(update.effective_user.id):
        return
    result = await fetch_movie(query)
    if not result:
        return
    d = result["detail"]
    number = d.get("number") or query.upper()
    title_cn = translate(d.get("title") or "", src="ja")
    await update.inline_query.answer(
        [InlineQueryResultArticle(
            id=number, title=f"🎬 {number}", description=title_cn[:80],
            thumbnail_url=d.get("thumb_url"),
            input_message_content=InputTextMessageContent(
                build_detail(d, result["magnets"])[:4000],
                parse_mode=ParseMode.HTML, disable_web_page_preview=True))],
        cache_time=300)


# ---------- handlers：管理员 ----------
async def cmd_progress(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """管理员查看 PikPak 离线下载进度。"""
    if not is_admin(update.effective_user.id):
        return
    rows = db.list_by_status(("pikpak_queued",))
    if not rows:
        await update.message.reply_text("📭 PikPak 里没有进行中的离线任务。")
        return
    lines = ["📊 <b>PikPak 离线进度</b>"]
    import asyncio as _aio
    loop = _aio.get_running_loop()
    for r in rows:
        head = f"\n#{r['id']} <code>{_esc(r['code'])}</code>"
        task_id = (r.get("pikpak_task_id") or "").strip()
        if not task_id:
            lines.append(f"{head} — ⏳ 等待加入…")
            continue
        ok, task = await loop.run_in_executor(
            None, pikpak_client.get_task, task_id)
        if not ok:
            lines.append(f"{head} — ⚠️ 查不到任务：{_esc(str(task)[:60])}")
            continue
        phase = task.get("phase", "")
        if phase == "PHASE_TYPE_COMPLETE":
            db.update_status(r["id"], "pikpak_done")
            lines.append(f"{head} — ✅ 已下完")
            continue
        if phase == "PHASE_TYPE_ERROR":
            db.update_status(r["id"], "failed")
            lines.append(f"{head} — ❌ 失败：{_esc(task.get('message','')[:60])}")
            continue
        lines.append(f"{head} — ☁️ {pikpak_client.task_progress_summary(task)}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


MY_PAGE_SIZE = 10


def _my_page_text(user_id: int, page: int) -> tuple:
    """组装我的求片某一页。(文本, 按钮)"""
    total = db.count_user_requests(user_id)
    if total == 0:
        return "📭 你还没有求过片。", None
    pages = (total + MY_PAGE_SIZE - 1) // MY_PAGE_SIZE
    page = max(0, min(page, pages - 1))
    rows = db.get_user_requests(user_id, MY_PAGE_SIZE, page * MY_PAGE_SIZE)
    lines = [f"📋 <b>我的求片</b>（{page + 1}/{pages}，共 {total}）"]
    for r in rows:
        emoji = STATUS_EMOJI.get(r["status"], "•")
        line = f"\n#{r['id']} <code>{_esc(r['code'])}</code> {emoji}"
        if r["status"] == "pikpak_queued":
            line += " PikPak离线中"
        else:
            line += f" {_esc(r['status'])}"
        if r.get("title_cn"):
            line += f"\n<i>{_esc(r['title_cn'][:40])}</i>"
        lines.append(line)
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("◀ 上一页", callback_data=f"my:{page - 1}"))
    if page < pages - 1:
        btns.append(InlineKeyboardButton("下一页 ▶", callback_data=f"my:{page + 1}"))
    kb = InlineKeyboardMarkup([btns]) if btns else None
    return "\n".join(lines), kb


async def cmd_my(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """用户查看自己的求片记录和状态（翻页）。"""
    text, kb = _my_page_text(update.effective_user.id, 0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def my_page_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """我的求片翻页按钮。"""
    query = update.callback_query
    await query.answer()
    try:
        page = int((query.data or "my:0").split(":")[1])
    except (ValueError, IndexError):
        page = 0
    text, kb = _my_page_text(query.from_user.id, page)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except Exception:
        pass


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """管理员查看求片统计。"""
    if not is_admin(update.effective_user.id):
        return
    s = db.get_stats()
    lines = ["📊 <b>求片统计</b>",
             f"\n总求片：{s['total']}｜今日：{s['today']}｜近7天：{s['week']}"]
    if s["by_status"]:
        lines.append("\n<b>状态分布</b>")
        for st, n in sorted(s["by_status"].items(), key=lambda x: -x[1]):
            lines.append(f"{STATUS_EMOJI.get(st, '•')} {st}: {n}")
    if s["top"]:
        lines.append("\n<b>热门番号</b>")
        for code, n in s["top"]:
            lines.append(f"<code>{_esc(code)}</code> ×{n}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    rows = db.list_by_status()
    if not rows:
        await update.message.reply_text("📭 没有未完结的求片。")
        return
    lines = ["📋 <b>未完结求片</b>"]
    for r in rows:
        lines.append(
            f"\n#{r['id']} <code>{_esc(r['code'])}</code> [{_esc(r['status'])}]\n"
            f"<i>{_esc(r['title_cn'][:40])}</i> — {_esc(r['username'])}")
    lines.append("\n\n管理员私聊发磁力链接可认领下载。")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def magnet_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """管理员私聊发磁力 → 列出未完结求片供选择。"""
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return
    m = MAGNET_RE.search(update.message.text or "")
    if not m:
        return
    magnet = m.group(0)
    rows = db.list_by_status(("pending", "downloading", "uploading", "failed"))
    if not rows:
        await update.message.reply_text("📭 没有待处理的求片，这个磁力先记下了（未关联）。")
        return
    _pending_magnets[update.message.message_id] = magnet
    kb = [[InlineKeyboardButton(f"#{r['id']} {r['code']}",
                               callback_data=f"dl:{r['id']}:{update.message.message_id}")]
          for r in rows[:20]]
    await update.message.reply_text(
        "🧲 收到磁力，关联到哪个求片？",
        reply_markup=InlineKeyboardMarkup(kb))


async def dl_pick(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """管理员选定求片 → 加入 PikPak（可覆盖自动添加）。"""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) != 3:
        return
    rid, src_msg_id = int(parts[1]), int(parts[2])
    magnet = _pending_magnets.pop(src_msg_id, "")
    req = db.get_request(rid)
    if not magnet or not req:
        await query.edit_message_text("❌ 磁力或求片已失效。")
        return
    await query.edit_message_text(
        f"☁️ 加入 PikPak <code>{_esc(req['code'])}</code>（#{rid}）…\n稍候。",
        parse_mode=ParseMode.HTML)
    start_download(ctx.application, rid, [{"magnet": magnet, "label": "管理员手动提供"}],
                   notify_chat_id=query.message.chat_id, auto=False)


def start_download(app: Application, rid: int, magnets: list,
                   notify_chat_id: int | None = None,
                   auto: bool = False, cover_path: str | None = None):
    """启动下载任务；已有任务会被取消并替换。

    magnets: 磁力 dict 列表（已按中字优先/体积排序），依次尝试直到成功。
    手动单磁力传 [{"magnet": magnet, "label": "..."}]。
    cover_path: 求片时下载的封面，worker 发唯一群通知时用，用完即删。
    """
    old = _download_tasks.pop(rid, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(
        _download_worker(app, rid, magnets, notify_chat_id, auto, cover_path))
    _download_tasks[rid] = task
    task.add_done_callback(lambda t: _download_tasks.pop(rid, None))
    return task


def _magnet_info(m: dict) -> tuple[str, str]:
    """从磁力 dict 取 (magnet链接, 显示标签)。"""
    if "magnet" in m:  # 手动单磁力
        return m["magnet"], m.get("label", "")
    link = jdb.magnet_link(m)
    cnsub = "中字" if m.get("cnsub") else "无字幕"
    return link, f"{m.get('name') or ''} [{cnsub}·{jdb.format_size(m.get('size'))}]"


async def _pikpak_worker(app: Application, rid: int, magnets: list,
                         notify_chat_id: int | None, auto: bool,
                         cover_path: str | None = None):
    """PikPak 离线下载：取最优磁力 → 加入 PikPak → 发唯一的群通知。"""
    req = db.get_request(rid)
    if not req:
        _cleanup_cover(cover_path)
        return

    async def _notify_admin(text: str):
        if notify_chat_id:
            try:
                await app.bot.send_message(chat_id=notify_chat_id, text=text,
                                           parse_mode=ParseMode.HTML)
            except Exception:
                pass

    async def _report(title_emoji: str, status_line: str):
        """发本求片唯一的群通知（含封面），并缓存 cover file_id 供复用。"""
        file_id = await send_report(app, req, f"求片报告 #{rid}", title_emoji,
                                    status_line, cover_path)
        if file_id:
            db.set_cover_file_id(rid, file_id)
        _cleanup_cover(cover_path)

    if not magnets:
        await _report("⏳", f"⏳ 状态：暂无磁力源，等待管理员手动提供磁力")
        return

    magnet, label = _magnet_info(magnets[0])
    if not magnet:
        db.update_status(rid, "failed")
        await _report("❌", "❌ 状态：无可用磁力")
        return

    import asyncio as _aio
    loop = _aio.get_running_loop()
    ok, result = await loop.run_in_executor(
        None, pikpak_client.add_offline_task, magnet, req["code"])
    if not ok:
        db.update_status(rid, "failed", magnet=magnet)
        await _report("❌", f"❌ 状态：加入 PikPak 失败：{_esc(result[:120])}")
        await _notify_admin(f"❌ <b>#{rid}</b> 加入 PikPak 失败：{_esc(result[:120])}")
        return

    db.update_status(rid, "pikpak_queued", magnet=magnet, pikpak_task_id=result)
    log.info("#%d pikpak task %s: %s", rid, result, req["code"])
    await _report("☁️",
                  "☁️ 状态：已加入 PikPak 离线下载\n"
                  f"🧲 {_esc(label[:80])}\n"
                  "用 /progress 查看进度")
    await _notify_admin(f"☁️ #{rid} <code>{_esc(req['code'])}</code> 已加入 PikPak 离线")


def _cleanup_cover(cover_path: str | None):
    if cover_path:
        shutil.rmtree(os.path.dirname(cover_path), ignore_errors=True)


# 旧 aria2 下载流程已停用（改走 PikPak），保留空壳兼容
async def _download_worker(app: Application, rid: int, magnets: list,
                           notify_chat_id: int | None, auto: bool,
                           cover_path: str | None = None):
    await _pikpak_worker(app, rid, magnets, notify_chat_id, auto, cover_path)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """管理员取消求片：/cancel <id或番号>。"""
    if not is_admin(update.effective_user.id) or not ctx.args:
        await update.message.reply_text("用法：<code>/cancel id或番号</code>",
                                        parse_mode=ParseMode.HTML)
        return
    req = _find_request(ctx.args[0])
    if not req:
        await update.message.reply_text(f"❌ 没找到 { _esc(ctx.args[0]) } 的未完结求片。",
                                        parse_mode=ParseMode.HTML)
        return
    rid = req["id"]
    db.update_status(rid, "cancelled")
    # 取消进行中的任务
    task = _download_tasks.pop(rid, None)
    if task and not task.done():
        task.cancel()
    await update.message.reply_text(
        f"🚫 #{rid} <code>{_esc(req['code'])}</code> 已取消。"
        f"（PikPak 里的离线任务需手动删除）",
        parse_mode=ParseMode.HTML)


async def cmd_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """管理员重试：/retry <id或番号>，重新触发自动下载。"""
    if not is_admin(update.effective_user.id) or not ctx.args:
        await update.message.reply_text("用法：<code>/retry id或番号</code>",
                                        parse_mode=ParseMode.HTML)
        return
    req = _find_request(ctx.args[0])
    if not req or req["status"] not in ("pending", "failed"):
        await update.message.reply_text("❌ 没找到可重试的求片（仅 pending/failed 可重试）。",
                                        parse_mode=ParseMode.HTML)
        return
    rid = req["id"]
    if rid in _download_tasks:
        await update.message.reply_text(f"⏳ #{rid} 正在 PikPak 离线中，无需重试。",
                                        parse_mode=ParseMode.HTML)
        return
    thinking = await update.message.reply_text(f"🔍 正在找 <code>{_esc(req['code'])}</code> 的磁力…",
                                               parse_mode=ParseMode.HTML)
    result = await fetch_movie(req["code"])
    magnets = (result or {}).get("magnets") or []
    if not magnets:
        await thinking.edit_text(f"❌ #{rid} 暂无磁力源。", parse_mode=ParseMode.HTML)
        return
    await thinking.delete()
    start_download(ctx.application, rid, magnets, auto=True,
                   notify_chat_id=update.effective_chat.id)
    await update.message.reply_text(
        f"🔄 #{rid} <code>{_esc(req['code'])}</code> 已重新加入 PikPak。",
        parse_mode=ParseMode.HTML)


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not ctx.args:
        return
    try:
        rid = int(ctx.args[0])
    except ValueError:
        return
    req = db.get_request(rid)
    if not req:
        await update.message.reply_text("❌ 没有这个求片。")
        return
    db.update_status(rid, "fulfilled")
    await update.message.reply_text(f"✅ #{rid} 已手动标记入库。")


# ---------- 定时检查入库 ----------
_check_lock = asyncio.Lock()


async def check_library_once(app: Application):
    """检查一次入库，有入库的发群通知（定时任务和 webhook 共用）。

    用 try_fulfill 做原子状态变更：并发的检查任务里只有一个能真正把
    状态翻成 fulfilled 并发通知，杜绝重复入库通知。
    """
    async with _check_lock:
        rows = db.list_by_status()
        for r in rows:
            in_lib, lib_name = emby_client.is_in_library(r["code"])
            if in_lib:
                if db.try_fulfill(r["id"]):
                    await send_report(app, r, "自动入库报告", "🎊",
                                      "✅ 状态：已存入媒体库，请刷新观看")
                    log.info("request #%d fulfilled: %s", r["id"], r["code"])
            await asyncio.sleep(2)


async def check_library_loop(app: Application):
    await asyncio.sleep(60)  # 启动后等 1 分钟再开始
    while True:
        try:
            await check_library_once(app)
        except Exception:
            log.exception("check loop error")
        await asyncio.sleep(config.CHECK_INTERVAL_MIN * 60)


# ---------- Emby webhook（即时入库通知） ----------
async def _emby_webhook_handler(request):
    """Emby webhook 插件 POST 到这里，library.new 时立即检查入库。"""
    bot_app = request.app["bot_app"]
    if config.WEBHOOK_TOKEN:
        if request.query.get("token", "") != config.WEBHOOK_TOKEN:
            return web.Response(status=403, text="forbidden")
    try:
        data = await request.json()
    except Exception:
        data = {}
    event = str(data.get("Event") or data.get("event") or "")
    log.info("emby webhook event: %s", event)
    if "library" in event.lower() or not event:
        asyncio.create_task(check_library_once(bot_app))
    return web.Response(text="ok")


async def _start_webhook_server(bot_app: Application):
    if web is None:
        log.warning("aiohttp not installed, emby webhook disabled")
        return
    aapp = web.Application()
    aapp["bot_app"] = bot_app
    aapp.router.add_post("/emby-webhook", _emby_webhook_handler)
    runner = web.AppRunner(aapp)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.WEBHOOK_PORT).start()
    log.info("emby webhook listening on :%d/emby-webhook", config.WEBHOOK_PORT)


def _kill_stale_aria2():
    """杀掉上一个 bot 实例留下的 aria2c 孤儿进程（只杀命令行里带 bot 下载目录的），
    避免它们与恢复后新启动的 aria2 抢写同一个文件。"""
    import signal
    try:
        out = subprocess.run(["pgrep", "-af", "aria2c"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception as e:
        log.warning("pgrep aria2c failed: %s", e)
        return
    killed = 0
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_s, cmd = parts
        if config.DOWNLOAD_DIR not in cmd:
            continue
        try:
            os.kill(int(pid_s), signal.SIGTERM)
            killed += 1
        except (OSError, ValueError):
            pass
    if killed:
        log.info("killed %d stale aria2c process(es)", killed)


async def _post_init(app: Application):
    # 启动时恢复：旧 aria2 流程已停用，downloading/uploading 遗留状态退回 pending；
    # pikpak_queued 的任务在 PikPak 云端继续，不用管，/progress 可查。
    for r in db.list_by_status(("downloading", "uploading")):
        db.update_status(r["id"], "pending")
        log.info("reset stale %s #%d to pending (aria2 retired)", r["code"], r["id"])
    n_pikpak = len(db.list_by_status(("pikpak_queued",)))
    if n_pikpak:
        log.info("%d pikpak_queued task(s) continue in cloud", n_pikpak)
    admin_id = next(iter(config.ADMIN_IDS), None)

    # 左下角命令菜单：默认用户 + 管理员专属
    user_cmds = [
        BotCommand("start", "开始使用"),
        BotCommand("help", "帮助说明"),
        BotCommand("s", "搜番：简介/封面/磁力"),
        BotCommand("q", "求片"),
        BotCommand("my", "我的求片"),
    ]
    admin_cmds = user_cmds + [
        BotCommand("pending", "查看未完结求片"),
        BotCommand("progress", "查看下载进度"),
        BotCommand("retry", "重试下载（id或番号）"),
        BotCommand("cancel", "取消求片（id或番号）"),
        BotCommand("done", "手动标记入库"),
        BotCommand("stats", "求片统计"),
    ]
    try:
        await app.bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        for admin_id in config.ADMIN_IDS:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(admin_id))
    except Exception as e:
        log.warning("set commands failed: %s", e)
    asyncio.create_task(check_library_loop(app))
    asyncio.create_task(_start_webhook_server(app))


def main():
    if not config.BOT_TOKEN:
        raise SystemExit("请设置 BOT_TOKEN")
    db.init_db()
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    app = Application.builder().token(config.BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("s", cmd_search))
    app.add_handler(CommandHandler("q", cmd_request))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(dl_pick, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(pick_candidate, pattern=r"^pick:"))
    app.add_handler(CallbackQueryHandler(panel_callback, pattern=r"^qp:"))
    app.add_handler(CallbackQueryHandler(my_page_callback, pattern=r"^my:"))
    app.add_handler(MessageHandler(filters.PHOTO, photo_search))
    app.add_handler(InlineQueryHandler(inline_search))
    # 私聊磁力 / 私聊裸番号（放最后，避免抢命令）
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
                                   magnet_or_code))
    log.info("emby request bot started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


async def magnet_or_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """私聊文本分流：磁力→管理员下载；裸番号→求片。"""
    text = (update.message.text or "").strip()
    if MAGNET_RE.search(text):
        await magnet_handler(update, ctx)
        return
    if CODE_RE.match(text):
        await bare_code_handler(update, ctx)


if __name__ == "__main__":
    main()
