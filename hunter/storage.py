"""Tiny JSON persistence for settings and found usernames."""
import json
import os
import threading
import time

DEFAULT_SETTINGS = {
    "mode": "preset",          # preset | template | words | similar
    "similar_base": "",
    "replace_rules": [],       # [{"base": "vcvcv", "pos": [1]}]
    "preset": "five",
    "template": "LLLLL",
    "prefix": "",
    "suffix": "",
    "words": [],
    "word_variants": False,
    "threads": 3,
    "delay": 1.0,              # seconds between checks per thread
    "max_checks": 0,           # 0 = unlimited
    "claim_mode": "channel",   # off | channel | account
    "notify_fragment": False,  # also report names listed on Fragment
    "notify": "claimed",       # all | claimed | off  (hunt notifications in chat)
}


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"owner": None, "settings": dict(DEFAULT_SETTINGS), "found": []}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data.update(loaded)
                self.data["settings"] = {**DEFAULT_SETTINGS, **loaded.get("settings", {})}
            except (OSError, ValueError):
                pass

    def save(self):
        with self.lock:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    @property
    def settings(self):
        return self.data["settings"]

    def set(self, key, value):
        with self.lock:
            self.data["settings"][key] = value
            self.save()

    def add_found(self, username, status, source, claimed=None):
        with self.lock:
            self.data["found"].append({
                "username": username, "status": status, "source": source,
                "claimed": claimed, "time": int(time.time()),
            })
            self.save()

    def update_found(self, username, **kw):
        with self.lock:
            for item in reversed(self.data["found"]):
                if item["username"] == username:
                    item.update(kw)
                    break
            self.save()

    def clear_found(self):
        with self.lock:
            self.data["found"] = []
            self.save()
