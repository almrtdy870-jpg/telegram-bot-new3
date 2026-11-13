"""Tiny JSON persistence for settings and found usernames."""
import json
import os
import threading
import time

DEFAULT_SETTINGS = {
    "mode": "preset",          # preset | template | words | similar
    "similar_base": "",
    "replace_rules": [],       # [{"base": "vcvcv", "pos": [1], "chars": ["aeo"]}]
    "replace_rev": 0,          # bumped on every rules change (live reload)
    "combo_a": [],             # word combinations: first words
    "combo_b": [],             # ...second words
    "combo_both": False,       # also try second+first
    "combo_claim": False,      # claim free combinations by itself (default: just list them)
    "sources": [],             # selected hunting methods, hunted together (endless)
    "sources_rev": 0,          # bumped when the selection changes (live reload)
    "preset": "five",
    "template": "LLLLL",
    "prefix": "",
    "suffix": "",
    "words": [],
    "word_variants": False,
    "threads": 3,
    "delay": 1.0,              # seconds between checks per thread
    "acc_interval": 0,         # seconds between account checks per account; 0 = auto
    "max_checks": 0,           # 0 = unlimited
    "claim_mode": "channel",   # off | channel | account
    "notify_fragment": False,  # also report names listed on Fragment
    "notify": "claimed",       # all | claimed | off  (hunt notifications in chat)
    "auto_resume": False,      # restart the hunt by itself after a server restart
    "claim_cap_day": 10,       # automatic claims per day (0 = no limit); extra finds are listed
    "auto_digits": "689",      # auto-claim only names whose trailing digits are all in this set ("" = any)
    "report_hour": 21,         # local hour of the daily report (-1 = off)
    "notify_channel": "",      # @channel (or -100... id) that gets a post for every claimed username
}


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"owner": None, "settings": dict(DEFAULT_SETTINGS), "found": []}
        self.dirty = False      # changed since the last Telegram backup
        self.tz_offset = 3      # hours added to UTC for "today" (set by the bot)
        self.file_saved_at = 0  # saved_at of the data file found at start-up
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data.update(loaded)
                self.data["settings"] = {**DEFAULT_SETTINGS, **loaded.get("settings", {})}
                self.file_saved_at = loaded.get("saved_at", 0)
            except (OSError, ValueError):
                pass

    def save(self):
        with self.lock:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            self.data["saved_at"] = time.time()
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self.dirty = True

    def dump(self) -> bytes:
        with self.lock:
            return json.dumps(self.data, ensure_ascii=False).encode()

    def load_backup(self, loaded: dict) -> bool:
        """Replace current data with a backup if the backup is newer."""
        with self.lock:
            # compare with the file we started from: changes made in the few
            # seconds before the restore must not block it
            if loaded.get("saved_at", 0) <= self.file_saved_at:
                return False
            self.data.update(loaded)
            self.data["settings"] = {**DEFAULT_SETTINGS, **loaded.get("settings", {})}
            self.save()
            self.dirty = False
            return True

    # -- per-day counters (checks / claims / asked / errors) ------------
    def today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + self.tz_offset * 3600))

    def day(self) -> dict:
        with self.lock:
            d = self.data.get("daily")
            if not isinstance(d, dict) or d.get("date") != self.today():
                d = {"date": self.today(), "checks": 0, "claims": 0, "asked": 0}
                self.data["daily"] = d
            return d

    def day_inc(self, key, n=1):
        with self.lock:
            d = self.day()
            d[key] = d.get(key, 0) + n

    def force_load(self, loaded: dict):
        """Replace everything with a backup the owner chose explicitly."""
        with self.lock:
            revs = (self.data["settings"].get("replace_rev", 0), self.data["settings"].get("sources_rev", 0))
            self.data.update(loaded)
            self.data["settings"] = {**DEFAULT_SETTINGS, **loaded.get("settings", {})}
            # bump revisions so a running hunt reloads its formulas / methods
            self.data["settings"]["replace_rev"] = max(revs[0], self.data["settings"].get("replace_rev", 0)) + 1
            self.data["settings"]["sources_rev"] = max(revs[1], self.data["settings"].get("sources_rev", 0)) + 1
            self.save()

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
