"""The hunt loop: N worker threads pulling candidates from a shared generator."""
import threading
import time

from . import checker
from .generator import make_stream


class Hunter:
    def __init__(self, store, on_found, on_status, claimer_getter):
        self.store = store
        self.on_found = on_found        # (username, status, source, claim_result)
        self.on_status = on_status      # (text) periodic status updates
        self.get_claimer = claimer_getter
        self.running = False
        self.stats = self._new_stats()
        self._gen = None
        self._gen_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._threads = []
        self.seen = set()
        self.last_checked = ""

    @staticmethod
    def _new_stats():
        return {"checked": 0, "taken": 0, "available": 0, "fragment": 0,
                "errors": 0, "claimed": 0, "started": time.time(), "finished": None}

    # -- control --------------------------------------------------------
    def start(self):
        if self.running:
            return False
        self.stats = self._new_stats()
        self._gen = make_stream(self.store.settings, self.seen)
        self.running = True
        n = max(1, min(int(self.store.settings.get("threads", 5)), 50))
        self._threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(n)]
        for t in self._threads:
            t.start()
        threading.Thread(target=self._reporter, daemon=True).start()
        threading.Thread(target=self._joiner, daemon=True).start()
        return True

    def stop(self):
        self.running = False

    def _joiner(self):
        for t in self._threads:
            t.join()
        self.running = False
        self.stats["finished"] = time.time()
        self.on_status(self.summary(final=True))

    # -- work -----------------------------------------------------------
    def _next(self):
        with self._gen_lock:
            max_checks = int(self.store.settings.get("max_checks", 0) or 0)
            if max_checks and self.stats["checked"] >= max_checks:
                return None
            try:
                return next(self._gen)
            except StopIteration:
                return None

    def _bump(self, key):
        with self._stats_lock:
            self.stats[key] += 1

    def _worker(self):
        errors_in_row = 0
        while self.running:
            name = self._next()
            if name is None:
                break
            claimer = self.get_claimer()
            status, source = checker.full_check(name, claimer)
            self.last_checked = name
            self._bump("checked")

            if status == checker.UNKNOWN:
                self._bump("errors")
                errors_in_row += 1
                # back off when t.me starts rate limiting us
                time.sleep(min(60, 2 ** min(errors_in_row, 6)))
                continue
            errors_in_row = 0

            if status == checker.TAKEN or status == checker.INVALID:
                self._bump("taken")
            elif status == checker.FRAGMENT:
                self._bump("fragment")
                if self.store.settings.get("notify_fragment"):
                    self.store.add_found(name, status, source)
                    self.on_found(name, status, source, None)
            elif status == checker.AVAILABLE:
                self._bump("available")
                self._handle_available(name, source, claimer)

            delay = float(self.store.settings.get("delay", 0.5) or 0)
            if delay:
                time.sleep(delay)

    def _handle_available(self, name, source, claimer):
        mode = self.store.settings.get("claim_mode", "off")
        claim_result = None
        if mode != "off" and claimer is not None and claimer.ready:
            ok, msg = claimer.claim(name, mode)
            claim_result = (ok, msg)
            if ok:
                self._bump("claimed")
                if mode == "account":  # an account holds a single username
                    self.store.set("claim_mode", "off")
        self.store.add_found(name, checker.AVAILABLE, source,
                             claimed=claim_result[0] if claim_result else None)
        self.on_found(name, checker.AVAILABLE, source, claim_result)

    # -- reporting ------------------------------------------------------
    def _reporter(self):
        while self.running:
            for _ in range(20):
                if not self.running:
                    return
                time.sleep(1)
            self.on_status(self.summary())

    def summary(self, final=False):
        s = self.stats
        end = s["finished"] or time.time()
        elapsed = max(1, end - s["started"])
        rate = s["checked"] / elapsed * 60
        head = "🏁 <b>انتهى الصيد</b>" if final else "🎯 <b>الصيد شغّال...</b>"
        mins, secs = divmod(int(elapsed), 60)
        hrs, mins = divmod(mins, 60)
        return (f"{head}\n\n"
                f"🔎 المفحوص: <b>{s['checked']}</b>\n"
                f"✅ متاح: <b>{s['available']}</b>\n"
                f"🔒 تم التثبيت: <b>{s['claimed']}</b>\n"
                f"💎 فراجمنت: <b>{s['fragment']}</b>\n"
                f"❌ محجوز: <b>{s['taken']}</b>\n"
                f"⚠️ أخطاء: <b>{s['errors']}</b>\n"
                f"⚡ السرعة: <b>{rate:.0f}</b> فحص/دقيقة\n"
                f"⏳ المدة: {hrs:02d}:{mins:02d}:{secs:02d}\n"
                f"🕐 آخر يوزر: <code>{self.last_checked or '-'}</code>")
