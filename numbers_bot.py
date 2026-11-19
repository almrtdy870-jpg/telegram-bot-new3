# -*- coding: utf-8 -*-
"""
بوت مورد أرقام (أرقام وهمية لاستلام كود التفعيل).

يشتري الأرقام من مزوّد 5sim.net ويبيعها للزباين بسعرك، مع نظام رصيد ولوحة أدمن.

متغيرات البيئة:
  BOT_TOKEN       توكن البوت من @BotFather (مطلوب)
  FIVESIM_TOKEN   مفتاح API من 5sim.net (مطلوب للشراء)
  ADMIN_IDS       آيديات الأدمن مفصولة بفاصلة (مطلوب)
  WEBHOOK_URL     رابط الخدمة على Render (اختياري، بدونه يشتغل polling)
  NUMBERS_DATA    مسار ملف البيانات (الافتراضي data/numbers.json)
"""
import json
import os
import threading
import time
import traceback

import requests
from flask import Flask, request

# ───────────────────────── الإعدادات ─────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
FIVESIM_TOKEN = os.environ.get("FIVESIM_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip().rstrip("/")
DATA_FILE = os.environ.get("NUMBERS_DATA", "data/numbers.json")
PORT = int(os.environ.get("PORT", 10000))

TG_API = f"https://api.telegram.org/bot{TOKEN}"
FIVESIM_API = "https://5sim.net/v1"
POLL_EVERY = 5          # ثواني بين كل فحص للطلبات الفعالة
ORDER_LIFETIME = 20 * 60  # بعدها يتلغى الطلب تلقائياً ويرجع الرصيد

app = Flask(__name__)

# الخدمات المعروضة (slug بـ 5sim ← الاسم بالبوت)
SERVICES = {
    "telegram": "✈️ تليجرام",
    "whatsapp": "💬 واتساب",
    "facebook": "📘 فيسبوك",
    "instagram": "📸 انستغرام",
    "google": "🔎 جوجل / جيميل",
    "tiktok": "🎵 تيك توك",
    "twitter": "🐦 تويتر / X",
    "snapchat": "👻 سناب شات",
    "discord": "🎮 ديسكورد",
    "microsoft": "🪟 مايكروسوفت",
    "openai": "🤖 ChatGPT",
    "other": "📦 أي خدمة ثانية",
}

COUNTRIES = {
    "russia": "🇷🇺 روسيا", "ukraine": "🇺🇦 أوكرانيا", "kazakhstan": "🇰🇿 كازاخستان",
    "usa": "🇺🇸 أمريكا", "usavirtual": "🇺🇸 أمريكا (افتراضي)", "england": "🇬🇧 بريطانيا",
    "canada": "🇨🇦 كندا", "germany": "🇩🇪 ألمانيا", "france": "🇫🇷 فرنسا",
    "netherlands": "🇳🇱 هولندا", "spain": "🇪🇸 إسبانيا", "italy": "🇮🇹 إيطاليا",
    "poland": "🇵🇱 بولندا", "sweden": "🇸🇪 السويد", "finland": "🇫🇮 فنلندا",
    "portugal": "🇵🇹 البرتغال", "romania": "🇷🇴 رومانيا", "estonia": "🇪🇪 إستونيا",
    "lithuania": "🇱🇹 ليتوانيا", "latvia": "🇱🇻 لاتفيا", "czech": "🇨🇿 التشيك",
    "indonesia": "🇮🇩 إندونيسيا", "india": "🇮🇳 الهند", "philippines": "🇵🇭 الفلبين",
    "vietnam": "🇻🇳 فيتنام", "thailand": "🇹🇭 تايلاند", "malaysia": "🇲🇾 ماليزيا",
    "cambodia": "🇰🇭 كمبوديا", "laos": "🇱🇦 لاوس", "myanmar": "🇲🇲 ميانمار",
    "bangladesh": "🇧🇩 بنغلاديش", "pakistan": "🇵🇰 باكستان", "china": "🇨🇳 الصين",
    "hongkong": "🇭🇰 هونغ كونغ", "japan": "🇯🇵 اليابان", "southkorea": "🇰🇷 كوريا",
    "egypt": "🇪🇬 مصر", "morocco": "🇲🇦 المغرب", "algeria": "🇩🇿 الجزائر",
    "tunisia": "🇹🇳 تونس", "saudiarabia": "🇸🇦 السعودية", "iraq": "🇮🇶 العراق",
    "jordan": "🇯🇴 الأردن", "yemen": "🇾🇪 اليمن", "israel": "🇮🇱 إسرائيل",
    "turkey": "🇹🇷 تركيا", "nigeria": "🇳🇬 نيجيريا", "kenya": "🇰🇪 كينيا",
    "southafrica": "🇿🇦 جنوب أفريقيا", "ghana": "🇬🇭 غانا", "brazil": "🇧🇷 البرازيل",
    "mexico": "🇲🇽 المكسيك", "colombia": "🇨🇴 كولومبيا", "argentina": "🇦🇷 الأرجنتين",
    "chile": "🇨🇱 تشيلي", "peru": "🇵🇪 بيرو", "georgia": "🇬🇪 جورجيا",
    "uzbekistan": "🇺🇿 أوزبكستان", "kyrgyzstan": "🇰🇬 قيرغيزستان", "azerbaijan": "🇦🇿 أذربيجان",
    "armenia": "🇦🇲 أرمينيا", "belarus": "🇧🇾 بيلاروسيا", "moldova": "🇲🇩 مولدوفا",
}

COUNTRIES_PER_PAGE = 16

# ───────────────────────── التخزين ─────────────────────────
_lock = threading.RLock()

DEFAULT_DB = {
    "settings": {
        "multiplier": 0.02,      # سعر المزوّد (روبل) × هذا = سعرك بعملة البوت
        "min_price": 0.10,       # أقل سعر بيع
        "currency": "$",
        "recharge_text": "💳 للشحن راسل الأدمن وأرسل له الآيدي مالتك.",
        "enabled": True,
        "overrides": {},         # "service:country" → سعر ثابت
    },
    "users": {},                 # uid → {balance, spent, orders, name, joined, banned}
    "orders": {},                # order_id → {...}
    "stats": {"sold": 0, "revenue": 0.0, "cost": 0.0},
}


def _load():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    db = json.loads(json.dumps(DEFAULT_DB))
    for k, v in data.items():
        if isinstance(v, dict) and k in db:
            db[k].update(v)
        else:
            db[k] = v
    return db


DB = _load()


def save():
    with _lock:
        os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DB, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DATA_FILE)


