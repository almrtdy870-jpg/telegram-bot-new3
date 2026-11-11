"""The hunt loop: N worker threads pulling candidates from a shared generator.

When an account is linked but flood-limited, "maybe available" names (seen only
via t.me/fragment) are not reported one by one; they go to a verify queue that a
background thread drains with the account once the flood ends. Confirmed names
that can't be claimed yet go to a claim queue the same way.
"""
import threading
import time

from . import checker
from .claimer import fmt_wait
from .generator import make_stream

MAX_QUEUE = 1000


class Hunter:
    def __init__(self, store, on_found, on_status, claimer_getter):
        self.store = store
        # on_found(username, status, source, claim_result)
        self.on_found = on_found
        self.on_status = on_status      # (text) periodic status updates
        self.get_claimer = claimer_getter
        self.running = False
        self.stats = self._new_stats()
        self._gen = None
        self._gen_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._q_lock = threading.Lock()
        self._threads = []
        self.seen = set()
        self.last_checked = ""
        self.verify_q = []   # need an exact account check
        self.claim_q = []    # confirmed free, waiting for claim flood to end
        threading.Thread(target=self._queue_worker, daemon=True).start()

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

    # -- queues ---------------------------------------------------------
    def queue_claim(self, name):
        with self._q_lock:
            if name not in self.claim_q:
                self.claim_q.append(name)

    def _queue_verify(self, name):
        with self._q_lock:
            if name not in self.verify_q and len(self.verify_q) < MAX_QUEUE:
                self.verify_q.append(name)

    def _pop(self, q):
        with self._q_lock:
            return q.pop(0) if q else None

    def _queue_worker(self):
        while True:
            time.sleep(2)
            c = self.get_claimer()
            if c is None or not c.ready:
                continue
            # 1) claims waiting for the flood to end
            if self.claim_q and not c.claim_flood_left:
                if self.store.settings.get("claim_mode", "off") == "off":
                    pass  # keep them until claiming is switched back on
                else:
                    name = self._pop(self.claim_q)
                    if name:
                        self._try_claim(name, "account (مؤجل)", c, notify_wait=False)
                        continue
            # 2) names that need an exact check
            if self.verify_q and not c.check_flood_left:
                name = self._pop(self.verify_q)
                if not name:
                    continue
                res = c.check(name)
                if res is None:
                    self._queue_verify(name)  # flood hit again, retry later
                elif res == checker.AVAILABLE:
                    self._bump("available")
                    self._confirmed(name, "account (مؤجل)", c)
                elif res == checker.FRAGMENT:
                    self._bump("fragment")
                else:
                    self._bump("taken")

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

            if status in (checker.TAKEN, checker.INVALID):
                self._bump("taken")
            elif status == checker.FRAGMENT:
                self._bump("fragment")
                if self.store.settings.get("notify_fragment"):
                    self.store.add_found(name, status, source)
                    self.on_found(name, status, source, None)
            elif status == checker.AVAILABLE:
                if claimer is not None and claimer.ready and source != "account":
                    # account is flood-limited: verify later instead of spamming
                    self._queue_verify(name)
                else:
                    self._bump("available")
                    self._confirmed(name, source, claimer)

            delay = float(self.store.settings.get("delay", 0.5) or 0)
            if delay:
                time.sleep(delay)

    def _confirmed(self, name, source, claimer):
        mode = self.store.settings.get("claim_mode", "off")
        if mode != "off" and claimer is not None and claimer.ready:
            self._try_claim(name, source, claimer)
        else:
            self.store.add_found(name, checker.AVAILABLE, source)
            self.on_found(name, checker.AVAILABLE, source, None)

    def _try_claim(self, name, source, claimer, notify_wait=True):
        mode = self.store.settings.get("claim_mode", "off")
        if mode == "off":
            mode = "channel"
        ok, msg = claimer.claim(name, mode)
        if not ok and msg.startswith("FLOOD:"):
            first_time = name not in self.claim_q
            self.queue_claim(name)
            if notify_wait and first_time:
                self.store.add_found(name, checker.AVAILABLE, source, claimed=False)
                self.on_found(name, checker.AVAILABLE, source,
                              (False, f"الحساب بفلود ({fmt_wait(claimer.claim_flood_left)}) "
                                      "- بالانتظار وراح ينحجز تلقائياً"))
            return
        if ok:
            self._bump("claimed")
            if mode == "account":  # an account holds a single username
                self.store.set("claim_mode", "off")
        self.store.add_found(name, checker.AVAILABLE, source, claimed=ok)
        self.on_found(name, checker.AVAILABLE, source, (ok, msg))

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
        text = (f"{head}\n\n"
                f"🔎 المفحوص: <b>{s['checked']}</b>\n"
                f"✅ متاح مؤكد: <b>{s['available']}</b>\n"
                f"🔒 تم الحجز: <b>{s['claimed']}</b>\n"
                f"💎 فراجمنت: <b>{s['fragment']}</b>\n"
                f"❌ محجوز: <b>{s['taken']}</b>\n"
                f"⚠️ أخطاء: <b>{s['errors']}</b>\n"
                f"⚡ السرعة: <b>{rate:.0f}</b> فحص/دقيقة\n"
                f"⏳ المدة: {hrs:02d}:{mins:02d}:{secs:02d}\n"
                f"🕐 آخر يوزر: <code>{self.last_checked or '-'}</code>")
        if self.verify_q or self.claim_q:
            text += (f"\n\n🕓 بانتظار التحقق: <b>{len(self.verify_q)}</b>"
                     f"\n📌 بانتظار الحجز: <b>{len(self.claim_q)}</b>")
        c = self.get_claimer()
        if c is not None and c.ready and c.flood_left:
            text += "\n\n🧊 <b>الحساب بفلود</b>"
            if c.check_flood_left:
                text += f"\n• الفحص: {fmt_wait(c.check_flood_left)}"
            if c.claim_flood_left:
                text += f"\n• الحجز: {fmt_wait(c.claim_flood_left)}"
            text += "\nالمحتمل يتجمّع وينفحص وينحجز تلقائياً بعد الفلود."
        return text
