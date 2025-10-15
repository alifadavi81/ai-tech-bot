# bot.py (patched + UI improvements: project card, avatar, chat_action, pagination)
import os
import json
import re
import logging
import html as _html
import httpx
import asyncio
from dotenv import load_dotenv
from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.exceptions import TelegramForbiddenError

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
USER_STATE = {}
EXT_RESULTS = {}  # keyed by user_id: {"items": [...], "source":"github"|"local", "domain":..., "facet":...}

def reset_state(uid: int):
    USER_STATE[uid] = {"mode": None, "domain": None, "facet": None, "last_domain": None}

# ================== Local DB (projects.json) ==================
DB = {"robotics": [], "iot": [], "python": [], "py_libs": []}

def load_projects_json():
    path = os.path.join(os.getcwd(), "projects.json")
    if not os.path.exists(path):
        logger.warning("projects.json یافت نشد؛ جستجوی محلی غیرفعال است.")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # accept either top-level list => robotics, or dict with keys
        if isinstance(data, list):
            DB["robotics"] = data
            logger.info("projects.json به صورت آرایه بود؛ در robotics بارگذاری شد.")
            return
        if isinstance(data, dict):
            # فقط کلید‌های مورد انتظار را بردار
            for key in ("robotics", "iot", "python", "py_libs"):
                if key in data and isinstance(data[key], list):
                    # map py_libs -> python and py_libs both kept (backward comp)
                    if key == "py_libs":
                        DB["py_libs"] = data[key]
                        # اگر python خالی بود، از py_libs هم استفاده کن
                        if not DB["python"]:
                            DB["python"] = data[key]
                    else:
                        DB[key] = data[key]
            logger.info("projects.json با ساختار شیء بارگذاری شد.")
            return
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
        try:
            await msg.answer(text, reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception:
            pass

def norm(s: str) -> str:
    return (s or "").lower()

def text_like(t: str, q: str) -> bool:
    t = norm(t); q = norm(q)
    q = re.sub(r"\s+", " ", q)
    return all(word in t for word in q.split())

def pick_nonempty_fields(item, fields):
    return [f for f in fields if (item.get(f) and str(item.get(f)).strip())]

# ================== Local Search / Facets ==================
FACETS = {
    "schematic": {"label": "📐 شماتیک مدار", "fields": ["schematic"]},
    "code":      {"label": "💻 کد",          "fields": ["code"]},
    "parts":     {"label": "🧩 قطعه‌ها",      "fields": ["parts", "bom"]},
    "guide":     {"label": "📘 راهنمای طراحی", "fields": ["guide", "readme"]}
}

LANG_LABEL = {"c": "C (Arduino/UNO)", "cpp": "C++ (ESP32/UNO)", "micropython": "MicroPython"}

def local_search(domain: str, facet: str, query: str, limit=8):
    items = DB.get(domain, [])
    results = []
    for it in items:
        hay = " ".join([
            it.get("title",""),
            it.get("description","") or it.get("desc",""),
            " ".join(it.get("tags", []))
        ])
        if not query or text_like(hay, query):
            fields = FACETS[facet]["fields"]
            if pick_nonempty_fields(it, fields):
                results.append(it)
        if len(results) >= limit:
            break
    return results

# ================== GitHub Search (multi) ==================
def build_github_queries(domain: str, facet: str, user_query: str) -> list[str]:
    base = user_query.strip()
    queries: list[str] = []

    if facet == "code":
        langs = ["arduino", "c", "cpp", "python", "javascript"] if domain == "iot" or domain == "python" else ["arduino", "c", "cpp"]
        for l in langs:
            queries.append(f"{base} language:{l} in:file")

    elif facet == "schematic":
        sch_terms = ["extension:sch", "extension:kicad_sch", "extension:kicad_pcb", "extension:fzz", "kicad", "eagle", "fritzing", "schematic"]
        for t in sch_terms: queries.append(f"{base} {t} in:path")

    elif facet == "parts":
        seeds = [
            'BOM in:file','"bill of materials" in:file','"parts list" in:file','components in:file',
            'filename:README.md in:file','filename:BOM in:file','filename:parts.txt in:file','filename:bill_of_materials.csv in:file'
        ]
        for s in seeds: queries.append(f"{base} {s}")

    elif facet == "guide":
        seeds = [
            'filename:README.md in:file','path:docs in:path','path:hardware in:path','path:design in:path',
            '"hardware design" in:file','schematic in:file','setup in:file','wiring in:file','assembly in:file',
        ]
        for s in seeds: queries.append(f"{base} {s}")
    else:
        queries.append(f"{base} in:file")

    uniq, seen = [], set()
    for q in queries:
        q2 = re.sub(r"\s+", " ", q).strip()
        if q2 and q2 not in seen:
            uniq.append(q2); seen.add(q2)
    return uniq[:10]

async def github_code_search_multi(queries: list[str], per_page=5, cap=8):
    all_items = []
    seen_keys = set()

    async def run_one(q: str):
        nonlocal all_items
        try:
            url = "https://api.github.com/search/code"
            params = {"q": q, "per_page": str(per_page), "page": "1"}
            data = await _http_get_json(url, params, headers=_gh_headers())
        except httpx.HTTPStatusError as e:
            # try simplifying query if GitHub complains (422)
            if e.response.status_code == 422:
                simple = re.sub(r'(language:[^\s]+|extension:[^\s]+|filename:[^\s]+|path:[^\s]+|in:(file|path))', '', q, flags=re.I)
                simple = re.sub(r'\s+', ' ', (simple + ' in:file')).strip()
                try:
                    url = "https://api.github.com/search/code"
                    params = {"q": simple, "per_page": str(per_page), "page": "1"}
                    data = await _http_get_json(url, params, headers=_gh_headers())
                except Exception:
                    return
            else:
                # other http errors -> log and return
                logger.warning(f"GitHub search error for query: {q} status={getattr(e.response, 'status_code', 'NA')}")
                return
        except Exception as e:
            logger.exception(f"Exception during GitHub search for query '{q}': {e}")
            return

        for item in data.get("items", []):
            repo = item.get("repository", {})
            html_repo = repo.get("html_url", "")
            default_branch = repo.get("default_branch") or "main"
            path = item.get("path")
            raw_url = _to_raw_url(html_repo, path, default_branch)
            key = f"{repo.get('full_name')}/{path}"
            if key in seen_keys: continue
            seen_keys.add(key)
            all_items.append({
                "name": item.get("name"),
                "path": path,
                "repo": repo.get("full_name"),
                "html_url": item.get("html_url"),
                "raw_url": raw_url,
            })
            if len(all_items) >= cap: break

    for q in queries:
        if len(all_items) >= cap: break
        await run_one(q)

    return all_items

# ================== UI Helpers / Project Card / Avatar ==================
async def github_avatar_for_repo(full_name: str) -> str | None:
    """Return owner's avatar_url for a repo (or None)."""
    try:
        url = f"https://api.github.com/repos/{full_name}"
        data = await _http_get_json(url, headers=_gh_headers())
        owner = data.get("owner", {})
        return owner.get("avatar_url")
    except Exception:
        return None

async def send_project_card(chat_id: int, item: dict, domain: str = None, idx: int | None = None):
    """
    Sends a visually nicer project 'card' with thumbnail (if available), title, short desc and action buttons.
    domain + idx used to create proper callback_data for open/download actions.
    """
    title = _html.escape(item.get("title") or item.get("id") or "پروژه")
    desc_raw = (item.get("description") or item.get("desc") or "")[:500]
    desc = _html.escape(desc_raw)
    caption = f"📦 <b>{title}</b>\n{desc}\n\n"

    kb_rows = []
    # view on GitHub (url) if available
    if item.get("html_url"):
        kb_rows.append([InlineKeyboardButton(text="🔗 مشاهده در GitHub", url=item["html_url"])])

    # Open code / parts / schematic callbacks depending on domain/idx and available fields
    if domain is not None and idx is not None:
        # provide code button if exists
        codes = (item.get("code") or {})
        if codes:
            kb_rows.append([InlineKeyboardButton(text="💻 باز کردن کد", callback_data=f"proj_{domain}_{idx}")])
        # specific quick-search buttons
        kb_rows.append([InlineKeyboardButton(text="🔎 جستجوی قطعه‌ها", callback_data=f"find_parts_{domain}_{idx}")])
        kb_rows.append([InlineKeyboardButton(text="🔎 جستجوی شماتیک", callback_data=f"find_schematic_{domain}_{idx}")])

    # back button
    kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"back_to_{domain}" if domain else "back_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    # Try to use avatar/thumbnail if repo info present
    thumb_sent = False
    if item.get("repo"):
        avatar = await github_avatar_for_repo(item.get("repo"))
        if avatar:
            try:
                await bot.send_chat_action(chat_id=chat_id, action="upload_photo")
                await bot.send_photo(chat_id, photo=avatar, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
                thumb_sent = True
            except Exception:
                thumb_sent = False

    # fallback: if item has a local thumb_url
    if not thumb_sent and item.get("thumb_url"):
        try:
            await bot.send_chat_action(chat_id=chat_id, action="upload_photo")
            await bot.send_photo(chat_id, photo=item["thumb_url"], caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
            thumb_sent = True
        except Exception:
            thumb_sent = False

    if not thumb_sent:
        # plain message
        await bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=kb)

# ================== Results keyboard (one button per row) + pagination for ext results ==================
def results_kb(items, prefix="local", domain=None, facet=None, page: int = 0, page_size: int = 8):
    """
    Build an InlineKeyboardMarkup where each row contains one button (better readability).
    For prefix == 'ext' and len(items) > page_size, include Previous/Next buttons with callback data 'ext_page_{page}'.
    Items list should be the full list (pagination slicing done here).
    """
    start = page * page_size
    end = start + page_size
    slice_items = items[start:end]

    kb_rows = []
    for i, it in enumerate(slice_items, start=start):
        title = (it.get("title") or it.get("name") or it.get("path") or "item")[:48]
        # Show an extra small "🔗" suffix if html_url exists (visual cue)
        label = f"{title} {'🔗' if it.get('html_url') else ''}"
        kb_rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}_open_{i}")])

    # If ext and many items -> pagination
    if prefix == "ext" and len(items) > page_size:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⏮️ قبلی", callback_data=f"ext_page_{page-1}"))
        if end < len(items):
            nav_row.append(InlineKeyboardButton(text="⏭️ بعدی", callback_data=f"ext_page_{page+1}"))
        if nav_row:
            kb_rows.append(nav_row)

    # Fallback / continue in GitHub for local origin
    if prefix == "local" and domain and facet:
        kb_rows.append([InlineKeyboardButton(text="🔎 ادامه در GitHub", callback_data=f"fallback_{domain}_{facet}")])

    # Back button
    if domain:
        kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data=f"back_to_{domain}")])
    else:
        kb_rows.append([InlineKeyboardButton(text="⬅️ بازگشت به منو اصلی", callback_data="back_main")])

    return InlineKeyboardMarkup(inline_keyboard=kb_rows)

