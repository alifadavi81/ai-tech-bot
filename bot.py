# bot.py — aiogram 3.7 + aiohttp webhook (Render Web Service) + GitHub search (422-safe)

import os
import re
import io
import json
import logging
import html as _html
from typing import Dict, List, Any

import httpx
from aiohttp import web
from aiogram import Dispatcher, F
from aiogram.client.bot import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

# ======================= Logging =======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ai-tech-bot")

# ======================= ENV =======================
def _require_env_any(*names: str) -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    raise RuntimeError(f"❌ یکی از این متغیرها باید ست شود: {', '.join(names)}")

BOT_TOKEN = _require_env_any("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret123")

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "projects.json")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# ======================= Aiogram Core =======================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ======================= DB =======================
if not os.path.exists(DB_PATH):
    logger.warning("⚠️ فایل %s پیدا نشد؛ از دیتابیس خالی استفاده می‌کنم.", DB_PATH)
    db: Dict[str, Any] = {"robotics": [], "iot": [], "py_libs": []}
else:
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f) or {"robotics": [], "iot": [], "py_libs": []}
    except Exception as e:
        logger.exception("failed to load DB %s: %s", DB_PATH, e)
        db = {"robotics": [], "iot": [], "py_libs": []}

def safe_get_items_by_cat(cat: str):
    return db.get(cat, [])

# ======================= GitHub helpers =======================
def _base_headers() -> Dict[str, str]:
    h = {
        "User-Agent": "ai-tech-bot/1.0 (+https://github.com/)",
        "Accept": "application/vnd.github.text-match+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h

async def _http_get_json(url: str, params: Dict[str, str] | None = None) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=25, headers=_base_headers()) as c:
            r = await c.get(url, params=params)
            if r.status_code >= 400:
                logger.error("GitHub API error %s: %s | q=%s", r.status_code, r.text[:400], (params or {}).get("q", ""))
                raise RuntimeError(f"GitHub request failed (status {r.status_code}).")
            return r.json()
    except Exception as e:
        logger.exception("Github JSON fetch failed: %s", e)
        raise RuntimeError("GitHub request failed.")

async def fetch_text(url: str, timeout: int = 25) -> str:
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_base_headers()) as c:
            r = await c.get(url)
            if r.status_code >= 400:
                raise RuntimeError(f"Download failed (status {r.status_code}).")
            return r.text
    except Exception as e:
        logger.exception("fetch_text error: %s", e)
        raise

def _to_raw_url(repo_html_url: str, path: str, default_branch: str | None = None) -> str:
    if not repo_html_url or not path:
        return ""
    if default_branch:
        repo_part = repo_html_url.rstrip("/").replace("https://github.com/", "")
        return f"https://raw.githubusercontent.com/{repo_part}/{default_branch}/{path.lstrip('/')}"
    if "/blob/" in repo_html_url:
        return repo_html_url.replace("https://github.com/", "https://raw.githubusercontent.com/").replace("/blob/", "/")
    repo_part = repo_html_url.rstrip("/").replace("https://github.com/", "")
    return f"https://raw.githubusercontent.com/{repo_part}/HEAD/{path.lstrip('/')}"

async def github_code_search(q: str, per_page=5) -> List[Dict[str, str]]:
    # ساده و سازگار با پارسر، بدون پرانتزهای پیچیده
    url = "https://api.github.com/search/code"
    params = {"q": q.strip() or "arduino", "per_page": str(max(1, min(int(per_page or 5), 10)))}
    data = await _http_get_json(url, params)
    results: List[Dict[str, str]] = []
    for item in data.get("items", []):
        repo = item.get("repository", {}) or {}
        html_repo = repo.get("html_url", "")
        default_branch = repo.get("default_branch")
        path = item.get("path")
        raw_url = _to_raw_url(html_repo, path, default_branch)
        results.append({
            "name": item.get("name"),
            "path": path,
            "repo": repo.get("full_name"),
            "html_url": item.get("html_url"),
            "raw_url": raw_url,
        })
    return results

