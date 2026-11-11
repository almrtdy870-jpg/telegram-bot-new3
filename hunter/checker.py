"""Username availability checks.

Layers (fast -> precise):
  1. t.me page     : public, fast. A profile title means the name is taken.
  2. Telethon      : account.checkUsername from your own account (exact answer),
                     used only on names that passed layer 1.
  3. fragment.com  : fallback when no account is configured; detects names that
                     are taken/sold or listed for sale on Fragment.
"""
import re
import threading
import time

import requests

AVAILABLE = "available"   # free to claim
TAKEN = "taken"
FRAGMENT = "fragment"     # for sale / auction on Fragment
INVALID = "invalid"
UNKNOWN = "unknown"       # network error / rate limited

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_local = threading.local()
_FRAG_STATUS = re.compile(
    r'class="[^"]*tm-section-header-status[^"]*"[^>]*>\s*([^<]+?)\s*<', re.I)


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        _local.s = s
    return s


def parse_tme(html: str) -> str:
    if "tgme_page_title" in html:
        return TAKEN
    return AVAILABLE


def check_tme(username: str, timeout: float = 10) -> str:
    try:
        r = _session().get(f"https://t.me/{username}", timeout=timeout)
    except requests.RequestException:
        return UNKNOWN
    if r.status_code != 200:
        return UNKNOWN
    return parse_tme(r.text)


def parse_fragment(final_url: str, html: str):
    """Return TAKEN / FRAGMENT, or None when Fragment gives no signal."""
    if "/username/" not in final_url:
        return None  # redirected to search -> not tracked by Fragment
    m = _FRAG_STATUS.search(html)
    if not m:
        return None
    status = m.group(1).strip().lower()
    if "unavailable" in status:
        return None
    if "taken" in status or "sold" in status:
        return TAKEN
    if "available" in status or "auction" in status or "sale" in status:
        return FRAGMENT
    return None


def check_fragment(username: str, timeout: float = 10):
    try:
        r = _session().get(f"https://fragment.com/username/{username}",
                           timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return UNKNOWN
    if r.status_code != 200:
        return UNKNOWN
    return parse_fragment(r.url, r.text)


def full_check(username: str, claimer=None):
    """Return (status, source). source is where the verdict came from."""
    tme = check_tme(username)
    if tme == UNKNOWN:
        time.sleep(1)
        tme = check_tme(username)
    if tme == TAKEN:
        return TAKEN, "t.me"
    if tme == UNKNOWN:
        return UNKNOWN, "t.me"

    if claimer is not None and claimer.ready:
        res = claimer.check(username)
        if res is not None:
            return res, "account"

    frag = check_fragment(username)
    if frag in (TAKEN, FRAGMENT):
        return frag, "fragment"
    return AVAILABLE, "t.me+fragment"
