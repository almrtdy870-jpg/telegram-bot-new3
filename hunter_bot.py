"""بوت صيد يوزرات تليجرام.

Environment variables:
    BOT_TOKEN       (required) token from @BotFather
    ADMIN_IDS       comma-separated Telegram user ids allowed to use the bot.
                    If empty, the first person to send /start becomes the owner.
    API_ID, API_HASH
                    from my.telegram.org - needed for sessions (exact checks,
                    claiming, and adding accounts from inside the bot).
    TG_SESSION      optional - one StringSession (gen_session.py)
    TG_SESSIONS     optional - more StringSessions separated by commas/new lines
    WEBHOOK_URL     optional - e.g. https://my-app.onrender.com ; if set the
                    bot runs in webhook mode (Flask), otherwise long polling.
    DATA_FILE       optional - JSON data file (default data/hunter.json)
    TZ_OFFSET       optional - hours added to UTC for displayed times (default 3)
    PORT            web port for webhook mode (default 10000)
"""
import atexit
import hashlib
import hmac
import html
import json
import urllib.parse
import itertools
import os
import re
import threading
import time

from flask import Flask, request

from hunter import checker
from hunter.backup import FILENAME as BACKUP_FILENAME
from hunter.backup import TelegramBackup
from hunter.claimer import Claimer, fmt_wait
from hunter.engine import Hunter
from hunter.events import error, errors, event, events
from hunter.generator import source_label
from hunter.trends import KIND_LABEL, fetch_trends, formulas_for, usable
from hunter.generator import (MAX_REPLACE_POSITIONS, MAX_RULES, masked, parse_rule_line, rule_key, PRESETS, REPLACE_SETS,
                              clean_chars, replace_count, replace_stream, rule_label,
                              rule_sets,
                              similar_stream,
                              space_size, validate_template)
from hunter.rules import is_valid, normalize
from hunter.sessions import LoginFlow, SessionPool
from hunter.storage import Store
from hunter.tg import BotAPI

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required")

ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
PORT = int(os.environ.get("PORT", 10000))
API_ID = os.environ.get("API_ID", "").strip()
API_HASH = os.environ.get("API_HASH", "").strip()
TZ_OFFSET = float(os.environ.get("TZ_OFFSET", 3))
BOOT = time.time()

bot = BotAPI(TOKEN)
store = Store(os.environ.get("DATA_FILE", "data/hunter.json"))
store.data.setdefault("sessions", [])
store.data.setdefault("total_checks", 0)
app = Flask(__name__)

# where the automatic Telegram backup lives (owner's private chat with the bot)
BACKUP_CHAT = (os.environ.get("BACKUP_CHAT_ID", "").strip()
               or next(iter(sorted(ADMIN_IDS)), None) or store.data.get("owner"))
backup = TelegramBackup(bot, store, BACKUP_CHAT)

states = {}   # chat id -> pending input state
pending_restore = {}  # chat id -> backup data waiting for confirmation
rep_tmp = {}  # chat id -> base word while choosing positions
logins = {}   # chat id -> LoginFlow in progress
hunt_ctx = {"chat_id": None, "status_msg": None}


def clock(ts=None):
    return time.strftime("%H:%M:%S", time.gmtime((ts or time.time()) + TZ_OFFSET * 3600))


