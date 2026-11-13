"""Several Telegram accounts working together, plus in-bot login.

SessionPool exposes the same interface as a single Claimer (ready, check,
claim, *_flood_left, me) and spreads the work across every connected account:
checks rotate between accounts that aren't flood-limited, and claims go to an
account that still has room for public channels.
"""
import asyncio
import itertools
import threading
import time

from .claimer import Claimer

try:
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
except ImportError:
    TelegramClient = None


class SessionPool:
    def __init__(self):
        self.sessions = []          # list[Claimer]
        self._lock = threading.Lock()
        self._rr = itertools.count()
        # names an account already confirmed as not free -> {name: time}
        # (endless rounds would otherwise burn the daily check quota on them)
        self.verdicts = {}
        self.last_claim_account = None  # id of the account that made the last successful claim

    # -- membership -----------------------------------------------------
    def add(self, claimer: Claimer):
        with self._lock:
            self.sessions.append(claimer)

    def remove(self, idx: int):
        with self._lock:
            if 0 <= idx < len(self.sessions):
                c = self.sessions.pop(idx)
                c.disconnect()
                return c
        return None

    def has_string(self, s: str) -> bool:
        return any(c.session_string == s for c in self.sessions)

    @property
    def active(self):
        return [c for c in self.sessions if c.ready]

    # -- Claimer-compatible interface ------------------------------------
    @property
    def ready(self):
        return bool(self.active)

    @property
    def me(self):
        a = self.active
        return a[0].me if a else None

    @property
    def last_error(self):
        errs = [c.last_error for c in self.sessions if c.last_error]
        return errs[-1] if errs else "لا توجد جلسات"

    @property
    def check_flood_left(self):
        a = self.active
        if not a:
            return 0
        return min(c.check_flood_left for c in a)

    @property
    def claim_flood_left(self):
        a = [c for c in self.active if not c.full]
        if not a:
            return 0
        return min(c.claim_flood_left for c in a)

    @property
    def flood_left(self):
        return max(self.check_flood_left, self.claim_flood_left)

    def _rotation(self):
        a = self.active
        if not a:
            return []
        start = next(self._rr) % len(a)
        return a[start:] + a[:start]

    def set_base_interval(self, seconds, auto=False):
        for c in self.sessions:
            c.set_base_interval(seconds, auto)

    @property
    def avg_interval(self):
        a = self.active
        return sum(c.interval for c in a) / len(a) if a else 0

    VERDICT_TTL = 6 * 3600
    VERDICT_MAX = 300_000

    def check(self, username):
        cached = self.verdicts.get(username)
        if cached and time.time() - cached[1] < self.VERDICT_TTL:
            return cached[0]
        res = self._check(username)
        if res is not None and res != "available":
            if len(self.verdicts) >= self.VERDICT_MAX:
                self.verdicts.clear()
            self.verdicts[username] = (res, time.time())
        return res

    def _check(self, username):
        # use the account that is free soonest instead of queueing on one
        free = [c for c in self._rotation() if not c.check_flood_left]
        with self._lock:  # pick + book atomically so threads spread out
            free.sort(key=lambda c: c.next_check_at)
        for c in free:
            res = c.check(username)
            if res is not None:
                return res
        return None

    def claim(self, username, mode="channel"):
        candidates = [c for c in self._rotation() if not c.full]
        if not candidates:
            return False, "كل الجلسات وصلت حد القنوات العامة"
        for c in candidates:
            if c.claim_flood_left:
                continue
            ok, msg = c.claim(username, mode)
            if ok:
                label = f"@{c.me.username}" if c.me and c.me.username else c.label
                self.last_claim_account = c.me.id if c.me else None
                self.verdicts.pop(username, None)
                return True, f"{msg} ({label})"
            if msg.startswith("FLOOD:") or c.full:
                continue  # try the next account
            return False, msg  # occupied / invalid: other accounts won't help
        if any(not c.full for c in self.active):
            return False, f"FLOOD:{max(1, self.claim_flood_left)}"
        return False, "كل الجلسات وصلت حد القنوات العامة"


def account_name(c):
    if c.me is None:
        return c.label
    return f"@{c.me.username}" if c.me.username else str(c.me.id)


def _pool_holder(self, username):
    """The connected account whose public channel carries this username."""
    for c in self.active:
        if c.holds(username):
            return c
    return None


def _pool_release(self, username):
    c = self.holder(username)
    if c is None:
        return False, "ما لگيت اليوزر على أي حساب متصل"
    ok, msg = c.release(username)
    if ok:
        self.verdicts.pop(username.lower(), None)
    return ok, f"{msg} ({account_name(c)})"


def _pool_move(self, username, target):
    """Move a claimed username to another account: release, then claim there.
    If the new claim fails the original account takes it back."""
    src = self.holder(username)
    if src is None:
        return False, "ما لگيت اليوزر على أي حساب متصل"
    if src is target:
        return False, "اليوزر أصلاً على هذا الحساب"
    if target.full or target.claim_flood_left or not target.ready:
        return False, "الحساب الهدف مو جاهز (فلود / ممتلئ / غير متصل)"
    ok, msg = src.release(username)
    if not ok:
        return False, msg
    ok, msg = target.claim(username, "channel")
    if ok:
        return True, f"انتقل من {account_name(src)} إلى {account_name(target)}"
    back, back_msg = src.claim(username, "channel")
    return False, (f"فشل النقل ({msg})، " + ("ورجع اليوزر لحسابه الأصلي." if back
                                              else f"وما گدرت أرجعه: {back_msg}. احجزه يدوياً هسه!"))


def _pool_usage(self, max_age=600):
    """[(claimer, [(username, title)])] for every connected account."""
    return [(c, c.public_channels(max_age)) for c in self.active]


SessionPool.holder = _pool_holder
SessionPool.release = _pool_release
SessionPool.move = _pool_move
SessionPool.usage = _pool_usage


class LoginFlow:
    """Phone + code (+ 2FA password) login that ends with a StringSession."""

    def __init__(self, api_id, api_hash):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self.client = None
        self.phone = None
        self.phone_code_hash = None

    def _run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def send_code(self, phone):
        """Return (ok, message)."""
        async def go():
            self.client = TelegramClient(StringSession(), self.api_id, self.api_hash)
            await self.client.connect()
            sent = await self.client.send_code_request(phone)
            self.phone, self.phone_code_hash = phone, sent.phone_code_hash
        try:
            self._run(go())
            return True, ""
        except errors.FloodWaitError as e:
            return False, f"فلود، انتظر {e.seconds} ثانية"
        except errors.PhoneNumberInvalidError:
            return False, "الرقم غير صحيح"
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def sign_in(self, code=None, password=None):
        """Return ('ok', session) | ('password', '') | ('error', message)."""
        async def go():
            try:
                if password is not None:
                    await self.client.sign_in(password=password)
                else:
                    await self.client.sign_in(self.phone, code,
                                              phone_code_hash=self.phone_code_hash)
            except errors.SessionPasswordNeededError:
                return "password", ""
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as e:
                return "error", "الكود غلط أو منتهي" if "Invalid" in type(e).__name__ else "الكود منتهي، ابدأ من جديد"
            except errors.PasswordHashInvalidError:
                return "error", "باسورد التحقق بخطوتين غلط"
            return "ok", self.client.session.save()
        try:
            return self._run(go())
        except Exception as e:  # noqa: BLE001
            return "error", str(e)

    def close(self):
        async def go():
            if self.client:
                await self.client.disconnect()
        try:
            self._run(go(), timeout=15)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