# ======================= UI Helpers =======================
EXT_RESULTS: dict[int, list[dict]] = {}
PROJECTS_PER_PAGE = 5

async def safe_edit(msg: Message, text: str, reply_markup=None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await msg.answer(text, reply_markup=reply_markup)

def build_project_keyboard(cat: str, page: int = 0):
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    kb = InlineKeyboardBuilder()
    items = safe_get_items_by_cat(cat)
    total = len(items)
    start = page * PROJECTS_PER_PAGE
    end = start + PROJECTS_PER_PAGE
    for proj in items[start:end]:
        kb.button(text=f"📌 {proj.get('title','پروژه')}", callback_data=f"proj_{cat}_{proj.get('id')}")
    if page > 0:
        kb.button(text="⏮️ قبلی", callback_data=f"proj_page_{cat}_{page-1}")
    if end < total:
        kb.button(text="⏭️ بعدی", callback_data=f"proj_page_{cat}_{page+1}")
    kb.button(text="🏠 خانه", callback_data="home")
    kb.button(text="⬅️ برگشت", callback_data=f"back_{cat}")
    kb.adjust(2)
    return kb.as_markup()

def code_menu(cat: str, proj_id: str, proj: dict):
    kb = InlineKeyboardBuilder()
    if proj.get("codes"):
        for c in proj["codes"]:
            kb.button(text=f"📄 {c.get('filename','code')}", callback_data=f"code_{cat}_{proj_id}_{c.get('id')}")
    if proj.get("zip_url"):
        kb.button(text="📦 دریافت زیپ کامل", callback_data=f"zip_{cat}_{proj_id}")
    kb.button(text="⬅️ برگشت", callback_data=f"back_{cat}")
    kb.button(text="🏠 خانه", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()

# ======================= Handlers =======================
@dp.message(CommandStart())
async def start(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 رباتیک", callback_data="cat_robotics")
    kb.button(text="🌐 اینترنت اشیا", callback_data="cat_iot")
    kb.button(text="📚 کتابخانه‌های پایتون", callback_data="cat_py_libs")
    kb.button(text="🔍 جستجو GitHub", callback_data="search")
    kb.adjust(2)
    await msg.answer("👋 <b>سلام! خوش اومدی</b>\n\n📂 یکی از گزینه‌های زیر رو انتخاب کن:", reply_markup=kb.as_markup())

@dp.callback_query(F.data == "home")
async def go_home(cb: CallbackQuery):
    await start(cb.message)
    await cb.answer()

@dp.callback_query(F.data == "search")
async def do_search(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("🔍 چی میخوای جستجو کنم؟ (یه کلمه کلیدی بفرست)")

@dp.message()
async def handle_query(msg: Message):
    q = (msg.text or "").strip()
    if not q:
        await msg.answer("یک عبارت برای جستجو بفرست.")
        return
    await msg.answer("⏳ در حال جستجو روی GitHub...")
    try:
        results = await github_code_search(q, per_page=5)
        if not results:
            await msg.answer("❌ چیزی پیدا نشد.")
            return
        EXT_RESULTS[msg.from_user.id] = results
        kb = InlineKeyboardBuilder()
        for i, r in enumerate(results):
            kb.button(text=f"{r['name']} 📂 {r['repo']}", callback_data=f"ext_open_{i}")
        kb.adjust(1)
        await msg.answer("📌 <b>نتایج جستجو:</b>", reply_markup=kb.as_markup())
    except Exception as e:
        await msg.answer(f"⚠️ خطا: {e}")

@dp.callback_query(F.data.startswith("ext_open_"))
async def ext_open(cb: CallbackQuery):
    try:
        idx = int(cb.data.split("_", 2)[2])
    except Exception:
        await cb.answer("نامعتبر", show_alert=True)
        return
    items = EXT_RESULTS.get(cb.from_user.id) or []
    if idx < 0 or idx >= len(items):
        await cb.answer("⏰ منقضی شده", show_alert=True)
        return
    item = items[idx]
    try:
        code = await fetch_text(item["raw_url"])
    except Exception:
        await cb.message.answer("❌ دانلود کد ناموفق بود.")
        await cb.answer()
        return
    caption = f"🔗 <a href='{item['html_url']}'>مشاهده در GitHub</a>\n⚠️ لایسنس رو چک کن."
    safe = _html.escape(code)
    if len(caption) + len(safe) < 3500:
        await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>\n\n{caption}")
    else:
        doc = BufferedInputFile(code.encode("utf-8"), filename=item.get("name") or "snippet.txt")
        await cb.message.answer_document(doc, caption=caption)
    await cb.answer()

@dp.callback_query(F.data.startswith("cat_"))
async def show_category(cb: CallbackQuery):
    cat = cb.data.split("_", 1)[1]
    items = safe_get_items_by_cat(cat)
    if not items:
        await cb.answer("⚠️ خالیه!", show_alert=True); return
    kb = build_project_keyboard(cat, page=0)
    await safe_edit(cb.message, f"📂 <b>دسته: {cat}</b>", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("proj_page_"))
async def change_project_page(cb: CallbackQuery):
    try:
        _, _, cat, page_str = cb.data.split("_")
        page = int(page_str)
        kb = build_project_keyboard(cat, page)
        await safe_edit(cb.message, f"📂 <b>دسته: {cat}</b>", reply_markup=kb)
        await cb.answer()
    except Exception as e:
        logger.exception("change_project_page failed: %s", e)
        await cb.answer("⚠️ خطا در تغییر صفحه.", show_alert=True)

@dp.callback_query(F.data.startswith("back_"))
async def go_back(cb: CallbackQuery):
    cat = cb.data.split("_", 1)[1]
    kb = build_project_keyboard(cat, page=0)
    await safe_edit(cb.message, f"📂 <b>دسته: {cat}</b>", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("proj_"))
async def project_detail(cb: CallbackQuery):
    try:
        _, cat, proj_id = cb.data.split("_", 2)
        items = safe_get_items_by_cat(cat)
        proj = next((p for p in items if str(p.get("id")) == proj_id), None)
        if not proj:
            await cb.answer("❌ پروژه پیدا نشد.", show_alert=True); return
        title = proj.get("title", "(بدون عنوان)")
        desc = proj.get("description", "")
        boards = ", ".join(proj.get("boards", []) or [])
        parts = ", ".join(proj.get("parts", []) or [])
        txt = f"📌 <b>{_html.escape(title)}</b>\n\n"
        if desc:   txt += f"📝 {_html.escape(desc)}\n\n"
        if boards: txt += f"⚙️ بردها: {boards}\n"
        if parts:  txt += f"🛒 قطعات: {parts}\n"
        await safe_edit(cb.message, txt, reply_markup=code_menu(cat, proj_id, proj))
        await cb.answer()
    except Exception as e:
        logger.exception("project_detail failed: %s", e)
        await cb.answer("⚠️ خطا در باز کردن پروژه.", show_alert=True)

# ======================= Webhook lifecycle =======================
async def on_startup(app: web.Application):
    if PUBLIC_URL:
        target = f"{PUBLIC_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(target, secret_token=WEBHOOK_SECRET)
        logger.info("✅ Webhook set: %s", target)
    else:
        logger.warning("⚠️ PUBLIC_URL تعریف نشده؛ وبهوک ست نشد.")

async def on_shutdown(app: web.Application):
    try:
        await bot.delete_webhook()
        logger.info("🧹 Webhook deleted")
    except Exception as e:
        logger.warning("Webhook delete failed: %s", e)
    try:
        await bot.session.close()
        logger.info("🧹 Bot session closed")
    except Exception as e:
        logger.warning("Bot session close failed: %s", e)

def build_app():
    app = web.Application()

    async def root_get(_request: web.Request):
        return web.Response(text="OK")

    app.router.add_get("/", root_get)

    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

# ======================= Run =======================
if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=PORT)
