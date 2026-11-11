"""بوت صيد يوزرات تليجرام.

Environment variables:
    BOT_TOKEN       (required) token from @BotFather
    ADMIN_IDS       comma-separated Telegram user ids allowed to use the bot.
                    If empty, the first person to send /start becomes the owner.
    API_ID, API_HASH, TG_SESSION
                    optional - your own account (StringSession) for exact
                    checks and auto-claiming. Generate with gen_session.py.
    WEBHOOK_URL     optional - e.g. https://my-app.onrender.com ; if set the
                    bot runs in webhook mode (Flask), otherwise long polling.
    DATA_FILE       optional - path of the JSON data file (default data/hunter.json)
    PORT            web port for webhook mode (default 10000)
"""
import html
import os
import threading
import time

from flask import Flask, request

from hunter import checker
from hunter.claimer import Claimer, fmt_wait
from hunter.engine import Hunter
from hunter.generator import PRESETS, space_size, validate_template
from hunter.rules import is_valid, normalize
from hunter.storage import Store
from hunter.tg import BotAPI

TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required")

ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

bot = BotAPI(TOKEN)
store = Store(os.environ.get("DATA_FILE", "data/hunter.json"))
app = Flask(__name__)

# chat id -> pending input state
states = {}
# chat id that started the current hunt + its live status message
hunt_ctx = {"chat_id": None, "status_msg": None}

# ---------------------------------------------------------------- claimer
_claimer = None
_claimer_lock = threading.Lock()


def get_claimer():
    global _claimer
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    session = os.environ.get("TG_SESSION")
    if not (api_id and api_hash and session):
        return None
    with _claimer_lock:
        if _claimer is None:
            _claimer = Claimer(api_id, api_hash, session)
            _claimer.start()
    return _claimer


# ---------------------------------------------------------------- hunter
def on_found(username, status, source, claim_result):
    chat_id = hunt_ctx["chat_id"]
    if not chat_id:
        return
    if status == checker.FRAGMENT:
        bot.send(chat_id, f"💎 <b>يوزر معروض في فراجمنت</b>\n@{username}\n"
                          f"https://fragment.com/username/{username}")
        return
    text = f"🎉 <b>يوزر متاح!</b>\n\n👤 @{username}\n🧭 المصدر: {source}"
    markup = None
    if claim_result is not None:
        ok, msg = claim_result
        text += f"\n{'🔒' if ok else '⚠️'} التثبيت: {html.escape(msg)}"
    if not (claim_result and claim_result[0]):
        markup = {"inline_keyboard": [[{"text": "📌 احجزه بقناة", "callback_data": f"claim:{username}"}]]}
    bot.send(chat_id, text, markup)


def on_status(text):
    chat_id, msg_id = hunt_ctx["chat_id"], hunt_ctx["status_msg"]
    if not chat_id:
        return
    markup = {"inline_keyboard": [[{"text": "⏹ إيقاف", "callback_data": "stop"},
                                   {"text": "🔄 تحديث", "callback_data": "stats"}]]}
    if not hunter.running:
        markup = {"inline_keyboard": [[{"text": "🏆 الصيد", "callback_data": "found"},
                                       {"text": "🔙 القائمة", "callback_data": "menu"}]]}
    if msg_id:
        bot.edit(chat_id, msg_id, text, markup)
    else:
        bot.send(chat_id, text, markup)


hunter = Hunter(store, on_found, on_status, get_claimer)


# ---------------------------------------------------------------- UI
CLAIM_LABELS = {"off": "إيقاف", "channel": "قناة", "account": "حسابي"}
MODE_LABELS = {"preset": "جاهز", "template": "قالب", "words": "كلمات"}


def target_label(s):
    if s["mode"] == "words":
        return f"كلمات ({len(s['words'])})"
    if s["mode"] == "similar":
        return f"مشابه @{s['similar_base']}"
    if s["mode"] == "template":
        return f"قالب {s['template']}"
    return PRESETS.get(s["preset"], PRESETS["five"])[0]


