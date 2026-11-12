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
import time

# A method whose whole round is tiny (e.g. one word) would otherwise re-check the
# same names many times a second. Rounds are spaced at least this far apart;
# meanwhile the stream yields IDLE so the workers can do other methods.
MIN_ROUND_SECONDS = 120
IDLE = ""

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


REPLACE_SETS = {
    "letters": ("كل الحروف a-z", LETTERS),
    "digits": ("أرقام 0-9", DIGITS),
    "alnum": ("حروف + أرقام", LETTERS + DIGITS),
    "vowels": ("حروف العلة aeiou", VOWELS),
    "consonants": ("الحروف الساكنة", CONSONANTS),
}


def clean_chars(text: str) -> str:
    """Keep allowed username chars, de-duplicated, in the order typed."""
    out = []
    for ch in (text or "").lower():
        if ch in LITERALS and ch not in out:
            out.append(ch)
    return "".join(out)


_SHORT = {LETTERS: "a-z", DIGITS: "0-9", LETTERS + DIGITS: "a-z0-9",
          VOWELS: "aeiou", CONSONANTS: "ساكنة"}


def chars_label(chars: str) -> str:
    if chars in _SHORT:
        return _SHORT[chars]
    return chars if len(chars) <= 10 else chars[:10] + "…"


def rule_sets(rule):
    """Characters to try for each position of the rule (same order as pos).
    `chars` may be one string for every position (old rules) or a list."""
    pos = rule.get("pos", [])
    chars = rule.get("chars") or LETTERS
    if isinstance(chars, str):
        return [chars] * len(pos)
    sets = [c or LETTERS for c in chars]
    return (sets + [sets[-1] if sets else LETTERS] * len(pos))[:len(pos)]


def rule_label(rule) -> str:
    """Short label, e.g. '1←aeo' or '1←abc 3←0-9'."""
    return " ".join(f"{p}←{chars_label(s)}" for p, s in zip(rule.get("pos", []), rule_sets(rule)))


def replace_count(rule) -> int:
    n = 1
    for s in rule_sets(rule):
        n *= len(s)
    return n


def replace_stream(rules, prefix="", suffix="", seen=None, shuffle=True):
    """'Change letter on demand': for each rule {base, pos:[1-based]} try every
    letter a-z at the chosen positions, keeping the rest of `base` as is."""
    seen = seen if seen is not None else set()
    names = []
    for rule in rules:
        base = normalize(rule.get("base", ""))
        pairs = [(p - 1, s) for p, s in zip(rule.get("pos", []), rule_sets(rule))
                 if 1 <= p <= len(base)]
        if not base or not pairs:
            continue
        pos = [p for p, _ in pairs]
        for combo in itertools.product(*[s for _, s in pairs]):
            chars = list(base)
            for p, ch in zip(pos, combo):
                chars[p] = ch
            name = f"{prefix}{''.join(chars)}{suffix}"
            if name in seen or not is_valid(name):
                continue
            seen.add(name)
            names.append(name)
    if shuffle:
        random.shuffle(names)  # random order, but every combination exactly once
    yield from names


MAX_RULES = 1000
MAX_RULE_NAMES = 50_000
WILDCARDS = "*?"


def masked(rule) -> str:
    """Formula with * at the changing positions, e.g. boss**."""
    pos = set(rule.get("pos", []))
    return "".join("*" if i + 1 in pos else ch for i, ch in enumerate(rule.get("base", "")))


def rule_key(rule):
    return (masked(rule), tuple(rule_sets(rule)))


def _named_set(token):
    t = token.lower()
    return {"a-z": LETTERS, "0-9": DIGITS, "a-z0-9": LETTERS + DIGITS, "0-9a-z": LETTERS + DIGITS,
            "aeiou": VOWELS}.get(t) or clean_chars(t)


def parse_rule_line(line):
    """Parse one bulk line into a rule. Returns (rule, None) or (None, error).

    boss**            -> the * places get a-z
    boss** abc        -> both places get 'abc'
    boss** abc 0-9    -> first * gets abc, second gets 0-9
    bosspo 5,6 [sets] -> same, using positions instead of *
    """
    line = "".join(str(int(c)) if c.isdigit() else c for c in line.strip())
    parts = line.replace("،", ",").split()
    if not parts:
        return None, None
    word, rest = parts[0].lower().lstrip("@"), parts[1:]
    if any(ch in WILDCARDS for ch in word):
        pos = [i + 1 for i, ch in enumerate(word) if ch in WILDCARDS]
        base = "".join("a" if ch in WILDCARDS else ch for ch in word)
    else:
        if not rest or not all(c.isdigit() or c == "," for c in rest[0]):
            return None, "حط * مكان الحرف اللي يتغيّر (مثل boss**) أو اكتب الأماكن (مثل bosspo 5,6)"
        pos = sorted({int(x) for x in rest[0].split(",") if x})
        base, rest = word, rest[1:]
    if not (5 <= len(base) <= 32) or not base.isascii() or not all(c.isalnum() or c == "_" for c in base):
        return None, "الصيغة لازم 5-32 حرف إنجليزي/رقم/_"
    if not pos or any(not 1 <= p <= len(base) for p in pos):
        return None, "الأماكن غلط"
    if len(pos) > MAX_REPLACE_POSITIONS:
        return None, f"أقصى شي {MAX_REPLACE_POSITIONS} أماكن"
    if not rest:
        sets = [LETTERS] * len(pos)
    elif len(rest) == 1:
        sets = [_named_set(rest[0])] * len(pos)
    elif len(rest) == len(pos):
        sets = [_named_set(t) for t in rest]
    else:
        return None, "عدد مجموعات الحروف لازم 1 أو بعدد الأماكن"
    if any(not s for s in sets):
        return None, "الحروف غلط"
    rule = {"base": base, "pos": pos, "chars": sets}
    if replace_count(rule) > MAX_RULE_NAMES:
        return None, f"تطلع أكثر من {MAX_RULE_NAMES:,} يوزر"
    return rule, None


