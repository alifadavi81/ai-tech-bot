# bot.py
import os
import re
import io
import json
import html as _html
import zipfile
import logging
from typing import Iterable, List, Dict, Any, Tuple

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
    InputMediaPhoto,
    InputMediaDocument,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import httpx

# ======================= Logging =======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ai-tech-bot")

# ======================= ENV & Config =======================
def _require_env_any(*names: str) -> str:
    """از بین چند اسم اولین env موجود رو برگردون؛ اگر هیچکدوم نبود خطا بده."""
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    raise RuntimeError(f"❌ یکی از این متغیرها باید ست شود: {', '.join(names)}")

# توکن: هر کدوم بود قبول
BOT_TOKEN = _require_env_any("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")

# URL پابلیک رندر (بدون / آخر هم اوکی)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook")
if not WEBHOOK_PATH.startswith("/"):
    WEBHOOK_PATH = "/" + WEBHOOK_PATH

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret123")
PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "projects.json")
# اختیاری: برای محدودکردن 403/402 در GitHub
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# ======================= Bot / Dispatcher =======================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# ======================= Load DB =======================
if not os.path.exists(DB_PATH):
    logger.warning("⚠️ فایل %s پیدا نشد؛ از دیتابیس خالی استفاده می‌کنم.", DB_PATH)
    db: Dict[str, Any] = {"robotics": [], "iot": [], "py_libs": []}
else:
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            db: Dict[str, Any] = json.load(f) or {}
    except Exception as e:
        logger.exception("failed to load DB %s: %s", DB_PATH, e)
        db = {"robotics": [], "iot": [], "py_libs": []}

logger.info("Loaded %s with keys: %s", DB_PATH, list(db.keys()))
robotics: List[Dict[str, Any]] = db.get("robotics", [])
iot: List[Dict[str, Any]] = db.get("iot", [])
py_libs: List[Dict[str, Any]] = db.get("py_libs", [])

# ======================= In-Memory State =======================
USER_STATE: Dict[int, str] = {}        # 'search'
CURRENT_LIB: Dict[int, str] = {}
LAST_QUERY: Dict[int, str] = {}
SEARCH_FILTER: Dict[int, str] = {}     # any/arduino/cpp/micropython
SEARCH_MODE: Dict[int, str] = {}       # code/schematic/parts/howto

EXT_RESULTS: Dict[int, List[Dict[str, str]]] = {}  # کد
EXT_SCHEM: Dict[int, List[Dict[str, str]]] = {}    # شماتیک/تصویر
EXT_README: Dict[int, List[Dict[str, str]]] = {}   # README/how-to

# ======================= Helpers =======================
TG_MAX = 4096
IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
DOC_EXTS = (".svg", ".fzz", ".sch", ".kicad_sch", ".kicad_pcb", ".brd", ".pcb", ".fcstd", ".dxf", ".pdf", ".md")

def chunk_text(s: str, n: int) -> Iterable[str]:
    for i in range(0, len(s), n):
        yield s[i:i + n]

def safe_get_items_by_cat(category: str) -> List[Dict[str, Any]]:
    if category == "robotics":
        return robotics
    if category == "iot":
        return iot
    return []

async def safe_edit(message: Message, text: str, **kwargs):
    # جلوگیری از 400/409 در ادیت
    try:
        await message.edit_text(text, **kwargs)
    except Exception:
        await message.answer(text, **kwargs)

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _trim(s: str, n: int = 200) -> str:
    s = (s or "").strip()
    return (s[: n - 1] + "…") if len(s) > n else s

async def fetch_text(url: str, timeout: int = 25) -> str:
    try:
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "ai-tech-bot/1.0"}) as c:
            r = await c.get(url)
            if r.status_code >= 400:
                raise RuntimeError(f"Download failed (status {r.status_code}).")
            return r.text
    except Exception as e:
        logger.exception("fetch_text error: %s", e)
        raise

