import os
import json
import re
import logging
import html as _html
import httpx
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command

# ================== Config ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "10000"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # اختیاری

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN در env تنظیم نشده است.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai-tech-bot")

dp = Dispatcher()
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

MAX_TEXT_LEN = 4000

# ================== In-Memory ==================
# USER_STATE[user_id] = {"mode": "py"|"search"|None, "domain": "robotics"|"iot"|None, "facet": "schematic"|"code"|"parts"|"guide"|None}
USER_STATE = {}
EXT_RESULTS = {}   # نتایج GitHub و DB برای هر کاربر

def reset_state(uid: int):
    USER_STATE[uid] = {"mode": None, "domain": None, "facet": None}

# ================== Local DB (projects.json) ==================
# ساختار قابل‌قبول:
# 1) شیء با کلیدهای robotics/iot/py_libs که هر کدام آرایه‌ای از آیتم‌ها هستند
# 2) یا آرایه‌ی ساده که به robotics نسبت داده می‌شود
DB = {"robotics": [], "iot": [], "py_libs": []}

def load_projects_json():
    path = os.path.join(os.getcwd(), "projects.json")
    if not os.path.exists(path):
        logger.warning("projects.json یافت نشد؛ جستجوی محلی غیرفعال است.")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            DB["robotics"] = data
            logger.info("projects.json به صورت آرایه بود؛ در robotics بارگذاری شد.")
        elif isinstance(data, dict):
            for k in ("robotics", "iot", "py_libs"):
                if isinstance(data.get(k), list):
                    DB[k] = data.get(k, [])
            logger.info("projects.json با ساختار شیء بارگذاری شد.")
        else:
            logger.warning("ساختار projects.json نامعتبر است.")
    except Exception as e:
        logger.exception(f"خواندن projects.json خطا داد: {e}")

load_projects_json()

# ================== Utils ==================
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

async def fetch_text(url, headers=None):
    async with httpx.AsyncClient(timeout=25, headers=headers or {"User-Agent": "ai-tech-bot/1.0"}) as client:
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

def norm(s: str) -> str:
    return (s or "").lower()

def text_like(t: str, q: str) -> bool:
    t = norm(t); q = norm(q)
    q = re.sub(r"\s+", " ", q)
    return all(word in t for word in q.split())

def pick_nonempty_fields(item, fields):
    return [f for f in fields if (item.get(f) and str(item.get(f)).strip())]

# ================== Local Search ==================
FACETS = {
    "schematic": {"label": "📐 شماتیک مدار", "fields": ["schematic"]},
    "code":      {"label": "💻 کد",          "fields": ["code"]},
    "parts":     {"label": "🧩 قطعه‌ها",      "fields": ["parts", "bom"]},
    "guide":     {"label": "📘 راهنمای طراحی", "fields": ["guide", "readme"]}
}

def local_search(domain: str, facet: str, query: str, limit=8):
    items = DB.get(domain, [])
    results = []
    for it in items:
        hay = " ".join([
            it.get("title",""),
            it.get("desc",""),
            " ".join(it.get("tags", []))
        ])
        if not query or text_like(hay, query):
            fields = FACETS[facet]["fields"]
            if pick_nonempty_fields(it, fields):
                results.append(it)
        if len(results) >= limit:
            break
    return results

# ================== GitHub Search (facet-aware, multi-query) ==================
def build_github_queries(domain: str, facet: str, user_query: str) -> list[str]:
    """
    به‌جای یک کوئری پیچیده، چند کوئری سبک و معتبر برای GitHub Code Search می‌سازیم.
    بدون پرانتز، بدون fork:false. اگر لازم شد، هر seed یک کوئری مجزا می‌شود.
    """
    base = user_query.strip()
    queries: list[str] = []

    if facet == "code":
        if domain == "iot":
            langs = ["arduino", "c", "cpp", "python", "javascript"]
        else:
            langs = ["arduino", "c", "cpp"]
        for l in langs:
            queries.append(f"{base} language:{l} in:file")

    elif facet == "schematic":
        sch_terms = [
            "extension:sch",
            "extension:kicad_sch",
            "extension:kicad_pcb",
            "extension:fzz",
            "kicad",
            "eagle",
            "fritzing",
            "schematic",
        ]
        for t in sch_terms:
            queries.append(f"{base} {t} in:path")

    elif facet == "parts":
        seeds = [
            'BOM in:file',
            '"bill of materials" in:file',
            '"parts list" in:file',
            'components in:file',
            'filename:README.md in:file',
            'filename:BOM in:file',
            'filename:parts.txt in:file',
            'filename:bill_of_materials.csv in:file'
        ]
        for s in seeds:
            queries.append(f"{base} {s}")

    elif facet == "guide":
        seeds = [
            'filename:README.md in:file',
            'path:docs in:path',
            'path:hardware in:path',
            'path:design in:path',
            '"hardware design" in:file',
            'schematic in:file',
            'setup in:file',
            'wiring in:file',
            'assembly in:file',
        ]
        for s in seeds:
            queries.append(f"{base} {s}")
    else:
        queries.append(f"{base} in:file")

    # حذف تکراری‌ها
    uniq, seen = [], set()
    for q in queries:
        q2 = re.sub(r"\s+", " ", q).strip()
        if q2 and q2 not in seen:
            uniq.append(q2)
            seen.add(q2)
    return uniq[:10]