# ================== Spinner (animated + timeout) ==================
async def with_spinner(msg_obj, base_text: str, coro, timeout=30):
    spinner_chars = ["⏳", "🔎", "⌛️"]
    dots = ["", ".", "..", "..."]
    edit_msg = msg_obj
    task = asyncio.create_task(coro)
    try:
        i = 0
        start = asyncio.get_event_loop().time()
        while not task.done():
            elapsed = int(asyncio.get_event_loop().time() - start)
            if elapsed > timeout:
                task.cancel()
                try:
                    await edit_msg.edit_text("⏰ زمان جستجو تمام شد.")
                except Exception:
                    pass
                return None
            s = f"{spinner_chars[i % len(spinner_chars)]} {base_text}{dots[i % len(dots)]} ({elapsed}s)"
            try:
                await edit_msg.edit_text(s)
            except Exception:
                pass
            i += 1
            await asyncio.sleep(0.9)
        return await task
    finally:
        if task.cancelled():
            try:
                await edit_msg.edit_text("❌ جستجو لغو شد.")
            except Exception:
                pass

# ================== Handlers ==================
@dp.message(Command("start"))
async def start(msg: Message):
    reset_state(msg.from_user.id)
    text = (
        "👋 <b>سلام! خوش اومدی</b>\n\n"
        "یک دسته رو انتخاب کن:"
    )
    try:
        await msg.answer(text, reply_markup=main_menu_kb().as_markup())
    except TelegramForbiddenError:
        # user blocked the bot — ignore to avoid crashing the process
        logger.warning(f"کاربر {msg.from_user.id} بات را بلاک کرده؛ پیام ارسال نشد.")
    except Exception as e:
        logger.exception(f"خطا در ارسال /start: {e}")

