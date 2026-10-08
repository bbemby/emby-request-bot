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
import downloader
import drive
import emby_client
import javdb_client as jdb
from config import config
from translator import translate

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("emby_request_bot")

CODE_RE = re.compile(r"^[A-Z0-9]+-[0-9]+$", re.I)
MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]+", re.I)

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
    return f'<a href="tg://user?id={user_id}">{name}</a>'


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
        # pending 但没有下载任务在跑 → 自动重新触发（比如重启后被重置的）
        if (dup["status"] == "pending" and rid not in _download_tasks
                and config.AUTO_DOWNLOAD):
            result = await fetch_movie(code)
            magnets = (result or {}).get("magnets") or []
            if magnets:
                await thinking.edit_text(
                    f"📮 <code>{_esc(code)}</code> 已在队列（#{rid}），重新开始下载…",
                    parse_mode=ParseMode.HTML)
                start_download(app, rid, magnets, auto=True)
                return
        await thinking.edit_text(
            f"📮 <code>{_esc(code)}</code> 已经有人求过了（#{rid}），排队中…",
            parse_mode=ParseMode.HTML)
        return

    # 3) 取影片信息
    result = await fetch_movie(code)
    if not result:
        await thinking.edit_text(f"❌ 没找到 <code>{_esc(code)}</code>，检查番号是否正确。",
                                 parse_mode=ParseMode.HTML)
        return
    d = result["detail"]
    title_cn = translate(d.get("title") or "", src="ja")

    # 4) 入库
    rid = db.add_request(code, title_cn, user_id, username)

    # 5) 封面 + 群通知
    cover = d.get("cover_url")
    cover_path = await _download_cover(code, cover) if cover else None
    caption = f"📩 <b>新求片 #{rid}</b>\n👤 {_user_mention(username, user_id)}\n🎬 <b>{_esc(code)}</b>\n<i>{_esc(title_cn)}</i>"
    text = (f"📩 <b>新求片 #{rid}</b>\n👤 {_user_mention(username, user_id)}\n"
            f"🎬 <b>{_esc(code)}</b>\n<i>{_esc(title_cn)}</i>\n"
            f"🕐 {time.strftime('%Y-%m-%d %H:%M')}")
    file_id = await post_to_group(app, text, cover_path, caption)
    if file_id:
        db.set_cover_file_id(rid, file_id)
    if cover_path:
        shutil.rmtree(os.path.dirname(cover_path), ignore_errors=True)

    await thinking.edit_text(
        f"📩 求片 <code>{_esc(code)}</code> 已登记（#{rid}），入库后会通知你！",
        parse_mode=ParseMode.HTML)

    # 6) 自动下载：依次尝试最优磁力（中字优先）
    magnets = result["magnets"]
    if config.AUTO_DOWNLOAD and magnets:
        start_download(app, rid, magnets, auto=True)
    elif not magnets:
        await post_to_group(
            app, f"⏳ <b>#{rid}</b> <code>{_esc(code)}</code> 暂无磁力源，等待管理员手动提供磁力。")