# ======================= GitHub Search (Robust) =======================
def _base_headers() -> Dict[str, str]:
    h = {
        "Accept": "application/vnd.github.text-match+json",
        "User-Agent": "ai-tech-bot/1.0 (+https://github.com/)",  # ضروری برای GitHub
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h

def _sanitize_query(q: str) -> str:
    # تمیز کردن کوئری برای جلوگیری از 422
    q = (q or "").strip()
    q = q.replace("\n", " ").replace("\r", " ")
    q = re.sub(r"\s+", " ", q)
    return q if len(q) >= 2 else (q + " arduino")

def _clamp_per_page(n: int, default: int = 10, lo: int = 1, hi: int = 10) -> int:
    try:
        n = int(n)
    except Exception:
        n = default
    return max(lo, min(hi, n))

def _lang_bias(filter_: str) -> str:
    if filter_ == "arduino":
        return '(language:Arduino OR extension:ino)'
    if filter_ == "cpp":
        return '(language:C++ OR extension:cpp OR extension:h)'
    if filter_ == "micropython":
        return '((micropython OR "MicroPython") OR (language:Python extension:py))'
    return ""  # any

def _compose_query(user_q: str, bias: str, extra: str = "") -> str:
    base = _sanitize_query(user_q)
    parts = [base, '(arduino OR "esp32" OR micropython OR robotics OR iot)']
    if bias:
        parts.append(bias)
    if extra:
        parts.append(extra)
    return " ".join([p for p in parts if p])

async def _http_get_json(url: str, params: Dict[str, str] | None = None) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=25, headers=_base_headers()) as c:
            r = await c.get(url, params=params)
            if r.status_code >= 400:
                # همه خطاها را لاگ کن ولی پیام عمومی برگردان
                logger.error("GitHub API error %s: %s | q=%s", r.status_code, r.text[:400], (params or {}).get("q", ""))
                raise RuntimeError(f"GitHub request failed (status {r.status_code}).")
            return r.json()
    except Exception as e:
        logger.exception("Github JSON fetch failed: %s", e)
        raise RuntimeError("GitHub request failed.")

def _to_raw_url(repo_html_url: str, path: str) -> str:
    raw = re.sub(r"^https://github.com/", "https://raw.githubusercontent.com/", repo_html_url.rstrip("/") + "/")
    return raw + "HEAD/" + path.lstrip("/")

async def github_code_search(query: str, per_page: int, filter_: str) -> List[Dict[str, str]]:
    if not query:
        return []
    bias = _lang_bias(filter_)
    q = _compose_query(query, bias, extra="in:file,readme")

    url = "https://api.github.com/search/code"
    params = {
        "q": q,
        "per_page": str(_clamp_per_page(per_page, default=8, hi=10)),
        "sort": "best-match",
        "order": "desc",
    }
    data = await _http_get_json(url, params=params)

    out: List[Dict[str, str]] = []
    for item in data.get("items", []):
        name = item.get("name") or "code"
        path = item.get("path") or ""
        repo = (item.get("repository") or {}).get("full_name") or ""
        html_url_repo = (item.get("repository") or {}).get("html_url") or ""
        html_url = item.get("html_url") or ""
        raw_url = _to_raw_url(html_url_repo, path) if (html_url_repo and path) else html_url
        out.append({"title": f"{name} — {repo}/{path}", "html_url": html_url, "raw_url": raw_url})
    return out