def main_menu():
    s = store.settings
    run = "⏹ إيقاف الصيد" if hunter.running else "▶️ بدء الصيد"
    return {"inline_keyboard": [
        [{"text": run, "callback_data": "stop" if hunter.running else "start"}],
        [{"text": f"🎯 النوع: {target_label(s)}", "callback_data": "presets"}],
        [{"text": "🧬 بحث مشابه ليوزر", "callback_data": "set_similar"},
         {"text": "📌 حجز يوزر", "callback_data": "manual_claim"}],
        [{"text": "✍️ قالب مخصص", "callback_data": "set_template"},
         {"text": "📜 قائمة كلمات", "callback_data": "set_words"}],
        [{"text": f"➕ بادئة: {s['prefix'] or '-'}", "callback_data": "set_prefix"},
         {"text": f"➕ لاحقة: {s['suffix'] or '-'}", "callback_data": "set_suffix"}],
        [{"text": f"⚡ الخيوط: {s['threads']}", "callback_data": "set_threads"},
         {"text": f"⏱ التأخير: {s['delay']}s", "callback_data": "set_delay"}],
        [{"text": f"🔢 حد الفحص: {s['max_checks'] or '∞'}", "callback_data": "set_max"},
         {"text": f"🔒 التثبيت: {CLAIM_LABELS[s['claim_mode']]}", "callback_data": "cycle_claim"}],
        [{"text": f"🔁 تنويعات الكلمات: {'✅' if s['word_variants'] else '❌'}",
          "callback_data": "toggle_variants"},
         {"text": f"💎 إشعار فراجمنت: {'✅' if s['notify_fragment'] else '❌'}",
          "callback_data": "toggle_fragment"}],
        [{"text": "🔍 فحص يوزرات", "callback_data": "check"},
         {"text": "📊 الإحصائيات", "callback_data": "stats"}],
        [{"text": "🏆 الصيد", "callback_data": "found"},
         {"text": "📤 تصدير", "callback_data": "export"}],
        [{"text": "👤 حالة الحساب", "callback_data": "account"},
         {"text": "🗑 مسح الصيد", "callback_data": "clear_found"}],
        [{"text": "❓ شرح القوالب", "callback_data": "help"}],
    ]}


def presets_menu():
    rows, row = [], []
    for key, (label, tpls) in PRESETS.items():
        mark = "✅ " if store.settings["mode"] == "preset" and store.settings["preset"] == key else ""
        row.append({"text": f"{mark}{label}", "callback_data": f"preset:{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "🔙 رجوع", "callback_data": "menu"}])
    return {"inline_keyboard": rows}


BACK = {"inline_keyboard": [[{"text": "🔙 رجوع", "callback_data": "menu"}]]}

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
    "⚠️ أقل طول لليوزر 5 أحرف، ويجب أن يبدأ بحرف ولا ينتهي بـ _.\n"
    "🔒 التثبيت يحتاج ربط حسابك (API_ID / API_HASH / TG_SESSION)."
)


def home_text():
    c = get_claimer()
    acc = "غير مربوط"
    if c is not None:
        acc = f"@{c.me.username or c.me.id}" if c.ready and c.me else f"خطأ: {html.escape(c.last_error)}"
    return ("🦅 <b>بوت صيد يوزرات تليجرام</b>\n\n"
            f"• الحالة: {'🟢 يصيد' if hunter.running else '⚪ متوقف'}\n"
            f"• الحساب: {acc}\n"
            f"• المصادة المحفوظة: {len(store.data['found'])}\n\n"
            "اختر من الأزرار:")


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
def start_hunt(chat_id):
    s = store.settings
    if s["mode"] == "words" and not s["words"]:
        return "⚠️ قائمة الكلمات فارغة، أرسل الكلمات أولاً."
    if s["claim_mode"] != "off" and (get_claimer() is None or not get_claimer().ready):
        return "⚠️ التثبيت مفعّل لكن الحساب غير مربوط. أوقف التثبيت أو اربط الحساب."
    hunt_ctx["chat_id"] = chat_id
    msg = bot.send(chat_id, "🚀 جاري بدء الصيد...")
    hunt_ctx["status_msg"] = msg["message_id"] if msg else None
    if not hunter.start():
        return "⚠️ الصيد شغّال أصلاً."
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


def export_found(chat_id):
    found = store.data["found"]
    if not found:
        bot.send(chat_id, "📭 ماكو يوزرات مصادة بعد.", BACK)
        return
    body = "\n".join(
        f"@{f['username']}\t{f['status']}\t{f['source']}\t"
        f"{'claimed' if f.get('claimed') else ''}\t"
        f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(f['time']))}"
        for f in found)
    bot.send_document(chat_id, "found_usernames.txt", body.encode(), f"🏆 {len(found)} يوزر")