# ---------- handlers：用户 ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Emby 求片 Bot</b>\n\n"
        "<code>/s 番号</code> — 搜番看简介/封面/磁力\n"
        "<code>/q 番号</code> — 求片（私聊直接发番号也行）\n"
        "📷 私聊发图 — 以图搜番\n\n"
        "求片后入库会自动通知！",
        parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    is_adm = is_admin(update.effective_user.id)
    text = ("🎬 <b>Emby 求片 Bot</b>\n\n"
            "<b>用户</b>\n"
            "/s <code>番号</code> — 搜番\n"
            "/q <code>番号</code> — 求片\n"
            "私聊发番号 / 发图 — 求片 / 以图搜番\n\n")
    if is_adm:
        text += ("<b>管理员</b>\n"
                 "/pending — 未完结求片\n"
                 "/progress — 下载进度\n"
                 "/retry <code>id或番号</code> — 重试下载\n"
                 "私聊发磁力链接 — 选求片下载（可覆盖自动下载）\n"
                 "/cancel <code>id或番号</code> — 取消求片\n"
                 "/done <code>id</code> — 手动标记入库\n")
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
    """管理员查看下载进度。"""
    if not is_admin(update.effective_user.id):
        return
    rows = db.list_by_status(("downloading", "uploading"))
    if not rows:
        await update.message.reply_text("📭 没有进行中的下载。")
        return
    lines = ["📊 <b>下载进度</b>"]
    for r in rows:
        p = downloader.get_progress(r["id"])
        head = f"\n#{r['id']} <code>{_esc(r['code'])}</code>"
        if not p:
            # 没有进度数据：可能是任务刚启动，或僵尸任务（自愈）
            if r["id"] not in _download_tasks:
                db.update_status(r["id"], "pending")
                lines.append(f"{head} — ⚠️ 任务已失效，退回待处理")
            else:
                lines.append(f"{head} — ⏳ 准备中…")
            continue
        # 已耗时
        elapsed = ""
        if p.get("started"):
            mins = int((time.time() - p["started"]) / 60)
            elapsed = f" · 已用{mins}分钟" if mins else " · 刚开始"
        if p["phase"] == "uploading":
            lines.append(f"{head} — 📤 上传 Drive 中…{elapsed}")
            continue
        if p["phase"] == "metadata":
            lines.append(f"{head} — 🔍 获取种子元数据…{elapsed}")
            continue
        total = p["total"] or 1
        pct = min(100.0, p["downloaded"] / total * 100)
        bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        speed = p["speed"] / 1024 / 1024
        lines.append(
            f"{head}\n[{bar}] {pct:.0f}%\n"
            f"{p['downloaded'] / 1e9:.2f}GB / {total / 1e9:.2f}GB · {speed:.1f}MB/s")
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
    """管理员选定求片 → 开始下载（可覆盖自动下载）。"""
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
        f"⬇️ 开始下载 <code>{_esc(req['code'])}</code>（#{rid}）…\n只下最大视频文件，稍候。",
        parse_mode=ParseMode.HTML)
    start_download(ctx.application, rid, [{"magnet": magnet, "label": "管理员手动提供"}],
                   notify_chat_id=query.message.chat_id, auto=False)


def start_download(app: Application, rid: int, magnets: list,
                   notify_chat_id: int | None = None,
                   auto: bool = False):
    """启动下载任务；已有任务会被取消并替换。

    magnets: 磁力 dict 列表（已按中字优先/体积排序），依次尝试直到成功。
    手动单磁力传 [{"magnet": magnet, "label": "..."}]。
    """
    old = _download_tasks.pop(rid, None)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(
        _download_worker(app, rid, magnets, notify_chat_id, auto))
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


