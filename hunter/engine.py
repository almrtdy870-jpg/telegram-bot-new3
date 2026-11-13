"""The hunt loop: N worker threads pulling candidates from a shared generator.

When an account is linked but flood-limited, "maybe available" names (seen only
via t.me/fragment) are not reported one by one; they go to a verify queue that a
background thread drains with the account once the flood ends. Confirmed names
that can't be claimed yet go to a claim queue the same way.
"""
import re
import threading
import time

from . import checker, generator
from .claimer import fmt_wait
from .events import error, event
from .generator import make_stream

MAX_QUEUE = 150  # keep it small: every queued name costs a rate-limited account check


class Hunter:
    def __init__(self, store, on_found, on_status, claimer_getter, on_maybe=None, on_combo=None):
        self.on_maybe = on_maybe        # (list_of_names) batched unverified results
        self.maybe_buf = []
        # on_ask([(name, reason)]): free names that were NOT claimed automatically
        # reason: combo | cap (daily limit) | digit (weak trailing digit)
        self.on_ask = on_combo
        self.ask_buf = []
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
        self.reported = set()
        self.verify_q = []   # need an exact account check
        self.claim_q = []    # confirmed free, waiting for claim flood to end
        self.watch_hits = []  # usernames from the watchlist that just got claimed
        threading.Thread(target=self._queue_worker, daemon=True).start()
        threading.Thread(target=self._watch_worker, daemon=True).start()

    @staticmethod
    def _new_stats():
        return {"checked": 0, "taken": 0, "available": 0, "fragment": 0,
                "errors": 0, "claimed": 0, "started": time.time(), "finished": None}

    # -- control --------------------------------------------------------
    def start(self):
        if self.running:
            return False
        self.stats = self._new_stats()
        self._gen = make_stream(self.store.settings, self.seen,
                                on_round=lambda label, r: event(f"🔁 {label}: خلصت الدورة، بدت الدورة {r}", "info"))
        self.running = True
        n = max(1, min(int(self.store.settings.get("threads", 5)), 50))
        self._threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(n)]
        event("تم تشغيل الصيد", "ok")
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
        self._flush_maybe()
        self.store.save()
        event(f"توقف الصيد ({self.stats['checked']} فحص)", "warn")
        self.on_status(self.summary(final=True))

    # -- queues ---------------------------------------------------------
    def queue_claim(self, name):
        with self._q_lock:
            if name not in self.claim_q:
                self.claim_q.append(name)

    def _queue_verify(self, name):
        with self._q_lock:
            if name not in self.verify_q:
                self.verify_q.append(name)
                if len(self.verify_q) > MAX_QUEUE:
                    del self.verify_q[0]  # drop the oldest, keep the freshest

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

    # -- watchlist (independent of the hunt) ----------------------------
    def _watch_worker(self):
        """Poll watched usernames fast and claim the instant one is free.
        Runs even when the main hunt is stopped."""
        i = 0
        while True:
            time.sleep(4)
            watch = list(self.store.settings.get("watch", []))
            if not watch:
                continue
            c = self.get_claimer()
            if c is None or not c.ready or c.check_flood_left:
                continue
            name = watch[i % len(watch)]
            i += 1
            try:
                res = c.check(name)
            except Exception:  # noqa: BLE001
                continue
            if res != checker.AVAILABLE:
                continue
            event(f"🎯 اليوزر المراقَب @{name} صار متاح! جاري الحجز...", "ok")
            mode = self.store.settings.get("claim_mode", "off") or "channel"
            ok, msg = c.claim(name, mode if mode != "off" else "channel")
            self._remove_watch(name)
            if ok:
                self.store.day_inc("claims")
                self.store.add_found(name, checker.AVAILABLE, "watch", claimed=True)
                self.store.update_found(name, account=getattr(c, "last_claim_account", None))
                event(f"🎯🔒 انحجز اليوزر المراقَب @{name}", "ok")
            else:
                self.store.add_found(name, checker.AVAILABLE, "watch")
                error(f"🎯 @{name} صار متاح بس ما انحجز: {msg}")
            with self._q_lock:
                self.watch_hits.append((name, ok, msg))

    def _remove_watch(self, name):
        with self.store.lock:
            w = self.store.settings.get("watch", [])
            if name in w:
                w.remove(name)
                self.store.save()

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
            if key == "checked":
                self.store.data["total_checks"] = self.store.data.get("total_checks", 0) + 1
                self.store.day_inc("checks")

    def _worker(self):
        errors_in_row = 0
        while self.running:
            name = self._next()
            if name is None:
                break
            if name == "":  # every selected method is between rounds
                time.sleep(1)
                continue
            if name in self.reported:  # already found / claimed: don't spend checks on it
                continue
            claimer = self.get_claimer()
            status, source = checker.full_check(name, claimer)
            self.last_checked = name
            self._bump("checked")

            if status == checker.UNKNOWN:
                self._bump("errors")
                error(f"فشل فحص @{name} ({source}) - احتمال حظر مؤقت")
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
                if source == "account":
                    self._bump("available")
                    self._confirmed(name, source, claimer)
                elif claimer is not None:
                    # sessions exist but are flood-limited / offline:
                    # verify silently later instead of spamming
                    self._queue_verify(name)
                else:
                    # no sessions at all: only t.me/fragment, unverified - batch it
                    with self._q_lock:
                        self.maybe_buf.append(name)
                    self.store.add_found(name, "unverified", source)

            delay = float(self.store.settings.get("delay", 0.5) or 0)
            if delay:
                time.sleep(delay)

    def _confirmed(self, name, source, claimer):
        # endless rounds re-check names: report/claim each one only once
        if name in self.reported:
            return
        self.reported.add(name)
        mode = self.store.settings.get("claim_mode", "off")
        can_claim = mode != "off" and claimer is not None and claimer.ready
        reason = self._ask_reason(name) if can_claim or name in generator.COMBO_NAMES else None
        if reason:
            # free, but not worth / not allowed to claim automatically: list it
            event(f"🟢 متاح (ينتظر قرارك) @{name}", "ok")
            self.store.add_found(name, checker.AVAILABLE, source)
            self.store.day_inc("asked")
            with self._q_lock:
                self.ask_buf.append((name, reason))
            return
        if mode != "off" and claimer is not None and claimer.ready:
            self._try_claim(name, source, claimer)
        else:
            event(f"✅ متاح @{name}", "ok")
            self.store.add_found(name, checker.AVAILABLE, source)
            self.on_found(name, checker.AVAILABLE, source, None)

    def _ask_reason(self, name):
        """Why this free name should be listed instead of claimed (or None)."""
        s = self.store.settings
        if name in generator.COMBO_NAMES and not s.get("combo_claim"):
            return "combo"
        cap = int(s.get("claim_cap_day", 0) or 0)
        if cap and self.store.day().get("claims", 0) >= cap:
            return "cap"
        strong = str(s.get("auto_digits", "") or "")
        m = re.search(r"(\d+)$", name)
        if strong and m and any(ch not in strong for ch in m.group(1)):
            return "digit"
        return None

    def _try_claim(self, name, source, claimer, notify_wait=True):
        mode = self.store.settings.get("claim_mode", "off")
        if mode == "off":
            mode = "channel"
        ok, msg = claimer.claim(name, mode)
        if not ok and msg.startswith("FLOOD:"):
            first_time = name not in self.claim_q
            if first_time:
                error(f"فلود حجز ({fmt_wait(claimer.claim_flood_left)}) - @{name} بالانتظار")
            self.queue_claim(name)
            if notify_wait and first_time:
                self.store.add_found(name, checker.AVAILABLE, source, claimed=False)
                self.on_found(name, checker.AVAILABLE, source,
                              (False, f"الحساب بفلود ({fmt_wait(claimer.claim_flood_left)}) "
                                      "- بالانتظار وراح ينحجز تلقائياً"))
            return
        if ok:
            event(f"🔒 انحجز @{name}", "ok")
        else:
            error(f"ما انحجز @{name}: {msg}")
        if ok:
            self._bump("claimed")
            self.store.day_inc("claims")
            if mode == "account":  # an account holds a single username
                self.store.set("claim_mode", "off")
        self.store.add_found(name, checker.AVAILABLE, source, claimed=ok)
        if ok:
            self.store.update_found(name, account=getattr(claimer, "last_claim_account", None))
        self.on_found(name, checker.AVAILABLE, source, (ok, msg))

    # -- reporting ------------------------------------------------------
    def _reporter(self):
        while self.running:
            for _ in range(20):
                if not self.running:
                    return
                time.sleep(1)
            self._flush_maybe()
            self.store.save()
            self.on_status(self.summary())

    def _flush_maybe(self):
        with self._q_lock:
            names, self.maybe_buf = self.maybe_buf, []
            asks, self.ask_buf = self.ask_buf, []
            hits, self.watch_hits = self.watch_hits, []
        for name, ok, msg in hits:
            self.on_found(name, checker.AVAILABLE, "watch", (ok, msg))
        if names and self.on_maybe:
            self.on_maybe(names)
        if asks and self.on_ask:
            self.on_ask(asks)

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