# --- Menus (kept same) ---
def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 رباتیک", callback_data="cat_robotics")
    kb.button(text="🌐 اینترنت اشیا", callback_data="cat_iot")
    kb.button(text="🐍 پایتون (جستجوی محلی)", callback_data="py_home")
    kb.button(text="🔍 جستجوی آزاد GitHub", callback_data="search_free")
    kb.adjust(2)
    return kb

def projects_list_kb(domain: str):
    kb = InlineKeyboardBuilder()
    items = DB.get(domain, [])
    if not items:
        kb.button(text="موردی در دیتابیس نیست", callback_data="noop")
    else:
        for i, it in enumerate(items):
            title = it.get("title") or it.get("id") or f"item {i+1}"
            kb.button(text=f"• {title[:48]}", callback_data=f"proj_{domain}_{i}")
    # بازگشت به منوی اصلی
    kb.button(text="⬅️ بازگشت به منو اصلی", callback_data="back_main")
    kb.adjust(1)
    return kb

def language_menu_kb(domain: str, idx: int):
    item = DB.get(domain, [])[idx]
    codes = (item.get("code") or {})
    kb = InlineKeyboardBuilder()
    for lang_key, label in LANG_LABEL.items():
        if lang_key in codes:
            kb.button(text=label, callback_data=f"code_{domain}_{idx}_{lang_key}")
    kb.button(text="🔎 جستجوی قطعه‌ها", callback_data=f"find_parts_{domain}_{idx}")
    kb.button(text="🔎 جستجوی شماتیک", callback_data=f"find_schematic_{domain}_{idx}")
    kb.button(text="⬅️ بازگشت", callback_data=f"back_to_{domain}")
    kb.adjust(1)
    return kb

