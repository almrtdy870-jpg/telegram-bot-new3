import os
import time
import threading
from flask import Flask, request
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

app = Flask(__name__)

TOKEN = os.environ.get("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = int(os.environ.get("PORT", 10000))
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

users_db = {}

def get_user(chat_id):
    if chat_id not in users_db:
        users_db[chat_id] = {
            "emails": [],
            "template": "Hello Telegram Support,\nMy group has been restricted. Please review.",
            "subject": "Appeal to lift restriction on Telegram group",
            "target_email": "recover@telegram.org",
            "delay": 5,     # الفاصل الزمني الافتراضي بالثواني (السليب)
            "count": 200,   # عدد الرفع الافتراضي
            "state": "IDLE",
            "is_running": False
        }
    return users_db[chat_id]

def send_telegram(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Telegram API Error: {e}")

def edit_message(chat_id, message_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Edit Message Error: {e}")

def get_main_menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "【 ايميلاتي 】", "callback_data": "my_emails"}, {"text": "【 اضافة ايميل 】", "callback_data": "add_email"}],
            [{"text": "【 حذف ايميل 】", "callback_data": "del_email"}, {"text": "【 ضبط الكليشة 】", "callback_data": "set_template"}],
            [{"text": "【 تعيين موضوع 】", "callback_data": "set_subject"}, {"text": "【 تعيين ايميل الدعم 】", "callback_data": "set_target"}],
            [{"text": "【 السليب 】", "callback_data": "set_delay"}, {"text": "【 عدد الرفع 】", "callback_data": "set_count"}],
            [{"text": "【 عرض معلوماتي 】", "callback_data": "my_info"}, {"text": "【 ايقاف 】", "callback_data": "stop_report"}],
            [{"text": "【 بدء الرفع 】", "callback_data": "start_report"}, {"text": "【 قناة التحديثات 】", "url": "https://t.me/your_channel"}],
            [{"text": "【 المطور 】", "url": "https://t.me/your_username"}]
        ]
    }

def send_email_report(sender_email, sender_password, recipient_email, subject, body_text):
    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = recipient_email
    msg['Subject'] = subject
    msg.attach(MIMEText(body_text, 'plain'))

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())
        server.quit()
        return True, "Success"
    except Exception as e:
        print(f"SMTP Error: {e}")
        return False, str(e)

