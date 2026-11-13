"""Auction trends from Fragment: which usernames sell / get bids for the most.

Fragment has no public API, so the public listing pages are parsed. The page
layout is not guaranteed, so parsing is deliberately tolerant: every link to
/username/<name> is collected in page order, with the first TON amount found
in the same table row when there is one.

The names themselves are sold on Fragment and can't be claimed for free; they
are used as bases for change-letter formulas (name + lucky digits / letters).
"""
import re

import requests

from .checker import _session
from .generator import parse_rule_line
from .rules import normalize

PAGES = {
    "sold": "https://fragment.com/?sort=price_desc&filter=sold",
    "auction": "https://fragment.com/?sort=price_desc&filter=auction",
    "sale": "https://fragment.com/?sort=price_desc&filter=sale",
}
KIND_LABEL = {"sold": "انباع", "auction": "مزايدة شغّالة", "sale": "معروض للبيع"}

_ROW = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_NAME = re.compile(r'href="/username/([a-z0-9_]{3,32})"', re.I)
_PRICE = re.compile(r'icon-ton[^>]*>\s*([\d][\d,\.\s]*)', re.I)

# never hunt around brands (Telegram can take them back) or shady topics
BLOCKED = set("""
telegram durov fragment toncoin apple iphone google gmail android nike adidas puma gucci dior armani
prada chanel hermes rolex cartier versace zara ikea facebook instagram whatsapp messenger meta tiktok
youtube twitter netflix spotify amazon microsoft windows xbox playstation sony samsung huawei xiaomi
nokia tesla spacex bmw mercedes audi toyota honda ferrari porsche lamborghini binance coinbase okx
bybit kucoin huobi bitget tether paypal visa mastercard stripe openai chatgpt claude anthropic
nvidia intel oracle ibm adobe disney marvel pepsi cocacola mcdonalds starbucks uber airbnb booking
alibaba aliexpress tencent wechat baidu jpmorgan goldman citibank hsbc sber polymarket fifa uefa nba
nfl potus whitehouse fbi cia nasa interpol
casino bet bets betting poker slots gambling porn sex xxx escort drugs weed cocaine hack hacker
carding bocai paofen chengdui dubo
""".split())


def _price(text):
    try:
        return int(float(text.replace(",", "").replace(" ", "")))
    except ValueError:
        return 0


def parse_listing(html: str, kind: str):
    out, seen = [], set()
    rows = _ROW.findall(html) or [html]
    for row in rows:
        names = _NAME.findall(row)
        if not names:
            continue
        m = _PRICE.search(row)
        price = _price(m.group(1)) if m else 0
        # a real row has one username; the no-table fallback has many
        for name in (names[:1] if len(rows) > 1 else names):
            name = name.lower()
            if name not in seen:
                seen.add(name)
                out.append({"name": name, "price": price, "kind": kind})
    return out


def fetch_trends(timeout=20):
    """Return (items, error). items are sorted by price, best first."""
    items, errors, seen = [], [], set()
    for kind, url in PAGES.items():
        try:
            r = _session().get(url, timeout=timeout)
        except requests.RequestException as e:
            errors.append(f"{kind}: {type(e).__name__}")
            continue
        if r.status_code != 200:
            errors.append(f"{kind}: HTTP {r.status_code}")
            continue
        found = parse_listing(r.text, kind)
        if not found:
            errors.append(f"{kind}: ما لگيت يوزرات بالصفحة (ممكن تغيّر شكلها)")
        for it in found:
            if it["name"] not in seen:
                seen.add(it["name"])
                items.append(it)
    items.sort(key=lambda x: -x["price"])
    return items, "؛ ".join(errors)


def usable(name: str) -> bool:
    name = normalize(name)
    return (3 <= len(name) <= 20 and name.isascii() and name.replace("_", "").isalnum()
            and name[0].isalpha() and name not in BLOCKED)


def formulas_for(word: str):
    """Change-letter rules around a strong word: lucky digits and clean letters."""
    w = normalize(word)
    if len(w) >= 4:
        # one extra character only: a single lucky digit
        lines = [f"{w}* 1235689"]
    else:
        # 3-letter words need two characters to reach Telegram's 5-char minimum
        lines = [f"{w}** 689"]
    rules = []
    for line in lines:
        rule, _err = parse_rule_line(line)
        if rule:
            rule["src"] = "trend"
            rules.append(rule)
    return rules