async def github_schematic_search(query: str, per_page: int) -> List[Dict[str, str]]:
    if not query:
        return []
    filters = (
        '(extension:png OR extension:jpg OR extension:jpeg OR extension:webp OR '
        'extension:svg OR extension:fzz OR extension:sch OR extension:kicad_sch OR '
        'extension:kicad_pcb OR extension:pdf)'
    )
    q = _compose_query(query, bias="", extra=f"{filters} in:path")

    url = "https://api.github.com/search/code"
    params = {
        "q": q,
        "per_page": str(_clamp_per_page(per_page, default=10, hi=10)),
        "sort": "best-match",
        "order": "desc",
    }
    data = await _http_get_json(url, params=params)

    out: List[Dict[str, str]] = []
    for item in data.get("items", []):
        name = item.get("name") or "file"
        path = item.get("path") or ""
        repo = (item.get("repository") or {}).get("full_name") or ""
        html_url_repo = (item.get("repository") or {}).get("html_url") or ""
        html_url = item.get("html_url") or ""
        raw_url = _to_raw_url(html_url_repo, path) if (html_url_repo and path) else html_url
        ext = ("." + name.split(".")[-1].lower()) if "." in name else ""
        out.append({"title": f"{name} — {repo}/{path}", "html_url": html_url, "raw_url": raw_url, "ext": ext})
    return out

async def github_readme_search(query: str, per_page: int) -> List[Dict[str, str]]:
    if not query:
        return []

    url = "https://api.github.com/search/code"
    # کوئری اصلی ساده + fallbackها (برای پارسر جدید GitHub)
    queries = [
        _compose_query(query, bias="", extra='filename:README in:file (extension:md OR extension:markdown OR extension:rst)'),
        _compose_query(query, bias="", extra='in:readme'),  # ایندکس اختصاصی README
        _compose_query(query, bias="", extra='filename:README.md in:path'),
        _compose_query(query, bias="", extra='filename:README in:file extension:md'),
    ]

    data = None
    last_err: Exception | None = None
    for q in queries:
        params = {
            "q": q,
            "per_page": str(_clamp_per_page(per_page, default=8, hi=10)),
            "sort": "best-match",
            "order": "desc",
        }
        try:
            data = await _http_get_json(url, params=params)
            break
        except Exception as e:
            last_err = e
            continue

    if not data:
        if last_err:
            raise last_err
        return []

    out: List[Dict[str, str]] = []
    for item in data.get("items", []):
        name = item.get("name") or "README.md"
        path = item.get("path") or ""
        repo = (item.get("repository") or {}).get("full_name") or ""
        html_url_repo = (item.get("repository") or {}).get("html_url") or ""
        html_url = item.get("html_url") or ""
        raw_url = _to_raw_url(html_url_repo, path) if (html_url_repo and path) else html_url
        ext = ("." + name.split(".")[-1].lower()) if "." in name else ".md"
        out.append({"title": f"{name} — {repo}/{path}", "html_url": html_url, "raw_url": raw_url, "ext": ext})
    return out

# ======================= Keyboards =======================
def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🤖 رباتیک", callback_data="cat_robotics")
    kb.button(text="🌐 اینترنت اشیا (IoT)", callback_data="cat_iot")
    kb.button(text="🐍 پایتون", callback_data="cat_libs")
    kb.button(text="🔎 جستجو", callback_data="search_start")
    kb.adjust(1)
    return kb.as_markup()

