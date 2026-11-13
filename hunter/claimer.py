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
import base64
import struct
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
AUTO_MIN = 2.5        # auto mode never goes faster than this
AUTO_MAX_BASE = 12.0  # ...and never learns a base slower than this


# Telegram production data centres (needed to turn a Pyrogram session into Telethon's)
DC_IPS = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100",
          4: "149.154.167.91", 5: "91.108.56.130"}
# Pyrogram string-session layouts by decoded length
_PYRO_FORMATS = {271: ">BI?256sQ?", 263: ">B?256sI?", 267: ">B?256sQ?"}


def to_telethon_session(s: str) -> str:
    """Accept a Telethon StringSession or convert a Pyrogram one. Returns a
    Telethon string, or the input unchanged when it can't be recognised."""
    s = (s or "").strip()
    if s.startswith("1") and TelegramClient is not None:
        try:
            StringSession(s)
            return s  # already a valid Telethon session
        except Exception:  # noqa: BLE001
            pass
    try:
        data = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, TypeError):
        return s
    fmt = _PYRO_FORMATS.get(len(data))
    if not fmt or TelegramClient is None:
        return s
    fields = struct.unpack(fmt, data)
    dc_id, auth_key = fields[0], fields[3] if len(fmt) == 10 else fields[2]
    if dc_id not in DC_IPS:
        return s
    from telethon.crypto import AuthKey
    ss = StringSession()
    ss.set_dc(dc_id, DC_IPS[dc_id], 443)
    ss.auth_key = AuthKey(auth_key)
    return ss.save()


def fmt_wait(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class Claimer:
    def __init__(self, api_id, api_hash, session_string, label="session"):
        self.label = label
        self.full = False  # hit the public-channel limit
        self.frozen = False  # Telegram froze this account
        self._public = None      # cached [(username, title)] of public channels this account owns
        self._public_at = 0.0
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
        self.auto = True  # learn the best interval for this account by itself
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    # -- plumbing -------------------------------------------------------
    def _mark_frozen(self, exc):
        if "FROZEN" not in str(exc).upper() or self.frozen:
            return False
        self.frozen = True
        self.ready = False  # stop using it so it doesn't get worse
        self.last_error = "الحساب مجمّد من تليجرام"
        name = (f"@{self.me.username}" if self.me and self.me.username
                else str(self.me.id) if self.me else self.label)
        error(f"🥶 الحساب {name} مجمّد من تليجرام - انوقف استخدامه. احذفه وقدّم طعن من @SpamBot")
        return True

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
        self.client = TelegramClient(StringSession(to_telethon_session(self.session_string)),
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

    def set_base_interval(self, seconds: float, auto: bool = False):
        self.auto = auto
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
            elif self.auto and self._ok_streak >= 120 and self.base_interval > AUTO_MIN:
                # long healthy run: this account can go a bit faster
                self.base_interval = max(AUTO_MIN, self.base_interval * 0.9)
                self.interval = self.base_interval
                self._ok_streak = 0
        return res

    async def _check(self, username):
        try:
            ok = await self.client(functions.account.CheckUsernameRequest(username))
            return checker.AVAILABLE if ok else checker.TAKEN
        except errors.FloodWaitError as e:
            self.check_flood_until = time.time() + e.seconds
            # slow this account down so it floods less next time
            if self.auto:  # remember that this account needs to be slower
                self.base_interval = min(AUTO_MAX_BASE, self.base_interval * 1.5)
            self.interval = min(MAX_INTERVAL, max(self.interval * 2, self.base_interval * 2))
            self._ok_streak = 0
            name = (f"@{self.me.username}" if self.me and self.me.username
                    else str(self.me.id) if self.me else self.label)
            error(f"🧊 فلود فحص على {name}: {fmt_wait(e.seconds)} - صار يفحص كل {self.interval:.0f}ث")
            return None
        except errors.UsernameInvalidError:
            return checker.INVALID
        except errors.UsernameOccupiedError:
            return checker.TAKEN
        except errors.RPCError as e:
            if "PURCHASE_AVAILABLE" in str(e).upper():
                return checker.FRAGMENT
            if self._mark_frozen(e):
                return None
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

    # -- owned public channels -------------------------------------------
    def public_channels(self, max_age=600):
        """[(username, title)] of the public channels this account owns
        (each one uses a slot of Telegram's ~10 public-channel limit)."""
        if not self.ready:
            return []
        if self._public is not None and time.time() - self._public_at < max_age:
            return self._public
        try:
            self._public = self._run(self._list_public(), timeout=40)
            self._public_at = time.time()
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return self._public or []
        return self._public

    async def _list_public(self):
        res = await self.client(functions.channels.GetAdminedPublicChannelsRequest())
        return [((c.username or "").lower(), c.title or "") for c in res.chats if getattr(c, "username", None)]

    def holds(self, username):
        return any(u == username.lower() for u, _t in self.public_channels())

    def release(self, username):
        """Free a claimed username. Channels the bot created for it are
        deleted; any other channel only loses its username. Returns (ok, msg)."""
        if not self.ready:
            return False, "الحساب غير متصل"
        with self._claim_lock:
            try:
                out = self._run(self._release(username.lower()), timeout=60)
            except Exception as e:  # noqa: BLE001
                return False, str(e)
        self._public = None
        if out[0]:
            self.full = False
        return out

    async def _release(self, username):
        try:
            res = await self.client(functions.channels.GetAdminedPublicChannelsRequest())
            ch = next((c for c in res.chats if (getattr(c, "username", "") or "").lower() == username), None)
            if ch is None:
                return False, "ما لگيت قناة بهذا اليوزر على هذا الحساب"
            if (ch.title or "") in (f"@{username}", "Reserved"):
                await self.client(functions.channels.DeleteChannelRequest(ch))
                return True, "انحذفت القناة وتحرر اليوزر"
            await self.client(functions.channels.UpdateUsernameRequest(ch, ""))
            return True, "انشال اليوزر من القناة (القناة باقية لأن اسمها متغيّر)"
        except errors.FloodWaitError as e:
            self.claim_flood_until = time.time() + e.seconds
            return False, f"فلود {fmt_wait(e.seconds)}"
        except errors.RPCError as e:
            if self._mark_frozen(e):
                return False, "الحساب مجمّد من تليجرام"
            return False, f"{type(e).__name__}: {e}"

    async def _claim(self, username, mode):
        try:
            if mode == "account":
                await self.client(functions.account.UpdateUsernameRequest(username))
                return True, "تم تثبيته على حسابك"
            holder = await self._get_holder()
            await self.client(functions.channels.UpdateUsernameRequest(holder, username))
            self._holder = None  # this channel now owns the username
            self._public = None
            try:
                await self.client(functions.channels.EditTitleRequest(holder, f"@{username}"))
            except errors.RPCError:
                pass
            return True, "تم تثبيته على قناة"
        except errors.FloodWaitError as e:
            self.claim_flood_until = time.time() + e.seconds
            return False, f"FLOOD:{e.seconds}"
        except errors.RPCError as e:
            if self._mark_frozen(e):
                return False, "الحساب مجمّد من تليجرام"
            name = type(e).__name__
            if "TooMuch" in name or "TOO_MUCH" in str(e).upper():
                self._holder = None
                self.full = True
                return False, "وصلت الحد الأقصى للقنوات العامة على الحساب"
            if "Occupied" in name or "OCCUPIED" in str(e).upper():
                return False, "انحجز قبلك (محجوز)"
            return False, f"{name}: {e}"