def S():
    return DB["settings"]


def user(uid, name=None):
    uid = str(uid)
    with _lock:
        u = DB["users"].get(uid)
        if u is None:
            u = DB["users"][uid] = {"balance": 0.0, "spent": 0.0, "orders": 0,
                                    "name": name or "", "joined": int(time.time()), "banned": False}
            save()
        elif name and u.get("name") != name:
            u["name"] = name
        return u


def add_balance(uid, amount):
    with _lock:
        u = user(uid)
        u["balance"] = round(u["balance"] + amount, 4)
        save()
        return u["balance"]


def money(x):
    return f"{x:.2f}{S()['currency']}"


# ───────────────────────── تليجرام ─────────────────────────
def tg(method, **params):
    try:
        r = requests.post(f"{TG_API}/{method}", json=params, timeout=30)
        return r.json()
    except Exception as e:
        print(f"[tg] {method} error: {e}")
        return {}


def send(chat_id, text, kb=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    return tg("sendMessage", **p)


def edit(chat_id, msg_id, text, kb=None):
    p = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    res = tg("editMessageText", **p)
    if not res.get("ok") and "not modified" not in str(res.get("description", "")):
        send(chat_id, text, kb)


def answer(cb_id, text="", alert=False):
    tg("answerCallbackQuery", callback_query_id=cb_id, text=text, show_alert=alert)


def btn(text, data):
    return {"text": text, "callback_data": data}


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ───────────────────────── 5sim ─────────────────────────
class ProviderError(Exception):
    pass


PROVIDER_ERRORS = {
    "no free phones": "ماكو أرقام متوفرة هسه لهاي الدولة، جرّب دولة ثانية.",
    "not enough user balance": "رصيد المورد خلص، راسل الأدمن.",
    "not enough rating": "حساب المورد عليه قيود، راسل الأدمن.",
    "select country": "الدولة غير صحيحة.",
    "select operator": "المشغّل غير صحيح.",
    "bad country": "الدولة غير مدعومة.",
    "bad operator": "المشغّل غير مدعوم.",
    "no product": "الخدمة غير متوفرة بهاي الدولة.",
    "server offline": "المورد طافي هسه، جرّب بعدين.",
    "order not found": "الطلب ما موجود.",
    "order expired": "الطلب انتهى.",
    "order has sms": "وصل كود، ما يمكن الإلغاء.",
    "hosting order": "طلب غير مدعوم.",
}


def fivesim(path, auth=True, params=None):
    headers = {"Accept": "application/json"}
    if auth:
        if not FIVESIM_TOKEN:
            raise ProviderError("مفتاح FIVESIM_TOKEN مو مضبوط.")
        headers["Authorization"] = f"Bearer {FIVESIM_TOKEN}"
    try:
        r = requests.get(FIVESIM_API + path, headers=headers, params=params, timeout=25)
    except requests.RequestException as e:
        raise ProviderError(f"تعذّر الاتصال بالمورد: {e}")
    try:
        data = r.json()
    except ValueError:
        msg = r.text.strip().lower()
        raise ProviderError(PROVIDER_ERRORS.get(msg, f"خطأ من المورد: {esc(r.text.strip()[:120]) or r.status_code}"))
    if r.status_code >= 400:
        raise ProviderError(f"خطأ من المورد ({r.status_code})")
    return data


_price_cache = {}


def prices_for(product):
    """{country: (cost, count, operator)} لأرخص مشغّل متوفر بكل دولة."""
    now = time.time()
    hit = _price_cache.get(product)
    if hit and now - hit[0] < 120:
        return hit[1]
    data = fivesim("/guest/prices", auth=False, params={"product": product})
    block = data.get(product, data) if isinstance(data, dict) else {}
    out = {}
    for country, ops in block.items():
        if not isinstance(ops, dict):
            continue
        best = None
        for op, info in ops.items():
            if not isinstance(info, dict):
                continue
            cost, count = info.get("cost", 0), info.get("count", 0)
            if count and count > 0 and cost and (best is None or cost < best[0]):
                best = (float(cost), int(count), op)
        if best:
            out[country] = best
    _price_cache[product] = (now, out)
    return out


def sell_price(product, country, cost):
    ov = S()["overrides"].get(f"{product}:{country}")
    if ov is not None:
        return round(float(ov), 2)
    return round(max(cost * S()["multiplier"], S()["min_price"]), 2)


def country_name(c):
    return COUNTRIES.get(c, "🌍 " + c.capitalize())


def service_name(p):
    return SERVICES.get(p, p)


# ───────────────────────── الواجهات ─────────────────────────
def main_menu(uid):
    u = user(uid)
    text = (f"👋 هلا بيك ببوت الأرقام\n\n"
            f"🆔 الآيدي: <code>{uid}</code>\n"
            f"💰 رصيدك: <b>{money(u['balance'])}</b>\n\n"
            f"اختار شنو تريد 👇")
    kb = [
        [btn("📱 شراء رقم", "buy")],
        [btn("💰 رصيدي", "bal"), btn("💳 شحن رصيد", "recharge")],
        [btn("🧾 طلباتي", "myorders")],
    ]
    if int(uid) in ADMIN_IDS:
        kb.append([btn("⚙️ لوحة الأدمن", "admin")])
    return text, kb


def services_menu():
    items = list(SERVICES.items())
    kb = [[btn(items[i][1], f"svc:{items[i][0]}")] + ([btn(items[i + 1][1], f"svc:{items[i + 1][0]}")] if i + 1 < len(items) else [])
          for i in range(0, len(items), 2)]
    kb.append([btn("🔙 رجوع", "home")])
    return "📱 اختار الخدمة اللي تريد الرقم إلها:", kb


def countries_menu(product, page=0):
    prices = prices_for(product)
    rows = sorted(prices.items(), key=lambda kv: sell_price(product, kv[0], kv[1][0]))
    if not rows:
        return f"❌ ماكو أرقام متوفرة هسه لـ {service_name(product)}.", [[btn("🔙 رجوع", "buy")]]
    pages = (len(rows) - 1) // COUNTRIES_PER_PAGE + 1
    page = max(0, min(page, pages - 1))
    chunk = rows[page * COUNTRIES_PER_PAGE:(page + 1) * COUNTRIES_PER_PAGE]
    kb = []
    for i in range(0, len(chunk), 2):
        row = []
        for country, (cost, count, op) in chunk[i:i + 2]:
            row.append(btn(f"{country_name(country)} • {money(sell_price(product, country, cost))}",
                           f"cty:{product}:{country}"))
        kb.append(row)
    nav = []
    if page > 0:
        nav.append(btn("⬅️ السابق", f"svc:{product}:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("التالي ➡️", f"svc:{product}:{page + 1}"))
    if nav:
        kb.append(nav)
    kb.append([btn("🔙 رجوع", "buy")])
    return (f"{service_name(product)}\n🌍 اختار الدولة (مرتبة من الأرخص) — صفحة {page + 1}/{pages}"), kb


def confirm_menu(uid, product, country):
    prices = prices_for(product)
    if country not in prices:
        return "❌ هاي الدولة ما بيها أرقام هسه.", [[btn("🔙 رجوع", f"svc:{product}")]]
    cost, count, op = prices[country]
    price = sell_price(product, country, cost)
    u = user(uid)
    text = (f"🧾 <b>تأكيد الطلب</b>\n\n"
            f"الخدمة: {service_name(product)}\n"
            f"الدولة: {country_name(country)}\n"
            f"المتوفر: {count}\n"
            f"السعر: <b>{money(price)}</b>\n"
            f"رصيدك: {money(u['balance'])}\n\n"
            f"ℹ️ إذا ما وصل الكود خلال {ORDER_LIFETIME // 60} دقيقة، الطلب يتلغى ويرجعلك رصيدك كامل.")
    kb = [[btn("✅ اشتري", f"ok:{product}:{country}")], [btn("🔙 رجوع", f"svc:{product}")]]
    return text, kb


def order_text(o):
    status = {
        "wait": "⏳ بانتظار الكود...",
        "got": "✅ وصل الكود",
        "cancel": "❌ ملغي (رجع رصيدك)",
        "timeout": "⌛ انتهى الوقت (رجع رصيدك)",
        "done": "✔️ مكتمل",
    }.get(o["status"], o["status"])
    t = (f"📱 <b>رقمك</b>: <code>{esc(o['phone'])}</code>\n"
         f"الخدمة: {service_name(o['product'])}\n"
         f"الدولة: {country_name(o['country'])}\n"
         f"السعر: {money(o['price'])}\n"
         f"الحالة: {status}\n")
    for code in o.get("codes", []):
        t += f"\n🔑 الكود: <code>{esc(code)}</code>"
    if o["status"] == "wait":
        left = max(0, int(o["created"] + ORDER_LIFETIME - time.time()))
        t += f"\n\n⏱ الوقت المتبقي: {left // 60}:{left % 60:02d}"
    return t


def order_kb(o):
    if o["status"] == "wait":
        return [[btn("🔄 تحديث", f"ord:{o['id']}"), btn("❌ إلغاء واسترجاع", f"cnl:{o['id']}")]]
    if o["status"] == "got":
        return [[btn("🔄 كود ثاني", f"ord:{o['id']}"), btn("✔️ إنهاء", f"fin:{o['id']}")]]
    return [[btn("📱 شراء رقم جديد", "buy"), btn("🏠 الرئيسية", "home")]]


# ───────────────────────── منطق الطلبات ─────────────────────────
def buy(uid, product, country):
    with _lock:
        if not S()["enabled"] and int(uid) not in ADMIN_IDS:
            raise ProviderError("البوت متوقف مؤقتاً للصيانة.")
        prices = prices_for(product)
        if country not in prices:
            raise ProviderError("ماكو أرقام متوفرة بهاي الدولة هسه.")
        cost, count, op = prices[country]
        price = sell_price(product, country, cost)
        u = user(uid)
        if u.get("banned"):
            raise ProviderError("حسابك محظور.")
        if u["balance"] + 1e-9 < price:
            raise ProviderError(f"رصيدك ما يكفي. تحتاج {money(price)} ورصيدك {money(u['balance'])}.")
        # نخصم أول حتى ما ينشترى رقمين بنفس الرصيد
        u["balance"] = round(u["balance"] - price, 4)
        save()
    try:
        data = fivesim(f"/user/buy/activation/{country}/{op}/{product}")
        if not isinstance(data, dict) or not data.get("phone"):
            raise ProviderError("المورد ما رجّع رقم، جرّب مرة ثانية.")
    except Exception:
        add_balance(uid, price)
        _price_cache.pop(product, None)
        raise
    oid = str(data["id"])
    o = {"id": oid, "uid": str(uid), "phone": data["phone"], "product": product,
         "country": country, "operator": op, "price": price, "cost": float(data.get("price", cost)),
         "status": "wait", "codes": [], "created": time.time(), "chat": int(uid), "msg": None}
    with _lock:
        DB["orders"][oid] = o
        u = user(uid)
        u["orders"] += 1
        save()
    return o


def refund(o, new_status):
    with _lock:
        if o["status"] != "wait":
            return False
        o["status"] = new_status
        add_balance(o["uid"], o["price"])
        save()
    return True


def mark_paid(o):
    """أول كود يوصل: الطلب ينحسب مبيوع."""
    with _lock:
        user(o["uid"])["spent"] = round(user(o["uid"])["spent"] + o["price"], 4)
        st = DB["stats"]
        st["sold"] += 1
        st["revenue"] = round(st["revenue"] + o["price"], 4)
        st["cost"] = round(st["cost"] + o["cost"], 4)


def sync_order(o):
    """يسحب حالة الطلب من المورد ويبلّغ الزبون إذا وصل كود جديد."""
    if o["status"] not in ("wait", "got"):
        return
    try:
        data = fivesim(f"/user/check/{o['id']}")
    except ProviderError as e:
        print(f"[check] {o['id']}: {e}")
        if o["status"] == "wait" and time.time() > o["created"] + ORDER_LIFETIME + 300:
            refund(o, "timeout")
        return
    new_codes = []
    for sms in data.get("sms") or []:
        code = sms.get("code") or sms.get("text") or ""
        if code and code not in o["codes"]:
            new_codes.append(code)
    remote = data.get("status", "")
    if new_codes:
        with _lock:
            first = o["status"] == "wait"
            o["codes"].extend(new_codes)
            o["status"] = "got"
            if first:
                mark_paid(o)
            save()
        for c in new_codes:
            send(o["chat"], f"🔔 وصل كود لرقم <code>{esc(o['phone'])}</code>\n\n🔑 <code>{esc(c)}</code>",
                 order_kb(o))
        return
    if o["status"] == "wait":
        expired = time.time() > o["created"] + ORDER_LIFETIME
        if remote in ("CANCELED", "TIMEOUT", "BANNED") or expired:
            if expired and remote not in ("CANCELED", "TIMEOUT", "BANNED"):
                try:
                    fivesim(f"/user/cancel/{o['id']}")
                except ProviderError:
                    pass
            if refund(o, "timeout"):
                send(o["chat"], f"⌛ ما وصل كود للرقم <code>{esc(o['phone'])}</code>.\n"
                                f"تلغى الطلب ورجعلك <b>{money(o['price'])}</b> لرصيدك.",
                     [[btn("📱 جرّب رقم ثاني", "buy")]])
    elif o["status"] == "got" and (remote in ("FINISHED", "TIMEOUT", "CANCELED", "BANNED")
                                   or time.time() > o["created"] + ORDER_LIFETIME):
        with _lock:
            o["status"] = "done"
            save()


def poller():
    while True:
        try:
            with _lock:
                active = [o for o in DB["orders"].values() if o["status"] in ("wait", "got")]
            for o in active:
                sync_order(o)
        except Exception:
            traceback.print_exc()
        time.sleep(POLL_EVERY)


# ───────────────────────── الأدمن ─────────────────────────
pending_input = {}   # uid → (action, extra)


def admin_menu():
    st, s = DB["stats"], S()
    try:
        bal = fivesim("/user/profile").get("balance", "?")
        prov = f"{bal} ₽"
    except ProviderError as e:
        prov = f"⚠️ {e}"
    active = sum(1 for o in DB["orders"].values() if o["status"] == "wait")
    text = (f"⚙️ <b>لوحة الأدمن</b>\n\n"
            f"👥 المستخدمين: {len(DB['users'])}\n"
            f"📦 الأرقام المباعة: {st['sold']}\n"
            f"💵 المبيعات: {money(st['revenue'])}\n"
            f"💸 الكلفة عند المورد: {st['cost']:.2f} ₽\n"
            f"⏳ طلبات فعالة: {active}\n"
            f"🏦 رصيد المورد (5sim): {prov}\n\n"
            f"📈 معامل التسعير: × {s['multiplier']} (سعر المورد بالروبل × المعامل)\n"
            f"🔻 أقل سعر: {money(s['min_price'])}\n"
            f"🔌 حالة البوت: {'شغّال ✅' if s['enabled'] else 'متوقف ⛔'}")
    kb = [
        [btn("➕ إضافة رصيد", "a:add"), btn("➖ خصم رصيد", "a:take")],
        [btn("👤 معلومات مستخدم", "a:info"), btn("🚫 حظر/فك حظر", "a:ban")],
        [btn("📈 معامل التسعير", "a:mult"), btn("🔻 أقل سعر", "a:min")],
        [btn("🏷 سعر ثابت لخدمة/دولة", "a:ovr"), btn("💱 رمز العملة", "a:cur")],
        [btn("💳 كليشة الشحن", "a:rtext"), btn("📢 إذاعة", "a:bc")],
        [btn("⛔ إيقاف/تشغيل البوت", "a:toggle")],
        [btn("🏠 الرئيسية", "home")],
    ]
    return text, kb


ADMIN_PROMPTS = {
    "add": "أرسل: <code>الآيدي المبلغ</code>\nمثال: <code>123456789 5</code>",
    "take": "أرسل: <code>الآيدي المبلغ</code> للخصم",
    "info": "أرسل آيدي المستخدم",
    "ban": "أرسل آيدي المستخدم (يتبدل بين حظر وفك حظر)",
    "mult": "أرسل المعامل الجديد.\nمثال: إذا سعر المورد 10₽ والمعامل 0.02 يصير سعرك 0.20$",
    "min": "أرسل أقل سعر بيع (مثال 0.15)",
    "ovr": ("أرسل: <code>الخدمة الدولة السعر</code>\nمثال: <code>telegram russia 0.5</code>\n"
            "ولحذف السعر الثابت أرسل: <code>telegram russia -</code>"),
    "cur": "أرسل رمز العملة (مثال $ أو د.ع أو نقطة)",
    "rtext": "أرسل كليشة الشحن الجديدة (اللي تطلع للزبون لما يضغط شحن رصيد)",
    "bc": "أرسل الرسالة اللي تريد تذيعها لكل المستخدمين",
}


def handle_admin_input(uid, chat, action, text):
    s = S()
    parts = text.split()
    try:
        if action in ("add", "take"):
            target, amount = parts[0], float(parts[1])
            if not target.isdigit():
                raise ValueError
            if target not in DB["users"]:
                user(target)
            new = add_balance(target, amount if action == "add" else -amount)
            send(chat, f"✅ تم. رصيد <code>{target}</code> صار {money(new)}")
            verb = "انضاف" if action == "add" else "انخصم"
            send(int(target), f"💰 {verb} {money(amount)} {'لـ' if action == 'add' else 'من'} رصيدك.\nرصيدك الحالي: {money(new)}")
        elif action == "info":
            u = DB["users"].get(parts[0])
            if not u:
                return send(chat, "❌ هذا المستخدم ما موجود.")
            send(chat, f"👤 <code>{parts[0]}</code> {esc(u.get('name', ''))}\n"
                       f"💰 الرصيد: {money(u['balance'])}\n💵 صرف: {money(u['spent'])}\n"
                       f"📦 الطلبات: {u['orders']}\n🚫 محظور: {'نعم' if u.get('banned') else 'لا'}")
        elif action == "ban":
            u = user(parts[0])
            with _lock:
                u["banned"] = not u.get("banned")
                save()
            send(chat, f"{'🚫 انحظر' if u['banned'] else '✅ انفك حظر'} <code>{parts[0]}</code>")
        elif action == "mult":
            s["multiplier"] = float(parts[0]); save()
            _price_cache.clear()
            send(chat, f"✅ المعامل صار × {s['multiplier']}")
        elif action == "min":
            s["min_price"] = float(parts[0]); save()
            send(chat, f"✅ أقل سعر صار {money(s['min_price'])}")
        elif action == "ovr":
            key = f"{parts[0].lower()}:{parts[1].lower()}"
            if parts[2] == "-":
                s["overrides"].pop(key, None)
            else:
                s["overrides"][key] = float(parts[2])
            save()
            send(chat, f"✅ تم تحديث سعر {key}")
        elif action == "cur":
            s["currency"] = text.strip()[:6]; save()
            send(chat, f"✅ العملة صارت {esc(s['currency'])}")
        elif action == "rtext":
            s["recharge_text"] = text; save()
            send(chat, "✅ تم تحديث كليشة الشحن")
        elif action == "bc":
            ids = list(DB["users"].keys())
            send(chat, f"📢 جاري الإرسال لـ {len(ids)} مستخدم...")

            def run():
                ok = 0
                for i in ids:
                    if send(int(i), text).get("ok"):
                        ok += 1
                    time.sleep(0.05)
                send(chat, f"✅ انرسلت لـ {ok} من {len(ids)}")
            threading.Thread(target=run, daemon=True).start()
    except (IndexError, ValueError):
        send(chat, "❌ الصيغة غلط.\n" + ADMIN_PROMPTS[action])


# ───────────────────────── معالجة التحديثات ─────────────────────────
def on_message(msg):
    chat = msg["chat"]["id"]
    if msg["chat"].get("type") != "private":
        return
    frm = msg.get("from", {})
    uid = frm.get("id")
    name = (frm.get("first_name", "") + " " + frm.get("last_name", "")).strip()
    u = user(uid, name)
    text = msg.get("text", "") or ""

    if int(uid) in ADMIN_IDS and uid in pending_input and not text.startswith("/"):
        action = pending_input.pop(uid)
        return handle_admin_input(uid, chat, action, text)
    pending_input.pop(uid, None)

    if u.get("banned"):
        return send(chat, "🚫 حسابك محظور.")

    if text.startswith("/admin") and int(uid) in ADMIN_IDS:
        return send(chat, *admin_menu())
    if int(uid) in ADMIN_IDS and text.startswith(("/add ", "/take ")):
        cmd, rest = text.split(" ", 1)
        return handle_admin_input(uid, chat, cmd[1:], rest)
    send(chat, *main_menu(uid))


def on_callback(cb):
    uid = cb["from"]["id"]
    chat = cb["message"]["chat"]["id"]
    mid = cb["message"]["message_id"]
    data = cb.get("data", "")
    u = user(uid)
    is_admin = int(uid) in ADMIN_IDS

    if u.get("banned"):
        return answer(cb["id"], "🚫 حسابك محظور", True)

    try:
        if data == "home":
            answer(cb["id"]); edit(chat, mid, *main_menu(uid))
        elif data == "buy":
            answer(cb["id"]); edit(chat, mid, *services_menu())
        elif data.startswith("svc:"):
            _, product, *page = data.split(":")
            answer(cb["id"], "⏳ جاري جلب الأسعار...")
            edit(chat, mid, *countries_menu(product, int(page[0]) if page else 0))
        elif data.startswith("cty:"):
            _, product, country = data.split(":")
            answer(cb["id"]); edit(chat, mid, *confirm_menu(uid, product, country))
        elif data.startswith("ok:"):
            _, product, country = data.split(":")
            answer(cb["id"], "⏳ جاري الشراء...")
            try:
                o = buy(uid, product, country)
            except ProviderError as e:
                return edit(chat, mid, f"❌ {e}", [[btn("🔙 رجوع", f"svc:{product}"), btn("🏠 الرئيسية", "home")]])
            o["msg"] = mid
            edit(chat, mid, order_text(o), order_kb(o))
        elif data.startswith("ord:"):
            o = DB["orders"].get(data[4:])
            if not o or o["uid"] != str(uid):
                return answer(cb["id"], "الطلب ما موجود", True)
            sync_order(o)
            answer(cb["id"], "تم التحديث")
            edit(chat, mid, order_text(o), order_kb(o))
        elif data.startswith("cnl:"):
            o = DB["orders"].get(data[4:])
            if not o or o["uid"] != str(uid):
                return answer(cb["id"], "الطلب ما موجود", True)
            sync_order(o)
            if o["status"] != "wait":
                answer(cb["id"], "ما يمكن الإلغاء، الكود وصل أو الطلب منتهي.", True)
                return edit(chat, mid, order_text(o), order_kb(o))
            try:
                fivesim(f"/user/cancel/{o['id']}")
            except ProviderError as e:
                return answer(cb["id"], f"تعذّر الإلغاء: {e}", True)
            refund(o, "cancel")
            answer(cb["id"], f"✅ انلغى ورجعلك {money(o['price'])}", True)
            edit(chat, mid, order_text(o), order_kb(o))
        elif data.startswith("fin:"):
            o = DB["orders"].get(data[4:])
            if o and o["uid"] == str(uid) and o["status"] == "got":
                try:
                    fivesim(f"/user/finish/{o['id']}")
                except ProviderError:
                    pass
                with _lock:
                    o["status"] = "done"; save()
                edit(chat, mid, order_text(o), order_kb(o))
            answer(cb["id"], "✔️ تم")
        elif data == "bal":
            answer(cb["id"], f"💰 رصيدك: {money(u['balance'])}\n💵 مجموع صرفك: {money(u['spent'])}", True)
        elif data == "recharge":
            answer(cb["id"])
            edit(chat, mid, f"{S()['recharge_text']}\n\n🆔 الآيدي مالتك: <code>{uid}</code>",
                 [[btn("🔙 رجوع", "home")]])
        elif data == "myorders":
            answer(cb["id"])
            mine = sorted((o for o in DB["orders"].values() if o["uid"] == str(uid)),
                          key=lambda o: -o["created"])[:10]
            if not mine:
                return edit(chat, mid, "🧾 ما عندك طلبات بعد.", [[btn("🔙 رجوع", "home")]])
            icons = {"wait": "⏳", "got": "✅", "done": "✔️", "cancel": "❌", "timeout": "⌛"}
            kb = [[btn(f"{icons.get(o['status'], '')} {o['phone']} • {service_name(o['product'])}", f"ord:{o['id']}")]
                  for o in mine]
            kb.append([btn("🔙 رجوع", "home")])
            edit(chat, mid, "🧾 آخر طلباتك:", kb)
        elif data == "admin" and is_admin:
            answer(cb["id"]); edit(chat, mid, *admin_menu())
        elif data.startswith("a:") and is_admin:
            action = data[2:]
            if action == "toggle":
                S()["enabled"] = not S()["enabled"]; save()
                answer(cb["id"], "تم")
                return edit(chat, mid, *admin_menu())
            pending_input[uid] = action
            answer(cb["id"])
            send(chat, ADMIN_PROMPTS[action] + "\n\n(أرسل /cancel للإلغاء)")
        else:
            answer(cb["id"])
    except ProviderError as e:
        answer(cb["id"], str(e), True)


def handle_update(upd):
    try:
        if "message" in upd:
            on_message(upd["message"])
        elif "callback_query" in upd:
            on_callback(upd["callback_query"])
    except Exception:
        traceback.print_exc()


# ───────────────────────── التشغيل ─────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "numbers bot is running"


@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    upd = request.get_json(silent=True) or {}
    threading.Thread(target=handle_update, args=(upd,), daemon=True).start()
    return "ok"


def polling_loop():
    tg("deleteWebhook")
    offset = 0
    while True:
        try:
            r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset, "timeout": 50}, timeout=60).json()
            for upd in r.get("result", []):
                offset = upd["update_id"] + 1
                threading.Thread(target=handle_update, args=(upd,), daemon=True).start()
        except Exception as e:
            print(f"[polling] {e}")
            time.sleep(3)


_started = False


def start_background():
    global _started
    if _started:
        return
    _started = True
    if not TOKEN:
        print("⚠️ BOT_TOKEN مو مضبوط")
        return
    threading.Thread(target=poller, daemon=True).start()
    tg("setMyCommands", commands=[{"command": "start", "description": "القائمة الرئيسية"}])
    if WEBHOOK_URL:
        tg("setWebhook", url=f"{WEBHOOK_URL}/webhook/{TOKEN}", drop_pending_updates=False)
        print("✅ webhook mode")
    else:
        threading.Thread(target=polling_loop, daemon=True).start()
        print("✅ polling mode")


start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