def list_projects(category: str):
    items = safe_get_items_by_cat(category)
    kb = InlineKeyboardBuilder()
    for p in items:
        kb.button(text=p.get("title", "—"), callback_data=f"proj_{category}_{p.get('id','')}")
    kb.button(text="🔙 بازگشت", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

def list_libs():
    kb = InlineKeyboardBuilder()
    for lib in py_libs:
        name = lib.get("name", "—")
        kb.button(text=name, callback_data=f"lib_{name}")
    kb.button(text="🔙 بازگشت", callback_data="back_main")
    kb.adjust(2)
    return kb.as_markup()

def code_menu(category: str, proj_id: str, proj: Dict[str, Any], current_lang: str | None = None):
    kb = InlineKeyboardBuilder()
    for lang in ("c", "cpp", "micropython"):
        kb.button(text=lang.upper(), callback_data=f"code_{category}_{proj_id}_{lang}")
    if current_lang and (proj.get("code") or {}).get(current_lang):
        kb.button(text=f"⬇️ دانلود {current_lang.upper()}", callback_data=f"dls_{category}_{proj_id}_{current_lang}")
    kb.button(text="🗜️ دانلود همه (ZIP)", callback_data=f"zip_{category}_{proj_id}")
    kb.button(text="🔙 بازگشت", callback_data=f"back_projlist_{category}")
    kb.adjust(2, 2)
    return kb.as_markup()

def back_to_libs():
    kb = InlineKeyboardBuilder()
    kb.button(text="⬇️ دانلود مثال", callback_data="dllib_example")
    kb.button(text="⬇️ JSON کتابخانه", callback_data="dllib_json")
    kb.button(text="🔙 بازگشت", callback_data="cat_libs")
    kb.adjust(2)
    return kb.as_markup()

def search_results_kb(results: List[Tuple[str, str, str]]):
    kb = InlineKeyboardBuilder()
    for kind, key, title in results:
        if kind == "proj":
            cat, pid = key.split("_", 1)
            kb.button(text=f"📁 {title}", callback_data=f"proj_{cat}_{pid}")
        elif kind == "lib":
            kb.button(text=f"🐍 {title}", callback_data=f"lib_{key}")
    kb.button(text="🔙 بازگشت", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

def ext_results_kb(results: List[Dict[str, str]]):
    kb = InlineKeyboardBuilder()
    for i, it in enumerate(results):
        kb.button(text=f"📄 {_trim(it['title'], 60)}", callback_data=f"ext_open_{i}")
    kb.button(text="🔙 بازگشت", callback_data="back_main")
    kb.adjust(1)
    return kb.as_markup()

def search_mode_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="💻 کُد", callback_data="mode_code")
    kb.button(text="🧩 شماتیک/دیـاگرام", callback_data="mode_schematic")
    kb.button(text="🛒 قطعات (BOM)", callback_data="mode_parts")
    kb.button(text="📘 نحوهٔ عملکرد/راهنما", callback_data="mode_howto")
    kb.button(text="🎛️ فیلتر زبان وب (اختیاری)", callback_data="ext_filter_menu")
    kb.button(text="🔙 بازگشت", callback_data="back_main")
    kb.adjust(2, 2, 1, 1)
    return kb.as_markup()

def filter_menu_kb(current: str):
    kb = InlineKeyboardBuilder()
    for key, title in [("any", "Any"), ("arduino", "Arduino"), ("cpp", "C++"), ("micropython", "MicroPython")]:
        prefix = "✅ " if key == current else ""
        kb.button(text=f"{prefix}{title}", callback_data=f"set_filter_{key}")
    kb.button(text="🔙 پایان", callback_data="ext_filter_close")
    kb.adjust(2)
    return kb.as_markup()

# ======================= Local Search =======================
def _search_projects_by_any(query: str) -> List[Tuple[str, str, str]]:
    q = _norm(query)
    if not q:
        return []
    results: List[Tuple[str, str, str]] = []
    for cat, items in (("robotics", robotics), ("iot", iot)):
        for p in items:
            hay = " ".join([
                str(p.get("title", "")),
                str(p.get("description", "")),
                ",".join(p.get("boards", []) or []),
                ",".join(p.get("parts", []) or []),
            ]).lower()
            if q in hay:
                results.append(("proj", f"{cat}_{p.get('id','')}", p.get("title", "(بدون عنوان)")))
    for lib in py_libs:
        hay = " ".join([
            str(lib.get("name", "")),
            str(lib.get("category", "")),
            str(lib.get("description", "")),
        ]).lower()
        if q in hay:
            results.append(("lib", lib.get("name", ""), lib.get("name", "(lib)")))
    return results[:50]

def _search_projects_by_parts(query: str) -> List[Tuple[str, str, str]]:
    q = _norm(query)
    if not q:
        return []
    out: List[Tuple[str, str, str]] = []
    for cat, items in (("robotics", robotics), ("iot", iot)):
        for p in items:
            parts = [str(x).lower() for x in (p.get("parts") or [])]
            if any(q in part for part in parts):
                out.append(("proj", f"{cat}_{p.get('id','')}", p.get("title", "(بدون عنوان)")))
    return out[:50]

def _search_projects_by_desc(query: str) -> List[Tuple[str, str, str]]:
    q = _norm(query)
    if not q:
        return []
    out: List[Tuple[str, str, str]] = []
    for cat, items in (("robotics", robotics), ("iot", iot)):
        for p in items:
            if q in str(p.get("description", "")).lower():
                out.append(("proj", f"{cat}_{p.get('id','')}", p.get("title", "(بدون عنوان)")))
    return out[:50]

# ======================= Handlers =======================
@dp.message(CommandStart())
async def start_cmd(msg: Message):
    await msg.answer("سلام 👋\nاز منو یکی از دسته‌بندی‌ها رو انتخاب کن:", reply_markup=main_menu())

# --- Search flow ---
@dp.callback_query(F.data == "search_start")
async def search_start(cb: CallbackQuery):
    SEARCH_MODE.pop(cb.from_user.id, None)
    USER_STATE.pop(cb.from_user.id, None)
    await safe_edit(cb.message, "اول مشخص کن دنبال چی هستی:", reply_markup=search_mode_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("mode_"))
async def choose_mode(cb: CallbackQuery):
    mode = cb.data.split("_", 1)[1]  # code/schematic/parts/howto
    if mode not in ("code", "schematic", "parts", "howto"):
        await cb.answer("حالت نامعتبر.", show_alert=True); return
    SEARCH_MODE[cb.from_user.id] = mode
    USER_STATE[cb.from_user.id] = "search"
    prompt = {
        "code": "🔎 کلمه/موضوع کُد رو بفرست (مثلاً: esp32 mqtt).",
        "schematic": "🔎 موضوع شماتیک/دیـاگرام رو بفرست (مثلاً: line follower schematic).",
        "parts": "🔎 نام قطعه یا مدل رو بفرست (مثلاً: L298N یا HC-SR04).",
        "howto": "🔎 موضوع راهنما/نحوه عملکرد رو بفرست (مثلاً: servo sweep arduino).",
    }[mode]
    await safe_edit(cb.message, prompt)
    await cb.answer()

@dp.message(F.text)
async def on_text(msg: Message):
    if USER_STATE.get(msg.from_user.id) != "search":
        await msg.answer("برای شروع از منوی زیر استفاده کن:", reply_markup=main_menu())
        return

    query = msg.text or ""
    LAST_QUERY[msg.from_user.id] = query
    mode = SEARCH_MODE.get(msg.from_user.id, "code")

    try:
        if mode == "code":
            local = _search_projects_by_any(query)
            if local:
                await msg.answer("✅ نتایج داخلی (پروژه/کتابخانه):", reply_markup=search_results_kb(local))
                return
            await msg.answer("دارم توی وب می‌گردم برای کد… ⏳")
            flt = SEARCH_FILTER.get(msg.from_user.id, "any")
            ext = await github_code_search(query, per_page=8, filter_=flt)
            if not ext:
                await msg.answer("❌ کدی در وب پیدا نشد. عبارت دقیق‌تر بده یا فیلتر زبان را عوض کن (🎛️).")
                return
            EXT_RESULTS[msg.from_user.id] = ext
            await msg.answer("نتایج یافت‌شده در GitHub (کد):", reply_markup=ext_results_kb(ext))
            return

        if mode == "schematic":
            await msg.answer("دارم شماتیک‌ها رو از وب میارم… ⏳")
            items = await github_schematic_search(query, per_page=10)
            if not items:
                await msg.answer("❌ شماتیکی پیدا نشد.")
                return
            EXT_SCHEM[msg.from_user.id] = items
            photos: List[InputMediaPhoto] = []
            docs: List[InputMediaDocument] = []
            for it in items[:10]:
                url = it["raw_url"]; cap = f"{it['title']}\nمنبع: {it['html_url']}"
                ext = (it.get("ext") or "").lower()
                if any(ext.endswith(x) for x in IMG_EXTS):
                    photos.append(InputMediaPhoto(media=url, caption=cap if not photos else None))
                else:
                    docs.append(InputMediaDocument(media=url, caption=cap))
            if photos:
                await msg.answer_media_group(photos[:10])
            for d in docs[:6]:
                await msg.answer_document(d)
            await msg.answer("✅ شماتیک‌ها ارسال شد. (به لایسنس/منبع توجه کن)")
            return

        if mode == "parts":
            hits = _search_projects_by_parts(query)
            if hits:
                await msg.answer("✅ پروژه‌هایی که با این قطعه مرتبط‌اند:", reply_markup=search_results_kb(hits))
            else:
                await msg.answer("❌ در دیتابیس داخلی پروژه‌ای با این قطعه پیدا نشد. (می‌خوای به‌جاش کُد یا شماتیک بگردم؟ از «🔎 جستجو» حالت رو عوض کن.)")
            return

        if mode == "howto":
            local = _search_projects_by_desc(query)
            if local:
                await msg.answer("✅ راهنما/توضیح در پروژه‌های داخلی:", reply_markup=search_results_kb(local))
                return
            await msg.answer("دارم راهنماهای README/MD رو از وب میارم… ⏳")
            rd = await github_readme_search(query, per_page=8)
            if not rd:
                await msg.answer("❌ راهنمایی در وب پیدا نشد.")
                return
            EXT_README[msg.from_user.id] = rd
            kb = InlineKeyboardBuilder()
            for i, it in enumerate(rd):
                kb.button(text=f"📘 {_trim(it['title'], 60)}", callback_data=f"ext_readme_{i}")
            kb.button(text="🔙 بازگشت", callback_data="back_main")
            kb.adjust(1)
            await msg.answer("راهنماهای مرتبط:", reply_markup=kb.as_markup())
            return

    except RuntimeError as e:
        # پیام عمومی و بدون لو دادن جزئیات
        await msg.answer("⚠️ خطای ارتباط با منبع کد. لطفاً بعداً دوباره تلاش کن یا عبارت جستجو را تغییر بده.")
    except Exception as e:
        logger.exception("search flow failed: %s", e)
        await msg.answer("❌ خطای غیرمنتظره در جستجو. دوباره امتحان کن یا کوئری رو تغییر بده.")

# --- External openers ---
@dp.callback_query(F.data.startswith("ext_open_"))
async def ext_open(cb: CallbackQuery):
    try:
        idx = int(cb.data.split("_", 2)[2])
        ext = EXT_RESULTS.get(cb.from_user.id) or []
        item = ext[idx]
    except Exception:
        await cb.answer("نامعتبر", show_alert=True); return

    try:
        code = await fetch_text(item["raw_url"])
        caption = f"منبع: {item['html_url']}\n⚠️ توجه به لایسنس/کپی‌رایت سورس"
        safe = _html.escape(code)
        if len(caption) + len(safe) < 3500:
            await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>\n\n{_html.escape(caption)}")
        else:
            doc = BufferedInputFile(code.encode("utf-8"), filename="snippet.txt")
            await cb.message.answer_document(doc, caption=caption)
    except Exception:
        await cb.message.answer("دانلود کد ناموفق بود.")
    await cb.answer()

@dp.callback_query(F.data.startswith("ext_readme_"))
async def ext_readme(cb: CallbackQuery):
    try:
        idx = int(cb.data.split("_", 2)[2])
        rd = EXT_README.get(cb.from_user.id) or []
        item = rd[idx]
    except Exception:
        await cb.answer("نامعتبر", show_alert=True); return

    try:
        txt = await fetch_text(item["raw_url"])
        cap = f"منبع: {item['html_url']}"
        safe = _html.escape(txt)
        if len(cap) + len(safe) < 3500:
            await safe_edit(cb.message, f"<pre><code>{safe}</code></pre>\n\n{_html.escape(cap)}")
        else:
            doc = BufferedInputFile(txt.encode("utf-8"), filename="README.txt")
            await cb.message.answer_document(doc, caption=cap)
    except Exception:
        await cb.message.answer("دانلود README ناموفق بود.")
    await cb.answer()

# --- Filter menus ---
@dp.callback_query(F.data == "ext_filter_menu")
async def ext_filter_menu(cb: CallbackQuery):
    cur = SEARCH_FILTER.get(cb.from_user.id, "any")
    await safe_edit(cb.message, f"🎛️ فیلتر فعلی: <b>{cur}</b>\nیک گزینه انتخاب کن:", reply_markup=filter_menu_kb(cur))
    await cb.answer()

@dp.callback_query(F.data.startswith("set_filter_"))
async def set_filter(cb: CallbackQuery):
    key = cb.data.split("_", 2)[2]
    if key not in ("any", "arduino", "cpp", "micropython"):
        await cb.answer("نامعتبر", show_alert=True); return
    SEARCH_FILTER[cb.from_user.id] = key
    await safe_edit(
        cb.message,
        f"✅ فیلتر زبان روی <b>{key}</b> تنظیم شد.\nحالا دوباره جستجو کن (از منوی «🔎 جستجو» حالت رو انتخاب کن).",
        reply_markup=search_mode_kb()
    )
    await cb.answer("فیلتر تنظیم شد")

@dp.callback_query(F.data == "ext_filter_close")
async def ext_filter_close(cb: CallbackQuery):
    await safe_edit(cb.message, "اوکی.", reply_markup=search_mode_kb())
    await cb.answer()

# --- Categories & projects ---
@dp.callback_query(F.data == "back_main")
async def back_main(cb: CallbackQuery):
    await safe_edit(cb.message, "🔙 بازگشت به منوی اصلی:", reply_markup=main_menu())
    await cb.answer()

@dp.callback_query(F.data.startswith("back_projlist_"))
async def back_projlist(cb: CallbackQuery):
    cat = cb.data.split("_", 2)[2]
    await safe_edit(cb.message, f"🔙 بازگشت به لیست پروژه‌های {cat}:", reply_markup=list_projects(cat))
    await cb.answer()

@dp.callback_query(F.data.startswith("cat_"))
async def open_category(cb: CallbackQuery):
    cat = cb.data.split("_", 1)[1]
    if cat == "robotics":
        await safe_edit(cb.message, "🤖 پروژه‌های رباتیک:", reply_markup=list_projects("robotics"))
    elif cat == "iot":
        await safe_edit(cb.message, "🌐 پروژه‌های اینترنت اشیا:", reply_markup=list_projects("iot"))
    elif cat == "libs":
        await safe_edit(cb.message, "🐍 کتابخانه‌های پایتون:", reply_markup=list_libs())
    await cb.answer()

@dp.callback_query(F.data.startswith("proj_"))
async def project_detail(cb: CallbackQuery):
    _, cat, proj_id = cb.data.split("_", 2)
    items = safe_get_items_by_cat(cat)
    proj = next((p for p in items if str(p.get("id")) == proj_id), None)
    if not proj:
        await cb.answer("❌ پروژه پیدا نشد", show_alert=True)
        return

    title_h = _html.escape(proj.get("title", ""))
    desc_h = _html.escape(proj.get("description", ""))
    boards_h = _html.escape(", ".join(proj.get("boards", []) or []))
    parts_h = _html.escape(", ".join(proj.get("parts", []) or []))

    text = f"""📌 <b>{title_h}</b>

{desc_h}

⚡️ بوردها: {boards_h or '—'}
🧩 قطعات: {parts_h or '—'}"""

    await safe_edit(cb.message, text, reply_markup=code_menu(cat, proj_id, proj, current_lang=None))
    await cb.answer()

# --- Code show + downloads ---
@dp.callback_query(F.data.startswith("code_"))
async def send_code(cb: CallbackQuery):
    prefix, lang = cb.data.rsplit("_", 1)
    _, cat, proj_id = prefix.split("_", 2)

    items = safe_get_items_by_cat(cat)
    proj = next((p for p in items if str(p.get("id")) == proj_id), None)
    if not proj:
        await cb.answer("❌ پروژه پیدا نشد", show_alert=True)
        return

    code_raw = (proj.get("code") or {}).get(lang)
    if not code_raw:
        await cb.answer("کدی برای این زبان موجود نیست", show_alert=True)
        return

    title_h = _html.escape(proj.get("title", ""))
    code_h = _html.escape(code_raw)

    header = f"📌 <b>{title_h}</b> - {lang.upper()}\n\n"
    html_block = f"<pre><code>{code_h}</code></pre>"
    text = header + html_block

    if len(text) <= 3500:
        await safe_edit(cb.message, text, reply_markup=code_menu(cat, proj_id, proj, current_lang=lang))
    else:
        filename = f"{proj.get('title','project')}_{lang}.txt".replace(" ", "_")
        doc = BufferedInputFile(code_raw.encode("utf-8"), filename=filename)
        await cb.message.answer_document(
            document=doc,
            caption=f"📌 {proj.get('title','')} - {lang.upper()}",
            reply_markup=code_menu(cat, proj_id, proj, current_lang=lang),
        )
    await cb.answer()

def _lang_filename(title: str, lang: str) -> str:
    base = (title or "project").replace(" ", "_")
    ext = {"c": ".c", "cpp": ".cpp", "micropython": ".py"}.get(lang, ".txt")
    return f"{base}{ext}"

@dp.callback_query(F.data.startswith("zip_"))
async def zip_all(cb: CallbackQuery):
    _, cat, proj_id = cb.data.split("_", 2)
    items = safe_get_items_by_cat(cat)
    proj = next((p for p in items if str(p.get("id")) == proj_id), None)
    if not proj:
        await cb.answer("❌ پروژه پیدا نشد", show_alert=True)
        return

    code_map: Dict[str, str] = (proj.get("code") or {})
    if not code_map:
        await cb.answer("کدی برای این پروژه ثبت نشده.", show_alert=True)
        return

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for lang, content in code_map.items():
            if not content:
                continue
            z.writestr(_lang_filename(proj.get("title", "project"), lang), content)
    mem.seek(0)

    fname = f"{proj.get('title','project').replace(' ','_')}.zip"
    await cb.message.answer_document(BufferedInputFile(mem.read(), filename=fname), caption="🗜️ همهٔ کدها (ZIP)")
    await cb.answer()

@dp.callback_query(F.data.startswith("dls_"))
async def download_single(cb: CallbackQuery):
    prefix, lang = cb.data.rsplit("_", 1)
    _, cat, proj_id = prefix.split("_", 2)

    items = safe_get_items_by_cat(cat)
    proj = next((p for p in items if str(p.get("id")) == proj_id), None)
    if not proj:
        await cb.answer("❌ پروژه پیدا نشد", show_alert=True)
        return

    code_raw = (proj.get("code") or {}).get(lang)
    if not code_raw:
        await cb.answer("برای این زبان کدی موجود نیست", show_alert=True)
        return

    filename = _lang_filename(proj.get("title", "project"), lang)
    file = BufferedInputFile(code_raw.encode("utf-8"), filename=filename)
    await cb.message.answer_document(file, caption=f"⬇️ {proj.get('title','')} — {lang.upper()}")
    await cb.answer()

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

# ======================= WebApp =======================
def build_app():
    app = web.Application()

    # Health/Root: Render روی GET/HEAD تست می‌زند؛ فقط GET اضافه می‌کنیم (HEAD خودکار هندل می‌شود)
    async def root_get(request: web.Request):
        return web.Response(text="OK")

    app.router.add_get("/", root_get)

    # Webhook endpoint
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
