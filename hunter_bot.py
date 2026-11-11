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
import html
import os
import re
import threading
import time

from flask import Flask, request

from hunter import checker
from hunter.claimer import Claimer, fmt_wait
from hunter.engine import Hunter
from hunter.events import error, errors, event, events
from hunter.generator import PRESETS, similar_stream, space_size, validate_template
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

states = {}   # chat id -> pending input state
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


def env_sessions():
    raw = [os.environ.get("TG_SESSION", ""), os.environ.get("TG_SESSIONS", "")]
    out = []
    for chunk in raw:
        out += [x.strip() for x in re.split(r"[,\s]+", chunk) if x.strip()]
    return out


def connect_session(string, label):
    """Connect one StringSession and add it to the pool. Return the Claimer."""
    c = Claimer(API_ID, API_HASH, string, label)
    c.start()
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
    if s["mode"] == "words":
        return f"كلمات ({len(s['words'])})"
    if s["mode"] == "similar":
        return f"مشابه @{s['similar_base']}"
    if s["mode"] == "template":
        return f"قالب {s['template']}"
    return PRESETS.get(s["preset"], PRESETS["five"])[0]


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
             f"• مدة التشغيل: {hms(time.time() - BOOT)}\n")
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


def main_menu():
    run = btn("⏹ إيقاف الصيد", "stop") if hunter.running else btn("▶️ تشغيل الصيد", "start")
    return {"inline_keyboard": [
        [run],
        [btn("📊 الإحصائيات", "stats"), btn("🔑 الجلسات", "sessions")],
        [btn("⚙️ الإعدادات", "settings"), btn("📜 سجل الأخطاء", "errors")],
        [btn("🎯 نوع الصيد", "presets"), btn("🧬 بحث مشابه", "set_similar")],
        [btn("🔍 فحص يوزر", "check"), btn("📌 حجز يوزر", "manual_claim")],
        [btn("🏆 النتائج", "found"), btn("🔄 تحديث", "menu")],
        [btn(f"الإشعارات: {NOTIFY_LABELS[store.settings.get('notify', 'claimed')]}", "cycle_notify")],
    ]}


def settings_menu():
    s = store.settings
    return {"inline_keyboard": [
        [btn("✍️ قالب مخصص", "set_template"), btn("📜 قائمة كلمات", "set_words")],
        [btn(f"➕ بادئة: {s['prefix'] or '-'}", "set_prefix"),
         btn(f"➕ لاحقة: {s['suffix'] or '-'}", "set_suffix")],
        [btn(f"⚡ الخيوط: {s['threads']}", "set_threads"),
         btn(f"⏱ التأخير: {s['delay']}s", "set_delay")],
        [btn(f"🔢 حد الفحص: {s['max_checks'] or '∞'}", "set_max"),
         btn(f"🔒 الحجز: {CLAIM_LABELS[s['claim_mode']]}", "cycle_claim")],
        [btn(f"🔁 تنويعات: {'✅' if s['word_variants'] else '❌'}", "toggle_variants"),
         btn(f"💎 فراجمنت: {'✅' if s['notify_fragment'] else '❌'}", "toggle_fragment")],
        [btn("❓ شرح القوالب", "help")],
        [btn("🔙 رجوع", "menu")],
    ]}