# --- Category handlers ---
@dp.callback_query(F.data == "cat_robotics")
async def cat_robotics(cb: CallbackQuery):
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "browse", "domain": "robotics", "facet": None, "last_domain": "robotics"}
    await safe_edit(cb.message, "🤖 لیست پروژه‌های رباتیک:", reply_markup=projects_list_kb("robotics").as_markup())
    await cb.answer()

@dp.callback_query(F.data == "cat_iot")
async def cat_iot(cb: CallbackQuery):
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "browse", "domain": "iot", "facet": None, "last_domain": "iot"}
    await safe_edit(cb.message, "🌐 لیست پروژه‌های اینترنت اشیا:", reply_markup=projects_list_kb("iot").as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("back_to_"))
async def back_to_domain(cb: CallbackQuery):
    # callback_data format: back_to_<domain>
    parts = cb.data.split("_", 2)
    domain = parts[2] if len(parts) > 2 else None
    if domain == "robotics":
        await safe_edit(cb.message, "🤖 لیست پروژه‌های رباتیک:", reply_markup=projects_list_kb("robotics").as_markup())
    elif domain == "iot":
        await safe_edit(cb.message, "🌐 لیست پروژه‌های اینترنت اشیا:", reply_markup=projects_list_kb("iot").as_markup())
    else:
        # fallback to main menu
        await safe_edit(cb.message, "🏠 منوی اصلی:", reply_markup=main_menu_kb().as_markup())
    await cb.answer()

@dp.callback_query(F.data.startswith("proj_"))
async def open_project(cb: CallbackQuery):
    parts = cb.data.split("_")
    # expected: ["proj", "<domain>", "<idx>"]
    if len(parts) < 3:
        await cb.answer("پروژه نامعتبر است.", show_alert=True); return
    domain = parts[1]; idx = int(parts[2])
    items = DB.get(domain, [])
    if idx < 0 or idx >= len(items):
        await cb.answer("پروژه نامعتبر است.", show_alert=True); return
    it = items[idx]
    # send a project card (visual)
    try:
        await send_project_card(cb.message.chat.id, it, domain=domain, idx=idx)
    except Exception:
        # fallback to textual view
        desc = it.get("description") or it.get("desc") or ""
        title = it.get("title") or it.get("id") or "پروژه"
        await safe_edit(
            cb.message,
            f"📦 <b>{_html.escape(title)}</b>\n{_html.escape(desc)}\n\n"
            "یک زبان رو انتخاب کن یا از گزینه‌های زیر استفاده کن:",
            reply_markup=language_menu_kb(domain, idx).as_markup()
        )
    await cb.answer()