def hms(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------- sessions
pool = SessionPool()
_pool_loaded = threading.Event()


def apply_interval(target):
    """acc_interval 0 = automatic (start at 4s and learn per account)."""
    v = float(store.settings.get("acc_interval", 0) or 0)
    target.set_base_interval(v or 4.0, auto=not v)


def interval_label():
    v = float(store.settings.get("acc_interval", 0) or 0)
    if v:
        return f"{v:g}s"
    avg = pool.avg_interval
    return f"تلقائي ({avg:.1f}s)" if avg else "تلقائي"


def env_sessions():
    raw = [os.environ.get("TG_SESSION", ""), os.environ.get("TG_SESSIONS", "")]
    out = []
    for chunk in raw:
        out += [x.strip() for x in re.split(r"[,\s]+", chunk) if x.strip()]
    return out


def connect_session(string, label):
    """Connect one StringSession and add it to the pool. Return the Claimer."""
    c = Claimer(API_ID, API_HASH, string, label)
    apply_interval(c)
    c.start()
    c.duplicate = False
    if c.ready and any(o.ready and o.me and o.me.id == c.me.id for o in pool.sessions):
        # same Telegram account twice: floods are per account, so it only hurts
        c.duplicate = True
        c.disconnect()
        with store.lock:
            if string in store.data["sessions"]:
                store.data["sessions"].remove(string)
                store.save()
        event(f"تجاهلت جلسة مكررة لنفس الحساب {c.me.username or c.me.id}", "warn")
        return c
    pool.add(c)
    if c.ready:
        event(f"تم الاتصال بالجلسة @{c.me.username or c.me.id}", "ok")
    else:
        error(f"فشل الاتصال بجلسة ({label}): {c.last_error}")
    return c


def load_sessions():
    if API_ID and API_HASH:
        for s in env_sessions():
            if not pool.has_string(s):
                connect_session(s, "Render")
        for s in list(store.data["sessions"]):
            if not pool.has_string(s):
                connect_session(s, "البوت")
    _pool_loaded.set()


def get_claimer():
    return pool if pool.sessions else None


# ---------------------------------------------------------------- hunter
def on_found(username, status, source, claim_result):
    chat_id = hunt_ctx["chat_id"]
    if not chat_id:
        return
    mode = store.settings.get("notify", "claimed")
    claimed = bool(claim_result and claim_result[0])
    if mode == "off" or (mode == "claimed" and not claimed):
        return  # still saved in results + shown on the dashboard
    if status == checker.FRAGMENT:
        bot.send(chat_id, f"💎 <b>يوزر معروض في فراجمنت</b>\n@{username}\n"
                          f"https://fragment.com/username/{username}")
        return
    text = f"🎉 <b>يوزر متاح!</b>\n\n👤 @{username}\n🧭 المصدر: {source}"
    markup = None
    if claim_result is not None:
        ok, msg = claim_result
        text += f"\n{'🔒' if ok else '⚠️'} الحجز: {html.escape(msg)}"
    if not (claim_result and claim_result[0]):
        markup = {"inline_keyboard": [[{"text": "📌 احجزه بقناة", "callback_data": f"claim:{username}"}]]}
    bot.send(chat_id, text, markup)


def on_status(_summary=None):
    """Live dashboard: keep editing the dashboard message while hunting."""
    chat_id, msg_id = hunt_ctx["chat_id"], hunt_ctx["status_msg"]
    if not hunter.running and store.data.get("resume"):
        store.data["resume"] = False       # hunt ended (finished or stopped)
        store.save()
    if not chat_id:
        return
    if msg_id:
        bot.edit(chat_id, msg_id, dashboard(), main_menu())
    else:
        bot.send(chat_id, dashboard(), main_menu())


def on_maybe(names):
    chat_id = hunt_ctx["chat_id"]
    if not chat_id or store.settings.get("notify", "claimed") != "all":
        return
    bot.send(chat_id, f"🟡 <b>محتملة غير مؤكدة ({len(names)})</b>\n"
                      "ماكو جلسة متصلة، فهاي من t.me بس وأغلبها ممكن تكون محجوزة أو محظورة:\n\n"
                      + " ".join(f"@{n}" for n in names[:60])
                      + "\n\n💡 ضيف جلسة من 🔑 الجلسات حتى ينفحصن صح وينحجزن.")


hunter = Hunter(store, on_found, on_status, get_claimer, on_maybe)

# ---------------------------------------------------------------- texts
CLAIM_LABELS = {"off": "إيقاف", "channel": "قناة", "account": "حسابي"}
NOTIFY_LABELS = {"all": "🔔 الكل", "claimed": "🔒 المحجوز بس", "off": "🔕 إيقاف"}
LINE = "━━━━━━━━━━━━━━━"
LEVEL_ICON = {"ok": "🟢", "warn": "🟡", "info": "🔵", "error": "🔴"}


def target_label(s):
    srcs = s.get("sources") or []
    if not srcs:
        return "ماكو طريقة مختارة"
    if len(srcs) == 1:
        return source_label(s, srcs[0])
    return f"{len(srcs)} طرق سوه"


def set_source(key, on=True):
    """Select / unselect a hunting method; a running hunt picks it up live."""
    with store.lock:
        srcs = list(store.settings.get("sources") or [])
        if on and key not in srcs:
            srcs.append(key)
        elif not on and key in srcs:
            srcs.remove(key)
        else:
            return False
        store.data["settings"]["sources"] = srcs
        store.data["settings"]["sources_rev"] = store.settings.get("sources_rev", 0) + 1
        store.save()
    return True


def live_src_note():
    return "\n🟢 الصيد شغّال - دخلت فوراً." if hunter.running else "\nاضغط ▶️ تشغيل الصيد حتى يبدي."


def dashboard():
    s = store.settings
    active, total = len(pool.active), len(pool.sessions)
    found = store.data["found"]
    claimed = sum(1 for f in found if f.get("claimed"))
    online = "🟢 SYSTEM ONLINE"
    run = "🟢 يعمل" if hunter.running else "⚪ متوقف"
    sess_state = "متصل" if active else ("غير متصل" if total else "لا توجد")

    text = (f"⚡ <b>CONTROL PANEL</b>   {online}\n"
            f"<b>لوحة مراقبة الصيد</b>\n{LINE}\n"
            f"🔑 الجلسات النشطة: <b>{active:02d}</b>/{total:02d}  •  {sess_state}\n"
            f"🎯 النتائج المسجلة: <b>{len(found)}</b>\n"
            f"🔒 المحجوزة: <b>{claimed}</b>\n"
            f"🔍 إجمالي الفحوصات: <b>{store.data.get('total_checks', 0):,}</b>\n"
            f"{LINE}\n"
            f"📡 <b>حالة النظام المباشرة</b>\n"
            f"• حالة التشغيل: {run}\n"
            f"• نوع الصيد: {html.escape(target_label(s))}\n"
            f"• الحجز: {CLAIM_LABELS[s['claim_mode']]}\n")
    if hunter.running:
        st = hunter.stats
        el = max(1, time.time() - st["started"])
        text += (f"• الفحص الحالي: {st['checked']:,}  ({st['checked'] / el * 60:.0f}/دقيقة)\n"
                 f"• آخر يوزر: <code>{hunter.last_checked or '-'}</code>\n")
    text += (f"• آخر تحديث: {clock()}\n"
             f"• الأخطاء: {errors.count}\n"
             f"• مدة التشغيل: {hms(time.time() - BOOT)}\n"
             f"• 💾 آخر نسخة احتياطية: {clock(backup.last_backup) if backup.last_backup else '—'}\n")
    if pool.ready and pool.flood_left:
        text += "• 🧊 فلود:"
        if pool.check_flood_left:
            text += f" فحص {fmt_wait(pool.check_flood_left)}"
        if pool.claim_flood_left:
            text += f" • حجز {fmt_wait(pool.claim_flood_left)}"
        text += "\n"
    if hunter.running and not pool.ready:
        text += "• ⚠️ ماكو جلسة متصلة - الفحص غير دقيق\n"
    if hunter.verify_q or hunter.claim_q:
        text += f"• 🕓 بانتظار التحقق {len(hunter.verify_q)} • الحجز {len(hunter.claim_q)}\n"
    text += f"{LINE}\n📋 <b>آخر الأحداث</b>\n"
    last = events.last(5)
    if not last:
        text += "• لا توجد أحداث بعد\n"
    for ts, level, msg in reversed(last):
        text += f"{LEVEL_ICON.get(level, '•')} {html.escape(msg)}  <i>{clock(ts)[:5]}</i>\n"
    return text


HELP = (
    "📘 <b>شرح القوالب</b>\n\n"
    "<code>L</code> حرف  •  <code>D</code> رقم  •  <code>X</code> حرف أو رقم\n"
    "<code>V</code> حرف علة (aeiou)  •  <code>C</code> حرف ساكن\n"
    "<code>=</code> تكرار الحرف السابق\n"
    "أي حرف صغير أو رقم أو _ يُكتب كما هو.\n\n"
    "<b>أمثلة:</b>\n"
    "<code>LLLLL</code> → qwxzk\n"
    "<code>L=L=L</code> → aabbc\n"
    "<code>L_L_L</code> → a_b_c\n"
    "<code>CVCVC</code> → bodax\n"
    "<code>LDDDD</code> → k4821\n"
    "<code>iraqLL</code> → iraqzx\n\n"
    "🧬 <b>بحث مشابه</b>: تدز يوزر مثل <code>@lion</code> ويفحص "
    "<code>liion</code> <code>l_ion</code> <code>li0n</code> <code>lions</code> <code>thelion</code> ...\n\n"
    "⚠️ أقل طول لليوزر 5 أحرف، ويجب أن يبدأ بحرف ولا ينتهي بـ _."
)


# ---------------------------------------------------------------- keyboards
def btn(text, data):
    return {"text": text, "callback_data": data}


BACK = {"inline_keyboard": [[btn("🔙 رجوع", "menu")]]}


# The web dashboard (Mini App) is off unless ENABLE_WEBAPP=1 is set on the server.
WEBAPP_URL = (f"{WEBHOOK_URL}/app" if WEBHOOK_URL.startswith("https://")
              and os.environ.get("ENABLE_WEBAPP", "").strip() == "1" else "")


def main_menu():
    run = btn("⏹ إيقاف الصيد", "stop") if hunter.running else btn("▶️ تشغيل الصيد", "start")
    top = [[{"text": "📱 لوحة التحكم", "web_app": {"url": WEBAPP_URL}}]] if WEBAPP_URL else []
    return {"inline_keyboard": top + [
        [run],
        [btn("🎯 طرق الصيد", "modes"), btn("🛠 الأدوات", "tools")],
        [btn("🔑 الجلسات", "sessions"), btn("⚙️ الإعدادات", "settings")],
        [btn("🏆 النتائج", "found"), btn("📊 الإحصائيات", "stats")],
    ]}


def modes_text():
    srcs = store.settings.get("sources") or []
    active = ("\n".join(f"✅ {html.escape(source_label(store.settings, k))}" for k in srcs)
              if srcs else "— ماكو، اختار وحدة أو أكثر من الأزرار")
    return ("🎯 <b>طرق الصيد</b>\n\n"
            "كل طريقة تختارها <b>تنصاد سوه</b> ويّا الباقي، وبشكل <b>دائري</b> بلا توقف.\n\n"
            f"<b>المختار الآن ({len(srcs)}):</b>\n{active}\n\n"
            "🎯 <b>أنواع جاهزة</b>: خماسي، سداسي، مكرر، شبه ثلاثي...\n"
            "🔤 <b>تغيير حرف بالطلب</b>: صيغك، دائري ولحد 1000 صيغة\n"
            "🧬 <b>بحث مشابه</b>: يوزرات قريبة من يوزر تحدده\n"
            "✍️ <b>قالب مخصص</b>: شكل تكتبه بنفسك (L D X ...)\n"
            "📜 <b>قائمة كلمات</b>: كلمات تدزها أو ملف .txt\n"
            "📈 <b>ترند المزاد</b>: يجيب أقوى يوزرات Fragment ويصيد على تنويعاتها")


def modes_menu():
    rows = [
        [btn("🎯 أنواع جاهزة", "presets"), btn("🔤 تغيير حرف بالطلب", "replace")],
        [btn("🧬 بحث مشابه", "set_similar"), btn("✍️ قالب مخصص", "set_template")],
        [btn("📜 قائمة كلمات", "set_words"), btn("📈 ترند المزاد", "trends")],
    ]
    for i, k in enumerate(store.settings.get("sources") or []):
        rows.append([btn(f"❌ إلغاء: {source_label(store.settings, k)}"[:60], f"src_off:{i}")])
    rows.append([btn("🔙 رجوع", "menu")])
    return {"inline_keyboard": rows}


def tools_menu():
    return {"inline_keyboard": [
        [btn("🔍 فحص يوزر", "check"), btn("📌 حجز يوزر", "manual_claim")],
        [btn("🔙 رجوع", "menu")],
    ]}


SETTINGS_TEXT = "⚙️ <b>الإعدادات</b>\n\nاختار شنو تريد تغيّر:"


RULES_PER_PAGE = 8


def set_rules(rules):
    """Save rules and bump the revision so a running hunt picks them up live."""
    with store.lock:
        store.data["settings"]["replace_rules"] = rules
        store.data["settings"]["replace_rev"] = store.settings.get("replace_rev", 0) + 1
        store.save()


def replace_live():
    return hunter.running and "replace" in (store.settings.get("sources") or [])


def replace_text():
    rules = store.settings.get("replace_rules", [])
    text = ("🔤 <b>تغيير حرف بالطلب</b>\n\n"
            "تدز صيغة (مثل <code>vcvcv</code>)، وتختار رقم الحرف اللي يتغيّر، "
            "وتختار <b>أنت</b> شنو الحروف اللي يجربها بمكانه، ويفحصها بترتيب عشوائي:\n"
            "<code>vcvcv</code> + الحرف 1 + <code>aeo</code> ← <code>acvcv</code> <code>ecvcv</code> <code>ocvcv</code>\n\n")
    if not rules:
        return text + "📭 ماكو صيغ بعد، اضغط ➕ إضافة صيغة جديدة."
    total = sum(replace_count(r) for r in rules)
    text += (f"• الصيغ المضافة: <b>{len(rules)}</b> / {MAX_RULES}\n"
             f"• عدد اليوزرات بالدورة: <b>{total:,}</b>\n"
             "• 🔁 يصيد كل الصيغ سوه بشكل دائري، وإذا خلص يرجع من جديد")
    if replace_live():
        text += "\n• 🟢 <b>الصيد شغّال</b> — أي صيغة تضيفها تدخل فوراً"
    return text


def replace_menu(page=0):
    rules = store.settings.get("replace_rules", [])
    pages = max(1, (len(rules) + RULES_PER_PAGE - 1) // RULES_PER_PAGE)
    page = max(0, min(page, pages - 1))
    rows = [[btn("➕ إضافة صيغة", "rep_add"), btn("📥 إضافة جماعية", "rep_bulk")]]
    start = page * RULES_PER_PAGE
    for i, r in enumerate(rules[start:start + RULES_PER_PAGE], start):
        rows.append([btn(f"🛠 {masked(r)} ({rule_label(r)})", f"rep_info:{i}"),
                     btn("❌ حذف", f"rep_del:{i}")])
    if pages > 1:
        rows.append([btn("◀️", f"rep_page:{page - 1}") if page > 0 else btn(" ", "noop"),
                     btn(f"{page + 1}/{pages}", "noop"),
                     btn("▶️", f"rep_page:{page + 1}") if page < pages - 1 else btn(" ", "noop")])
    if rules:
        if replace_live():
            rows.append([btn("🟢 الصيد شغّال على كل الصيغ", "menu")])
        else:
            rows.append([btn("▶️ صيد كل الصيغ (دائري)", "rep_start")])
        rows.append([btn("🗑 مسح الكل", "rep_clear")])
    rows.append([btn("🔙 رجوع", "modes")])
    return {"inline_keyboard": rows}


def add_rules(new_rules):
    """Append rules (skipping duplicates / over the limit). Return (added, skipped)."""
    rules = list(store.settings.get("replace_rules", []))
    keys = {rule_key(r) for r in rules}
    added = skipped = 0
    for r in new_rules:
        if len(rules) >= MAX_RULES or rule_key(r) in keys:
            skipped += 1
            continue
        rules.append(r)
        keys.add(rule_key(r))
        added += 1
    if added:
        set_rules(rules)
        set_source("replace", True)
    return added, skipped


# ---------------------------------------------------------------- auction trends
MAX_TREND_RULES = 100
TREND_EVERY = 24 * 3600


def tr():
    return store.data.setdefault("trends", {"auto": True, "last": 0, "seen": [], "pending": []})


def fetch_trends_now():
    """Read Fragment and queue new strong names for approval. Return (new, error)."""
    items, err = fetch_trends()
    t = tr()
    with store.lock:
        t["last"] = time.time()
        known = set(t["seen"]) | {p["name"] for p in t["pending"]}
        new = [i for i in items if usable(i["name"]) and i["name"] not in known][:20]
        t["pending"] = (t["pending"] + new)[:30]
        store.save()
    if err:
        error(f"📈 ترند المزاد: {err}")
    if new:
        event(f"📈 ترند المزاد: {len(new)} كلمة جديدة بانتظار موافقتك", "info")
    return len(new), err


def accept_trends(names):
    """Turn approved names into change-letter formulas. Return rules added."""
    rules = []
    for n in names:
        rules += formulas_for(n)
    added, _skipped = add_rules(rules)
    t = tr()
    with store.lock:
        t["pending"] = [p for p in t["pending"] if p["name"] not in names]
        t["seen"] = (t["seen"] + [n for n in names if n not in t["seen"]])[-2000:]
        store.save()
    # keep the automatic formulas bounded: drop the oldest ones
    all_rules = list(store.settings.get("replace_rules", []))
    trend_idx = [i for i, r in enumerate(all_rules) if r.get("src") == "trend"]
    extra = len(trend_idx) - MAX_TREND_RULES
    if extra > 0:
        drop = set(trend_idx[:extra])
        set_rules([r for i, r in enumerate(all_rules) if i not in drop])
    return added


def skip_trends():
    t = tr()
    with store.lock:
        t["seen"] = (t["seen"] + [p["name"] for p in t["pending"]])[-2000:]
        t["pending"] = []
        store.save()


def trends_text():
    t = tr()
    n_rules = sum(1 for r in store.settings.get("replace_rules", []) if r.get("src") == "trend")
    text = ("📈 <b>ترند المزاد</b>\n\n"
            "البوت يقرا مزاد Fragment ويجيب أقوى اليوزرات (المنباعة والمزايد عليها)، "
            "وأنت تختار، ويصيد على <b>الكلمة + رقم حظ واحد</b> بشكل دائري.\n\n"
            f"• الجلب اليومي: {'✅ شغّال' if t['auto'] else '❌ متوقف'}\n"
            f"• آخر جلب: {clock(t['last']) if t['last'] else '—'}\n"
            f"• صيغ الترند المضافة: {n_rules} / {MAX_TREND_RULES}\n")
    if t["pending"]:
        text += f"\n<b>بانتظار موافقتك ({len(t['pending'])}):</b>\n"
        for i, p in enumerate(t["pending"][:10], 1):
            price = f" — {KIND_LABEL.get(p['kind'], '')} {p['price']:,} TON" if p.get("price") else ""
            text += f"{i}. <code>{p['name']}</code>{price}\n"
        if len(t["pending"]) > 10:
            text += f"... و{len(t['pending']) - 10} بعد\n"
    else:
        text += "\n📭 ماكو كلمات جديدة بانتظارك. اضغط 🔄 حتى أجيب الترند هسه."
    return text


def trends_menu():
    t = tr()
    rows, row = [], []
    for p in t["pending"][:10]:
        row.append(btn(f"✅ {p['name']}", f"tr_ok:{p['name']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if t["pending"]:
        rows.append([btn("✅ صيد على الكل", "tr_all"), btn("❌ تجاهل الكل", "tr_skip")])
    rows.append([btn("🔄 جيب الترند هسه", "tr_fetch")])
    rows.append([btn(f"⏰ الجلب اليومي: {'✅' if t['auto'] else '❌'}", "tr_auto"),
                 btn("➕ كلمة يدوية", "tr_word")])
    rows.append([btn("🔙 رجوع", "modes")])
    return {"inline_keyboard": rows}


def trend_fetch_and_show(chat_id):
    new, err = fetch_trends_now()
    head = (f"📈 لگيت <b>{new}</b> كلمة جديدة.\n\n" if new else
            ("⚠️ ما گدرت أقرا المزاد: " + html.escape(err) + "\n"
             "تكدر تضيف الكلمات بإيدك من ➕ كلمة يدوية.\n\n" if err else "📭 ماكو كلمات جديدة هسه.\n\n"))
    bot.send(chat_id, head + trends_text(), trends_menu())


def trend_loop():
    """Once a day: fetch the trend and ask the owner which names to hunt around."""
    _restored.wait(120)
    while True:
        time.sleep(600)
        try:
            t = tr()
            if not t["auto"] or time.time() - t["last"] < TREND_EVERY:
                continue
            new, _err = fetch_trends_now()
            chat_id = backup.chat_id or store.data.get("resume_chat")
            if new and chat_id:
                bot.send(chat_id, f"📈 <b>ترند المزاد اليوم</b>: {new} كلمة جديدة.\n\n" + trends_text(),
                         trends_menu())
        except Exception as e:  # noqa: BLE001
            error(f"📈 ترند المزاد: {e!r}"[:200])


def live_note():
    return "\n🟢 انضافت للصيد الشغّال مباشرة." if replace_live() else ""


def rep_chars_menu(same=False):
    rows, row = [], []
    if same:
        rows.append([btn("♻️ نفس حروف المكان السابق", "rep_cs:same")])
    for key, (label, _cs) in REPLACE_SETS.items():
        row.append(btn(label, f"rep_cs:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("🔙 رجوع", "replace")])
    return {"inline_keyboard": rows}


POS_SHORTCUTS = {
    "last2": "آخر حرفين", "last1": "آخر حرف", "first2": "أول حرفين",
    "first1": "أول حرف", "firstlast": "الأول + الأخير", "last3": "آخر 3 حروف",
}


def shortcut_positions(key, n):
    return {"last1": [n], "last2": [n - 1, n], "last3": [n - 2, n - 1, n],
            "first1": [1], "first2": [1, 2], "firstlast": [1, n]}.get(key, [])


def rep_pos_menu(base):
    n = len(base)
    def lbl(k):
        marked = "".join("_" if i + 1 in shortcut_positions(k, n) else ch for i, ch in enumerate(base))
        return f"{POS_SHORTCUTS[k]}  {marked}"
    return {"inline_keyboard": [
        [btn(lbl("last2"), "rep_pp:last2")],
        [btn(lbl("last1"), "rep_pp:last1"), btn(lbl("first1"), "rep_pp:first1")],
        [btn(lbl("first2"), "rep_pp:first2"), btn(lbl("firstlast"), "rep_pp:firstlast")],
        [btn(lbl("last3"), "rep_pp:last3")],
        [btn("🔙 رجوع", "replace")],
    ]}


def set_positions(chat_id, nums):
    base = rep_tmp.get(chat_id)
    if not isinstance(base, str):
        states.pop(chat_id, None)
        bot.send(chat_id, "⚠️ انتهت العملية، ابدأ من جديد.", replace_menu())
        return
    nums = sorted(set(nums))
    if not nums or any(not 1 <= x <= len(base) for x in nums) or len(nums) > MAX_REPLACE_POSITIONS:
        bot.send(chat_id, f"⚠️ أرسل رقم من 1 إلى {len(base)} "
                          f"(أو لحد {MAX_REPLACE_POSITIONS} أرقام مثل <code>1,3</code>).", rep_pos_menu(base))
        return
    rep_tmp[chat_id] = {"base": base, "pos": nums, "sets": []}
    states[chat_id] = "rep_chars"
    ask_chars(chat_id)


def ask_chars(chat_id):
    """Ask which characters to try for the next position of the rule being built."""
    tmp = rep_tmp[chat_id]
    idx = len(tmp["sets"])
    p = tmp["pos"][idx]
    marked = "".join(f"[{ch}]" if k + 1 == p else ch for k, ch in enumerate(tmp["base"]))
    head = f"✍️ <b>المكان {p}</b>" + (f"  ({idx + 1} من {len(tmp['pos'])})" if len(tmp["pos"]) > 1 else "")
    done = "".join(f"\n• المكان {q}: <code>{s}</code>" for q, s in zip(tmp["pos"], tmp["sets"]))
    bot.send(chat_id, f"{head}\n<code>{marked}</code>{done}\n\n"
                      "شنو الحروف اللي تريد تجربها بهذا المكان؟\n"
                      "• <b>اكتبها بنفسك</b> ودزها، مثل <code>aeo</code> أو <code>xyz</code> أو <code>a b c 7</code>\n"
                      "• أو اختار من الأزرار:", rep_chars_menu(same=idx > 0))


def add_chars(chat_id, chars):
    tmp = rep_tmp.get(chat_id)
    if not isinstance(tmp, dict):
        states.pop(chat_id, None)
        bot.send(chat_id, "⚠️ انتهت العملية، ابدأ من جديد.", replace_menu())
        return
    tmp["sets"].append(chars)
    if len(tmp["sets"]) < len(tmp["pos"]):
        ask_chars(chat_id)
    else:
        finish_rule(chat_id)


def finish_rule(chat_id):
    tmp = rep_tmp.pop(chat_id, None)
    states.pop(chat_id, None)
    rule = {"base": tmp["base"], "pos": tmp["pos"], "chars": tmp["sets"]}
    n = replace_count(rule)
    if n > 50000:
        bot.send(chat_id, f"⚠️ هاي الصيغة تطلع {n:,} يوزر وهواية. قلل الأماكن أو الحروف.", replace_menu())
        return
    added, _ = add_rules([rule])
    if not added:
        bot.send(chat_id, f"⚠️ هاي الصيغة موجودة أصلاً، أو وصلت الحد ({MAX_RULES} صيغة).", replace_menu())
        return
    per_pos = "".join(f"• المكان {p}: <code>{s}</code>\n" for p, s in zip(rule["pos"], rule_sets(rule)))
    bot.send(chat_id, "✅ <b>تمت إضافة الصيغة</b>\n\n"
                      f"• الصيغة: <code>{masked(rule)}</code>\n"
                      f"{per_pos}"
                      f"• عدد اليوزرات: {n:,}{live_note()}\n\n"
                      + replace_text().split("\n\n", 2)[-1], replace_menu(10 ** 6))


def settings_menu():
    s = store.settings
    return {"inline_keyboard": [
        [btn(f"➕ بادئة: {s['prefix'] or '-'}", "set_prefix"),
         btn(f"➕ لاحقة: {s['suffix'] or '-'}", "set_suffix")],
        [btn(f"⚡ الخيوط: {s['threads']}", "set_threads"),
         btn(f"⏱ التأخير: {s['delay']}s", "set_delay")],
        [btn(f"🔢 حد الفحص: {s['max_checks'] or '∞'}", "set_max"),
         btn(f"🔒 الحجز: {CLAIM_LABELS[s['claim_mode']]}", "cycle_claim")],
        [btn(f"🐢 فاصل فحص الحساب: {interval_label()}", "set_acc_interval")],
        [btn(f"🔁 تنويعات: {'✅' if s['word_variants'] else '❌'}", "toggle_variants"),
         btn(f"💎 فراجمنت: {'✅' if s['notify_fragment'] else '❌'}", "toggle_fragment")],
        [btn(f"🔔 الإشعارات: {NOTIFY_LABELS[s.get('notify', 'claimed')]}", "cycle_notify")],
        [btn(f"♻️ تشغيل الصيد تلقائياً بعد إعادة التشغيل: {'✅' if s.get('auto_resume') else '❌'}",
             "toggle_resume")],
        [btn("📜 سجل الأخطاء", "errors"), btn("💾 نسخ احتياطي الحين", "backup_now")],
        [btn("❓ شرح القوالب", "help")],
        [btn("🔙 رجوع", "menu")],
    ]}


def presets_menu():
    rows, row = [], []
    for key, (label, _t) in PRESETS.items():
        mark = "✅ " if f"preset:{key}" in (store.settings.get("sources") or []) else ""
        row.append(btn(f"{mark}{label}", f"preset:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("🔙 رجوع", "modes")])
    return {"inline_keyboard": rows}


def results_menu():
    return {"inline_keyboard": [
        [btn("📤 تصدير", "export"), btn("🗑 مسح النتائج", "clear_found")],
        [btn("🔙 رجوع", "menu")],
    ]}


def sessions_menu():
    rows = []
    for i, c in enumerate(pool.sessions):
        if c.ready:
            icon = "🧊" if c.flood_left else ("🈵" if c.full else "🟢")
            name = f"@{c.me.username}" if c.me.username else str(c.me.id)
        else:
            icon, name = ("🥶", "مجمّد") if getattr(c, "frozen", False) else ("🔴", "غير متصل")
        rows.append([btn(f"{icon} {name}", f"sess:{i}"), btn("🗑", f"sessdel:{i}")])
    rows.append([btn("➕ إضافة بالرقم", "login"), btn("➕ إضافة بكود جلسة", "add_string")])
    rows.append([btn("💾 نسخة احتياطية", "sess_backup"), btn("🔄 تحديث", "sessions")])
    rows.append([btn("🔙 رجوع", "menu")])
    return {"inline_keyboard": rows}


def sessions_text():
    if not (API_ID and API_HASH):
        return ("🔑 <b>الجلسات</b>\n\n⚠️ لازم تضيف <code>API_ID</code> و <code>API_HASH</code> "
                "بمتغيرات Render حتى تشتغل الجلسات.")
    text = (f"🔑 <b>الجلسات</b> ({len(pool.active)}/{len(pool.sessions)} متصلة)\n\n"
            "كل الجلسات تشتغل سوه: الفحص يتوزع عليهن، والحجز يروح للي بيها مجال.\n"
            "كل ما تزيد جلسات، يقل الفلود ويصير الصيد أسرع.\n\n"
            "🟢 متصلة • 🧊 بفلود • 🈵 وصلت حد القنوات • 🔴 غير متصلة")
    if not _pool_loaded.is_set():
        text += "\n\n⏳ جاري الاتصال بالجلسات..."
    return text


def session_detail(i):
    if not 0 <= i < len(pool.sessions):
        return "⚠️ الجلسة غير موجودة"
    c = pool.sessions[i]
    if not c.ready:
        return f"🔴 <b>جلسة غير متصلة</b> ({c.label})\n\n<code>{html.escape(c.last_error)}</code>"
    t = (f"🟢 <b>{html.escape(c.me.first_name or '')}</b>\n\n"
         f"• اليوزر: @{c.me.username or '-'}\n• الآيدي: <code>{c.me.id}</code>\n"
         f"• المصدر: {c.label}\n"
         f"• يفحص كل: {c.interval:.1f} ثانية{' (تلقائي)' if c.auto else ''}\n")
    if c.check_flood_left:
        t += f"• 🧊 فلود الفحص: {fmt_wait(c.check_flood_left)}\n"
    if c.claim_flood_left:
        t += f"• 🧊 فلود الحجز: {fmt_wait(c.claim_flood_left)}\n"
    if c.full:
        t += "• 🈵 وصلت حد القنوات العامة\n"
    return t


# ---------------------------------------------------------------- auth
def allowed(user_id: str) -> bool:
    if ADMIN_IDS:
        return user_id in ADMIN_IDS
    owner = store.data.get("owner")
    if owner is None:
        store.data["owner"] = user_id
        store.save()
        if not backup.chat_id:
            backup.chat_id = user_id
        return True
    return owner == user_id


# ---------------------------------------------------------------- actions
def start_hunt(chat_id, mid=None):
    s = store.settings
    if "words" in (s.get("sources") or []) and not s["words"]:
        set_source("words", False)
    if not s.get("sources"):
        return "⚠️ ما مختار ولا طريقة صيد. روح 🎯 طرق الصيد واختار وحدة أو أكثر."
    if hunter.running:
        return "⚠️ الصيد شغّال أصلاً."
    hunt_ctx["chat_id"] = chat_id
    if mid is None:
        msg = bot.send(chat_id, "🚀 جاري بدء الصيد...")
        mid = msg["message_id"] if msg else None
    hunt_ctx["status_msg"] = mid
    with store.lock:
        store.data["resume"] = True        # restart the hunt after a redeploy
        store.data["resume_chat"] = chat_id
        store.save()
    hunter.start()
    on_status()
    if not pool.ready and store.settings.get("notify", "claimed") != "off":
        reason = ("الجلسات موجودة بس مو متصلة أو بفلود، فالمحتملات تنتظر وتنفحص وتنحجز أول ما تتصل."
                  if pool.sessions else "ماكو أي جلسة، فالنتائج غير مؤكدة وتجيك مجمّعة.")
        bot.send(chat_id, f"⚠️ <b>ماكو جلسة متصلة</b>\n{reason}\nشوف 🔑 الجلسات و 📜 سجل الأخطاء.")
    return None


def run_check_list(chat_id, names):
    claimer = get_claimer()
    lines = []
    for n in names[:50]:
        if not is_valid(n, min_len=4):
            lines.append(f"🚫 <code>{html.escape(n)}</code> صيغة غير صالحة")
            continue
        status, source = checker.full_check(n, claimer)
        icon = {checker.AVAILABLE: "✅ متاح", checker.TAKEN: "❌ محجوز",
                checker.FRAGMENT: "💎 فراجمنت", checker.INVALID: "🚫 غير صالح",
                checker.UNKNOWN: "⚠️ غير معروف"}[status]
        lines.append(f"{icon} — @{n} <i>({source})</i>")
    bot.send(chat_id, "🔍 <b>نتيجة الفحص</b>\n\n" + "\n".join(lines), BACK)


def run_claim_list(chat_id, names):
    if not pool.ready:
        bot.send(chat_id, "⚠️ ماكو جلسة متصلة. ضيف جلسة من 🔑 الجلسات.", BACK)
        return
    for n in names[:10]:
        if not is_valid(n):
            bot.send(chat_id, f"🚫 <code>{html.escape(n)}</code> صيغة غير صالحة (5 أحرف على الأقل)")
            continue
        ok, msg = pool.claim(n, "channel")
        if not ok and msg.startswith("FLOOD:"):
            hunter.queue_claim(n)
            bot.send(chat_id, f"🧊 كل الجلسات بفلود ({fmt_wait(pool.claim_flood_left)}).\n"
                              f"📌 @{n} انضاف لقائمة الانتظار وراح ينحجز تلقائياً بعد الفلود.")
            continue
        if ok:
            event(f"🔒 انحجز @{n} (يدوي)", "ok")
            store.add_found(n, checker.AVAILABLE, "manual", claimed=True)
            bot.send(chat_id, f"🔒 <b>تم الحجز!</b> @{n}\n{html.escape(msg)}")
        else:
            error(f"ما انحجز @{n}: {msg}")
            bot.send(chat_id, f"⚠️ ما انحجز @{n}\n<code>{html.escape(msg)}</code>")


def start_similar(chat_id, text):
    base = normalize(text.split()[0]) if text.split() else ""
    if not (3 <= len(base) <= 32) or not base.isascii() or not all(ch.isalnum() or ch == "_" for ch in base):
        bot.send(chat_id, "⚠️ يوزر غير صالح، أرسله بالإنجليزي مثل <code>@lion</code>", BACK)
        return
    states.pop(chat_id, None)
    store.set("similar_base", base)
    set_source("similar", False)  # restart this method with the new name
    set_source("similar", True)
    total = sum(1 for _ in similar_stream(base, store.settings["prefix"], store.settings["suffix"]))
    event(f"🧬 بحث مشابه لـ @{base} ({total})", "info")
    bot.send(chat_id, f"✅ انضاف <b>بحث مشابه لـ @{base}</b> ({total} يوزر، دائري)" + live_src_note(),
             main_menu())


def export_found(chat_id):
    found = store.data["found"]
    if not found:
        bot.send(chat_id, "📭 ماكو نتائج بعد.", BACK)
        return
    body = "\n".join(
        f"@{f['username']}\t{f['status']}\t{f['source']}\t"
        f"{'claimed' if f.get('claimed') else ''}\t"
        f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(f['time'] + TZ_OFFSET * 3600))}"
        for f in found)
    bot.send_document(chat_id, "found_usernames.txt", body.encode(), f"🏆 {len(found)} يوزر")


def found_text():
    found = store.data["found"][-40:]
    if not found:
        return "📭 ماكو نتائج بعد."
    def icon(f):
        if f.get("claimed"):
            return "🔒"
        return {"fragment": "💎", "unverified": "🟡"}.get(f["status"], "✅")
    lines = [f"{icon(f)} @{f['username']}" for f in reversed(found)]
    return (f"🏆 <b>النتائج ({len(store.data['found'])})</b>\n🔒 محجوز • ✅ متاح مؤكد • 🟡 غير مؤكد\n\n"
            + "\n".join(lines))


def errors_text():
    last = errors.last(15)
    if not last:
        return "📜 <b>سجل الأخطاء</b>\n\n✅ ماكو أخطاء."
    lines = [f"🔴 <i>{clock(ts)}</i>  {html.escape(m)}" for ts, _l, m in reversed(last)]
    return f"📜 <b>سجل الأخطاء</b> ({errors.count})\n\n" + "\n".join(lines)


# ---------------------------------------------------------------- session login
def delete_msg(chat_id, mid):
    bot.call("deleteMessage", chat_id=chat_id, message_id=mid)


def save_session(chat_id, string):
    if pool.has_string(string):
        bot.send(chat_id, "ℹ️ هاي الجلسة مضافة أصلاً.", sessions_menu())
        return
    c = connect_session(string, "البوت")
    if getattr(c, "duplicate", False):
        bot.send(chat_id, f"ℹ️ الحساب <b>{c.me.username or c.me.id}</b> مضاف أصلاً بجلسة ثانية.\n"
                          "ما ضفته مرتين، لأن الفلود ينحسب على الحساب، فالتكرار يضر وما ينفع.",
                 sessions_menu())
        return
    if not c.ready:
        pool.sessions.remove(c)
        c.disconnect()
        bot.send(chat_id, f"❌ ما اشتغلت الجلسة:\n<code>{html.escape(c.last_error)}</code>\n\n"
                          "💡 البوت يقبل جلسات Telethon و Pyrogram. تأكد إنك نسخت الكود كامل "
                          "بدون نقص، أو ضيف الحساب بـ ➕ إضافة بالرقم.",
                 sessions_menu())
        return
    with store.lock:
        store.data["sessions"].append(string)
        store.save()
    bot.send(chat_id, f"✅ <b>تمت إضافة الجلسة</b> @{c.me.username or c.me.id}\n\n"
                      "💾 تنحفظ تلقائياً بملف النسخة الاحتياطية المثبّت، وترجع وحدها بعد كل نشر.",
             sessions_menu())


def login_step(chat_id, state, text, mid):
    if state == "add_string":
        delete_msg(chat_id, mid)
        states.pop(chat_id, None)
        bot.send(chat_id, "⏳ جاري الاتصال...")
        save_session(chat_id, text.strip())
        return

    if state == "login_phone":
        phone = "+" + "".join(str(int(ch)) for ch in text if ch.isdigit())
        if len(phone) < 8:
            bot.send(chat_id, "⚠️ أرسل الرقم بالصيغة الدولية، مثل <code>+9647701234567</code>", BACK)
            return
        flow = LoginFlow(API_ID, API_HASH)
        ok, err = flow.send_code(phone)
        if not ok:
            flow.close()
            states.pop(chat_id, None)
            bot.send(chat_id, f"❌ ما گدرت أدز الكود: {html.escape(err)}", sessions_menu())
            return
        logins[chat_id] = flow
        states[chat_id] = "login_code"
        bot.send(chat_id, "📩 وصلك كود على تليجرام.\n\n"
                          "⚠️ <b>دزه بمسافات بين الأرقام</b> مثل: <code>1 2 3 4 5</code>\n"
                          "(إذا دزيته لزگ، تليجرام يلغيه لأنه يعتبره منشور)", BACK)
        return

    flow = logins.get(chat_id)
    if flow is None:
        states.pop(chat_id, None)
        bot.send(chat_id, "⚠️ انتهت العملية، ابدأ من جديد.", sessions_menu())
        return

    if state == "login_code":
        delete_msg(chat_id, mid)
        code = "".join(str(int(ch)) for ch in text if ch.isdigit())
        result, val = flow.sign_in(code=code)
    else:  # login_password
        delete_msg(chat_id, mid)
        result, val = flow.sign_in(password=text)

    if result == "password":
        states[chat_id] = "login_password"
        bot.send(chat_id, "🔐 الحساب عليه تحقق بخطوتين. دز الباسورد (راح ينمسح فوراً):", BACK)
        return
    if result == "error":
        if state == "login_password" or "منتهي" in val:
            bot.send(chat_id, f"❌ {html.escape(val)}\nجرّب مرة ثانية.", BACK)
        else:
            bot.send(chat_id, f"❌ {html.escape(val)}\nدز الكود مرة ثانية بمسافات.", BACK)
        return
    # ok
    states.pop(chat_id, None)
    logins.pop(chat_id, None)
    flow.close()
    save_session(chat_id, val)


# ---------------------------------------------------------------- prompts
PROMPTS = {
    "set_template": ("template", "✍️ أرسل القالب (مثال <code>L=L=L</code> أو <code>CVCVC</code>).\n\n"
                                 "تكدر تدز <b>أكثر من قالب</b> بنفس الرسالة (كل واحد بسطر أو بينهم مسافة) "
                                 "وتنصاد كلها سوه، مثلاً:\n<code>L_L_L\nL=L=L\nLL==L</code>\n\n"
                                 "اضغط ❓ شرح القوالب للتفاصيل."),
    "set_words": ("words", "📜 أرسل الكلمات (كل كلمة بسطر) أو ارفع ملف .txt"),
    "set_prefix": ("prefix", "➕ أرسل البادئة، أو <code>-</code> للحذف"),
    "set_suffix": ("suffix", "➕ أرسل اللاحقة، أو <code>-</code> للحذف"),
    "set_threads": ("threads", "⚡ أرسل عدد الخيوط (1 - 50).\nنصيحة: 3-5 حتى ما يصير فلود."),
    "set_delay": ("delay", "⏱ أرسل التأخير بالثواني لكل خيط (مثال 1)"),
    "set_acc_interval": ("acc_interval", "🐢 <b>فاصل فحص الحساب</b>\n\n"
                                         "✅ <b>الأفضل: دز <code>0</code> = تلقائي</b>\n"
                                         "البوت يبدي من 4 ثواني، ويسرّع كل حساب ماشي زين لحد 2.5ث، "
                                         "ويبطّئ الحساب اللي ياخذ فلود ويتعلّم السرعة المناسبة إله.\n\n"
                                         "أو دز رقم ثابت (مثل <code>4</code>) إذا تريد تتحكم بنفسك."),
    "set_max": ("max_checks", "🔢 أرسل الحد الأقصى للفحص، أو 0 لبلا حد"),
    "set_similar": ("similar", "🧬 أرسل اليوزر اللي تريد مشابهاته (مثال <code>@lion</code>).\n"
                               "يولّد: تكرار حرف، _ بالنص، حروف شبيهة (o→0 ، i→l)، تبديل، حذف، إضافة حرف، لواحق...\n"
                               "ويبدي الصيد فوراً."),
    "manual_claim": ("claim", "📌 أرسل اليوزر (أو أكثر، كل واحد بسطر) وأحجزه بقناة فوراً."),
    "check": ("check", "🔍 أرسل يوزر أو أكثر (كل واحد بسطر، حتى 50)"),
    "login": ("login_phone", "📱 أرسل رقم الحساب بالصيغة الدولية:\n<code>+9647701234567</code>"),
    "add_string": ("add_string", "🔑 الصق كود الجلسة (Telethon StringSession).\nراح تنمسح رسالتك فوراً للأمان."),
}


def handle_input(key, text):
    if key == "template":
        tpls = list(dict.fromkeys(re.split(r"[\s,،]+", text.strip())))
        tpls = [t for t in tpls if t][:50]
        bad = [t for t in tpls if not validate_template(t) or not 5 <= len(t) <= 32]
        if not tpls or bad:
            return ("⚠️ قالب غير صالح" + (f": <code>{html.escape(bad[0])}</code>" if bad else "")
                    + "\nكل قالب لازم 5 إلى 32 رمز، ويستخدم L D X V C = وحروف صغيرة/أرقام/_ فقط.")
        store.set("template", " ".join(tpls))
        set_source("template", False)
        set_source("template", True)
        total = sum(space_size(t) for t in tpls)
        lines = "\n".join(f"• <code>{t}</code> (~{space_size(t):,})" for t in tpls)
        return (f"✅ تم حفظ {len(tpls)} قالب" + (" — تنصاد سوه مخلوطة" if len(tpls) > 1 else "")
                + f"\n{lines}\nالمجموع: ~{total:,} احتمال (دائري)" + live_src_note())
    if key == "words":
        if "*" in text:
            # these are change-letter formulas pasted into the word list by mistake
            rules = [r for r, _e in (parse_rule_line(l) for l in text.splitlines() if l.strip()) if r]
            added, _skipped = add_rules(rules)
            store.set("words", [])
            set_source("words", False)
            return (f"✅ هاي <b>صيغ</b> مو كلمات، فحطيتها بمكانها الصحيح:\n"
                    f"🔤 تغيير حرف بالطلب ← انضافت <b>{added}</b> صيغة (دائري)." + live_src_note())
        words = [normalize(w) for w in text.replace(",", "\n").split()]
        words = list(dict.fromkeys(w for w in words if w and is_valid(w, min_len=3)))
        store.set("words", words)
        set_source("words", False)
        set_source("words", bool(words))
        return f"✅ تم حفظ {len(words)} كلمة وانضافت لطرق الصيد (دائري)." + live_src_note()
    if key in ("prefix", "suffix"):
        val = "" if text.strip() == "-" else normalize(text)
        if val and not (val.isascii() and all(ch.isalnum() or ch == "_" for ch in val)):
            return "⚠️ استخدم حروف إنجليزية صغيرة أو أرقام أو _ فقط."
        store.set(key, val)
        return f"✅ تم الحفظ: <code>{val or '-'}</code>"
    if key == "threads":
        if not text.isdigit() or not 1 <= int(text) <= 50:
            return "⚠️ أرسل رقم من 1 إلى 50."
        store.set("threads", int(text))
        return f"✅ الخيوط: {int(text)}" + (" (يطبَّق بالصيد الجاي)" if hunter.running else "")
    if key == "delay":
        try:
            v = float(text)
            assert 0 <= v <= 120
        except (ValueError, AssertionError):
            return "⚠️ أرسل رقم بين 0 و 120."
        store.set("delay", v)
        return f"✅ التأخير: {v} ثانية"
    if key == "acc_interval":
        t = text.strip().lower()
        try:
            v = 0.0 if t in ("0", "تلقائي", "auto") else float(t)
            assert v == 0 or 0.5 <= v <= 60
        except (ValueError, AssertionError):
            return "⚠️ دز 0 للتلقائي، أو رقم بين 0.5 و 60."
        store.set("acc_interval", v)
        for c in pool.sessions:
            apply_interval(c)
        return "✅ الفاصل صار <b>تلقائي</b> - البوت يرتبه لكل حساب وحده" if not v \
            else f"✅ كل حساب يفحص كل {v:g} ثانية (ثابت)"
    if key == "max_checks":
        if not text.isdigit():
            return "⚠️ أرسل رقم صحيح."
        store.set("max_checks", int(text))
        return f"✅ حد الفحص: {int(text) or '∞'}"
    return None


# ---------------------------------------------------------------- handlers
def handle_callback(cq):
    chat_id = str(cq["message"]["chat"]["id"])
    mid = cq["message"]["message_id"]
    data = cq["data"]
    if not allowed(str(cq["from"]["id"])):
        bot.answer(cq["id"], "🚫 هذا البوت خاص", True)
        return
    bot.answer(cq["id"])
    s = store.settings

    if data == "menu":
        states.pop(chat_id, None)
        bot.edit(chat_id, mid, dashboard(), main_menu())
    elif data == "start":
        err = start_hunt(chat_id, mid)
        if err:
            bot.send(chat_id, err, main_menu())
    elif data == "stop":
        if hunter.running:
            hunter.stop()
            event("أمر إيقاف من المستخدم", "warn")
        bot.edit(chat_id, mid, dashboard(), main_menu())
    elif data == "stats":
        bot.edit(chat_id, mid, hunter.summary(final=not hunter.running),
                 {"inline_keyboard": [[btn("🔄 تحديث", "stats"), btn("🔙 رجوع", "menu")]]})
    elif data == "settings":
        states.pop(chat_id, None)
        bot.edit(chat_id, mid, SETTINGS_TEXT, settings_menu())
    elif data.startswith("src_off:"):
        srcs = store.settings.get("sources") or []
        i = int(data[8:])
        if 0 <= i < len(srcs):
            k = srcs[i]
            set_source(k, False)
            event(f"❌ انلغت: {source_label(store.settings, k)}", "warn")
        bot.edit(chat_id, mid, modes_text(), modes_menu())
    elif data == "restore_file_ok":
        loaded = pending_restore.pop(chat_id, None)
        if not loaded:
            bot.edit(chat_id, mid, "⚠️ انتهت العملية، دز الملف مرة ثانية.", BACK)
            return
        store.force_load(loaded)
        backup.blocked = False
        event("♻️ استرجاع يدوي من ملف نسخة احتياطية", "ok")
        threading.Thread(target=load_sessions, daemon=True).start()
        n = len(store.settings.get("replace_rules", []))
        bot.edit(chat_id, mid, f"✅ <b>تم الاسترجاع</b>: {n} صيغة، {len(store.data.get('sessions', []))} جلسة.\n"
                               "الجلسات تتصل هسه. إذا الصيد متوقف اضغط ▶️.", main_menu())
    elif data == "restore_retry":
        status = backup.restore(attempts=2, wait=3)
        if status == "restored":
            event("♻️ تم استرجاع البيانات من النسخة الاحتياطية", "ok")
            threading.Thread(target=_startup_rest, daemon=True).start()
            bot.edit(chat_id, mid, "✅ <b>تم استرجاع بياناتك</b> (الصيغ، الإعدادات، الجلسات، النتائج).",
                     main_menu())
        elif status == "error":
            bot.edit(chat_id, mid, f"⚠️ بعده ما انقرا: <code>{html.escape(backup.last_error)}</code>\n"
                                   "جرّب بعد دقيقة.", RESTORE_KB)
        else:
            bot.edit(chat_id, mid, "ℹ️ ماكو نسخة أحدث من البيانات الحالية.", main_menu())
    elif data == "restore_fresh":
        backup.blocked = False
        bot.edit(chat_id, mid, "🆕 تمام، أكمّل بالبيانات الحالية، والنسخ الاحتياطي رجع يشتغل.", main_menu())
    elif data == "trends":
        states.pop(chat_id, None)
        bot.edit(chat_id, mid, trends_text(), trends_menu())
    elif data == "tr_fetch":
        bot.send(chat_id, "⏳ جاري قراءة المزاد...")
        threading.Thread(target=trend_fetch_and_show, args=(chat_id,), daemon=True).start()
    elif data.startswith("tr_ok:") or data == "tr_all":
        names = [p["name"] for p in tr()["pending"]] if data == "tr_all" else [data[6:]]
        added = accept_trends(names) if names else 0
        note = f"✅ انضافت <b>{added}</b> صيغة حول: {', '.join(names[:8])}" + live_src_note() if names else ""
        bot.edit(chat_id, mid, (note + "\n\n" if note else "") + trends_text(), trends_menu())
    elif data == "tr_skip":
        skip_trends()
        bot.edit(chat_id, mid, trends_text(), trends_menu())
    elif data == "tr_auto":
        t = tr()
        with store.lock:
            t["auto"] = not t["auto"]
            store.save()
        bot.edit(chat_id, mid, trends_text(), trends_menu())
    elif data == "tr_word":
        states[chat_id] = "trend_word"
        bot.send(chat_id, "➕ دز كلمة قوية أو أكثر (كل وحدة بسطر)، مثل <code>danbao</code>.\n"
                          "أسويلها الصيغة وحدي: الكلمة + رقم حظ واحد (1 2 3 5 6 8 9)، وتنصاد دائرياً.", BACK)
    elif data == "modes":
        states.pop(chat_id, None)
        bot.edit(chat_id, mid, modes_text(), modes_menu())
    elif data == "tools":
        states.pop(chat_id, None)
        bot.edit(chat_id, mid, "🛠 <b>الأدوات</b>\n\n🔍 <b>فحص يوزر</b>: تشوف إذا يوزر متاح\n"
                               "📌 <b>حجز يوزر</b>: تحجز يوزر بقناة فوراً", tools_menu())
    elif data == "errors":
        bot.edit(chat_id, mid, errors_text(),
                 {"inline_keyboard": [[btn("🗑 مسح السجل", "errors_clear"), btn("🔙 رجوع", "settings")]]})
    elif data == "errors_clear":
        errors.items.clear()
        errors.count = 0
        bot.edit(chat_id, mid, errors_text(), {"inline_keyboard": [[btn("🔙 رجوع", "settings")]]})
    elif data == "sessions":
        bot.edit(chat_id, mid, sessions_text(), sessions_menu())
    elif data.startswith("sess:"):
        bot.edit(chat_id, mid, session_detail(int(data[5:])),
                 {"inline_keyboard": [[btn("🔙 الجلسات", "sessions")]]})
    elif data.startswith("sessdel:"):
        i = int(data[8:])
        bot.edit(chat_id, mid, "❓ متأكد تريد تحذف هاي الجلسة؟\n(تنفصل عن البوت بس، حسابك ما يتأثر)",
                 {"inline_keyboard": [[btn("✅ نعم احذف", f"sessdel_ok:{i}"), btn("❌ لا", "sessions")]]})
    elif data.startswith("sessdel_ok:"):
        c = pool.remove(int(data[11:]))
        if c is not None:
            with store.lock:
                if c.session_string in store.data["sessions"]:
                    store.data["sessions"].remove(c.session_string)
                store.save()
            event("تم حذف جلسة", "warn")
            note = "\n⚠️ هاي الجلسة من متغيرات Render، ترجع بعد إعادة التشغيل. احذفها من Render." if c.label == "Render" else ""
            bot.edit(chat_id, mid, "🗑 تم حذف الجلسة." + note, sessions_menu())
        else:
            bot.edit(chat_id, mid, sessions_text(), sessions_menu())
    elif data == "sess_backup":
        strings = [c.session_string for c in pool.sessions]
        if not strings:
            bot.send(chat_id, "📭 ماكو جلسات.", BACK)
        else:
            bot.send_document(chat_id, "sessions.txt", ",".join(strings).encode(),
                              "💾 نسخة من جلساتك للاحتياط بس. البوت يحفظها تلقائياً بالملف المثبّت، "
                              "فما تحتاج تحطها برندر.\n⚠️ لا تنطي هذا الملف لأي أحد.")
    elif data in ("login", "add_string"):
        if not (API_ID and API_HASH):
            bot.send(chat_id, "⚠️ لازم تضيف API_ID و API_HASH بمتغيرات Render أول.", BACK)
            return
        key, prompt = PROMPTS[data]
        states[chat_id] = key
        bot.send(chat_id, prompt, BACK)
    elif data.startswith("claim:"):
        name = data.split(":", 1)[1]
        bot.send(chat_id, f"⏳ جاري حجز @{name} ...")
        threading.Thread(target=run_claim_list, args=(chat_id, [name]), daemon=True).start()
    elif data == "presets":
        bot.edit(chat_id, mid, "🎯 <b>اختر نوع اليوزرات:</b>", presets_menu())
    elif data.startswith("preset:"):
        key = data.split(":", 1)[1]
        if key in PRESETS:
            src = f"preset:{key}"
            on = src not in (store.settings.get("sources") or [])
            set_source(src, on)
            label, tpls = PRESETS[key]
            total = sum(space_size(t) for t in tpls)
            if on:
                text = (f"✅ تم اختيار <b>{label}</b>\nالقوالب: <code>{' | '.join(tpls)}</code>\n"
                        f"عدد الاحتمالات تقريباً: {total:,} (دائري)" + live_src_note())
            else:
                text = f"❌ تم إلغاء <b>{label}</b>"
            bot.edit(chat_id, mid, text + "\n\nتكدر تختار أكثر من نوع، وكلهن ينصادن سوه.", presets_menu())
    elif data in PROMPTS:
        key, prompt = PROMPTS[data]
        states[chat_id] = key
        bot.send(chat_id, prompt, BACK)
    elif data == "replace":
        states.pop(chat_id, None)
        bot.edit(chat_id, mid, replace_text(), replace_menu())
    elif data == "rep_add":
        states[chat_id] = "rep_base"
        bot.send(chat_id, "📝 أرسل الصيغة أو النص الأساسي (مثلاً <code>vcvcv</code> أو <code>python</code>):", BACK)
    elif data.startswith("rep_info:"):
        rules = store.settings.get("replace_rules", [])
        i = int(data[9:])
        if 0 <= i < len(rules):
            r = rules[i]
            base = r["base"]
            marked = "".join(f"[{ch}]" if k + 1 in r["pos"] else ch for k, ch in enumerate(base))
            sample = " ".join(f"<code>{n}</code>" for n in itertools.islice(replace_stream([r]), 6))
            bot.send(chat_id, f"🛠 <b>{masked(r)}</b>\nالأحرف اللي تتغيّر: <code>{marked}</code>\n"
                              + "".join(f"• المكان {p}: <code>{cs}</code>\n" for p, cs in zip(r["pos"], rule_sets(r)))
                              + f"عدد اليوزرات: {replace_count(r):,}\nأمثلة: {sample} ...",
                     {"inline_keyboard": [[btn("🔙 تغيير حرف", "replace")]]})
    elif data.startswith("rep_pp:"):
        base = rep_tmp.get(chat_id)
        if states.get(chat_id) == "rep_pos" and isinstance(base, str):
            set_positions(chat_id, shortcut_positions(data[7:], len(base)))
        else:
            bot.edit(chat_id, mid, replace_text(), replace_menu())
    elif data.startswith("rep_cs:"):
        key = data[7:]
        tmp = rep_tmp.get(chat_id)
        if states.get(chat_id) == "rep_chars" and key == "same" and isinstance(tmp, dict) and tmp["sets"]:
            add_chars(chat_id, tmp["sets"][-1])
        elif states.get(chat_id) == "rep_chars" and key in REPLACE_SETS:
            add_chars(chat_id, REPLACE_SETS[key][1])
        else:
            bot.edit(chat_id, mid, replace_text(), replace_menu())
    elif data.startswith("rep_del:"):
        rules = list(store.settings.get("replace_rules", []))
        i = int(data[8:])
        if 0 <= i < len(rules):
            rules.pop(i)
            set_rules(rules)
        bot.edit(chat_id, mid, replace_text(), replace_menu(i // RULES_PER_PAGE))
    elif data.startswith("rep_page:"):
        bot.edit(chat_id, mid, replace_text(), replace_menu(int(data[9:])))
    elif data == "noop":
        pass
    elif data == "rep_bulk":
        states[chat_id] = "rep_bulk"
        bot.send(chat_id, "📥 <b>إضافة جماعية</b> (لحد 1000 صيغة)\n\n"
                          "دز الصيغ كل وحدة بسطر، أو ارفع ملف <code>.txt</code>.\n"
                          "حط <code>*</code> مكان الحرف اللي يتغيّر:\n\n"
                          "<code>boss**</code>  ← آخر حرفين a-z\n"
                          "<code>*cvcv</code>  ← أول حرف a-z\n"
                          "<code>boss** aeo</code>  ← الحرفين من aeo\n"
                          "<code>boss** abc 0-9</code>  ← الأول من abc والثاني أرقام\n"
                          "<code>bosspo 5,6</code>  ← نفس boss** بالأرقام\n\n"
                          "مجموعات جاهزة: <code>a-z</code> <code>0-9</code> <code>a-z0-9</code> <code>aeiou</code>", BACK)
    elif data == "rep_clear":
        bot.edit(chat_id, mid, "❓ متأكد تريد تمسح كل الصيغ؟",
                 {"inline_keyboard": [[btn("✅ نعم", "rep_clear_ok"), btn("❌ لا", "replace")]]})
    elif data == "rep_clear_ok":
        set_rules([])
        set_source("replace", False)
        bot.edit(chat_id, mid, replace_text(), replace_menu())
    elif data == "rep_start":
        if not store.settings.get("replace_rules"):
            bot.edit(chat_id, mid, replace_text(), replace_menu())
            return
        if replace_live():
            bot.edit(chat_id, mid, replace_text(), replace_menu())
            return
        set_source("replace", True)
        if hunter.running:
            bot.edit(chat_id, mid, replace_text(), replace_menu())
            return
        total = sum(replace_count(r) for r in store.settings["replace_rules"])
        event(f"🔤 تغيير حرف: {len(store.settings['replace_rules'])} صيغة ({total:,}) - دائري", "info")
        err = start_hunt(chat_id, mid)
        if err:
            bot.send(chat_id, err, main_menu())
    elif data == "backup_now":
        ok = backup.backup(force=True)
        head = ("💾 <b>تم الحفظ</b>، وهذا فحص النسخة:" if ok
                else f"⚠️ <b>فشل الحفظ</b>: {html.escape(backup.last_error)}\nوهذا فحص النسخة الموجودة:")
        try:
            lines = backup.verify()[1]
        except Exception as e:  # noqa: BLE001
            lines = [f"❌ خطأ بالفحص: {html.escape(repr(e))}"]
        env_n = len(env_sessions())
        tip = (f"\n\n🔐 جلسات محفوظة برندر (TG_SESSIONS): <b>{env_n}</b>"
               + ("" if env_n else " — يُفضّل تحطها هناك حتى ما تروح أبداً"))
        bot.send(chat_id, head + "\n\n" + "\n".join(lines) + tip, settings_menu())
    elif data == "cycle_notify":
        order = ["claimed", "off", "all"]
        cur = store.settings.get("notify", "claimed")
        store.set("notify", order[(order.index(cur) + 1) % 3] if cur in order else "claimed")
        bot.edit(chat_id, mid, SETTINGS_TEXT, settings_menu())
    elif data == "cycle_claim":
        order = ["off", "channel", "account"]
        nxt = order[(order.index(s["claim_mode"]) + 1) % 3]
        if nxt != "off" and not pool.ready:
            bot.send(chat_id, "⚠️ لازم تضيف جلسة أول من 🔑 الجلسات.")
            nxt = "off"
        store.set("claim_mode", nxt)
        bot.edit(chat_id, mid, SETTINGS_TEXT, settings_menu())
    elif data == "toggle_resume":
        store.set("auto_resume", not s.get("auto_resume"))
        bot.edit(chat_id, mid, SETTINGS_TEXT, settings_menu())
    elif data in ("toggle_variants", "toggle_fragment"):
        key = "word_variants" if data == "toggle_variants" else "notify_fragment"
        store.set(key, not s[key])
        bot.edit(chat_id, mid, SETTINGS_TEXT, settings_menu())
    elif data == "found":
        bot.edit(chat_id, mid, found_text(), results_menu())
    elif data == "export":
        export_found(chat_id)
    elif data == "clear_found":
        bot.edit(chat_id, mid, "❓ متأكد تريد تمسح كل النتائج؟",
                 {"inline_keyboard": [[btn("✅ نعم", "clear_found_ok"), btn("❌ لا", "found")]]})
    elif data == "clear_found_ok":
        store.clear_found()
        bot.edit(chat_id, mid, "🗑 تم مسح النتائج.", BACK)
    elif data == "help":
        bot.edit(chat_id, mid, HELP, {"inline_keyboard": [[btn("🔙 الإعدادات", "settings")]]})


def handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    sender = msg.get("from") or {}
    # ignore service messages (pinned file, etc.), bots and anything that isn't
    # plain text or a document
    if sender.get("is_bot") or "pinned_message" in msg or not ("text" in msg or "document" in msg):
        return
    if not allowed(str(sender.get("id", ""))):
        bot.send(chat_id, "🚫 هذا البوت خاص.")
        return
    text = (msg.get("text") or "").strip()
    state = states.get(chat_id)

    # a backup file sent / forwarded by the owner: offer to restore it
    if msg.get("document", {}).get("file_name") == BACKUP_FILENAME and not state:
        raw = bot.download_text(msg["document"]["file_id"], limit=20_000_000)
        try:
            data = json.loads(raw or "")
            st = data.get("settings", {})
        except ValueError:
            bot.send(chat_id, "⚠️ ما گدرت أقرا هذا الملف.", BACK)
            return
        pending_restore[chat_id] = data
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(data.get("saved_at", 0) + TZ_OFFSET * 3600))
        bot.send(chat_id, "💾 <b>ملف نسخة احتياطية</b>\n\n"
                          f"• تاريخه: {when}\n"
                          f"• الصيغ: <b>{len(st.get('replace_rules', []))}</b>\n"
                          f"• الجلسات: <b>{len(data.get('sessions', []))}</b>\n"
                          f"• النتائج: <b>{len(data.get('found', []))}</b>\n"
                          f"• الكلمات: {len(st.get('words', []))}\n\n"
                          "⚠️ الاسترجاع <b>يبدّل</b> بياناتك الحالية بهاي النسخة.",
                 {"inline_keyboard": [[btn("✅ استرجع هاي النسخة", "restore_file_ok")],
                                      [btn("❌ إلغاء", "menu")]]})
        return

    if text.startswith("/start") or text.startswith("/menu"):
        states.pop(chat_id, None)
        bot.send(chat_id, dashboard(), main_menu())
        return
    if text.startswith("/stop"):
        hunter.stop()
        bot.send(chat_id, "🛑 تم إرسال أمر الإيقاف.", main_menu())
        return

    if state in ("add_string", "login_phone", "login_code", "login_password"):
        threading.Thread(target=login_step, args=(chat_id, state, text, msg["message_id"]),
                         daemon=True).start()
        return

    if "document" in msg and state in ("words", "rep_bulk"):
        content = bot.download_text(msg["document"]["file_id"])
        if content is None:
            bot.send(chat_id, "⚠️ ما گدرت أقرأ الملف (الحد 5MB).")
            return
        text = content

    if state == "similar":
        start_similar(chat_id, text)
        return
    if state == "trend_word":
        states.pop(chat_id, None)
        words = list(dict.fromkeys(normalize(w) for w in text.split()))
        good = [w for w in words if usable(w)]
        bad = [w for w in words if w and not usable(w)]
        added = accept_trends(good) if good else 0
        out = f"✅ انضافت <b>{added}</b> صيغة حول: {', '.join(good[:10])}" + live_src_note() if good \
            else "⚠️ ماكو كلمة صالحة."
        if bad:
            out += f"\n🚫 تجاهلت: {', '.join(html.escape(b) for b in bad[:10])} (غير صالحة أو ماركة/كلمة ممنوعة)"
        bot.send(chat_id, out, trends_menu())
        return
    if state == "rep_bulk":
        states.pop(chat_id, None)
        rules, bad = [], []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            rule, err = parse_rule_line(line)
            if rule:
                rules.append(rule)
            elif err:
                bad.append(f"{n}: <code>{html.escape(line.strip()[:30])}</code> — {err}")
        added, skipped = add_rules(rules)
        total = sum(replace_count(r) for r in store.settings.get("replace_rules", []))
        text_out = (f"📥 <b>الإضافة الجماعية</b>\n\n✅ انضافت: <b>{added}</b> صيغة"
                    + (f"\n♻️ مكررة/فوق الحد: {skipped}" if skipped else "")
                    + (f"\n⚠️ غلط: {len(bad)}" if bad else "")
                    + f"\n• المجموع: {len(store.settings.get('replace_rules', []))} صيغة ({total:,} يوزر بالدورة)"
                    + (live_note() if added else ""))
        if bad:
            text_out += "\n\n" + "\n".join(bad[:10]) + ("\n..." if len(bad) > 10 else "")
        bot.send(chat_id, text_out, replace_menu(10 ** 6))
        return
    if state == "rep_base":
        base = normalize(text.split()[0]) if text.split() else ""
        if not (5 <= len(base) <= 32) or not base.isascii() or not all(ch.isalnum() or ch == "_" for ch in base):
            bot.send(chat_id, "⚠️ الصيغة لازم تكون حروف إنجليزية/أرقام/_ (5 إلى 32 حرف). أرسلها مرة ثانية:", BACK)
            return
        rep_tmp[chat_id] = base
        states[chat_id] = "rep_pos"
        numbered = "  ".join(f"{i}:{ch}" for i, ch in enumerate(base, 1))
        bot.send(chat_id, f"🔢 الصيغة المرسلة: <code>{base}</code> (طولها {len(base)} حروف)\n"
                          f"<code>{numbered}</code>\n\n"
                          f"أرسل <b>رقم الحرف</b> الذي تريد تغييره (من 1 إلى {len(base)})، "
                          f"أو لحد {MAX_REPLACE_POSITIONS} أرقام مثل <code>{len(base) - 1},{len(base)}</code>\n"
                          "أو اختار من الأزرار:", rep_pos_menu(base))
        return
    if state == "rep_pos":
        nums = [int(x) for x in re.findall(r"\d+", "".join(str(int(c)) if c.isdigit() else c for c in text))]
        set_positions(chat_id, nums)
        return
    if state == "rep_chars":
        chars = clean_chars(text)
        if not chars:
            bot.send(chat_id, "⚠️ اكتب حروف إنجليزية صغيرة أو أرقام أو _ ، مثل <code>aeo</code>",
                     rep_chars_menu(same=bool(isinstance(rep_tmp.get(chat_id), dict) and rep_tmp[chat_id]["sets"])))
            return
        add_chars(chat_id, chars)
        return
    if state == "claim":
        states.pop(chat_id, None)
        names = [normalize(x) for x in text.split()]
        threading.Thread(target=run_claim_list, args=(chat_id, names), daemon=True).start()
        return
    if state == "check":
        states.pop(chat_id, None)
        names = [normalize(x) for x in text.split()]
        bot.send(chat_id, f"⏳ جاري فحص {min(len(names), 50)} يوزر...")
        threading.Thread(target=run_check_list, args=(chat_id, names), daemon=True).start()
        return
    if state:
        reply = handle_input(state, text)
        if reply and reply.startswith("✅"):
            states.pop(chat_id, None)
            bot.send(chat_id, reply, modes_menu() if state in ("template", "words") else settings_menu())
        elif reply:
            bot.send(chat_id, reply, BACK)
        return

    if text.startswith("@") and len(text.split()) == 1:
        threading.Thread(target=run_check_list, args=(chat_id, [normalize(text)]), daemon=True).start()
        return
    bot.send(chat_id, dashboard(), main_menu())


def dispatch(update):
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception as e:  # noqa: BLE001 - never kill the bot on one bad update
        error(f"خطأ داخلي: {e!r}"[:200])
        print(f"[bot] update error: {e!r}")


# ---------------------------------------------------------------- transports
@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}
    threading.Thread(target=dispatch, args=(update,), daemon=True).start()
    return "OK", 200


@app.route("/")
def index():
    return "Username hunter is running", 200


# ---------------------------------------------------------------- mini app (web dashboard)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hunter", "webapp.html"),
          encoding="utf-8") as _f:
    WEBAPP_HTML = _f.read()


def is_admin(user_id: str) -> bool:
    if ADMIN_IDS:
        return user_id in ADMIN_IDS
    return bool(user_id) and store.data.get("owner") == user_id


def webapp_user(init_data: str):
    """Validate Telegram WebApp initData; return the user id when it's the owner."""
    if not init_data:
        return None
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(calc, received):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", 0)) > 7 * 86400:
            return None
        uid = str(json.loads(pairs.get("user", "{}")).get("id", ""))
    except (ValueError, TypeError):
        return None
    return uid if is_admin(uid) else None


def webapp_state():
    s = store.settings
    found = store.data["found"]
    st = hunter.stats
    end = st["finished"] or time.time()
    el = max(1, end - st["started"])
    sessions = []
    for c in pool.sessions:
        if c.ready:
            icon = "🧊" if c.flood_left else ("🈵" if c.full else "🟢")
            name = f"@{c.me.username}" if c.me.username else str(c.me.id)
            note = (f"فلود {fmt_wait(c.flood_left)}" if c.flood_left else
                    ("وصلت حد القنوات" if c.full else "متصلة"))
        else:
            icon, name, note = "🔴", "غير متصلة", c.last_error[:60]
        sessions.append({"icon": icon, "name": name, "source": c.label, "note": note})
    return {
        "running": hunter.running,
        "mode": "replace" if "replace" in (s.get("sources") or []) else "",
        "mode_label": target_label(s),
        "sessions": {"active": len(pool.active), "total": len(pool.sessions), "list": sessions},
        "rules": len(s.get("replace_rules", [])),
        "results": len(found),
        "claimed": sum(1 for f in found if f.get("claimed")),
        "total_checks": store.data.get("total_checks", 0),
        "errors_count": errors.count,
        "now": clock(),
        "uptime": hms(time.time() - BOOT),
        "backup": clock(backup.last_backup) if backup.last_backup else "",
        "flood": {"check": fmt_wait(pool.check_flood_left) if pool.ready and pool.check_flood_left else "",
                  "claim": fmt_wait(pool.claim_flood_left) if pool.ready and pool.claim_flood_left else ""},
        "queues": {"verify": len(hunter.verify_q), "claim": len(hunter.claim_q)},
        "events": [{"t": clock(t)[:5], "level": lv, "msg": m} for t, lv, m in reversed(events.last(6))],
        "errors": [{"t": clock(t), "msg": m} for t, _lv, m in reversed(errors.last(15))],
        "stats": {"checked": st["checked"], "available": st["available"], "claimed": st["claimed"],
                  "fragment": st["fragment"], "taken": st["taken"], "errors": st["errors"],
                  "rate": round(st["checked"] / el * 60), "elapsed": hms(el) if st["checked"] else "-",
                  "last": hunter.last_checked},
        "settings": {"claim": CLAIM_LABELS[s["claim_mode"]],
                     "notify": NOTIFY_LABELS[s.get("notify", "claimed")],
                     "threads": s["threads"], "delay": s["delay"]},
    }


@app.route("/app")
def webapp_page():
    return WEBAPP_HTML, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


@app.route("/api/state")
def webapp_api_state():
    if not webapp_user(request.headers.get("X-Init-Data", "")):
        return {"error": "unauthorized"}, 401
    return webapp_state()


@app.route("/api/action", methods=["POST"])
def webapp_api_action():
    uid = webapp_user(request.headers.get("X-Init-Data", ""))
    if not uid:
        return {"error": "unauthorized"}, 401
    body = request.get_json(silent=True) or {}
    action, value = body.get("action"), body.get("value")
    s = store.settings
    message = ""
    if action == "start":
        message = start_hunt(uid) or ""
    elif action == "stop":
        if hunter.running:
            hunter.stop()
            event("أمر إيقاف من لوحة التحكم", "warn")
    elif action == "claim":
        order = ["off", "channel", "account"]
        nxt = order[(order.index(s["claim_mode"]) + 1) % 3]
        if nxt != "off" and not pool.ready:
            nxt, message = "off", "لازم تضيف جلسة أول من 🔑 الجلسات"
        store.set("claim_mode", nxt)
    elif action == "notify":
        order = ["claimed", "off", "all"]
        cur = s.get("notify", "claimed")
        store.set("notify", order[(order.index(cur) + 1) % 3] if cur in order else "claimed")
    elif action == "threads":
        try:
            store.set("threads", max(1, min(50, int(s["threads"]) + int(value))))
        except (TypeError, ValueError):
            pass
    elif action == "delay":
        try:
            store.set("delay", round(max(0.0, min(120.0, float(s["delay"]) + float(value))), 1))
        except (TypeError, ValueError):
            pass
    else:
        return {"error": "unknown action"}, 400
    return {"ok": True, "message": message, "state": webapp_state()}


def poll_forever():
    bot.call("deleteWebhook")
    offset = None
    print("[bot] polling...")
    while True:
        for upd in bot.get_updates(offset):
            offset = upd["update_id"] + 1
            threading.Thread(target=dispatch, args=(upd,), daemon=True).start()


RESTORE_KB = {"inline_keyboard": [[{"text": "🔄 حاول الاسترجاع", "callback_data": "restore_retry"}],
                                  [{"text": "🆕 كمّل بدون الاسترجاع", "callback_data": "restore_fresh"}]]}


def restore_failed_notice():
    error(f"💾 فشل استرجاع النسخة الاحتياطية: {backup.last_error}")
    if backup.chat_id:
        bot.send(backup.chat_id,
                 "⚠️ <b>ما گدرت أقرا النسخة الاحتياطية</b> بعد إعادة التشغيل.\n"
                 f"السبب: <code>{html.escape(backup.last_error)}</code>\n\n"
                 "بياناتك القديمة <b>بعدها محفوظة</b> بالملف المثبّت، وما راح أكتب فوگها.\n"
                 "اضغط 🔄 حتى أحاول مرة ثانية.", RESTORE_KB)


def backup_loop():
    """Periodic Telegram backups, independent from the rest of start-up."""
    _restored.wait(120)
    try:
        if backup.chat_id:
            if not backup.blocked and not backup.backup(force=not backup.message_id):
                error(f"💾 فشل النسخ الاحتياطي: {backup.last_error}")
    except Exception as e:  # noqa: BLE001
        error(f"💾 فشل النسخ الاحتياطي: {e}")
    while True:
        time.sleep(30)
        try:
            if not backup.blocked and store.dirty and time.time() - backup.last_backup >= backup.interval:
                before = backup.edits_failed
                if not backup.backup():
                    error(f"💾 فشل النسخ الاحتياطي: {backup.last_error}")
                elif backup.edits_failed > before:
                    error(f"💾 ما گدرت أعدّل ملف النسخة فدزيت ملف جديد. السبب: {backup.edit_error}")
        except Exception as e:  # noqa: BLE001
            error(f"💾 فشل النسخ الاحتياطي: {e}")


_restored = threading.Event()


def startup():
    """Restore the Telegram backup, reconnect sessions, resume the hunt."""
    try:
        status = backup.restore()
        if status == "restored":
            event("♻️ تم استرجاع البيانات من النسخة الاحتياطية"
                  + (" (انقذت نسخة أحدث)" if backup.rescued else ""), "ok")
        elif status == "error":
            restore_failed_notice()
        else:
            event(f"💾 الاسترجاع: {'ماكو نسخة' if status == 'none' else 'البيانات محدّثة'} - {backup.note}", "info")
    except Exception as e:  # noqa: BLE001
        backup.blocked = True
        backup.last_error = str(e)
        restore_failed_notice()
    finally:
        _restored.set()
    try:
        _startup_rest()
    except Exception as e:  # noqa: BLE001
        error(f"خطأ بالتشغيل: {e!r}"[:200])


def _startup_rest():
    if not store.data.get("auto_interval_v1"):
        # one-time: the previous version stored a fixed 4s; switch to automatic
        with store.lock:
            store.data["settings"]["acc_interval"] = 0
            store.data["auto_interval_v1"] = True
            store.save()
    if not store.data.get("sources_v1"):
        s = store.settings
        mode = s.get("mode", "preset")
        key = {"preset": f"preset:{s.get('preset', 'five')}", "template": "template",
               "similar": "similar", "words": "words", "replace": "replace"}.get(mode)
        with store.lock:
            if key and not s.get("sources"):
                store.data["settings"]["sources"] = [key]
            store.data["sources_v1"] = True
            store.save()
    if not store.data.get("trend_single_v1"):
        # one-time: trend formulas now add ONE character; drop the old two-digit ones
        rules = list(store.settings.get("replace_rules", []))
        keep = [r for r in rules if not (r.get("src") == "trend" and len(r.get("pos", [])) == 2
                                         and len(r.get("base", "")) >= 6)]
        if len(keep) != len(rules):
            set_rules(keep)
        with store.lock:
            store.data["trend_single_v1"] = True
            store.save()
    if not store.data.get("trend_digit_v2"):
        # one-time: trend formulas are a single lucky digit only; drop the letter ones
        rules = list(store.settings.get("replace_rules", []))
        keep = [r for r in rules if not (r.get("src") == "trend" and len(r.get("pos", [])) == 1
                                         and not all(c.isdigit() for c in "".join(rule_sets(r))))]
        if len(keep) != len(rules):
            set_rules(keep)
        with store.lock:
            store.data["trend_digit_v2"] = True
            store.save()
    load_sessions()
    if (store.settings.get("auto_resume") and store.data.get("resume")
            and store.data.get("resume_chat") and not hunter.running):
        chat_id = store.data["resume_chat"]
        bot.send(chat_id, "♻️ <b>البوت رجع اشتغل</b> بعد إعادة تشغيل الخادم، والصيد رجع يشتغل وحده.")
        event("♻️ استئناف الصيد تلقائياً", "ok")
        err = start_hunt(chat_id)
        if err:
            bot.send(chat_id, err, main_menu())


def _final_backup():
    try:
        backup.backup()
    except Exception:  # noqa: BLE001
        pass


atexit.register(_final_backup)
event("تم تشغيل البوت", "ok")
threading.Thread(target=startup, daemon=True).start()
threading.Thread(target=backup_loop, daemon=True).start()
threading.Thread(target=trend_loop, daemon=True).start()

# under gunicorn (webhook mode) register the webhook on import
if WEBHOOK_URL:
    bot.call("setWebhook", url=f"{WEBHOOK_URL}/webhook",
             allowed_updates=["message", "callback_query"])
if WEBAPP_URL:  # the chat's menu button opens the dashboard
    bot.call("setChatMenuButton",
             menu_button={"type": "web_app", "text": "لوحة التحكم", "web_app": {"url": WEBAPP_URL}})
else:  # normal command menu (/start ...) next to the message box
    bot.call("setChatMenuButton", menu_button={"type": "commands"})
    for _admin in (sorted(ADMIN_IDS) or [store.data.get("owner")]):
        if _admin:  # also clear a per-chat button set by an earlier version
            bot.call("setChatMenuButton", chat_id=_admin, menu_button={"type": "commands"})
bot.call("setMyCommands", commands=[
    {"command": "start", "description": "القائمة الرئيسية"},
    {"command": "stop", "description": "إيقاف الصيد"},
])

if __name__ == "__main__":
    if WEBHOOK_URL:
        app.run(host="0.0.0.0", port=PORT)
    else:
        poll_forever()