def presets_menu():
    rows, row = [], []
    for key, (label, _t) in PRESETS.items():
        mark = "✅ " if store.settings["mode"] == "preset" and store.settings["preset"] == key else ""
        row.append(btn(f"{mark}{label}", f"preset:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("🔙 رجوع", "menu")])
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
            icon, name = "🔴", "غير متصل"
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
         f"• المصدر: {c.label}\n")
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
        return True
    return owner == user_id


# ---------------------------------------------------------------- actions
def start_hunt(chat_id, mid=None):
    s = store.settings
    if s["mode"] == "words" and not s["words"]:
        return "⚠️ قائمة الكلمات فارغة، أرسل الكلمات أولاً من ⚙️ الإعدادات."
    if hunter.running:
        return "⚠️ الصيد شغّال أصلاً."
    hunt_ctx["chat_id"] = chat_id
    if mid is None:
        msg = bot.send(chat_id, "🚀 جاري بدء الصيد...")
        mid = msg["message_id"] if msg else None
    hunt_ctx["status_msg"] = mid
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
    store.set("mode", "similar")
    total = sum(1 for _ in similar_stream(base, store.settings["prefix"], store.settings["suffix"]))
    if hunter.running:
        bot.send(chat_id, f"✅ تم حفظ @{base} ({total} مشابه).\n⚠️ الصيد شغّال، أوقفه وشغّله من جديد.", main_menu())
        return
    event(f"🧬 بحث مشابه لـ @{base} ({total})", "info")
    err = start_hunt(chat_id)
    if err:
        bot.send(chat_id, err, main_menu())


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
    if not c.ready:
        pool.sessions.remove(c)
        c.disconnect()
        bot.send(chat_id, f"❌ ما اشتغلت الجلسة:\n<code>{html.escape(c.last_error)}</code>",
                 sessions_menu())
        return
    with store.lock:
        store.data["sessions"].append(string)
        store.save()
    bot.send(chat_id, f"✅ <b>تمت إضافة الجلسة</b> @{c.me.username or c.me.id}\n\n"
                      "💡 Render يمسح البيانات مع كل نشر. اضغط 💾 نسخة احتياطية وحط "
                      "الجلسات بمتغير <code>TG_SESSIONS</code> حتى تبقى.", sessions_menu())


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
    "set_template": ("template", "✍️ أرسل القالب (مثال <code>L=L=L</code> أو <code>CVCVC</code>).\nاضغط ❓ شرح القوالب للتفاصيل."),
    "set_words": ("words", "📜 أرسل الكلمات (كل كلمة بسطر) أو ارفع ملف .txt"),
    "set_prefix": ("prefix", "➕ أرسل البادئة، أو <code>-</code> للحذف"),
    "set_suffix": ("suffix", "➕ أرسل اللاحقة، أو <code>-</code> للحذف"),
    "set_threads": ("threads", "⚡ أرسل عدد الخيوط (1 - 50).\nنصيحة: 3-5 حتى ما يصير فلود."),
    "set_delay": ("delay", "⏱ أرسل التأخير بالثواني لكل خيط (مثال 1)"),
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
        tpl = text.replace(" ", "")
        if not validate_template(tpl):
            return "⚠️ قالب غير صالح. استخدم L D X V C = وحروف صغيرة/أرقام/_ فقط."
        store.set("template", tpl)
        store.set("mode", "template")
        return f"✅ تم حفظ القالب <code>{tpl}</code> (~{space_size(tpl):,} احتمال)"
    if key == "words":
        words = [normalize(w) for w in text.replace(",", "\n").split()]
        words = list(dict.fromkeys(w for w in words if w))
        store.set("words", words)
        store.set("mode", "words")
        return f"✅ تم حفظ {len(words)} كلمة ونوع الصيد صار «كلمات»."
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
        bot.edit(chat_id, mid, "⚙️ <b>الإعدادات</b>\n\nاختار شنو تريد تغيّر:", settings_menu())
    elif data == "errors":
        bot.edit(chat_id, mid, errors_text(),
                 {"inline_keyboard": [[btn("🗑 مسح السجل", "errors_clear"), btn("🔙 رجوع", "menu")]]})
    elif data == "errors_clear":
        errors.items.clear()
        errors.count = 0
        bot.edit(chat_id, mid, errors_text(), BACK)
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
                              "💾 انسخ المحتوى كله وحطه بمتغير TG_SESSIONS برندر حتى تبقى الجلسات بعد كل نشر.\n"
                              "⚠️ لا تنطي هذا الملف لأي أحد.")
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
            store.set("preset", key)
            store.set("mode", "preset")
            label, tpls = PRESETS[key]
            total = sum(space_size(t) for t in tpls)
            bot.edit(chat_id, mid, f"✅ تم اختيار <b>{label}</b>\nالقوالب: <code>{' | '.join(tpls)}</code>\n"
                                   f"عدد الاحتمالات تقريباً: {total:,}", presets_menu())
    elif data in PROMPTS:
        key, prompt = PROMPTS[data]
        states[chat_id] = key
        bot.send(chat_id, prompt, BACK)
    elif data == "cycle_notify":
        order = ["claimed", "off", "all"]
        cur = store.settings.get("notify", "claimed")
        store.set("notify", order[(order.index(cur) + 1) % 3] if cur in order else "claimed")
        bot.edit(chat_id, mid, dashboard(), main_menu())
    elif data == "cycle_claim":
        order = ["off", "channel", "account"]
        nxt = order[(order.index(s["claim_mode"]) + 1) % 3]
        if nxt != "off" and not pool.ready:
            bot.send(chat_id, "⚠️ لازم تضيف جلسة أول من 🔑 الجلسات.")
            nxt = "off"
        store.set("claim_mode", nxt)
        bot.edit(chat_id, mid, "⚙️ <b>الإعدادات</b>\n\nاختار شنو تريد تغيّر:", settings_menu())
    elif data in ("toggle_variants", "toggle_fragment"):
        key = "word_variants" if data == "toggle_variants" else "notify_fragment"
        store.set(key, not s[key])
        bot.edit(chat_id, mid, "⚙️ <b>الإعدادات</b>\n\nاختار شنو تريد تغيّر:", settings_menu())
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
    if not allowed(str(msg.get("from", {}).get("id", ""))):
        bot.send(chat_id, "🚫 هذا البوت خاص.")
        return
    text = (msg.get("text") or "").strip()
    state = states.get(chat_id)

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

    if "document" in msg and state == "words":
        content = bot.download_text(msg["document"]["file_id"])
        if content is None:
            bot.send(chat_id, "⚠️ ما گدرت أقرأ الملف (الحد 5MB).")
            return
        text = content

    if state == "similar":
        start_similar(chat_id, text)
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
            bot.send(chat_id, reply, settings_menu())
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


def poll_forever():
    bot.call("deleteWebhook")
    offset = None
    print("[bot] polling...")
    while True:
        for upd in bot.get_updates(offset):
            offset = upd["update_id"] + 1
            threading.Thread(target=dispatch, args=(upd,), daemon=True).start()


event("تم تشغيل البوت", "ok")
threading.Thread(target=load_sessions, daemon=True).start()

# under gunicorn (webhook mode) register the webhook on import
if WEBHOOK_URL:
    bot.call("setWebhook", url=f"{WEBHOOK_URL}/webhook",
             allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    if WEBHOOK_URL:
        app.run(host="0.0.0.0", port=PORT)
    else:
        poll_forever()