def replace_cycle(settings, on_round=None):
    """Endless 'change letter' hunt over ALL rules at once, in random order.

    Rules added or deleted while hunting are picked up immediately (the bot
    bumps settings['replace_rev']). When every name was checked, a new round
    starts from the beginning."""
    rnd, queue, loaded, seen = 1, [], set(), set()
    rev = object()
    fresh_round = True  # nothing yielded yet in the current round
    round_started = time.time()
    while True:
        cur = settings.get("replace_rev", 0)
        if cur != rev:
            rev = cur
            prefix, suffix = settings.get("prefix", ""), settings.get("suffix", "")
            rules = {rule_key(r): r for r in settings.get("replace_rules", [])}
            if loaded - rules.keys():
                queue = [(k, n) for k, n in queue if k in rules]
                loaded &= rules.keys()
            added = False
            for k, r in rules.items():
                if k not in loaded:
                    loaded.add(k)
                    queue.extend((k, n) for n in replace_stream([r], prefix, suffix, shuffle=False))
                    added = True
            if added:
                random.shuffle(queue)
        if not queue:
            if fresh_round or not settings.get("replace_rules"):
                return  # nothing (left) to check
            if time.time() - round_started < MIN_ROUND_SECONDS:
                yield IDLE  # small round: wait before repeating it
                continue
            round_started = time.time()
            rnd += 1
            seen.clear()
            loaded = set()
            rev = object()  # force a full reload for the new round
            fresh_round = True
            if on_round:
                on_round(rnd)
            continue
        fresh_round = False
        _k, name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        yield name


# ---------------------------------------------------------------- multi-source hunting
def source_label(settings, key) -> str:
    if key.startswith("preset:"):
        return PRESETS.get(key[7:], ("?",))[0]
    if key == "template":
        tpls = (settings.get("template") or "").split()
        return f"قالب {tpls[0]}" if len(tpls) == 1 else f"{len(tpls)} قوالب"
    if key == "similar":
        return f"مشابه @{settings.get('similar_base', '')}"
    if key == "words":
        return f"كلمات ({len(settings.get('words', []))})"
    if key == "replace":
        return f"تغيير حرف ({len(settings.get('replace_rules', []))} صيغة)"
    return key


def _source_once(settings, key, on_round):
    prefix, suffix = settings.get("prefix", ""), settings.get("suffix", "")
    if key.startswith("preset:"):
        return random_stream(PRESETS.get(key[7:], PRESETS["five"])[1], prefix, suffix)
    if key == "template":
        return random_stream((settings.get("template") or "LLLLL").split(), prefix, suffix)
    if key == "similar":
        return similar_stream(settings.get("similar_base", ""), prefix, suffix)
    if key == "words":
        return words_stream(settings.get("words", []), prefix, suffix, settings.get("word_variants", False))
    if key == "replace":
        return replace_cycle(settings, lambda r: on_round and on_round(source_label(settings, key), r))
    return iter(())


def source_cycle(settings, key, on_round=None):
    """One hunting method, endlessly: when it runs out it starts a new round."""
    rnd = 1
    while True:
        produced = False
        started = time.time()
        for name in _source_once(settings, key, on_round):
            produced = True
            yield name
        if not produced:
            return  # nothing to hunt (e.g. empty word list)
        while time.time() - started < MIN_ROUND_SECONDS:
            yield IDLE  # small round: wait before repeating it
        rnd += 1
        if on_round:
            on_round(source_label(settings, key), rnd)


def multi_stream(settings, on_round=None):
    """Hunt every selected method together (round-robin). Methods selected or
    removed while hunting are applied immediately (settings['sources_rev'])."""
    active, rev = {}, object()
    while True:
        cur = settings.get("sources_rev", 0)
        if cur != rev:
            rev = cur
            keys = list(settings.get("sources", []))
            for k in list(active):
                if k not in keys:
                    del active[k]
            for k in keys:
                if k not in active:
                    active[k] = source_cycle(settings, k, on_round)
        if not active:
            return
        for k in list(active):
            try:
                yield next(active[k])
            except StopIteration:
                del active[k]
            if settings.get("sources_rev", 0) != rev:
                break


def make_stream(settings, seen=None, on_round=None):
    if settings.get("sources"):
        return multi_stream(settings, on_round)
    mode = settings.get("mode", "preset")
    prefix = settings.get("prefix", "")
    suffix = settings.get("suffix", "")
    if mode == "replace":
        return replace_cycle(settings, on_round)
    if mode == "similar":
        # fresh `seen` so a repeated search re-checks everything
        return similar_stream(settings.get("similar_base", ""), prefix, suffix)
    if mode == "words":
        return words_stream(settings.get("words", []), prefix, suffix,
                            settings.get("word_variants", False), seen)
    if mode == "template":
        # several templates may be stored separated by spaces: hunt them together
        templates = (settings.get("template") or "LLLLL").split() or ["LLLLL"]
    else:
        templates = PRESETS.get(settings.get("preset"), PRESETS["five"])[1]
    return random_stream(templates, prefix, suffix, seen)
