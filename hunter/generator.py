"""Username candidate generators.

Template tokens (uppercase):
    L  any letter a-z
    D  digit 0-9
    X  letter or digit
    V  vowel (aeiou)
    C  consonant
    =  repeat previous character
Anything else in [a-z0-9_] is taken literally.
Example: "L=L=L" -> "aabbc", "LDDDD" -> "k4821", "pro_LLL" -> "pro_xqz".
"""
import random
import string

from .rules import is_valid, normalize

LETTERS = string.ascii_lowercase
DIGITS = string.digits
VOWELS = "aeiou"
CONSONANTS = "".join(c for c in LETTERS if c not in VOWELS)

TOKENS = {
    "L": LETTERS,
    "D": DIGITS,
    "X": LETTERS + DIGITS,
    "V": VOWELS,
    "C": CONSONANTS,
}
LITERALS = set(LETTERS + DIGITS + "_")

# key: (label, [templates])
PRESETS = {
    "five": ("خماسي حروف", ["LLLLL"]),
    "five_mix": ("خماسي مختلط", ["LXXXX"]),
    "six": ("سداسي حروف", ["LLLLLL"]),
    "semi3": ("شبه ثلاثي", ["L_L_L"]),
    "semi4": ("شبه رباعي", ["L_LLL", "LL_LL", "LLL_L"]),
    "repeat": ("مكرر", ["L=L=L", "LL=L=", "L=LL=", "L==LL", "LL==L", "L=L==", "LLL=="]),
    "triple_char": ("حرف ثلاثي مكرر", ["L==LL", "LL==L", "L=L==", "LL===", "L===L"]),
    "pronounce": ("ينلفظ", ["CVCVC", "CVCCV", "VCVCV", "CVCVCV"]),
    "digits": ("حرف + أرقام", ["LDDDD", "LLDDD", "LDDDL"]),
    "num_end": ("حروف + رقم", ["LLLLD", "LLLDD"]),
}


def validate_template(tpl: str) -> bool:
    if not tpl:
        return False
    for i, ch in enumerate(tpl):
        if ch in TOKENS or ch in LITERALS:
            continue
        if ch == "=" and i > 0:
            continue
        return False
    return True


def render(tpl: str, rng: random.Random = random) -> str:
    out = []
    for ch in tpl:
        if ch in TOKENS:
            out.append(rng.choice(TOKENS[ch]))
        elif ch == "=":
            out.append(out[-1] if out else rng.choice(LETTERS))
        else:
            out.append(ch)
    return "".join(out)


def space_size(tpl: str) -> int:
    n = 1
    for ch in tpl:
        if ch in TOKENS:
            n *= len(TOKENS[ch])
    return n


def random_stream(templates, prefix="", suffix="", seen=None, max_misses=5000):
    """Infinite stream of unique valid names; stops when the space looks exhausted."""
    seen = seen if seen is not None else set()
    rng = random.Random()
    misses = 0
    while misses < max_misses:
        name = f"{prefix}{render(rng.choice(templates), rng)}{suffix}"
        if name in seen or not is_valid(name):
            misses += 1
            continue
        misses = 0
        seen.add(name)
        if len(seen) > 2_000_000:
            seen.clear()
        yield name


def word_variants(word: str):
    yield word
    for s in ("s", "x", "z", "o", "y", "_x", "_1", "1", "0", "7", "_tg", "tg"):
        yield word + s
    for p in ("the", "i", "x", "mr", "its", "im", "my", "_"):
        yield p + word
    if len(word) > 1:
        yield word + word[-1]
        yield word[0] + "_" + word[1:]


def words_stream(words, prefix="", suffix="", variants=False, seen=None):
    seen = seen if seen is not None else set()
    for raw in words:
        base = normalize(raw)
        if not base:
            continue
        candidates = word_variants(base) if variants else [base]
        for c in candidates:
            name = f"{prefix}{c}{suffix}"
            if name in seen or not is_valid(name):
                continue
            seen.add(name)
            yield name


def make_stream(settings, seen=None):
    mode = settings.get("mode", "preset")
    prefix = settings.get("prefix", "")
    suffix = settings.get("suffix", "")
    if mode == "words":
        return words_stream(settings.get("words", []), prefix, suffix,
                            settings.get("word_variants", False), seen)
    if mode == "template":
        templates = [settings.get("template") or "LLLLL"]
    else:
        templates = PRESETS.get(settings.get("preset"), PRESETS["five"])[1]
    return random_stream(templates, prefix, suffix, seen)