async def _download_worker(app: Application, rid: int, magnets: list,
                           notify_chat_id: int | None, auto: bool):
    """下载 → 传 Drive → 群通知，全流程后台任务。多磁力依次重试。"""
    req = db.get_request(rid)
    if not req or not magnets:
        return

    async def _notify_admin(text: str):
        if notify_chat_id:
            try:
                await app.bot.send_message(chat_id=notify_chat_id, text=text,
                                           parse_mode=ParseMode.HTML)
            except Exception:
                pass

    src = "🤖 自动" if auto else "🛠️ 管理员"
    await post_to_group(
        app, f"⬇️ <b>开始下载 #{rid}</b> {src}\n🎬 <code>{_esc(req['code'])}</code>\n"
             f"共 {len(magnets)} 个磁力源，依次尝试…")

    local_path, info, ok_magnet = None, "", ""
    dest_dir = os.path.join(config.DOWNLOAD_DIR, req["code"])
    for i, m in enumerate(magnets):
        magnet, label = _magnet_info(m)
        if not magnet:
            continue
        db.update_status(rid, "downloading", magnet=magnet)
        log.info("#%d trying magnet %d/%d: %s", rid, i + 1, len(magnets), label[:60])
        try:
            local_path, info = await downloader.download_largest_video(magnet, dest_dir, rid=rid)
        except asyncio.CancelledError:
            db.update_status(rid, "pending", magnet="")
            downloader.clear_progress(rid)
            await post_to_group(app, f"🔄 <b>#{rid}</b> 下载被新的磁力替换。")
            raise
        downloader.clear_progress(rid)
        if local_path:
            ok_magnet = magnet
            break
        # 这个磁力失败，试下一个
        msg = (f"⚠️ <b>#{rid}</b> 磁力 {i + 1}/{len(magnets)} 失败：{_esc(info[:100])}\n"
               f"尝试下一个…")
        await post_to_group(app, msg)
        await _notify_admin(msg)

    if not local_path:
        db.update_status(rid, "pending", magnet="")
        msg = (f"❌ <b>#{rid}</b> <code>{_esc(req['code'])}</code> 所有磁力都失败了\n"
               f"已退回待处理，管理员可手动提供磁力。")
        await post_to_group(app, msg)
        await _notify_admin(msg)
        return

    # 上传 Drive
    db.update_status(rid, "uploading", magnet=ok_magnet)
    downloader.set_phase(rid, "uploading")
    await _notify_admin(f"📤 <b>#{rid}</b> 下载完成，正在上传 Drive…\n{_esc(info)}")
    ok, drive_info = await drive.upload_to_drive(local_path)
    downloader.clear_progress(rid)
    if not ok:
        db.update_status(rid, "failed")
        msg = f"❌ <b>#{rid}</b> Drive 上传失败：{_esc(drive_info)}"
        await post_to_group(app, msg)
        await _notify_admin(msg)
        return
    db.update_status(rid, "uploaded", drive_path=drive_info)

    # 群通知（复用封面 file_id）
    caption = (f"📥 <b>已上传待入库 #{rid}</b>\n👤 {_user_mention(req['username'], req['user_id'])}\n"
               f"🎬 <b>{_esc(req['code'])}</b>\n<i>{_esc(req['title_cn'])}</i>")
    if req.get("cover_file_id"):
        try:
            await app.bot.send_photo(chat_id=config.FEEDBACK_GROUP_ID,
                                     photo=req["cover_file_id"], caption=caption,
                                     parse_mode=ParseMode.HTML, has_spoiler=True)
        except Exception as e:
            log.warning("send group photo failed: %s", e)
            await post_to_group(app, caption)
    else:
        await post_to_group(app, caption)
    await _notify_admin(f"✅ #{rid} 处理完成：{_esc(drive_info)}")


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
    # 取消进行中的下载任务
    task = _download_tasks.pop(rid, None)
    if task and not task.done():
        task.cancel()
    downloader.clear_progress(rid)
    await update.message.reply_text(
        f"🚫 #{rid} <code>{_esc(req['code'])}</code> 已取消。",
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
        await update.message.reply_text(f"⏳ #{rid} 正在下载中，无需重试。",
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
        f"🔄 #{rid} <code>{_esc(req['code'])}</code> 已重新开始下载。",
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
async def check_library_loop(app: Application):
    await asyncio.sleep(60)  # 启动后等 1 分钟再开始
    while True:
        try:
            rows = db.list_by_status()
            for r in rows:
                in_lib, lib_name = emby_client.is_in_library(r["code"])
                if in_lib:
                    db.update_status(r["id"], "fulfilled")
                    caption = (f"✅ <b>已入库 #{r['id']}</b>\n👤 {_user_mention(r['username'], r['user_id'])}\n"
                               f"🎬 <b>{_esc(r['code'])}</b>\n<i>{_esc(r['title_cn'])}</i>\n"
                               f"📚 {_esc(lib_name[:60])}")
                    if r.get("cover_file_id"):
                        try:
                            await app.bot.send_photo(
                                chat_id=config.FEEDBACK_GROUP_ID, photo=r["cover_file_id"],
                                caption=caption, parse_mode=ParseMode.HTML, has_spoiler=True)
                            continue
                        except Exception:
                            pass
                    await post_to_group(app, caption)
                    log.info("request #%d fulfilled: %s", r["id"], r["code"])
                await asyncio.sleep(2)
        except Exception:
            log.exception("check loop error")
        await asyncio.sleep(config.CHECK_INTERVAL_MIN * 60)


async def _post_init(app: Application):
    # 启动时恢复：上次异常中断的 downloading/uploading 重置为 pending
    #（重启后内存中的任务和进度都丢了，DB 状态会变成僵尸）
    for r in db.list_by_status(("downloading", "uploading")):
        db.update_status(r["id"], "pending")
        log.info("reset stale %s #%d to pending", r["code"], r["id"])

    # 左下角命令菜单：默认用户 + 管理员专属
    user_cmds = [
        BotCommand("start", "开始使用"),
        BotCommand("help", "帮助说明"),
        BotCommand("s", "搜番：简介/封面/磁力"),
        BotCommand("q", "求片"),
    ]
    admin_cmds = user_cmds + [
        BotCommand("pending", "查看未完结求片"),
        BotCommand("progress", "查看下载进度"),
        BotCommand("retry", "重试下载（id或番号）"),
        BotCommand("cancel", "取消求片（id或番号）"),
        BotCommand("done", "手动标记入库"),
    ]
    try:
        await app.bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
        for admin_id in config.ADMIN_IDS:
            await app.bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(admin_id))
    except Exception as e:
        log.warning("set commands failed: %s", e)
    asyncio.create_task(check_library_loop(app))


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
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("progress", cmd_progress))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CallbackQueryHandler(dl_pick, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(pick_candidate, pattern=r"^pick:"))
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
