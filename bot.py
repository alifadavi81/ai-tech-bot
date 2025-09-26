import os
import logging
import html as _html
import httpx
import json
import re
import unicodedata
from pathlib import Path

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
DB_PATH = Path(os.getenv("PROJECTS_JSON", "projects.json"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN در env تنظیم نشده است.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-tech-bot")

dp = Dispatcher()
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

# ------------------ حافظه ساده ------------------
USER_MODE = {}        # حالت پایتون یا None
USER_KIND = {}        # نوع جستجو: code | schematic | parts | guide
EXT_RESULTS = {}      # نتایج آخرین جستجو برای هر کاربر
MAX_TEXT_LEN = 4000   # محدودیت نمایش تلگرام

def reset_mode(uid: int):
    USER_MODE[uid] = None

def set_kind(uid: int, kind: str | None):
    USER_KIND[uid] = kind

def get_kind(uid: int) -> str | None:
    return USER_KIND.get(uid)

# ------------------ دیتابیس محلی (projects.json) ------------------
PROJECTS = []
def load_local_db():
    global PROJECTS
    if DB_PATH.exists():
        try:
            PROJECTS = json.loads(DB_PATH.read_text(encoding="utf-8"))
            if not isinstance(PROJECTS, list):
                logger.warning("projects.json باید آرایه باشد.")
                PROJECTS = []
            else:
                logger.info(f"Local DB loaded: {len(PROJECTS)} items")
        except Exception as e:
            logger.exception(f"Failed to load projects.json: {e}")
load_local_db()

def normalize_fa(s: str) -> str:
    s = (s or "").replace("ي", "ی").replace("ك", "ک")
    s = re.sub(r"[\u200c\u200f]", " ", s)  # حذف نیم‌فاصله/علامت‌های RTL
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip()

def _extract_code_url(item: dict) -> str | None:
    links = item.get("links") or {}
    return (
        item.get("code")
        or links.get("code")
        or item.get("url")
        or item.get("github")
        or item.get("html_url")
    )

def search_local_db_for_code(q: str, limit: int = 6):
    if not PROJECTS:
        return []
    qn = normalize_fa(q).lower()
    out = []
    for item in PROJECTS:
        title = str(item.get("title") or item.get("name") or "")
        desc = str(item.get("description") or item.get("desc") or "")
        tags = item.get("tags", [])
        hay = normalize_fa(" ".join([title, desc, " ".join(map(str, tags))])).lower()
        if qn and qn not in hay:
            continue
        code_url = _extract_code_url(item)
        if not code_url:
            continue
        out.append({"title": title or "پروژه", "desc": desc, "url": code_url})
        if len(out) >= limit:
            break
    return out

async def show_local_code(msg: Message, items: list):
    if not items:
        return False
    await msg.answer("📂 <b>نتایج دیتابیس داخلی (کد):</b>")
    for it in items:
        btn = InlineKeyboardBuilder()
        btn.button(text="💻 باز کردن کد", url=it["url"])
        btn.adjust(1)
        title = _html.escape(it["title"])
        desc = _html.escape(it["desc"]) if it.get("desc") else ""
        text = f"🔹 <b>{title}</b>" + (f"\n{desc}" if desc else "")
        await msg.answer(text, reply_markup=btn.as_markup(), disable_web_page_preview=True)
    return True

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

# ------------------ GitHub Search (پایه) ------------------
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

# ------------------ فیلتر مسیرهای مزاحم ------------------
BLACKLIST_SUBSTRINGS = [
    "/test/", "/tests/", "/testing/",
    "/__tests__/", "/__mocks__/",
    "/node_modules/", "/vendor/", "/third_party/",
    "/dist/", "/build/", "/out/",
    "/.github/", "/.gitlab/", "/.circleci/",
    "/docs/_build/", "/examples/old/",
]

def is_noisy_path(path: str) -> bool:
    p = f"/{(path or '').strip('/')}/".lower()
    return any(bad in p for bad in BLACKLIST_SUBSTRINGS)

# ------------------ کوئری‌ساز تخصصی ------------------
def build_github_queries(kind: str, term: str) -> list[str]:
    t = term.strip()
    base = t.replace('"', '')
    queries = []

    if kind == "schematic":
        queries += [
            f'"{base}" filename:.kicad_sch in:path',
            f'"{base}" filename:.sch in:path',
            f'"{base}" filename:.brd OR filename:.pcb in:path',
            f'"{base}" path:schematic OR path:schematics',
            f'"{base}" filename:schematic.svg OR filename:schematic.png OR filename:schematic.pdf',
        ]
    elif kind == "parts":
        queries += [
            f'"{base}" filename:bom.csv OR filename:parts.csv',
            f'"{base}" filename:bom.tsv OR filename:parts.tsv',
            f'"{base}" filename:bom.md OR filename:parts.md',
            f'"{base}" \"bill of materials\" in:file',
            f'"{base}" path:bom OR path:hardware/bom OR path:docs/bom',
        ]
    elif kind == "guide":
        queries += [
            f'"{base}" filename:README.md in:path',
            f'"{base}" path:docs in:path',
            f'"{base}" \"hardware design\" in:file',
            f'"{base}" filename:GUIDE.md OR filename:DESIGN.md',
        ]
    else:  # code
        queries += [
            f'{base} language:python in:file',
            f'{base} language:c++ in:file',
            f'{base} language:c in:file',
            f'{base} language:rust in:file',
            f'{base} language:java in:file',
            f'{base} arduino in:file',
            f'{base} micropython in:file',
        ]
    uniq = []
    for q in queries:
        q = q.strip()
        if q and q not in uniq:
            uniq.append(q)
    return uniq

# ------------------ جستجوی هوشمند گیت‌هاب + رتبه‌بندی + فیلتر ------------------
def _score_item(kind: str, name: str, path: str) -> int:
    name = (name or "").lower()
    path = (path or "").lower()
    score = 0
    if kind == "schematic":
        if any(name.endswith(ext) for ext in (".kicad_sch", ".sch", ".brd", ".pcb")): score += 5
        if "schematic" in path: score += 3
        if name.endswith((".svg", ".png", ".pdf")) and "schem" in name: score += 2
    elif kind == "parts":
        if "bom" in name or "parts" in name: score += 5
        if name.endswith((".csv", ".tsv", ".md")): score += 2
        if "/bom" in path or "/hardware/bom" in path or "/docs/bom" in path: score += 3
    elif kind == "guide":
        if name in ("readme.md", "guide.md", "design.md"): score += 5
        if "/docs" in path: score += 3
    else:  # code
        if name.endswith((".py", ".c", ".cpp", ".ino", ".rs", ".java")): score += 3
        if "src/" in path: score += 2
        if "examples/" in path: score += 1
    # پنالتی نتایج مزاحم
    if is_noisy_path(path):
        score -= 4
    # حذف فایل‌های خیلی کوچک/نمونه‌های قدیمی را نمی‌توانیم بدون متادیتا تشخیص دهیم
    return score

async def github_search_smart(kind: str, term: str, per_query: int = 5, max_total: int = 10):
    seen = set()
    bag = []

    for q in build_github_queries(kind, term):
        try:
            res = await github_code_search(q, per_page=per_query)
        except Exception as e:
            logger.warning(f"GH search failed for {q}: {e}")
            continue

        for r in res:
            key = r["html_url"]
            if key in seen:
                continue
            seen.add(key)

            name = r.get("name") or ""
            path = r.get("path") or ""

            # فیلتر سخت مسیرهای مزاحم
            if is_noisy_path(path):
                # به‌جای حذف کامل می‌توانستیم پنالتی شدید بدهیم؛ ولی کاربر خواست نتایج نامربوط حذف شوند
                continue

            score = _score_item(kind, name, path)
            bag.append((score, r))

        if len(bag) >= max_total:
            break

    bag.sort(key=lambda x: (-x[0], x[1].get("name","")))
    results = [r for _, r in bag[:max_total]]
    return results

def pretty_label(item: dict) -> str:
    name = item.get("name") or "file"
    repo = item.get("repo") or ""
    path = item.get("path") or ""
    if len(path) > 48:
        path = "…" + path[-48:]
    return f"{name}  📂 {repo} — {path}"

# ------------------ Start & Menu ------------------
@dp.message(Command("start"))
async def start(msg: Message):
    reset_mode(msg.from_user.id)
    set_kind(msg.from_user.id, None)
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 رباتیک", callback_data="cat_robotics")
    kb.button(text="🌐 اینترنت اشیا", callback_data="cat_iot")
    kb.button(text="🐍 پایتون (جستجو)", callback_data="py_home")
    kb.button(text="🔍 جستجو", callback_data="search")  # منوی جدید جستجو
    kb.adjust(2)
    await msg.answer(
        "👋 <b>سلام! خوش اومدی</b>\n\n"
        "📂 یکی از گزینه‌های زیر رو انتخاب کن:",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "home")
async def go_home(cb: CallbackQuery):
    reset_mode(cb.from_user.id)
    set_kind(cb.from_user.id, None)
    await start(cb.message)
    await cb.answer()

# ------------------ منوی جستجو با ۴ گزینه ------------------
@dp.callback_query(F.data == "search")
async def do_search(cb: CallbackQuery):
    reset_mode(cb.from_user.id)
    set_kind(cb.from_user.id, None)
    kb = InlineKeyboardBuilder()
    kb.button(text="📘 کد", callback_data="search_kind_code")
    kb.button(text="🧩 قطعه‌ها (BOM)", callback_data="search_kind_parts")
    kb.button(text="🗺️ شماتیک مدار", callback_data="search_kind_schematic")
    kb.button(text="📐 راهنمای طراحی", callback_data="search_kind_guide")
    kb.button(text="🏠 خانه", callback_data="home")
    kb.adjust(2, 2, 1)
    await cb.message.answer(
        "🔍 نوع جستجو را انتخاب کن، بعد کلمه/عبارت را بفرست:",
        reply_markup=kb.as_markup()
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("search_kind_"))
async def set_search_kind(cb: CallbackQuery):
    kind = cb.data.split("_", 2)[2]  # code | parts | schematic | guide
    set_kind(cb.from_user.id, kind)
    label = {'code':'کد','parts':'قطعه‌ها (BOM)','schematic':'شماتیک مدار','guide':'راهنمای طراحی'}.get(kind, kind)
    await safe_edit(cb.message, f"✅ نوع جستجو: <b>{label}</b>\n\n📝 عبارتت را بفرست.")
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

# ------------------ دکمه ادامه جستجو در گیت‌هاب ------------------
@dp.callback_query(F.data.startswith("gh_more_"))
async def gh_more(cb: CallbackQuery):
    # فرمت: gh_more_{kind}::{query}
    try:
        _, payload = cb.data.split("_", 1)
        kind, term = payload.split("::", 1)
    except Exception:
        await cb.answer("فرمت نامعتبر", show_alert=True)
        return
    await cb.answer()
    try:
        gh = await github_search_smart(kind, term, per_query=5, max_total=10)
        if not gh:
            await cb.message.answer("❌ چیزی در GitHub پیدا نشد.")
            return
        EXT_RESULTS[cb.from_user.id] = gh
        kb = InlineKeyboardBuilder()
        for i, r in enumerate(gh):
            kb.button(text=pretty_label(r), callback_data=f"ext_open_{i}")
        kb.adjust(1)
        title = {
            "code": "📘 نتایج کُد از GitHub:",
            "schematic": "🗺️ نتایج شماتیک از GitHub:",
            "parts": "🧩 نتایج BOM/قطعه‌ها از GitHub:",
            "guide": "📐 نتایج راهنمای طراحی از GitHub:",
        }.get(kind, "نتایج GitHub:")
        await cb.message.answer(title, reply_markup=kb.as_markup())
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            await cb.message.answer("⚠️ GitHub rate limit. اگر شد در env یک GITHUB_TOKEN ست کن.")
        else:
            await cb.message.answer(f"⚠️ خطای GitHub: {e}")
    except Exception as e:
        await cb.message.answer(f"⚠️ خطا: {e}")

# ------------------ Messages ------------------
@dp.message()
async def handle_query(msg: Message):
    q = (msg.text or "").strip()
    if not q:
        return

    # حالت پایتون (قدیم)
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
                kb.button(text=pretty_label(r), callback_data=f"ext_open_{i}")
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

    # نوع جستجو از منو
    kind = get_kind(msg.from_user.id)  # code | schematic | parts | guide | None

    if kind == "code":
        # 1) اول دیتابیس داخلی
        local = search_local_db_for_code(q, limit=6)
        if local and await show_local_code(msg, local):
            kb = InlineKeyboardBuilder()
            kb.button(text="🔎 ادامه در GitHub", callback_data=f"gh_more_{kind}::{q}")
            kb.adjust(1)
            await msg.answer("می‌خواهی در GitHub هم بگردم؟", reply_markup=kb.as_markup())
            return
        else:
            await msg.answer("ℹ️ در دیتابیس داخلی نتیجه‌ای نبود؛ در GitHub جستجو می‌کنم…")

    elif kind in ("schematic","parts","guide"):
        await msg.answer("⏳ در حال جستجوی GitHub بر اساس فیلترهای تخصصی …")
    else:
        # پیش‌فرض کد
        kind = "code"
        await msg.answer("ℹ️ نوع جستجو تعیین نشده بود؛ به‌صورت پیش‌فرض «کد» جستجو می‌شود…")

    # 2) گیت‌هاب با کوئری‌های تخصصی + فیلتر
    try:
        gh = await github_search_smart(kind, q, per_query=5, max_total=10)
        if not gh:
            await msg.answer("❌ چیزی در GitHub پیدا نشد.")
            return

        EXT_RESULTS[msg.from_user.id] = gh
        kb = InlineKeyboardBuilder()
        for i, r in enumerate(gh):
            kb.button(text=pretty_label(r), callback_data=f"ext_open_{i}")
        kb.adjust(1)
        title = {
            "code": "📘 نتایج کُد از GitHub:",
            "schematic": "🗺️ نتایج شماتیک از GitHub:",
            "parts": "🧩 نتایج BOM/قطعه‌ها از GitHub:",
            "guide": "📐 نتایج راهنمای طراحی از GitHub:",
        }[kind]
        await msg.answer(title, reply_markup=kb.as_markup())
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            await msg.answer("⚠️ GitHub rate limit. اگر شد در env یک GITHUB_TOKEN ست کن.")
        else:
            await msg.answer(f"⚠️ خطای GitHub: {e}")
    except Exception as e:
        await msg.answer(f"⚠️ خطا: {e}")

# دکمه راهنمای «جستجوی بیشتر» برای حالت پایتون
@dp.callback_query(F.data == "py_more_info")
async def py_more_info(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("🔎 برای نتایج بیشتر، عبارت جدید یا دقیق‌تر بفرست (مثلاً: <code>fastapi language:python in:file</code>).")

# ------------------ باز کردن کد خارجی ------------------
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

# ------------------ وب‌هوک با aiohttp ------------------
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
    app.router.add_get("/", health_handler)  # مسیر سلامت برای Render
    # وب‌هوک
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    web.run_app(main(), host="0.0.0.0", port=PORT)
