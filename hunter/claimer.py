"""Exact checks and claiming through your own Telegram account (Telethon).

Runs Telethon on a private asyncio loop in a background thread so the rest of
the (threaded) bot can call it synchronously.
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


class Claimer:
    def __init__(self, api_id, api_hash, session_string):
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session_string = session_string
        self.client = None
        self.me = None
        self.ready = False
        self.flood_until = 0.0
        self.last_error = ""
        self._claim_lock = threading.Lock()
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
    def flood_left(self) -> int:
        return max(0, int(self.flood_until - time.time()))

    # -- check ----------------------------------------------------------
    def check(self, username):
        """Return AVAILABLE/TAKEN/FRAGMENT/INVALID, or None if unavailable now."""
        if not self.ready or self.flood_left:
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
            self.flood_until = time.time() + e.seconds
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
        with self._claim_lock:
            try:
                return self._run(self._claim(username, mode), timeout=90)
            except Exception as e:  # noqa: BLE001
                return False, str(e)

    async def _claim(self, username, mode):
        if mode == "account":
            try:
                await self.client(functions.account.UpdateUsernameRequest(username))
                return True, "تم تثبيته على حسابك"
            except errors.FloodWaitError as e:
                self.flood_until = time.time() + e.seconds
                return False, f"FloodWait {e.seconds}s"
            except errors.RPCError as e:
                return False, type(e).__name__ + ": " + str(e)

        channel = None
        try:
            res = await self.client(functions.channels.CreateChannelRequest(
                title=f"@{username}", about="Reserved", broadcast=True))
            channel = res.chats[0]
            await self.client(functions.channels.UpdateUsernameRequest(channel, username))
            return True, "تم تثبيته على قناة جديدة"
        except errors.FloodWaitError as e:
            self.flood_until = time.time() + e.seconds
            err = f"FloodWait {e.seconds}s"
        except errors.RPCError as e:
            err = type(e).__name__ + ": " + str(e)
        if channel is not None:  # don't leave empty channels behind
            try:
                await self.client(functions.channels.DeleteChannelRequest(channel))
            except errors.RPCError:
                pass
        if "ADMIN_PUBLIC_TOO_MUCH" in err.upper() or "TooMuch" in err:
            err = "وصلت الحد الأقصى للقنوات العامة على الحساب"
        return False, err
