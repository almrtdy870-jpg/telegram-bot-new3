"""Keep the bot's data alive across Render deploys/restarts using Telegram.

The data file is uploaded as ONE pinned document in the owner's chat with the
bot. Later backups replace that document in place (editMessageMedia), so the
chat doesn't fill up. On start-up the pinned document is downloaded and
restored when it is newer than the local file.
"""
import json
import threading
import time

FILENAME = "hunter_backup.json"
CAPTION = "💾 نسخة احتياطية تلقائية للبوت - لا تحذفها ولا تلغي تثبيتها"


class TelegramBackup:
    def __init__(self, bot, store, chat_id, interval=180):
        self.bot = bot
        self.store = store
        self.chat_id = chat_id
        self.interval = interval
        self.message_id = None
        self.last_backup = 0.0
        self.last_error = ""
        # True while a backup exists but could not be read: writing a new
        # (empty) backup now would destroy the good one
        self.blocked = False
        self.rescued = False  # newer data was recovered from a non-pinned message
        self.note = ""        # what the last restore found
        self.edit_error = ""  # why replacing the pinned file failed last time
        self.edits_ok = self.edits_failed = self.new_docs = 0
        self._lock = threading.Lock()

    # -- restore --------------------------------------------------------
    def restore_once(self) -> str:
        """One attempt. Returns 'restored', 'current' (backup not newer),
        'none' (no backup exists yet) or 'error' (exists but unreadable)."""
        if not self.chat_id:
            return "none"
        chat = self.bot.call("getChat", chat_id=self.chat_id)
        if chat is None:
            self.last_error = "ما گدرت أتصل بتليجرام (getChat)"
            return "error"
        pinned = chat.get("pinned_message") or {}
        doc = pinned.get("document") or {}
        if doc.get("file_name") != FILENAME:
            self.note = ("ماكو رسالة مثبّتة" if not pinned else
                         f"المثبّت مو ملف النسخة ({doc.get('file_name') or 'رسالة عادية'})")
            return "none"
        self.message_id = pinned.get("message_id")
        self.note = f"لگيت الملف المثبّت (رسالة {self.message_id})"
        raw = self.bot.download_text(doc["file_id"], limit=20_000_000)
        if not raw:
            self.last_error = "ما گدرت أنزّل ملف النسخة الاحتياطية"
            return "error"
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "ملف النسخة الاحتياطية تالف"
            return "error"
        # Always keep writing to the PINNED message: that is the one read on
        # start-up. (Older versions went back to a message id stored inside
        # the file, so saves and reads could end up on different messages.)
        data = self._rescue_newer(data, pinned.get("message_id"))
        data.pop("_backup_msg", None)
        restored = self.store.load_backup(data)
        self.last_backup = time.time()
        return "restored" if restored else "current"

    def _rescue_newer(self, data, pinned_id):
        """One-time repair for the old bug: if the file points at another
        message that holds NEWER data, use that data instead."""
        other = data.get("_backup_msg")
        if not other or other == pinned_id:
            return data
        fwd = self.bot.call("forwardMessage", chat_id=self.chat_id, from_chat_id=self.chat_id,
                            message_id=other, disable_notification=True)
        if not fwd:
            return data
        try:
            doc = fwd.get("document") or {}
            if doc.get("file_name") == FILENAME:
                raw = self.bot.download_text(doc["file_id"], limit=20_000_000)
                newer = json.loads(raw or "")
                if newer.get("saved_at", 0) > data.get("saved_at", 0):
                    data = newer
                    self.rescued = True
        except (ValueError, TypeError):
            pass
        finally:
            if fwd.get("message_id"):
                self.bot.call("deleteMessage", chat_id=self.chat_id, message_id=fwd["message_id"])
        return data

    def restore(self, attempts=6, wait=10) -> str:
        """Restore with retries. While the result is 'error', backups stay
        blocked so an unread backup is never overwritten by empty data."""
        status = "error"
        for i in range(attempts):
            status = self.restore_once()
            if status != "error":
                break
            if i < attempts - 1:
                time.sleep(wait)
        self.blocked = status == "error"
        return status

    def verify(self):
        """Read the pinned backup back from Telegram and compare it with the
        live data. Returns (ok, list_of_report_lines)."""
        lines = [f"• المحادثة: <code>{self.chat_id}</code>",
                 f"• عند التشغيل: {self.note or '—'}",
                 f"• تعديل الملف: نجح {self.edits_ok} / فشل {self.edits_failed} • ملفات جديدة: {self.new_docs}"]
        if self.edit_error:
            lines.append(f"• سبب فشل التعديل: <code>{self.edit_error}</code>")
        if not self.chat_id:
            return False, lines + ["❌ ماكو محادثة للنسخ (حط ADMIN_IDS برندر)"]
        chat = self.bot.call("getChat", chat_id=self.chat_id)
        if chat is None:
            return False, lines + ["❌ getChat فشل: البوت ما گدر يقرا المحادثة"]
        pinned = chat.get("pinned_message")
        if not pinned:
            return False, lines + ["❌ ماكو رسالة مثبّتة بالمحادثة (التثبيت انلغى؟)"]
        doc = pinned.get("document") or {}
        lines.append(f"• المثبّت: رسالة {pinned.get('message_id')} — {doc.get('file_name', 'مو ملف')}")
        if doc.get("file_name") != FILENAME:
            return False, lines + ["❌ الرسالة المثبّتة مو ملف النسخة. ألغِ تثبيت غيرها وخلي بس ملف النسخة."]
        raw = self.bot.download_text(doc["file_id"], limit=20_000_000)
        if not raw:
            return False, lines + ["❌ ما گدرت أنزّل الملف المثبّت"]
        try:
            data = json.loads(raw)
        except ValueError:
            return False, lines + ["❌ الملف المثبّت تالف"]
        st = data.get("settings", {})
        mine = self.store.data
        lines.append(f"• بالملف: {len(st.get('replace_rules', []))} صيغة، "
                     f"{len(data.get('sessions', []))} جلسة، {len(data.get('found', []))} نتيجة")
        lines.append(f"• بالبوت هسه: {len(mine['settings'].get('replace_rules', []))} صيغة، "
                     f"{len(mine.get('sessions', []))} جلسة، {len(mine.get('found', []))} نتيجة")
        same = (len(st.get("replace_rules", [])) == len(mine["settings"].get("replace_rules", []))
                and len(data.get("sessions", [])) == len(mine.get("sessions", [])))
        if not same:
            return False, lines + ["❌ الملف المثبّت قديم وما يطابق البيانات الحالية"]
        return True, lines + ["✅ النسخة سليمة وتطابق البيانات الحالية"]

    # -- backup ---------------------------------------------------------
    def backup(self, force=False) -> bool:
        if not self.chat_id:
            return False
        if self.blocked:
            self.last_error = "النسخ متوقف: النسخة القديمة ما انقرت بعد"
            return False
        with self._lock:
            if not force and not self.store.dirty:
                return True
            self.store.data.pop("_backup_msg", None)
            content = self.store.dump()
            self.store.dirty = False
            ok = False
            if self.message_id:
                ok = self.bot.edit_document(self.chat_id, self.message_id, FILENAME,
                                            content, CAPTION) is not None
                if ok:
                    self.edits_ok += 1
                else:
                    self.edits_failed += 1
                    self.edit_error = getattr(self.bot, "last_error", "")
            if not ok:  # first backup, or the old message was deleted
                old = self.message_id
                msg = self.bot.send_document(self.chat_id, FILENAME, content, CAPTION, silent=True)
                if msg and msg.get("message_id"):
                    if old:  # only one backup may stay pinned
                        self.bot.call("unpinChatMessage", chat_id=self.chat_id, message_id=old)
                    self.message_id = msg["message_id"]
                    self.new_docs += 1
                    self.bot.call("pinChatMessage", chat_id=self.chat_id,
                                  message_id=self.message_id, disable_notification=True)
                    ok = True
            if ok:
                self.last_backup = time.time()
                self.last_error = ""
            else:
                self.store.dirty = True
                self.last_error = "فشل رفع النسخة الاحتياطية: " + getattr(self.bot, "last_error", "")
            return ok

    def run_forever(self):
        while True:
            time.sleep(30)
            if self.store.dirty and time.time() - self.last_backup >= self.interval:
                try:
                    self.backup()
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