async def github_code_search_multi(queries: list[str], per_page=5, cap=8):
    """
    چند کوئری سبک را یکی‌یکی می‌زند تا به cap نتیجه برسیم.
    اگر 422 بیاید، کوئری ساده‌تر می‌شود (fallback فقط in:file).
    """
    all_items = []
    seen_keys = set()

    async def run_one(q: str):
        nonlocal all_items
        try:
            url = "https://api.github.com/search/code"
            params = {"q": q, "per_page": str(per_page), "page": "1"}
            data = await _http_get_json(url, params, headers=_gh_headers())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 422:
                # fallback: حذف qualifierها و فقط in:file
                simple = re.sub(
                    r'(language:[^\s]+|extension:[^\s]+|filename:[^\s]+|path:[^\s]+|in:(file|path))',
                    '',
                    q, flags=re.I
                )
                simple = re.sub(r'\s+', ' ', (simple + ' in:file')).strip()
                try:
                    url = "https://api.github.com/search/code"
                    params = {"q": simple, "per_page": str(per_page), "page": "1"}
                    data = await _http_get_json(url, params, headers=_gh_headers())
                except Exception:
                    return
            else:
                return
        except Exception:
            return

        for item in data.get("items", []):
            repo = item.get("repository", {})
            html_repo = repo.get("html_url", "")
            default_branch = repo.get("default_branch") or "main"
            path = item.get("path")
            raw_url = _to_raw_url(html_repo, path, default_branch)
            key = f"{repo.get('full_name')}/{path}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            all_items.append({
                "name": item.get("name"),
                "path": path,
                "repo": repo.get("full_name"),
                "html_url": item.get("html_url"),
                "raw_url": raw_url,
            })
            if len(all_items) >= cap:
                break

    for q in queries:
        if len(all_items) >= cap:
            break
        await run_one(q)

    return all_items

# ================== Old single-call (برای جستجوی آزاد/پایتون)
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

# ================== UI Builders ==================
def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 رباتیک", callback_data="cat_robotics")
    kb.button(text="🌐 اینترنت اشیا", callback_data="cat_iot")
    kb.button(text="🐍 پایتون (جستجو عمومی)", callback_data="py_home")
    kb.button(text="🔍 جستجو GitHub آزاد", callback_data="search_free")
    kb.adjust(2)
    return kb

def facet_menu_kb(domain: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=FACETS["schematic"]["label"], callback_data=f"facet_{domain}_schematic")
    kb.button(text=FACETS["code"]["label"],      callback_data=f"facet_{domain}_code")
    kb.button(text=FACETS["parts"]["label"],     callback_data=f"facet_{domain}_parts")
    kb.button(text=FACETS["guide"]["label"],     callback_data=f"facet_{domain}_guide")
    kb.button(text="🏠 خانه", callback_data="home")
    kb.adjust(2,1,1)
    return kb

def results_kb(items, prefix="local", domain=None, facet=None):
    kb = InlineKeyboardBuilder()
    for i, it in enumerate(items):
        title = it.get("title") or it.get("name") or it.get("path") or "item"
        kb.button(text=f"{title[:48]}", callback_data=f"{prefix}_open_{i}")
    if prefix == "local" and domain and facet:
        kb.button(text="🔎 ادامه در GitHub", callback_data=f"fallback_{domain}_{facet}")
    kb.button(text="🏠 خانه", callback_data="home")
    kb.adjust(1)
    return kb

# ================== Handlers ==================
@dp.message(Command("start"))
async def start(msg: Message):
    reset_state(msg.from_user.id)
    await msg.answer(
        "👋 <b>سلام! خوش اومدی</b>\n\n"
        "یک دسته رو انتخاب کن یا حالت‌های جستجو رو باز کن:",
        reply_markup=main_menu_kb().as_markup()
    )

@dp.callback_query(F.data == "home")
async def go_home(cb: CallbackQuery):
    reset_state(cb.from_user.id)
    await safe_edit(cb.message, "🏠 بازگشت به خانه.", reply_markup=main_menu_kb().as_markup())
    await cb.answer()

