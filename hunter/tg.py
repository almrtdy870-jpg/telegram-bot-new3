"""Minimal Telegram Bot API client."""
import json
import re

import requests


class BotAPI:
    def __init__(self, token):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self.s = requests.Session()
        self.last_error = ""  # "<method>: <Telegram's reason>" of the last failed call
        # premium (custom) emoji: callable returning {plain emoji: custom_emoji_id}.
        # Bots may send them when the bot's owner has Telegram Premium.
        self.emoji_source = None
        self._emoji_cache = (None, None)

    # -- premium emoji ---------------------------------------------------
    _TAG = re.compile(r"(<[^>]+>)")
    _SKIP_TAGS = ("code", "pre", "a", "tg-emoji")
    MAX_EMOJI = 90  # Telegram caps entities per message

    def _emoji_re(self, mapping):
        key = tuple(sorted(mapping))
        if self._emoji_cache[0] != key:
            alts = sorted(key, key=len, reverse=True)
            rx = re.compile("(" + "|".join(re.escape(a) for a in alts) + ")\ufe0f?") if alts else None
            self._emoji_cache = (key, rx)
        return self._emoji_cache[1]

    def premium(self, text):
        """Wrap plain emoji that have a premium twin in <tg-emoji> (HTML)."""
        try:
            mapping = self.emoji_source() if self.emoji_source else None
        except Exception:  # noqa: BLE001
            mapping = None
        if not mapping or not text:
            return text
        rx = self._emoji_re(mapping)
        if rx is None:
            return text
        out, skip, used = [], 0, 0
        for part in self._TAG.split(text):
            if part.startswith("<") and part.endswith(">"):
                name = part.strip("</>").split()[0].lower() if part.strip("</>") else ""
                if name in self._SKIP_TAGS:
                    skip += -1 if part.startswith("</") else 1
                    skip = max(0, skip)
                out.append(part)
                continue
            if skip or used >= self.MAX_EMOJI:
                out.append(part)
                continue

            def sub(m):
                nonlocal used
                if used >= self.MAX_EMOJI:
                    return m.group(0)
                eid = mapping.get(m.group(1)) or mapping.get(m.group(1).rstrip("\ufe0f"))
                if not eid:
                    return m.group(0)
                used += 1
                return f'<tg-emoji emoji-id="{eid}">{m.group(0)}</tg-emoji>'
            out.append(rx.sub(sub, part))
        return "".join(out)

    def _send_text(self, method, p):
        fancy = self.premium(p["text"])
        if fancy != p["text"]:
            res = self.call(method, **{**p, "text": fancy})
            if res is not None:
                return res
            # e.g. the owner's Premium ran out: send it with plain emoji
        return self.call(method, **p)

    def _fail(self, method, reason):
        self.last_error = f"{method}: {reason}"[:300].replace(self.token, "***")
        print(f"[tg] {self.last_error}")

    def call(self, method, timeout=30, **params):
        try:
            r = self.s.post(f"{self.base}/{method}", json=params, timeout=timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            self._fail(method, type(e).__name__)
            return None
        if not data.get("ok"):
            desc = data.get("description", "")
            if "message is not modified" not in desc:
                self._fail(method, desc)
            return None
        return data.get("result")

    def send(self, chat_id, text, markup=None):
        p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        return self._send_text("sendMessage", p)

    def edit(self, chat_id, message_id, text, markup=None):
        p = {"chat_id": chat_id, "message_id": message_id, "text": text,
             "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        return self._send_text("editMessageText", p)

    def answer(self, callback_id, text="", alert=False):
        return self.call("answerCallbackQuery", callback_query_id=callback_id,
                         text=text, show_alert=alert)

    def send_document(self, chat_id, filename, content: bytes, caption="", silent=False):
        try:
            r = self.s.post(f"{self.base}/sendDocument",
                            data={"chat_id": chat_id, "caption": caption,
                                  "disable_notification": "true" if silent else "false"},
                            files={"document": (filename, content)}, timeout=60)
            data = r.json()
            if not data.get("ok"):
                self._fail("sendDocument", data.get("description", "?"))
                return None
            return data.get("result")
        except (requests.RequestException, ValueError) as e:
            self._fail("sendDocument", type(e).__name__)
            return None

    def edit_document(self, chat_id, message_id, filename, content: bytes, caption=""):
        """Replace the file of an existing document message (no new message)."""
        try:
            media = {"type": "document", "media": "attach://file", "caption": caption}
            r = self.s.post(f"{self.base}/editMessageMedia",
                            data={"chat_id": chat_id, "message_id": message_id,
                                  "media": json.dumps(media)},
                            files={"file": (filename, content)}, timeout=60)
            data = r.json()
            if not data.get("ok"):
                self._fail("editMessageMedia", data.get("description", "?"))
                return None
            return data.get("result")
        except (requests.RequestException, ValueError) as e:
            self._fail("editMessageMedia", type(e).__name__)
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
