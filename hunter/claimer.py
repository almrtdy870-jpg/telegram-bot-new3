"""Exact checks and claiming through your own Telegram account (Telethon).

Runs Telethon on a private asyncio loop in a background thread so the rest of
the (threaded) bot can call it synchronously.

Flood control:
  * account checks are throttled globally (CHECK_INTERVAL seconds apart);
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

try:
    from telethon import TelegramClient, errors, functions
    from telethon.sessions import StringSession
except ImportError:  # optional dependency
    TelegramClient = None

CHECK_INTERVAL = 1.2  # seconds between account.checkUsername calls


def fmt_wait(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Claimer:
    def __init__(self, api_id, api_hash, session_string):
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
        self._last_check = 0.0
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

    @property
    def check_flood_left(self) -> int:
        return max(0, int(self.check_flood_until - time.time()))

    @property
    def claim_flood_left(self) -> int:
        return max(0, int(self.claim_flood_until - time.time()))

    @property
    def flood_left(self) -> int:
        return max(self.check_flood_left, self.claim_flood_left)

    # -- check ----------------------------------------------------------
    def check(self, username):
        """Return AVAILABLE/TAKEN/FRAGMENT/INVALID, or None if unavailable now."""
        if not self.ready or self.check_flood_left:
            return None
        with self._check_lock:  # global throttle across worker threads
            wait = self._last_check + CHECK_INTERVAL - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_check = time.time()
            if self.check_flood_left:
                return None
            try:
                return self._run(self._check(username), timeout=30)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                return None

    async def _check(self, username):
        try:
            ok = await self.client(functions.account.CheckUsernameRequest(username))
            return checker.AVAILABLE if ok else checker.TAKEN
        except errors.FloodWaitError as e:
            self.check_flood_until = time.time() + e.seconds
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
                return False, "وصلت الحد الأقصى للقنوات العامة على الحساب"
            if "Occupied" in name or "OCCUPIED" in str(e).upper():
                return False, "انحجز قبلك (محجوز)"
            return False, f"{name}: {e}"
