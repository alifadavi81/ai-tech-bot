import os
import logging
import html as _html
import httpx
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ------------------ تنظیمات ------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # مثلا: https://your-service.onrender.com/webhook
PORT = int(os.getenv("PORT", "10000"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # اختیاری، برای کاهش Rate Limit

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN در env تنظیم نشده است.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-tech-bot")

dp = Dispatcher()
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# ------------------ حافظه ساده ------------------
USER_MODE = {}       # حالت کاربر (py یا None)
EXT_RESULTS = {}     # نتایج آخرین جستجو
MAX_TEXT_LEN = 4000  # محدودیت نمایش تلگرام

def reset_mode(uid: int):
    USER_MODE[uid] = None

# ------------------ ابزارهای کمکی ------------------
def _gh_headers():
    h = {"Accept": "application/vnd.github+json", "User-Agent": "ai-tech-bot/1.0"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h

async def _http_get_json(url, params=None, headers=None):
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

async def fetch_text(url):
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "ai-tech-bot/1.0"}) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text

def _to_raw_url(html_repo, path, branch):
    return f"{html_repo.replace('https://github.com', 'https://raw.githubusercontent.com')}/{branch}/{path}"

async def safe_edit(msg: Message, text: str, reply_markup=None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:
        await msg.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)

# ------------------ GitHub Search ------------------
async def github_code_search(q: str, per_page=5, page=1):
    url = "https://api.github.com/search/code"
    params = {"q": q, "per_page": str(per_page), "page": str(page)}
    data = await _http_get_json(url, params, headers=_gh_headers())
    results = []
    for item in data.get("items", []):
        repo = item.get("repository", {})
        html_repo = repo.get("html_url", "")
        default_branch = repo.get("default_branch") or "main"
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

# ------------------ Start & Menu ------------------
@dp.message(Command("start"))
async def start(msg: Message):
    reset_mode(msg.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 رباتیک", callback_data="cat_robotics")
    kb.button(text="🌐 اینترنت اشیا", callback_data="cat_iot")
    kb.button(text="🐍 پایتون (جستجو)", callback_data="py_home")
    kb.button(text="🔍 جستجو GitHub", callback_data="search")
    kb.adjust(2)
    await msg.answer(
        "👋 <b>سلام! خوش اومدی</b>\n\n"
        "📂 یکی از گزینه‌های زیر رو انتخاب کن:",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "home")
async def go_home(cb: CallbackQuery):
    reset_mode(cb.from_user.id)
    await start(cb.message)
    await cb.answer()

# ------------------ Python Mode ------------------
@dp.callback_query(F.data == "py_home")
async def py_home(cb: CallbackQuery):
    USER_MODE[cb.from_user.id] = "py"
    kb = InlineKeyboardBuilder()
    kb.button(text="🚪 خروج از حالت پایتون", callback_data="py_exit")
    kb.button(text="🏠 خانه", callback_data="home")
    kb.adjust(1)
    await safe_edit(
        cb.message,
        "🐍 <b>جستجوی پایتون</b>\n"
        "نام کتابخانه یا موضوع رو بفرست (مثال: <code>requests</code> یا <code>تلگرام bot</code>).",
        reply_markup=kb.as_markup()
    )
    await cb.answer()

@dp.callback_query(F.data == "py_exit")
async def py_exit(cb: CallbackQuery):
    reset_mode(cb.from_user.id)
    await safe_edit(cb.message, "✅ از حالت پایتون خارج شدی. از منوی اصلی انتخاب کن.")
    await cb.answer()

# ------------------ Global GitHub search ------------------
@dp.callback_query(F.data == "search")
async def do_search(cb: CallbackQuery):
    reset_mode(cb.from_user.id)
    await cb.answer()
    await cb.message.answer("🔍 چی میخوای جستجو کنم؟ (یه کلمه کلیدی بفرست)")

# ------------------ Messages ------------------
@dp.message()
async def handle_query(msg: Message):
    q = (msg.text or "").strip()
    if not q:
        return
    mode = USER_MODE.get(msg.from_user.id)

    if mode == "py":
        await msg.answer("⏳ در حال جستجوی پایتون (GitHub code)...")
        try:
            query = f'{q} language:python in:file'
            results = await github_code_search(query, per_page=5)
            if not results:
                query2 = f'{q} language:python filename:README in:file'
                results = await github_code_search(query2, per_page=5)
            if not results:
                await msg.answer("❌ چیزی پیدا نشد. یک کلیدواژه‌ی ساده‌تر امتحان کن.")
                return

            EXT_RESULTS[msg.from_user.id] = results
            kb = InlineKeyboardBuilder()
            for i, r in enumerate(results):
                kb.button(text=f"{r['name']} 📂 {r['repo']}", callback_data=f"ext_open_{i}")
            # اگر صفحه‌بندی خواستی بعداً می‌تونی ext_more_2 و ... بسازی
            kb.button(text="ℹ️ جستجوی بیشتر؟ دوباره عبارت بفرست", callback_data="py_more_info")
            kb.adjust(1)
            await msg.answer("📌 <b>نتایج پایتون:</b>", reply_markup=kb.as_markup())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                await msg.answer("⚠️ GitHub rate limit. اگر شد در env یک GITHUB_TOKEN ست کن.")
            else:
                await msg.answer(f"⚠️ خطای GitHub: {e}")
        except Exception as e:
            await msg.answer(f"⚠️ خطا در جستجوی پایتون: {e}")
        return

    # حالت عادی
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
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            await msg.answer("⚠️ GitHub rate limit. اگر شد در env یک GITHUB_TOKEN ست کن.")
        else:
            await msg.answer(f"⚠️ خطای GitHub: {e}")
    except Exception as e:
        await msg.answer(f"⚠️ خطا: {e}")

# ------------------ Open external code ------------------
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
    if len(caption) + len(safe) < MAX_TEXT_LEN:
        await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>\n\n{caption}")
    else:
        doc = BufferedInputFile(code.encode("utf-8"), filename="snippet.py")
        await cb.message.answer_document(doc, caption=caption)
    await cb.answer()

# دکمه راهنمای «جستجوی بیشتر»
@dp.callback_query(F.data == "py_more_info")
async def py_more_info(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("🔎 برای نتایج بیشتر، عبارت جدید یا دقیق‌تر بفرست (مثلاً: <code>fastapi language:python in:file</code>).")

# ------------------ Webhook با aiohttp ------------------
async def on_startup(app: web.Application):
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL)
            logger.info(f"✅ Webhook set: {WEBHOOK_URL}")
        except Exception as e:
            logger.exception(f"Webhook set failed: {e}")
    else:
        logger.warning("WEBHOOK_URL تعریف نشده. لطفاً در env ست کن (https://<render>.onrender.com/webhook)")

async def on_shutdown(app: web.Application):
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("🧹 Webhook deleted")
    finally:
        await bot.session.close()
        logger.info("🧹 Bot session closed")

async def health_handler(request: web.Request):
    return web.Response(text="OK")

def main():
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    app = web.Application()
    app.router.add_get("/", health_handler)               # مسیر سلامت
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    web.run_app(main(), host="0.0.0.0", port=PORT)
