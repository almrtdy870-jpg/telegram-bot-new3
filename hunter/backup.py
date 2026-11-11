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
        self._lock = threading.Lock()

    # -- restore --------------------------------------------------------
    def restore(self) -> bool:
        """Load the pinned backup. Return True when data was restored."""
        if not self.chat_id:
            return False
        chat = self.bot.call("getChat", chat_id=self.chat_id)
        pinned = (chat or {}).get("pinned_message") or {}
        doc = pinned.get("document") or {}
        if doc.get("file_name") != FILENAME:
            return False
        self.message_id = pinned.get("message_id")
        raw = self.bot.download_text(doc["file_id"], limit=20_000_000)
        if not raw:
            return False
        try:
            data = json.loads(raw)
        except ValueError:
            self.last_error = "ملف النسخة الاحتياطية تالف"
            return False
        self.message_id = data.get("_backup_msg") or self.message_id
        restored = self.store.load_backup(data)
        self.last_backup = time.time()
        return restored

    # -- backup ---------------------------------------------------------
    def backup(self, force=False) -> bool:
        if not self.chat_id:
            return False
        with self._lock:
            if not force and not self.store.dirty:
                return True
            self.store.data["_backup_msg"] = self.message_id
            content = self.store.dump()
            self.store.dirty = False
            ok = False
            if self.message_id:
                ok = self.bot.edit_document(self.chat_id, self.message_id, FILENAME,
                                            content, CAPTION) is not None
            if not ok:  # first backup, or the old message was deleted
                msg = self.bot.send_document(self.chat_id, FILENAME, content, CAPTION, silent=True)
                if msg and msg.get("message_id"):
                    self.message_id = msg["message_id"]
                    self.bot.call("pinChatMessage", chat_id=self.chat_id,
                                  message_id=self.message_id, disable_notification=True)
                    ok = True
            if ok:
                self.last_backup = time.time()
                self.last_error = ""
            else:
                self.store.dirty = True
                self.last_error = "فشل رفع النسخة الاحتياطية"
            return ok

    def run_forever(self):
        while True:
            time.sleep(30)
            if self.store.dirty and time.time() - self.last_backup >= self.interval:
                try:
                    self.backup()
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