@dp.callback_query(F.data.startswith("code_"))
async def show_code(cb: CallbackQuery):
    parts = cb.data.split("_")
    # expected: ["code", "<domain>", "<idx>", "<lang>"]
    if len(parts) < 4:
        await cb.answer("فرمت نامعتبری دارد.", show_alert=True); return
    domain = parts[1]; idx = int(parts[2]); lang = parts[3]
    items = DB.get(domain, [])
    if idx < 0 or idx >= len(items):
        await cb.answer("پروژه منقضی شده.", show_alert=True); return
    it = items[idx]
    code_map = it.get("code") or {}
    code = code_map.get(lang)
    if not code:
        await cb.answer("برای این زبان کدی موجود نیست.", show_alert=True); return
    safe = _html.escape(code)
    title = it.get("title") or it.get("id") or "پروژه"
    caption = f"💻 <b>{_html.escape(title)}</b> — {LANG_LABEL.get(lang, lang)}"
    kb = InlineKeyboardBuilder()
    kb.button(text="⬇️ دانلود", callback_data=f"download_{domain}_{idx}_{lang}")
    kb.button(text="⬅️ بازگشت", callback_data=f"back_to_{domain}")
    kb.adjust(2,1)
    if len(caption) + len(safe) < MAX_TEXT_LEN:
        await safe_edit(cb.message, f"{caption}\n\n<pre><code>{safe}</code></pre>", reply_markup=kb.as_markup())
    else:
        await cb.message.answer(caption, reply_markup=kb.as_markup())
        docname = f"{(it.get('id') or 'project')}_{lang}" + (".ino" if lang in ("c","cpp") else ".py")
        await cb.message.answer_document(BufferedInputFile(code.encode("utf-8"), filename=docname), caption="📄 کد طولانی بود، به‌صورت فایل ارسال شد.")
    await cb.answer()

@dp.callback_query(F.data.startswith("download_"))
async def download_code(cb: CallbackQuery):
    parts = cb.data.split("_")
    if len(parts) < 4:
        await cb.answer("فرمت نامعتبری دارد.", show_alert=True); return
    domain = parts[1]; idx = int(parts[2]); lang = parts[3]
    items = DB.get(domain, [])
    if idx < 0 or idx >= len(items):
        await cb.answer("پروژه منقضی شده.", show_alert=True); return
    it = items[idx]
    code_map = it.get("code") or {}
    code = code_map.get(lang)
    if not code:
        await cb.answer("کدی برای دانلود نیست.", show_alert=True); return
    docname = f"{(it.get('id') or 'project')}_{lang}" + (".ino" if lang in ("c","cpp") else ".py")
    await cb.message.answer_document(BufferedInputFile(code.encode("utf-8"), filename=docname), caption="⬇️ دانلود کد")
    await cb.answer()

@dp.callback_query(F.data.startswith("find_parts_"))
async def find_parts(cb: CallbackQuery):
    # parse safely: expected "find_parts_{domain}_{idx}"
    parts = cb.data.split("_")
    if len(parts) < 4:
        await cb.answer("فرمت نامعتبری دارد.", show_alert=True); return
    domain = parts[2]; idx = int(parts[3])
    items = DB.get(domain, [])
    if idx < 0 or idx >= len(items):
        await cb.answer("آیتم نامعتبر.", show_alert=True); return
    item = items[idx]
    title = item.get("title") or item.get("id") or ""
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "search", "domain": domain, "facet": "parts"}
    # send chat action (typing)
    try:
        await bot.send_chat_action(cb.message.chat.id, action="typing")
    except Exception:
        pass
    sent = await cb.message.answer("🔎 در حال آماده‌سازی جستجو...")
    queries = build_github_queries(domain, "parts", title)
    async def _search():
        return await github_code_search_multi(queries, per_page=5, cap=24)  # retrieve more for pagination
    results = await with_spinner(sent, "در حال جستجوی قطعه‌ها در GitHub", _search())
    if not results:
        await cb.message.answer("❌ چیزی برای قطعه‌ها پیدا نشد.")
    else:
        EXT_RESULTS[cb.from_user.id] = {"items": results, "source": "github", "domain": domain, "facet": "parts"}
        kb = results_kb(results, prefix="ext", domain=domain, facet="parts", page=0)
        await cb.message.answer("📌 <b>نتایج قطعه‌ها (BOM/parts):</b>", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("find_schematic_"))
