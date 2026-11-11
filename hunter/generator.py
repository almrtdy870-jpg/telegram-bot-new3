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
import itertools
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


# characters that look alike (both directions are generated)
LOOKALIKE = {
    "o": "0", "i": "1l", "l": "1i", "e": "3", "a": "4", "s": "5z", "t": "7",
    "b": "8", "g": "9q", "q": "g", "z": "s2", "u": "v", "v": "uw", "w": "vv",
    "m": "n", "n": "m", "c": "k", "k": "c", "y": "i", "0": "o", "1": "il",
    "3": "e", "4": "a", "5": "s", "7": "t", "8": "b", "9": "g", "2": "z",
}
SIMILAR_SUFFIXES = ("s", "x", "z", "y", "o", "i", "e", "a", "_x", "_z", "_0", "_1",
                    "tg", "_tg", "bot", "vip", "pro", "real", "app", "hq", "me", "official")
SIMILAR_PREFIXES = ("the", "i", "x", "mr", "its", "im", "my", "real", "get", "go", "hey", "a")


def similar_candidates(base: str):
    """Variants of `base`, ordered roughly from most to least similar."""
    n = len(base)
    yield base
    # doubled letter: abcde -> aabcde
    for i in range(n):
        yield base[:i + 1] + base[i] + base[i + 1:]
    # underscore inside: ab_cde
    for i in range(1, n):
        yield base[:i] + "_" + base[i:]
    # look-alike substitutions (single, then all at once)
    for i, ch in enumerate(base):
        for rep in LOOKALIKE.get(ch, ""):
            yield base[:i] + rep + base[i + 1:]
    # swap neighbours
    for i in range(n - 1):
        yield base[:i] + base[i + 1] + base[i] + base[i + 2:]
    # drop one char
    for i in range(n):
        yield base[:i] + base[i + 1:]
    # remove underscores / doubled letters already in the name
    yield base.replace("_", "")
    # common short suffixes / prefixes
    for s in SIMILAR_SUFFIXES:
        yield base + s
    for p in SIMILAR_PREFIXES:
        yield p + base
    # one extra char at the end / start
    for ch in LETTERS + DIGITS:
        yield base + ch
    for ch in LETTERS:
        yield ch + base
    # replace one char with any letter/digit
    for i in range(n):
        for ch in LETTERS + DIGITS:
            yield base[:i] + ch + base[i + 1:]
    # insert one char anywhere
    for i in range(1, n):
        for ch in LETTERS + DIGITS:
            yield base[:i] + ch + base[i:]


def similar_stream(base, prefix="", suffix="", seen=None):
    seen = seen if seen is not None else set()
    base = normalize(base)
    for c in similar_candidates(base):
        name = f"{prefix}{c}{suffix}"
        if name in seen or not is_valid(name):
            continue
        seen.add(name)
        yield name


MAX_REPLACE_POSITIONS = 3  # 26^3 = 17,576 names per rule


def replace_count(rule) -> int:
    return len(LETTERS) ** len(rule.get("pos", []))


def replace_stream(rules, prefix="", suffix="", seen=None):
    """'Change letter on demand': for each rule {base, pos:[1-based]} try every
    letter a-z at the chosen positions, keeping the rest of `base` as is."""
    seen = seen if seen is not None else set()
    for rule in rules:
        base = normalize(rule.get("base", ""))
        pos = sorted({p - 1 for p in rule.get("pos", []) if 1 <= p <= len(base)})
        if not base or not pos:
            continue
        for combo in itertools.product(LETTERS, repeat=len(pos)):
            chars = list(base)
            for p, ch in zip(pos, combo):
                chars[p] = ch
            name = f"{prefix}{''.join(chars)}{suffix}"
            if name in seen or not is_valid(name):
                continue
            seen.add(name)
            yield name


def make_stream(settings, seen=None):
    mode = settings.get("mode", "preset")
    prefix = settings.get("prefix", "")
    suffix = settings.get("suffix", "")
    if mode == "replace":
        # fresh `seen` so re-running the same rules re-checks everything
        return replace_stream(settings.get("replace_rules", []), prefix, suffix)
    if mode == "similar":
        # fresh `seen` so a repeated search re-checks everything
        return similar_stream(settings.get("similar_base", ""), prefix, suffix)
    if mode == "words":
        return words_stream(settings.get("words", []), prefix, suffix,
                            settings.get("word_variants", False), seen)
    if mode == "template":
        templates = [settings.get("template") or "LLLLL"]
    else:
        templates = PRESETS.get(settings.get("preset"), PRESETS["five"])[1]
    return random_stream(templates, prefix, suffix, seen)
