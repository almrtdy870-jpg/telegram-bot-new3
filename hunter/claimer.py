"""Exact checks and claiming through your own Telegram account (Telethon).

Runs Telethon on a private asyncio loop in a background thread so the rest of
the (threaded) bot can call it synchronously.

Flood control:
  * account checks are throttled per account (`interval` seconds apart); the
    interval doubles after a FloodWait and slowly returns to `base_interval`;
  * check and claim floods are tracked separately, since Telegram limits each
    method on its own;
  * a single "holder" channel is reused until a username actually sticks, so
    failed claims don't create (and delete) channels - channel creation is one
    of the most heavily rate-limited actions.
"""
import asyncio
import threading
import time

from . import checker
from .events import error

try:
    from telethon import TelegramClient, errors, functions
    from telethon.sessions import StringSession
except ImportError:  # optional dependency
    TelegramClient = None

CHECK_INTERVAL = 4.0  # default seconds between account.checkUsername calls
MAX_INTERVAL = 60.0


def fmt_wait(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Claimer:
    def __init__(self, api_id, api_hash, session_string, label="session"):
        self.label = label
        self.full = False  # hit the public-channel limit
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session_string = session_string
        self.client = None
        self.me = None
        self.ready = False
        self.check_flood_until = 0.0
        self.claim_flood_until = 0.0
        self.last_error = ""
        self._holder = None
        self._claim_lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._next_slot = 0.0    # earliest time the next check may run
        self._slot_lock = threading.Lock()
        self.base_interval = CHECK_INTERVAL
        self.interval = CHECK_INTERVAL
        self._ok_streak = 0
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    # -- plumbing -------------------------------------------------------
    def _run(self, coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        if TelegramClient is None:
            self.last_error = "telethon غير مثبت"
            return False
        try:
            self.me = self._run(self._start())
            self.ready = self.me is not None
            if not self.ready:
                self.last_error = "الجلسة غير مسجلة الدخول (session string غير صالح)"
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            self.ready = False
        return self.ready

    async def _start(self):
        self.client = TelegramClient(StringSession(self.session_string),
                                     self.api_id, self.api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            return None
        return await self.client.get_me()

    def disconnect(self):
        self.ready = False
        async def go():
            if self.client:
                await self.client.disconnect()
        try:
            self._run(go(), timeout=15)
        except Exception:  # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    @property
    def check_flood_left(self) -> int:
        return max(0, int(self.check_flood_until - time.time()))

    @property
    def claim_flood_left(self) -> int:
        return max(0, int(self.claim_flood_until - time.time()))

    @property
    def flood_left(self) -> int:
        return max(self.check_flood_left, self.claim_flood_left)

    @property
    def next_check_at(self) -> float:
        return self._next_slot

    def set_base_interval(self, seconds: float):
        self.base_interval = max(0.5, float(seconds))
        self.interval = self.base_interval

    def _reserve_slot(self) -> float:
        """Book the next free time slot for a check (spaced `interval` apart)."""
        with self._slot_lock:
            slot = max(time.time(), self._next_slot)
            self._next_slot = slot + self.interval
            return slot

    # -- check ----------------------------------------------------------
    def check(self, username):
        """Return AVAILABLE/TAKEN/FRAGMENT/INVALID, or None if unavailable now."""
        if not self.ready or self.check_flood_left:
            return None
        wait = self._reserve_slot() - time.time()
        if wait > 0:
            time.sleep(wait)
        if self.check_flood_left:
            return None
        try:
            res = self._run(self._check(username), timeout=30)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return None
        if res is not None:
            # healthy for a while -> speed back up towards the base interval
            self._ok_streak += 1
            if self._ok_streak >= 30 and self.interval > self.base_interval:
                self.interval = max(self.base_interval, self.interval * 0.8)
                self._ok_streak = 0
        return res

    async def _check(self, username):
        try:
            ok = await self.client(functions.account.CheckUsernameRequest(username))
            return checker.AVAILABLE if ok else checker.TAKEN
        except errors.FloodWaitError as e:
            self.check_flood_until = time.time() + e.seconds
            # slow this account down so it floods less next time
            self.interval = min(MAX_INTERVAL, max(self.interval * 2, self.base_interval * 2))
            self._ok_streak = 0
            name = f"@{self.me.username}" if self.me and self.me.username else self.label
            error(f"🧊 فلود فحص على {name}: {fmt_wait(e.seconds)} - صار يفحص كل {self.interval:.0f}ث")
            return None
        except errors.UsernameInvalidError:
            return checker.INVALID
        except errors.UsernameOccupiedError:
            return checker.TAKEN
        except errors.RPCError as e:
            if "PURCHASE_AVAILABLE" in str(e).upper():
                return checker.FRAGMENT
            raise

    # -- claim ----------------------------------------------------------
    def claim(self, username, mode="channel"):
        """Return (ok, message). mode: 'channel' or 'account'."""
        if not self.ready:
            return False, "الحساب غير مربوط"
        if self.claim_flood_left:
            return False, f"FLOOD:{self.claim_flood_left}"
        with self._claim_lock:
            try:
                return self._run(self._claim(username, mode), timeout=90)
            except Exception as e:  # noqa: BLE001
                return False, str(e)

    async def _get_holder(self):
        if self._holder is None:
            res = await self.client(functions.channels.CreateChannelRequest(
                title="Reserved", about="Reserved", broadcast=True))
            self._holder = res.chats[0]
        return self._holder

    async def _claim(self, username, mode):
        try:
            if mode == "account":
                await self.client(functions.account.UpdateUsernameRequest(username))
                return True, "تم تثبيته على حسابك"
            holder = await self._get_holder()
            await self.client(functions.channels.UpdateUsernameRequest(holder, username))
            self._holder = None  # this channel now owns the username
            try:
                await self.client(functions.channels.EditTitleRequest(holder, f"@{username}"))
            except errors.RPCError:
                pass
            return True, "تم تثبيته على قناة"
        except errors.FloodWaitError as e:
            self.claim_flood_until = time.time() + e.seconds
            return False, f"FLOOD:{e.seconds}"
        except errors.RPCError as e:
            name = type(e).__name__
            if "TooMuch" in name or "TOO_MUCH" in str(e).upper():
                self._holder = None
                self.full = True
                return False, "وصلت الحد الأقصى للقنوات العامة على الحساب"
            if "Occupied" in name or "OCCUPIED" in str(e).upper():
                return False, "انحجز قبلك (محجوز)"
            return False, f"{name}: {e}"