def found_text():
    found = store.data["found"][-40:]
    if not found:
        return "📭 ماكو يوزرات مصادة بعد."
    lines = [f"{'🔒' if f.get('claimed') else ('💎' if f['status'] == 'fragment' else '✅')} @{f['username']}"
             for f in reversed(found)]
    return f"🏆 <b>آخر الصيد ({len(store.data['found'])} المجموع)</b>\n\n" + "\n".join(lines)


def account_text():
    c = get_claimer()
    if c is None:
        return ("👤 <b>الحساب غير مربوط</b>\n\nبدون حساب يشتغل الفحص عن طريق t.me + fragment "
                "(سريع لكن أقل دقة) وما يكدر يثبت.\n\nللربط: شغّل <code>python gen_session.py</code> "
                "وحط القيم بمتغيرات البيئة API_ID و API_HASH و TG_SESSION.")
    if not c.ready:
        return f"👤 ❌ فشل تسجيل الدخول: <code>{html.escape(c.last_error)}</code>"
    fl = ""
    if c.check_flood_left:
        fl += f"\n🧊 فلود الفحص: {fmt_wait(c.check_flood_left)}"
    if c.claim_flood_left:
        fl += f"\n🧊 فلود الحجز: {fmt_wait(c.claim_flood_left)}"
    if hunter.verify_q or hunter.claim_q:
        fl += f"\n🕓 بانتظار التحقق: {len(hunter.verify_q)} | 📌 بانتظار الحجز: {len(hunter.claim_q)}"
    return (f"👤 <b>الحساب مربوط</b>\n\n• الاسم: {html.escape(c.me.first_name or '')}\n"
            f"• اليوزر: @{c.me.username or '-'}\n• الآيدي: <code>{c.me.id}</code>{fl}")


PROMPTS = {
    "set_template": ("template", "✍️ أرسل القالب (مثال <code>L=L=L</code> أو <code>CVCVC</code>).\nاضغط ❓ شرح القوالب للتفاصيل."),
    "set_words": ("words", "📜 أرسل الكلمات (كل كلمة بسطر) أو ارفع ملف .txt"),
    "set_prefix": ("prefix", "➕ أرسل البادئة، أو <code>-</code> للحذف"),
    "set_suffix": ("suffix", "➕ أرسل اللاحقة، أو <code>-</code> للحذف"),
    "set_threads": ("threads", "⚡ أرسل عدد الخيوط (1 - 50).\nنصيحة: 3-10 حتى ما ينحظر الآيبي."),
    "set_delay": ("delay", "⏱ أرسل التأخير بالثواني لكل خيط (مثال 0.5)"),
    "set_max": ("max_checks", "🔢 أرسل الحد الأقصى للفحص، أو 0 لبلا حد"),
    "set_similar": ("similar", "🧬 أرسل اليوزر اللي تريد مشابهاته (مثال <code>@lion</code>).\n"
                                "يولّد: تكرار حرف، _ بالنص، حروف شبيهة (o→0 ، i→l)، تبديل، حذف، إضافة حرف، لواحق مثل s/x/tg...\n"
                                "ويبدي الصيد فوراً."),
    "manual_claim": ("claim", "📌 أرسل اليوزر (أو أكثر، كل واحد بسطر) وأحجزه بقناة جديدة فوراً."),
    "check": ("check", "🔍 أرسل يوزر أو أكثر (كل واحد بسطر، حتى 50)"),
}