async def find_schematic(cb: CallbackQuery):
    parts = cb.data.split("_")
    if len(parts) < 4:
        await cb.answer("فرمت نامعتبری دارد.", show_alert=True); return
    domain = parts[2]; idx = int(parts[3])
    items = DB.get(domain, [])
    if idx < 0 or idx >= len(items):
        await cb.answer("آیتم نامعتبر.", show_alert=True); return
    item = items[idx]
    title = item.get("title") or item.get("id") or ""
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "search", "domain": domain, "facet": "schematic"}
    try:
        await bot.send_chat_action(cb.message.chat.id, action="typing")
    except Exception:
        pass
    sent = await cb.message.answer("🔎 در حال آماده‌سازی جستجو...")
    queries = build_github_queries(domain, "schematic", title)
    async def _search():
        return await github_code_search_multi(queries, per_page=5, cap=24)
    results = await with_spinner(sent, "در حال جستجوی شماتیک در GitHub", _search())
    if not results:
        await cb.message.answer("❌ چیزی برای شماتیک پیدا نشد.")
    else:
        EXT_RESULTS[cb.from_user.id] = {"items": results, "source": "github", "domain": domain, "facet": "schematic"}
        kb = results_kb(results, prefix="ext", domain=domain, facet="schematic", page=0)
        await cb.message.answer("📌 <b>نتایج شماتیک:</b>", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data == "search_free")
async def do_search_free(cb: CallbackQuery):
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "search_free", "domain": None, "facet": None}
    await cb.answer()
    await cb.message.answer("🔍 عبارت جستجوی آزاد GitHub رو بفرست (مثال: <code>fastapi language:python in:file</code>)")

@dp.callback_query(F.data == "py_home")
async def py_home(cb: CallbackQuery):
    st = USER_STATE.get(cb.from_user.id) or {}
    USER_STATE[cb.from_user.id] = {**st, "mode": "py", "domain": "python", "facet": "code"}
    kb = InlineKeyboardBuilder()
    kb.button(text="🚪 خروج از حالت پایتون", callback_data="py_exit")
    kb.button(text="⬅️ بازگشت به منو اصلی", callback_data="back_main")
    kb.adjust(2)
    await safe_edit(
        cb.message,
        "🐍 <b>جستجوی پایتون (محلی + GitHub)</b>\n"
        "نام کتابخانه یا موضوع رو بفرست (مثال: <code>requests</code> یا <code>تلگرام bot</code>).",
        reply_markup=kb.as_markup()
    )
    await cb.answer()

@dp.callback_query(F.data == "py_exit")
async def py_exit(cb: CallbackQuery):
    reset_state(cb.from_user.id)
    await safe_edit(cb.message, "✅ از حالت پایتون خارج شدی.", reply_markup=main_menu_kb().as_markup())
    await cb.answer()

