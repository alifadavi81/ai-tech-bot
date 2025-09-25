import os
import json
import logging
import html as _html
from pathlib import Path
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, types
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.filters import CommandStart

import httpx
import io

# ------------------ تنظیمات لاگر ------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------ بارگذاری env ------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN در env تعریف نشده")

# ------------------ Aiogram ------------------
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# ------------------ DB ------------------
DB_PATH = Path("db.json")
if not DB_PATH.exists():
    logger.warning("⚠️ فایل %s پیدا نشد؛ دیتابیس خالی استفاده می‌کنم.", DB_PATH)
    db = {"robotics": [], "iot": [], "py_libs": []}
else:
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            db = json.load(f) or {}
    except Exception as e:
        logger.exception("failed to load DB %s: %s", DB_PATH, e)
        db = {"robotics": [], "iot": [], "py_libs": []}

# ------------------ Helpers ------------------
EXT_RESULTS: dict[int, list[dict]] = {}
PROJECTS_PER_PAGE = 5

def _base_headers():
    return {
        "User-Agent": "TelegramBot/1.0",
        "Accept": "application/vnd.github.v3+json",
    }

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

async def _http_get_json(url: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=25, headers=_base_headers()) as c:
            r = await c.get(url, params=params)
            if r.status_code == 403 and "rate limit" in (r.text or "").lower():
                ra = r.headers.get("Retry-After")
                raise RuntimeError(f"GitHub rate limit exceeded. Retry after {ra or '?'}s")
            if r.status_code >= 400:
                raise RuntimeError(f"GitHub request failed (status {r.status_code}).")
            return r.json()
    except Exception as e:
        logger.exception("Github JSON fetch failed: %s", e)
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