# ---- Domains
@dp.callback_query(F.data == "cat_robotics")
async def cat_robotics(cb: CallbackQuery):
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "search", "domain": "robotics", "facet": None}
    await safe_edit(cb.message, "🤖 رباتیک — یکی از فیلترها رو انتخاب کن:", reply_markup=facet_menu_kb("robotics").as_markup())
    await cb.answer()

@dp.callback_query(F.data == "cat_iot")
async def cat_iot(cb: CallbackQuery):
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "search", "domain": "iot", "facet": None}
    await safe_edit(cb.message, "🌐 اینترنت اشیا — یکی از فیلترها رو انتخاب کن:", reply_markup=facet_menu_kb("iot").as_markup())
    await cb.answer()

# ---- Facets
@dp.callback_query(F.data.startswith("facet_"))
async def facet_select(cb: CallbackQuery):
    _, domain, facet = cb.data.split("_", 2)
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "search", "domain": domain, "facet": facet}
    await cb.answer()
    await cb.message.answer(
        f"{FACETS[facet]['label']} — عبارت جستجو رو بفرست.\n"
        "🔎 اول از دیتابیس داخلی می‌گردم، اگر نبود میرم GitHub."
    )

# ---- Free GitHub search (old)
@dp.callback_query(F.data == "search_free")
async def do_search_free(cb: CallbackQuery):
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": None, "domain": None, "facet": None}
    await cb.answer()
    await cb.message.answer("🔍 عبارت جستجوی آزاد GitHub رو بفرست (مثال: <code>fastapi language:python in:file</code>)")

# ---- Python mode (old)
@dp.callback_query(F.data == "py_home")
async def py_home(cb: CallbackQuery):
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "py", "domain": None, "facet": None}
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
    reset_state(cb.from_user.id)
    await safe_edit(cb.message, "✅ از حالت پایتون خارج شدی.", reply_markup=main_menu_kb().as_markup())
    await cb.answer()

# ---- Message router
@dp.message()
async def handle_query(msg: Message):
    q = (msg.text or "").strip()
    if not q:
        return

    st = USER_STATE.get(msg.from_user.id) or {"mode": None, "domain": None, "facet": None}

    # Python library search (legacy)
    if st["mode"] == "py":
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
            EXT_RESULTS[msg.from_user.id] = {"items": results, "source": "github"}
            kb = results_kb(results, prefix="ext")
            await msg.answer("📌 <b>نتایج پایتون:</b>", reply_markup=kb.as_markup())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                await msg.answer("⚠️ GitHub rate limit. اگر شد در env یک GITHUB_TOKEN ست کن.")
            else:
                await msg.answer(f"⚠️ خطای GitHub: {e}")
        except Exception as e:
            await msg.answer(f"⚠️ خطا: {e}")
        return

    # Facet/domain search (new)
    if st["mode"] == "search" and st["domain"] and st["facet"]:
        domain = st["domain"]
        facet = st["facet"]
        await msg.answer("⏳ اول از دیتابیس محلی می‌گردم...")
        local = local_search(domain=domain, facet=facet, query=q, limit=8)
        if local:
            EXT_RESULTS[msg.from_user.id] = {"items": local, "source": "local", "domain": domain, "facet": facet}
            kb = results_kb(local, prefix="local", domain=domain, facet=facet)
            await msg.answer(f"✅ <b>نتایج محلی ({domain} / {FACETS[facet]['label']}):</b>", reply_markup=kb.as_markup())
            return

        await msg.answer("🔁 چیزی در محلی نبود؛ می‌رم GitHub...")
        try:
            queries = build_github_queries(domain, facet, q)
            results = await github_code_search_multi(queries, per_page=5, cap=8)
            if not results and facet != "code":
                # تلاش دوم: آزادتر
                results = await github_code_search_multi([q + " in:file"], per_page=5, cap=8)
            if not results:
                await msg.answer("❌ چیزی پیدا نشد. کلیدواژه‌ی دقیق‌تر بده.")
                return
            EXT_RESULTS[msg.from_user.id] = {"items": results, "source": "github", "domain": domain, "facet": facet}
            kb = results_kb(results, prefix="ext")
            await msg.answer(f"📌 <b>نتایج GitHub ({FACETS[facet]['label']}):</b>", reply_markup=kb.as_markup())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                await msg.answer("⚠️ GitHub rate limit. اگر شد در env یک GITHUB_TOKEN ست کن.")
            else:
                await msg.answer(f"⚠️ خطای GitHub: {e}")
        except Exception as e:
            await msg.answer(f"⚠️ خطا: {e}")
        return

    # Free GitHub search (legacy)
    await msg.answer("⏳ در حال جستجوی آزاد روی GitHub...")
    try:
        results = await github_code_search(q, per_page=5)
        if not results:
            await msg.answer("❌ چیزی پیدا نشد.")
            return
        EXT_RESULTS[msg.from_user.id] = {"items": results, "source": "github"}
        kb = results_kb(results, prefix="ext")
        await msg.answer("📌 <b>نتایج جستجو:</b>", reply_markup=kb.as_markup())
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            await msg.answer("⚠️ GitHub rate limit. اگر شد در env یک GITHUB_TOKEN ست کن.")
        else:
            await msg.answer(f"⚠️ خطای GitHub: {e}")
    except Exception as e:
        await msg.answer(f"⚠️ خطا: {e}")