@dp.message()
async def handle_query(msg: Message):
    q = (msg.text or "").strip()
    if not q:
        return
    # ensure user state exists
    USER_STATE.setdefault(msg.from_user.id, {"mode": None, "domain": None, "facet": None, "last_domain": None})
    st = USER_STATE.get(msg.from_user.id) or {"mode": None, "domain": None, "facet": None}

    # show typing indicator for longer operations
    try:
        await bot.send_chat_action(msg.chat.id, action="typing")
    except Exception:
        pass

    if st["mode"] == "py":
        sent = await msg.answer("⏳ آماده‌سازی جستجوی پایتون...")
        async def _search():
            local = local_search(domain="python", facet="code", query=q, limit=8)
            if local:
                return {"source": "local", "items": local}
            query = f'{q} language:python in:file'
            items = await github_code_search_multi([query], per_page=5, cap=24)
            if not items:
                query2 = f'{q} language:python filename:README in:file'
                items = await github_code_search_multi([query2], per_page=5, cap=24)
            return {"source": "github", "items": items}
        res = await with_spinner(sent, "در حال جستجوی پایتون (محلی → GitHub)", _search())
        if not res or not res.get("items"):
            await msg.answer("❌ چیزی پیدا نشد. یک کلیدواژه‌ی ساده‌تر امتحان کن.")
            return
        EXT_RESULTS[msg.from_user.id] = {"items": res["items"], "source": res["source"], "domain": "python", "facet": "code"}
        kb = results_kb(res["items"], prefix="ext", domain="python", facet="code", page=0)
        await msg.answer("📌 <b>نتایج پایتون:</b>", reply_markup=kb)
        return

    if st["mode"] == "search" and st["domain"] and st["facet"]:
        domain = st["domain"]; facet = st["facet"]
        sent = await msg.answer("⏳ اول از دیتابیس محلی می‌گردم...")
        async def _search():
            local = local_search(domain=domain, facet=facet, query=q, limit=8)
            if local:
                return {"source":"local","items":local}
            queries = build_github_queries(domain, facet, q)
            items = await github_code_search_multi(queries, per_page=5, cap=24)
            if not items and facet != "code":
                items = await github_code_search_multi([q + " in:file"], per_page=5, cap=24)
            return {"source":"github","items":items}
        res = await with_spinner(sent, "در حال جستجو (محلی → GitHub)", _search())
        if not res or not res.get("items"):
            await msg.answer("❌ چیزی پیدا نشد. کلیدواژه‌ی دقیق‌تر بده.")
            return
        EXT_RESULTS[msg.from_user.id] = {"items": res["items"], "source": res["source"], "domain": domain, "facet": facet}
        kb = results_kb(res["items"], prefix="ext", domain=domain, facet=facet, page=0)
        await msg.answer(f"📌 <b>نتایج ({domain} / {FACETS[facet]['label']}):</b>", reply_markup=kb)
        return

    if st.get("mode") == "search_free":
        sent = await msg.answer("⏳ در حال جستجوی آزاد روی GitHub...")
        async def _search():
            return await github_code_search_multi([q], per_page=5, cap=24)
        results = await with_spinner(sent, "در حال جستجوی آزاد روی GitHub", _search())
        if not results:
            await msg.answer("❌ چیزی پیدا نشد.")
            return
        EXT_RESULTS[msg.from_user.id] = {"items": results, "source": "github"}
        kb = results_kb(results, prefix="ext", page=0)
        await msg.answer("📌 <b>نتایج جستجو:</b>", reply_markup=kb)
        return

    # default free search
    await msg.answer("⏳ در حال جستجوی آزاد روی GitHub...")
    try:
        results = await github_code_search_multi([q], per_page=5, cap=24)
        if not results:
            await msg.answer("❌ چیزی پیدا نشد.")
            return
        EXT_RESULTS[msg.from_user.id] = {"items": results, "source": "github"}
        kb = results_kb(results, prefix="ext", page=0)
        await msg.answer("📌 <b>نتایج جستجو:</b>", reply_markup=kb)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            reset = e.response.headers.get("X-RateLimit-Reset")
            logger.warning(f"GitHub 403 rate limit; reset={reset}")
            await msg.answer("⚠️ GitHub rate limit. اگر شد در env یک GITHUB_TOKEN ست کن.")
        else:
            await msg.answer(f"⚠️ خطای GitHub: {e}")
    except Exception as e:
        await msg.answer(f"⚠️ خطا: {e}")

@dp.callback_query(F.data.startswith("local_open_"))
async def local_open(cb: CallbackQuery):
    user_id = cb.from_user.id
    st = EXT_RESULTS.get(user_id)
    if not st or not st.get("items"):
        await cb.answer("🔎 نتیجه‌ای مرتبط وجود ندارد — دوباره جستجو کن.", show_alert=True)
        return

    items = st.get("items") or []
    try:
        idx = int(cb.data.split("_", 2)[2])
    except Exception:
        await cb.answer("نامعتبر", show_alert=True)
        return

    if idx < 0 or idx >= len(items):
        await cb.answer("⏰ منقضی شده", show_alert=True)
        return

    item = items[idx]
    source = st.get("source", "local")

    # اگر نتیجه از GitHub آمده و raw_url موجود است، آن را دانلود کن و نمایش بده
    if source == "github" and item.get("raw_url"):
        try:
            code = await fetch_text(item["raw_url"], headers=_gh_headers())
        except Exception:
            # نشد مستقیم بخونیم -> ارجاع به GitHub
            await cb.message.answer(
                f"❌ دانلود مستقیم نشد.\n"
                f"🔗 <a href='{item.get('html_url','')}'>مشاهده در GitHub</a>\n"
                f"📁 <code>{item.get('repo','')}/{item.get('path','')}</code>",
                disable_web_page_preview=False
            )
            await cb.answer()
            return

        caption = (
            f"🔗 <a href='{item.get('html_url','')}'>مشاهده در GitHub</a>\n"
            f"📁 <code>{item.get('repo','')}/{item.get('path','')}</code>\n"
            f"⚠️ لایسنس رو چک کن."
        )
        safe = _html.escape(code)
        if len(caption) + len(safe) < MAX_TEXT_LEN:
            await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>\n\n{caption}")
        else:
            filename = item.get("name") or (item.get("path") or "snippet.txt").split("/")[-1]
            doc = BufferedInputFile(code.encode("utf-8"), filename=filename)
            await cb.message.answer_document(doc, caption=caption)
        await cb.answer()
        return

    # در غیر این صورت فرض می‌کنیم آیتم محلی از DB است و باید فیلدهای facet را چک کنیم
    state = USER_STATE.get(user_id) or {}
    facet = state.get("facet", "code")
    fields = FACETS.get(facet, FACETS["code"])["fields"]

    content = None
    # آیتم‌های محلی معمولاً فیلدها را دارند؛ اولین فیلد غیرخالی را بگیر
    for f in fields:
        if item.get(f):
            content = str(item.get(f))
            break

    if not content:
        await cb.message.answer("❌ برای این مورد، محتوای مرتبط در DB وجود ندارد.")
        await cb.answer()
        return

    # اگر مقدار یک URL است سعی کن آن را دریافت کنی
    if re.match(r"^https?://", content):
        try:
            body = await fetch_text(content)
            content = body
        except Exception:
            await cb.message.answer(f"🔗 <a href='{content}'>مشاهده محتوا</a>", disable_web_page_preview=False)
            await cb.answer()
            return

    safe = _html.escape(content)
    if len(safe) < MAX_TEXT_LEN:
        await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>")
    else:
        doc = BufferedInputFile(content.encode("utf-8"), filename=f"{facet}.txt")
        await cb.message.answer_document(doc, caption=f"📄 {FACETS[facet]['label']}")
    await cb.answer()