async def safe_edit(msg: types.Message, text: str, reply_markup=None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except Exception:
        await msg.answer(text, reply_markup=reply_markup)

def safe_get_items_by_cat(cat: str):
    return db.get(cat, [])

def code_menu(cat: str, proj_id: str, proj: dict):
    kb = InlineKeyboardBuilder()
    if proj.get("codes"):
        for c in proj["codes"]:
            kb.button(
                text=f"📄 {c.get('filename','code')}",
                callback_data=f"code_{cat}_{proj_id}_{c.get('id')}"
            )
    if proj.get("zip_url"):
        kb.button(text="📦 دریافت زیپ کامل", callback_data=f"zip_{cat}_{proj_id}")
    kb.button(text="⬅️ برگشت", callback_data=f"back_{cat}")
    kb.button(text="🏠 خانه", callback_data="home")
    kb.adjust(1)
    return kb.as_markup()

def build_project_keyboard(cat: str, page: int = 0):
    kb = InlineKeyboardBuilder()
    items = safe_get_items_by_cat(cat)
    total = len(items)
    start = page * PROJECTS_PER_PAGE
    end = start + PROJECTS_PER_PAGE
    for proj in items[start:end]:
        kb.button(text=f"📌 {proj.get('title','پروژه')}", callback_data=f"proj_{cat}_{proj.get('id')}")
    # ناوبری صفحات
    nav_buttons = []
    if page > 0:
        nav_buttons.append(("⏮️ قبلی", f"proj_page_{cat}_{page-1}"))
    if end < total:
        nav_buttons.append(("⏭️ بعدی", f"proj_page_{cat}_{page+1}"))
    for text, cb in nav_buttons:
        kb.button(text=text, callback_data=cb)
    # همیشه خانه و برگشت
    kb.button(text="🏠 خانه", callback_data="home")
    kb.button(text="⬅️ برگشت", callback_data=f"back_{cat}")
    kb.adjust(2)
    return kb.as_markup()

# ------------------ GitHub Search ------------------
async def github_code_search(q: str, per_page=5):
    url = "https://api.github.com/search/code"
    params = {"q": q, "per_page": str(per_page)}
    data = await _http_get_json(url, params)
    results = []
    for item in data.get("items", []):
        repo = item.get("repository", {})
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

# ------------------ Handlers ------------------
@dp.message(CommandStart())
async def start(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 رباتیک", callback_data="cat_robotics")
    kb.button(text="🌐 اینترنت اشیا", callback_data="cat_iot")
    kb.button(text="📚 پایتون", callback_data="cat_py_libs")
    kb.button(text="🔍 جستجو GitHub", callback_data="search")
    kb.adjust(2)
    await msg.answer(
        "👋 <b>سلام! خوش اومدی</b>\n\n"
        "📂 یکی از گزینه‌های زیر رو انتخاب کن:",
        reply_markup=kb.as_markup()
    )

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
    q = msg.text.strip()
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
        doc = BufferedInputFile(code.encode("utf-8"), filename="snippet.py")
        await cb.message.answer_document(doc, caption=caption)
    await cb.answer()

@dp.callback_query(F.data.startswith("cat_"))
async def show_category(cb: CallbackQuery):
    cat = cb.data.split("_", 1)[1]
    items = safe_get_items_by_cat(cat)
    if not items:
        await cb.answer("⚠️ خالیه!", show_alert=True)
        return
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
            await cb.answer("❌ پروژه پیدا نشد.", show_alert=True)
            return

        title = proj.get("title", "(بدون عنوان)")
        desc = proj.get("description", "")
        boards = ", ".join(proj.get("boards", []) or [])
        parts = ", ".join(proj.get("parts", []) or [])

        txt = f"📌 <b>{_html.escape(title)}</b>\n\n"
        if desc:
            txt += f"📝 {_html.escape(desc)}\n\n"
        if boards:
            txt += f"⚙️ بردها: {boards}\n"
        if parts:
            txt += f"🛒 قطعات: {parts}\n"

        await safe_edit(cb.message, txt, reply_markup=code_menu(cat, proj_id, proj))
        await cb.answer()
    except Exception as e:
        logger.exception("project_detail failed: %s", e)
        await cb.answer("⚠️ خطا در باز کردن پروژه.", show_alert=True)

@dp.callback_query(F.data.startswith("code_"))
async def send_code(cb: CallbackQuery):
    try:
        _, cat, proj_id, code_id = cb.data.split("_", 3)
        items = safe_get_items_by_cat(cat)
        proj = next((p for p in items if str(p.get("id")) == proj_id), None)
        if not proj:
            await cb.answer("❌ پروژه پیدا نشد.", show_alert=True)
            return
        code = next((c for c in proj.get("codes", []) if str(c.get("id")) == code_id), None)
        if not code:
            await cb.answer("❌ کد یافت نشد.", show_alert=True)
            return
        raw_url = code.get("raw_url")
        if not raw_url:
            await cb.answer("⚠️ آدرس کد موجود نیست.", show_alert=True)
            return
        content = await fetch_text(raw_url)
        safe = _html.escape(content)
        if len(safe) < 3500:
            await cb.message.answer(f"📄 <b>{code.get('filename','code')}</b>\n\n<pre><code>{safe}</code></pre>")
        else:
            doc = BufferedInputFile(content.encode("utf-8"), filename=code.get("filename", "code.py"))
            await cb.message.answer_document(doc)
        await cb.answer()
    except Exception as e:
        logger.exception("send_code failed: %s", e)
        await cb.answer("⚠️ خطا در دریافت کد.", show_alert=True)

@dp.callback_query(F.data.startswith("zip_"))
async def send_zip(cb: CallbackQuery):
    try:
        _, cat, proj_id = cb.data.split("_", 2)
        items = safe_get_items_by_cat(cat)
        proj = next((p for p in items if str(p.get("id")) == proj_id), None)
        if not proj:
            await cb.answer("❌ پروژه پیدا نشد.", show_alert=True)
            return
        url = proj.get("zip_url")
        if not url:
            await cb.answer("⚠️ لینک زیپ موجود نیست.", show_alert=True)
            return
        async with httpx.AsyncClient() as c:
            r = await c.get(url)
            if r.status_code >= 400:
                await cb.answer("❌ دانلود زیپ ناموفق.", show_alert=True)
                return
            buf = io.BytesIO(r.content)
            await cb.message.answer_document(BufferedInputFile(buf.read(), filename="project.zip"), caption="✅ پروژه آماده‌ست 📦")
        await cb.answer()
    except Exception as e:
        logger.exception("send_zip failed: %s", e)
        await cb.answer("⚠️ خطا در ارسال زیپ.", show_alert=True)

# ------------------ Main ------------------
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