def background_sender(chat_id, user):
    user["is_running"] = True
    target_count = user["count"]
    delay_time = user["delay"]
    success_count = 0
    fail_count = 0
    emails = user["emails"]
    
    for i in range(1, target_count + 1):
        if not user.get("is_running", False):
            send_telegram(chat_id, f"🛑 <b>تم إيقاف العملية بنجاح!</b>\n\n• ما تم إرساله قبل الإيقاف: {i-1}\n• الناجح: {success_count}\n• الفاشل: {fail_count}")
            return
            
        item = emails[(i - 1) % len(emails)]
        res, err = send_email_report(item["email"], item["password"], user["target_email"], user["subject"], user["template"])
        
        if res:
            success_count += 1
            send_telegram(chat_id, f"✅ <b>تم الرفع (البلاغ {i}/{target_count})</b>\n📧 الإيميل: <code>{item['email']}</code>\n🎯 إلى: <code>{user['target_email']}</code>")
        else:
            fail_count += 1
            send_telegram(chat_id, f"❌ <b>تم الفشل (البلاغ {i}/{target_count})</b>\n⚠️ السبب: <code>{err}</code>")
            
        for _ in range(delay_time):
            if not user.get("is_running", False):
                send_telegram(chat_id, f"🛑 <b>تم إيقاف العملية بنجاح!</b>\n\n• ما تم إرساله: {i}\n• الناجح: {success_count}\n• الفاشل: {fail_count}")
                return
            time.sleep(1)
        
    user["is_running"] = False
    send_telegram(chat_id, f"🎉 <b>اكتملت حملة البلاغات بالكامل!</b>\n\n• الناجح: {success_count}\n• الفاشل: {fail_count}\n• المجموع: {target_count}")

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()
    if not update:
        return "OK", 200
        
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        message_id = cq["message"]["message_id"]
        data = cq["data"]
        
        if ADMIN_CHAT_ID and str(chat_id) != str(ADMIN_CHAT_ID):
            return "OK", 200
            
        user = get_user(chat_id)
        
        if data == "my_emails":
            emails = user["emails"]
            if not emails:
                text = "📁 لا يوجد أي إيميلات مضافة حالياً."
            else:
                text = "📧 <b>الإيميلات المضافة:</b>\n\n"
                for i, e in enumerate(emails, 1):
                    text += f"{i}. <code>{e['email']}</code>\n"
            edit_message(chat_id, message_id, text, get_main_menu_keyboard())
            
        elif data == "add_email":
            user["state"] = "WAITING_EMAILS"
            edit_message(chat_id, message_id, "📥 أرسل الإيميل وكلمة السر (كلمة مرور التطبيق) بالشكل التالي:\n<code>email@gmail.com:password</code>", get_main_menu_keyboard())
            
        elif data == "set_template":
            user["state"] = "WAITING_TEMPLATE"
            edit_message(chat_id, message_id, "✍️ أرسل الآن الكليشة الجديدة مع الرابط والآيدي:", get_main_menu_keyboard())
            
        elif data == "set_subject":
            user["state"] = "WAITING_SUBJECT"
            edit_message(chat_id, message_id, "📝 أرسل الآن موضوع (عنوان) الرسالة الجديد:", get_main_menu_keyboard())
            
        elif data == "set_target":
            user["state"] = "WAITING_TARGET"
            edit_message(chat_id, message_id, "🎯 أرسل الآن إيميل الدعم المستهدف (الافتراضي recover@telegram.org):", get_main_menu_keyboard())
            
        elif data == "set_delay":
            user["state"] = "WAITING_DELAY"
            edit_message(chat_id, message_id, "⏱️ أرسل الآن الفاصل الزمني (السليب) بالثواني بين كل بلاغ (مثال: 5):", get_main_menu_keyboard())
            
        elif data == "set_count":
            user["state"] = "WAITING_COUNT"
            edit_message(chat_id, message_id, "🔢 أرسل الآن عدد البلاغات المطلوب إرسالها (مثال: 200):", get_main_menu_keyboard())
            
        elif data == "my_info":
            text = (f"📊 <b>معلوماتك الحالية:</b>\n\n"
                    f"• عدد الإيميلات: {len(user['emails'])}\n"
                    f"• إيميل الدعم المستهدف: <code>{user['target_email']}</code>\n"
                    f"• موضوع الرسالة: {user['subject']}\n"
                    f"• الفاصل الزمني (السليب): {user['delay']} ثوانٍ\n"
                    f"• عدد البلاغات: {user['count']}\n"
                    f"• الكليشة:\n<code>{user['template']}</code>")
            edit_message(chat_id, message_id, text, get_main_menu_keyboard())
            
        elif data == "del_email":
            user["emails"] = []
            edit_message(chat_id, message_id, "🗑️ تم حذف جميع الإيميلات بنجاح.", get_main_menu_keyboard())
            
        elif data == "start_report":
            if not user["emails"]:
                edit_message(chat_id, message_id, "⚠️ لا توجد إيميلات مضافة! أضف إيميل أولاً.", get_main_menu_keyboard())
                return "OK", 200
                
            if user.get("is_running", False):
                edit_message(chat_id, message_id, "⚠️ العملية تعمل بالفعل حالياً!", get_main_menu_keyboard())
                return "OK", 200
                
            edit_message(chat_id, message_id, f"🚀 <b>جاري بدء إرسال {user['count']} بلاغ...</b>\nستحصل على إشعار فوري لكل بلاغ، ويمكنك إيقافه في أي وقت بالضغط على زر **【 ايقاف 】**.", get_main_menu_keyboard())
            
            threading.Thread(target=background_sender, args=(chat_id, user)).start()
            
        elif data == "stop_report":
            if user.get("is_running", False):
                user["is_running"] = False
                edit_message(chat_id, message_id, "🛑 <b>تم إرسال أمر الإيقاف!</b>\nسيتم إيقاف البوت خلال ثوانٍ...", get_main_menu_keyboard())
            else:
                edit_message(chat_id, message_id, "ℹ️ لا توجد أي عملية إرسال تعمل حالياً.", get_main_menu_keyboard())
            
        return "OK", 200

    if "message" in update:
        message = update["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "").strip()
        
        if ADMIN_CHAT_ID and str(chat_id) != str(ADMIN_CHAT_ID):
            return "OK", 200
            
        user = get_user(chat_id)
        
        if text.startswith("/start"):
            user["state"] = "IDLE"
            send_telegram(chat_id, "🔥 أهلاً بك في بوت الرفع الخارجي المطور\nاختر من الأزرار أدناه للتحكم الكامل:", get_main_menu_keyboard())
            
        elif "@" in text and ":" in text and len(text.split(":")) == 2 and user["state"] == "IDLE":
            parts = text.split(":", 1)
            email = parts[0].strip()
            password = parts[1].strip()
            user["emails"].append({"email": email, "password": password})
            send_telegram(chat_id, f"✅ تم حفظ الإيميل بنجاح!\n📧 <code>{email}</code>\n\nاضغط الآن على زر **【 بدء الرفع 】**.", get_main_menu_keyboard())
            
        elif user["state"] == "WAITING_EMAILS":
            parts = text.split(":", 1)
            if len(parts) == 2:
                user["emails"].append({"email": parts[0].strip(), "password": parts[1].strip()})
                user["state"] = "IDLE"
                send_telegram(chat_id, f"✅ تم حفظ الإيميل بنجاح!", get_main_menu_keyboard())
            else:
                send_telegram(chat_id, "⚠️ الصيغة غير صحيحة. أرسل هكذا: email@gmail.com:password")
            
        elif user["state"] == "WAITING_TEMPLATE":
            user["template"] = text
            user["state"] = "IDLE"
            send_telegram(chat_id, f"✅ تم حفظ الكليشة الجديدة بنجاح!", get_main_menu_keyboard())
            
        elif user["state"] == "WAITING_SUBJECT":
            user["subject"] = text
            user["state"] = "IDLE"
            send_telegram(chat_id, f"✅ تم تحديث موضوع الرسالة إلى:\n<code>{text}</code>", get_main_menu_keyboard())
            
        elif user["state"] == "WAITING_TARGET":
            user["target_email"] = text
            user["state"] = "IDLE"
            send_telegram(chat_id, f"✅ تم تحديث إيميل الدعم المستهدف إلى:\n<code>{text}</code>", get_main_menu_keyboard())
            
        elif user["state"] == "WAITING_DELAY":
            if text.isdigit():
                user["delay"] = int(text)
                user["state"] = "IDLE"
                send_telegram(chat_id, f"✅ تم تحديث الفاصل الزمني (السليب) إلى: {text} ثوانٍ", get_main_menu_keyboard())
            else:
                send_telegram(chat_id, "⚠️ يرجى إرسال رقم صحيح يمثل الثواني:")
                
        elif user["state"] == "WAITING_COUNT":
            if text.isdigit():
                user["count"] = int(text)
                user["state"] = "IDLE"
                send_telegram(chat_id, f"✅ تم تحديث عدد الرفع إلى: {text} بلاغ", get_main_menu_keyboard())
            else:
                send_telegram(chat_id, "⚠️ يرجى إرسال رقم صحيح يمثل عدد البلاغات:")
            
        else:
            send_telegram(chat_id, "أهلاً بك! استخدم الأزرار أدناه للتحكم:", get_main_menu_keyboard())
            
    return "OK", 200

@app.route("/")
def index():
    return "Bot is active!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