def run_claim_list(chat_id, names):
    c = get_claimer()
    if c is None or not c.ready:
        bot.send(chat_id, "⚠️ لازم تربط حسابك أول (👤 حالة الحساب).", BACK)
        return
    for n in names[:10]:
        if not is_valid(n):
            bot.send(chat_id, f"🚫 <code>{html.escape(n)}</code> صيغة غير صالحة (5 أحرف على الأقل)")
            continue
        ok, msg = c.claim(n, "channel")
        if not ok and msg.startswith("FLOOD:"):
            hunter.queue_claim(n)
            bot.send(chat_id, f"🧊 الحساب بفلود ({fmt_wait(c.claim_flood_left)}).\n"
                              f"📌 @{n} انضاف لقائمة الانتظار وراح ينحجز تلقائياً بعد الفلود.")
            continue
        if ok:
            store.add_found(n, checker.AVAILABLE, "manual", claimed=True)
            bot.send(chat_id, f"🔒 <b>تم الحجز!</b> @{n}\n{html.escape(msg)}")
        else:
            bot.send(chat_id, f"⚠️ ما انحجز @{n}\n<code>{html.escape(msg)}</code>")


def start_similar(chat_id, text):
    base = normalize(text.split()[0]) if text.split() else ""
    if not (3 <= len(base) <= 32) or not all(ch.isalnum() or ch == "_" for ch in base) or not base.isascii():
        bot.send(chat_id, "⚠️ يوزر غير صالح، أرسله بالإنجليزي مثل <code>@lion</code>", BACK)
        return
    states.pop(chat_id, None)
    store.set("similar_base", base)
    store.set("mode", "similar")
    from hunter.generator import similar_stream
    total = sum(1 for _ in similar_stream(base, store.settings["prefix"], store.settings["suffix"]))
    if hunter.running:
        bot.send(chat_id, f"✅ تم حفظ @{base} ({total} مشابه).\n⚠️ الصيد شغّال، أوقفه وابدأ من جديد.", main_menu())
        return
    bot.send(chat_id, f"🧬 جاري البحث عن <b>{total}</b> يوزر مشابه لـ @{base} ...")
    err = start_hunt(chat_id)
    if err:
        bot.send(chat_id, err, main_menu())


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
        bot.edit(chat_id, mid, home_text(), main_menu())
    elif data == "start":
        err = start_hunt(chat_id)
        if err:
            bot.send(chat_id, err, main_menu())
    elif data == "stop":
        if hunter.running:
            hunter.stop()
            bot.send(chat_id, "🛑 تم إرسال أمر الإيقاف...", main_menu())
        else:
            bot.send(chat_id, "ℹ️ الصيد متوقف أصلاً.", main_menu())
    elif data == "stats":
        if hunter.running and hunt_ctx["status_msg"] == mid:
            on_status(hunter.summary())
        else:
            bot.edit(chat_id, mid, hunter.summary(final=not hunter.running), BACK)
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
        bot.edit(chat_id, mid, prompt, BACK)
    elif data == "cycle_claim":
        order = ["off", "channel", "account"]
        nxt = order[(order.index(s["claim_mode"]) + 1) % 3]
        if nxt != "off":
            c = get_claimer()
            if c is None or not c.ready:
                bot.send(chat_id, "⚠️ لازم تربط حسابك أول (👤 حالة الحساب).")
                nxt = "off"
        store.set("claim_mode", nxt)
        bot.edit(chat_id, mid, home_text(), main_menu())
    elif data == "toggle_variants":
        store.set("word_variants", not s["word_variants"])
        bot.edit(chat_id, mid, home_text(), main_menu())
    elif data == "toggle_fragment":
        store.set("notify_fragment", not s["notify_fragment"])
        bot.edit(chat_id, mid, home_text(), main_menu())
    elif data == "found":
        bot.edit(chat_id, mid, found_text(), BACK)
    elif data == "export":
        export_found(chat_id)
    elif data == "clear_found":
        store.clear_found()
        bot.edit(chat_id, mid, "🗑 تم مسح قائمة الصيد.", BACK)
    elif data == "account":
        bot.edit(chat_id, mid, account_text(), BACK)
    elif data == "help":
        bot.edit(chat_id, mid, HELP, BACK)