@dp.callback_query(F.data.startswith("ext_open_"))
async def ext_open(cb: CallbackQuery):
    user_id = cb.from_user.id
    st = EXT_RESULTS.get(user_id) or {}
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
            f"🔗 <a href='{item.get('html_url','')}'>مشاهده در GitHub</a>\n"
            f"📁 <code>{item.get('repo','')}/{item.get('path','')}</code>"
        )
        await cb.answer(); return
    caption = (
        f"🔗 <a href='{item.get('html_url','')}'>مشاهده در GitHub</a>\n"
        f"📁 <code>{item.get('repo')}/{item.get('path')}</code>\n"
        f"⚠️ لایسنس رو چک کن."
    )
    safe = _html.escape(code)
    if len(caption) + len(safe) < MAX_TEXT_LEN:
        await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>\n\n{caption}")
    else:
        doc = BufferedInputFile(code.encode("utf-8"), filename=item.get("name") or "snippet.txt")
        await cb.message.answer_document(doc, caption=caption)
    await cb.answer()

@dp.callback_query(F.data.startswith("ext_page_"))
async def ext_page_cb(cb: CallbackQuery):
    """
    Handle pagination for ext results. Callback data: ext_page_{page}
    """
    user_id = cb.from_user.id
    parts = cb.data.split("_")
    if len(parts) < 3:
        await cb.answer("نامعتبر", show_alert=True); return
    try:
        page = int(parts[2])
    except Exception:
        await cb.answer("نامعتبر", show_alert=True); return

    st = EXT_RESULTS.get(user_id)
    if not st or not st.get("items"):
        await cb.answer("نتیجه‌ای موجود نیست.", show_alert=True); return
    items = st["items"]
    domain = st.get("domain")
    facet = st.get("facet")
    kb = results_kb(items, prefix="ext", domain=domain, facet=facet, page=page)
    # edit message to show new page keyboard
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        try:
            await cb.message.answer("صفحه جدید:", reply_markup=kb)
        except Exception:
            pass
    await cb.answer()

# back to main handler
@dp.callback_query(F.data == "back_main")
async def back_main(cb: CallbackQuery):
    reset_state(cb.from_user.id)
    await safe_edit(cb.message, "🏠 منوی اصلی:", reply_markup=main_menu_kb().as_markup())
    await cb.answer()

# noop handler and fallback for unknown callbacks (UX safety)
@dp.callback_query(F.data == "noop")
async def noop_cb(cb: CallbackQuery):
    await cb.answer("⦿ در دیتابیس موردی وجود ندارد.", show_alert=False)

@dp.callback_query()
async def unknown_callback(cb: CallbackQuery):
    # این handler به عنوان fallback برای callbackهای نامشخص عمل می‌کند
    logger.info(f"Unknown callback received: {cb.data} from {cb.from_user.id}")
    try:
        await cb.answer("⦿ این دکمه دیگر معتبر نیست یا من آن را نمی‌شناسم.", show_alert=False)
    except Exception:
        pass

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