# ---- Open Local item
@dp.callback_query(F.data.startswith("local_open_"))
async def local_open(cb: CallbackQuery):
    st = EXT_RESULTS.get(cb.from_user.id) or {}
    items = (st.get("items") or [])
    try:
        idx = int(cb.data.split("_", 2)[2])
    except Exception:
        await cb.answer("نامعتبر", show_alert=True); return
    if idx < 0 or idx >= len(items):
        await cb.answer("⏰ منقضی شده", show_alert=True); return
    item = items[idx]

    state = USER_STATE.get(cb.from_user.id) or {}
    facet = state.get("facet", "code")
    fields = FACETS.get(facet, FACETS["code"])["fields"]
    content = None
    for f in fields:
        if item.get(f):
            content = str(item.get(f))
            break

    if not content:
        await cb.message.answer("❌ برای این مورد، محتوای مرتبط در DB وجود ندارد.")
        await cb.answer(); return

    if re.match(r"^https?://", content):
        try:
            body = await fetch_text(content)
            content = body
        except Exception:
            await cb.message.answer(f"🔗 <a href='{content}'>مشاهده محتوا</a>", disable_web_page_preview=False)
            await cb.answer(); return

    safe = _html.escape(content)
    if len(safe) < MAX_TEXT_LEN:
        await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>")
    else:
        doc = BufferedInputFile(content.encode("utf-8"), filename=f"{facet}.txt")
        await cb.message.answer_document(doc, caption=f"📄 {FACETS[facet]['label']}")
    await cb.answer()

# ---- Fallback to GitHub from local UI
@dp.callback_query(F.data.startswith("fallback_"))
async def do_fallback(cb: CallbackQuery):
    _, domain, facet = cb.data.split("_", 2)
    await cb.message.answer(
        f"🔁 برای ادامه جستجو در GitHub ({FACETS[facet]['label']}), یک عبارت بفرست.\n"
        "مثلاً: <code>line follower</code> یا <code>ESP32 MQTT</code>"
    )
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "search", "domain": domain, "facet": facet}
    await cb.answer()

# ---- Open External (GitHub) item
@dp.callback_query(F.data.startswith("ext_open_"))
async def ext_open(cb: CallbackQuery):
    st = EXT_RESULTS.get(cb.from_user.id) or {}
    items = (st.get("items") or [])
    try:
        idx = int(cb.data.split("_", 2)[2])
    except Exception:
        await cb.answer("نامعتبر", show_alert=True); return
    if idx < 0 or idx >= len(items):
        await cb.answer("⏰ منقضی شده", show_alert=True); return
    item = items[idx]
    try:
        code = await fetch_text(item["raw_url"], headers=_gh_headers())
    except Exception:
        await cb.message.answer(
            f"❌ دانلود مستقیم نشد.\n"
            f"🔗 <a href='{item['html_url']}'>مشاهده در GitHub</a>\n"
            f"📁 <code>{item['repo']}/{item['path']}</code>"
        )
        await cb.answer(); return

    caption = (
        f"🔗 <a href='{item['html_url']}'>مشاهده در GitHub</a>\n"
        f"📁 <code>{item['repo']}/{item['path']}</code>\n"
        f"⚠️ لایسنس رو چک کن."
    )
    safe = _html.escape(code)
    if len(caption) + len(safe) < MAX_TEXT_LEN:
        await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>\n\n{caption}")
    else:
        doc = BufferedInputFile(code.encode("utf-8"), filename=item["name"] or "snippet.txt")
        await cb.message.answer_document(doc, caption=caption)
    await cb.answer()

# ================== Webhook (aiohttp) ==================
async def on_startup(app: web.Application):
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL)
            logger.info(f"✅ Webhook set: {WEBHOOK_URL}")
        except Exception as e:
            logger.exception(f"Webhook set failed: {e}")
    else:
        logger.warning("WEBHOOK_URL تعریف نشده. (مثال: https://<render>.onrender.com/webhook)")

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
    app.router.add_get("/", health_handler)     # health
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app

if __name__ == "__main__":
    web.run_app(main(), host="0.0.0.0", port=PORT)
