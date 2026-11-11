"""Telegram username rules."""
import re

MIN_LEN = 5  # 4-char and shorter usernames are only sold on Fragment
MAX_LEN = 32
_VALID = re.compile(r"^[a-z][a-z0-9_]*$")


def normalize(name: str) -> str:
    name = (name or "").strip().lower()
    for p in ("https://", "http://"):
        if name.startswith(p):
            name = name[len(p):]
    for p in ("t.me/", "telegram.me/", "@"):
        if name.startswith(p):
            name = name[len(p):]
    return name.split("/")[0].split("?")[0]


def is_valid(name: str, min_len: int = MIN_LEN) -> bool:
    if not (min_len <= len(name) <= MAX_LEN):
        return False
    if not _VALID.match(name):
        return False
    if name.endswith("_") or "__" in name:
        return False
    return True
