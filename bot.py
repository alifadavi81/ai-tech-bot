import os
import logging
import asyncio
import httpx
import html as _html
from dotenv import load_dotenv

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, BufferedInputFile, Update
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ------------------ تنظیمات ------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}" if BASE_URL else None
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

assert BOT_TOKEN, "BOT_TOKEN env var is required"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ai-tech-bot")

dp = Dispatcher()
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

# ------------------ حافظه ساده ------------------
USER_MODE = {}      # حالت کاربر (py یا None)
EXT_RESULTS = {}    # نتایج آخرین جستجو
MAX_TEXT_LEN = 4000 # محدودیت تلگرام
MAX_RESULTS_CACHE = 10  # برای جلوگیری از رشد بی‌نهایت حافظه

def reset_mode(uid: int):
    USER_MODE[uid] = None

def _cache_results(uid: int, results):
    # نگهداری نتایج برای کاربر و محدود کردن اندازه
    EXT_RESULTS[uid] = results[:MAX_RESULTS_CACHE]

# ------------------ ابزارهای کمکی ------------------
def _github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ai-tech-bot/1.0",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers

async def _http_get_json(url, params=None, headers=None, timeout=20):
    # retry سبک برای جلوگیری از هنگ در خطاهای گذرا
    headers = {**(_github_headers() if headers is None else headers)}
    retry_delays = [0.5, 1.0, 2.0]
    last_exc = None
    for delay in retry_delays:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, params=params, headers=headers)
                if r.status_code in (403, 429):
                    # معمولاً rate limit گیت‌هاب
                    text = r.text
                    raise RuntimeError("GitHub rate limit or forbidden. Set GITHUB_TOKEN. "
                                       f"status={r.status_code} body={text[:200]}")
                r.raise_for_status()
                return r.json()
        except Exception as e:
            last_exc = e
            await asyncio.sleep(delay)
    raise last_exc

async def fetch_text(url, timeout=20):
    retry_delays = [0.5, 1.0]
    last_exc = None
    for delay in retry_delays:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(url, headers=_github_headers())
                r.raise_for_status()
                return r.text
        except Exception as e:
            last_exc = e
            await asyncio.sleep(delay)
    raise last_exc

def _to_raw_url(html_repo, path, branch):
    return f"{html_repo.replace('https://github.com', 'https://raw.githubusercontent.com')}/{branch}/{path}"

async def safe_edit(msg: types.Message, text: str, reply_markup=None):
    try:
        await msg.edit_text(text, reply_markup=reply_markup, disable_web_page_preview=True)
    except Exception:
        await msg.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)

# ------------------ GitHub Search ------------------
async def github_code_search(q: str, per_page=5):
    url = "https://api.github.com/search/code"
    params = {"q": q, "per_page": str(per_page)}
    data = await _http_get_json(url, params)
    results = []
    for item in data.get("items", []):
        repo = item.get("repository", {})
        html_repo = repo.get("html_url", "")
        default_branch = repo.get("default_branch", "main")
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

@dp.callback_query(F.data == "py_more")
async def py_more(cb: CallbackQuery):
    await cb.message.answer("🔁 لطفاً یک کلیدواژهٔ جدید بفرست تا جستجوی بیشتری انجام بدم.")
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
        info_msg = await msg.answer("⏳ در حال جستجوی پایتون (GitHub code)...")
        try:
            query = f'{q} language:python in:file'
            results = await github_code_search(query, per_page=5)
            if not results:
                query2 = f'{q} language:python filename:README in:file'
                results = await github_code_search(query2, per_page=5)
            if not results:
                await safe_edit(info_msg, "❌ چیزی پیدا نشد. یک کلیدواژه‌ی ساده‌تر امتحان کن.")
                return

            _cache_results(msg.from_user.id, results)
            kb = InlineKeyboardBuilder()
            for i, r in enumerate(results):
                kb.button(text=f"{r['name']} 📂 {r['repo']}", callback_data=f"ext_open_{i}")
            kb.button(text="🔄 جستجوی بیشتر", callback_data="py_more")
            kb.adjust(1)
            await safe_edit(info_msg, "📌 <b>نتایج پایتون:</b>", reply_markup=kb.as_markup())
        except Exception as e:
            await safe_edit(info_msg, f"⚠️ خطا در جستجوی پایتون: {e}")
        return

    # حالت عادی
    info_msg = await msg.answer("⏳ در حال جستجو روی GitHub...")
    try:
        results = await github_code_search(q, per_page=5)
        if not results:
            await safe_edit(info_msg, "❌ چیزی پیدا نشد.")
            return
        _cache_results(msg.from_user.id, results)
        kb = InlineKeyboardBuilder()
        for i, r in enumerate(results):
            kb.button(text=f"{r['name']} 📂 {r['repo']}", callback_data=f"ext_open_{i}")
        kb.adjust(1)
        await safe_edit(info_msg, "📌 <b>نتایج جستجو:</b>", reply_markup=kb.as_markup())
    except Exception as e:
        await safe_edit(info_msg, f"⚠️ خطا: {e}")

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
    except Exception as e:
        await cb.message.answer(f"❌ دانلود کد ناموفق بود: {e}")
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

# ------------------ Webhook server (aiohttp) ------------------
async def on_startup(app: web.Application):
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True, secret_token=WEBHOOK_SECRET or None)
        logger.info(f"✅ Webhook set: {WEBHOOK_URL}")
    else:
        logger.warning("⚠️ RENDER_EXTERNAL_URL not set; webhook not configured.")

async def on_shutdown(app: web.Application):
    await bot.session.close()
    logger.info("🧹 Bot session closed")

async def health(request: web.Request):
    return web.Response(text="ok")

async def webhook(request: web.Request):
    # اعتبارسنجی درخواست از طرف تلگرام
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403, text="forbidden")
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="bad json")

    try:
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        logger.exception("Failed to process update: %s", e)
        return web.Response(status=500, text="error")
    return web.json_response({"ok": True})

def create_app():
    app = web.Application()
    # health/landing routes
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    # telegram webhook route
    app.router.add_post(WEBHOOK_PATH, webhook)
    # lifecycle
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(app, host="0.0.0.0", port=port)
