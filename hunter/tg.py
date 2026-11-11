"""Minimal Telegram Bot API client."""
import json

import requests


class BotAPI:
    def __init__(self, token):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.s = requests.Session()

    def call(self, method, timeout=30, **params):
        try:
            r = self.s.post(f"{self.base}/{method}", json=params, timeout=timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"[tg] {method} error: {e}")
            return None
        if not data.get("ok"):
            desc = data.get("description", "")
            if "message is not modified" not in desc:
                print(f"[tg] {method} failed: {desc}")
            return None
        return data.get("result")

    def send(self, chat_id, text, markup=None):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        return self.call("sendMessage", **p)

    def edit(self, chat_id, message_id, text, markup=None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": text,
             "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        return self.call("editMessageText", **p)

    def answer(self, callback_id, text="", alert=False):
        return self.call("answerCallbackQuery", callback_query_id=callback_id,
                         text=text, show_alert=alert)

    def send_document(self, chat_id, filename, content: bytes, caption=""):
        try:
            r = self.s.post(f"{self.base}/sendDocument",
                            data={"chat_id": chat_id, "caption": caption},
                            files={"document": (filename, content)}, timeout=60)
            return r.json().get("result")
        except (requests.RequestException, ValueError) as e:
            print(f"[tg] sendDocument error: {e}")
            return None

    def download_text(self, file_id, limit=5_000_000):
        info = self.call("getFile", file_id=file_id)
        if not info or info.get("file_size", 0) > limit:
            return None
        url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        try:
            return self.s.get(url, timeout=60).content.decode("utf-8", "ignore")
        except requests.RequestException:
            return None

    def get_updates(self, offset, timeout=50):
        try:
            resp = self.s.get(f"{self.base}/getUpdates",
                              params={"offset": offset, "timeout": timeout,
                                      "allowed_updates": json.dumps(["message", "callback_query"])},
                              timeout=timeout + 10)
            return resp.json().get("result", [])
        except (requests.RequestException, ValueError):
            return []