def handle_input(chat_id, key, text):
    s = store.settings
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
        return f"✅ تم حفظ {len(words)} كلمة ووضع الصيد صار «كلمات»."
    if key in ("prefix", "suffix"):
        val = "" if text.strip() == "-" else normalize(text)
        if val and not all(ch.isalnum() or ch == "_" for ch in val):
            return "⚠️ استخدم حروف إنجليزية صغيرة أو أرقام أو _ فقط."
        store.set(key, val)
        return f"✅ تم الحفظ: <code>{val or '-'}</code>"
    if key == "threads":
        if not text.isdigit() or not 1 <= int(text) <= 50:
            return "⚠️ أرسل رقم من 1 إلى 50."
        store.set("threads", int(text))
        return f"✅ الخيوط: {text}" + (" (يطبَّق بالصيد الجاي)" if hunter.running else "")
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


def handle_message(msg):
    chat_id = str(msg["chat"]["id"])
    if not allowed(str(msg.get("from", {}).get("id", ""))):
        bot.send(chat_id, "🚫 هذا البوت خاص.")
        return
    text = (msg.get("text") or "").strip()
    state = states.get(chat_id)

    if text.startswith("/start") or text.startswith("/menu"):
        states.pop(chat_id, None)
        bot.send(chat_id, home_text(), main_menu())
        return
    if text.startswith("/stop"):
        hunter.stop()
        bot.send(chat_id, "🛑 تم إرسال أمر الإيقاف.")
        return

    # wordlist as a .txt file
    if "document" in msg and state == "words":
        content = bot.download_text(msg["document"]["file_id"])
        if content is None:
            bot.send(chat_id, "⚠️ ما كدرت أقرأ الملف (الحد 5MB).")
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
        reply = handle_input(chat_id, state, text)
        if reply and reply.startswith("✅"):
            states.pop(chat_id, None)
            bot.send(chat_id, reply, main_menu())
        elif reply:
            bot.send(chat_id, reply, BACK)
        return

    # quick check: just send @username
    if text.startswith("@") and len(text.split()) == 1:
        threading.Thread(target=run_check_list, args=(chat_id, [normalize(text)]), daemon=True).start()
        return
    bot.send(chat_id, home_text(), main_menu())


def dispatch(update):
    try:
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            handle_message(update["message"])
    except Exception as e:  # noqa: BLE001 - never kill the bot on one bad update
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


def setup_webhook():
    if WEBHOOK_URL:
        bot.call("setWebhook", url=f"{WEBHOOK_URL}/webhook",
                 allowed_updates=["message", "callback_query"])


def poll_forever():
    bot.call("deleteWebhook")
    offset = None
    print("[bot] polling...")
    while True:
        for upd in bot.get_updates(offset):
            offset = upd["update_id"] + 1
            threading.Thread(target=dispatch, args=(upd,), daemon=True).start()


# under gunicorn (webhook mode) register the webhook on import
if WEBHOOK_URL:
    setup_webhook()

if __name__ == "__main__":
    threading.Thread(target=get_claimer, daemon=True).start()  # warm up login
    if WEBHOOK_URL:
        app.run(host="0.0.0.0", port=PORT)
    else:
        poll_forever()
