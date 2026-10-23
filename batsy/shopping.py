"""Shopping: find a product and compare its price on every store that sells it.

"How much is the ESP32 DevKit V1" / "compare prices for a Raspberry Pi 5 8GB" / "where can I buy PETG filament"
-> the same product looked up on Amazon, Flipkart, Robu, Robocraze, Croma, Reliance Digital... at once, the
cheapest said out loud and the full comparison (price, listing name, stock, link) on screen and in a page you can
open. "Find me a 55 inch TV under 50000" / "best microcontroller for a drone" (no exact model) -> Claude picks a
few current models that fit, then each is priced across the stores.

How prices are read, most reliable first (no store has an API for this):
- Amazon and Flipkart: their own search results (Flipkart's are embedded as JSON in the page).
- Shopify stores (Robocraze...): their public product search JSON.
- Everything else: the store's own product page for this item, found with a site: web search, read through the
  schema.org Product data shops publish for Google (JSON-LD), or the price meta tags.
- Stores whose pages only fill in prices with JavaScript (Croma, Tata Cliq...): the price in the search result
  snippet if there is one ("approx."), otherwise just the link.
Prices are as listed, before delivery charges. Nothing here costs Claude tokens except picking models for a vague
request.
"""
import datetime as dt
import html as htmllib
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from batsy.config import WORKSPACE

log = logging.getLogger("batsy.shopping")
OUT_DIR = WORKSPACE / "shopping"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"


class Store:
    def __init__(self, key: str, name: str, domain: str, kind: str = "page"):
        self.key, self.name, self.domain, self.kind = key, name, domain, kind  # kind: amazon|flipkart|shopify|page|js


STORES = {s.key: s for s in [
    Store("amazon", "Amazon", "amazon.in", "amazon"),
    Store("flipkart", "Flipkart", "flipkart.com", "flipkart"),
    Store("robu", "Robu", "robu.in"),
    Store("robocraze", "Robocraze", "robocraze.com", "shopify"),
    Store("electronicscomp", "ElectronicsComp", "electronicscomp.com"),
    Store("thinkrobotics", "ThinkRobotics", "thinkrobotics.com", "shopify"),
    Store("quartz", "Quartz Components", "quartzcomponents.com", "shopify"),
    Store("evelta", "Evelta", "evelta.com"),
    Store("probots", "Probots", "probots.co.in"),
    Store("croma", "Croma", "croma.com", "js"),
    Store("reliance", "Reliance Digital", "reliancedigital.in", "js"),
    Store("vijaysales", "Vijay Sales", "vijaysales.com"),
    Store("tatacliq", "Tata CLiQ", "tatacliq.com", "js"),
    Store("jiomart", "JioMart", "jiomart.com", "js"),
    Store("myntra", "Myntra", "myntra.com", "js"),
    Store("ajio", "AJIO", "ajio.com", "js"),
    Store("nykaa", "Nykaa", "nykaa.com", "js"),
    Store("meesho", "Meesho", "meesho.com", "js"),
]}
GROUPS = {  # which stores to ask, by what kind of thing it is
    "maker": ["amazon", "flipkart", "robu", "robocraze", "electronicscomp", "thinkrobotics", "quartz", "evelta"],
    "gadget": ["amazon", "flipkart", "croma", "reliance", "vijaysales", "tatacliq", "jiomart"],
    "fashion": ["amazon", "flipkart", "myntra", "ajio", "meesho"],
    "beauty": ["amazon", "flipkart", "nykaa", "tatacliq"],
    "general": ["amazon", "flipkart", "jiomart", "tatacliq", "meesho"],
}
MAKER = re.compile(r"\b(arduino|esp32|esp8266|nodemcu|raspberry|pico|stm32|microcontroller|mcu|sensor|module|servo|"
                   r"stepper|motor driver|motors?|relay|breadboard|jumper|resistor|capacitor|transistor|mosfet|ic|"
                   r"pcb|solder\w*|multimeter|oscilloscope|lipo|18650|bms|buck|boost converter|lidar|ultrasonic|"
                   r"filament|pla|petg|abs|tpu|nozzle|hotend|extruder|3d printer|ender|bambu|prusa|drone|propeller|"
                   r"esc|flight controller|gps module|lora|zigbee|oled|lcd|tft|display module|keypad|encoder|"
                   r"gearbox|bearing|robot|robotics|kit|dev ?board|development board|jetson|cnc|laser module)\b")
GADGET = re.compile(r"\b(\d+ ?inch|4k|8k|oled|qled|bravia|tv|television|laptop|macbook|notebook|phone|smartphone|iphone|pixel|galaxy|oneplus|"
                    r"tablet|ipad|monitor|earbuds|earphones|headphones|headset|speaker|soundbar|smartwatch|watch|"
                    r"camera|dslr|mirrorless|gopro|printer|router|ssd|hdd|hard disk|pendrive|keyboard|mouse|"
                    r"gpu|graphics card|processor|cpu|ram|motherboard|console|ps5|xbox|fridge|refrigerator|"
                    r"washing machine|ac|air conditioner|microwave|oven|purifier|vacuum|geyser|fan|mixer|"
                    r"projector|kindle|power bank|charger)\b")
FASHION = re.compile(r"\b(shirt|t-?shirt|jeans|trousers|shoes|sneakers|dress|kurta|saree|jacket|hoodie|bag|"
                     r"backpack|wallet|sunglasses|sandals|slippers)\b")
BEAUTY = re.compile(r"\b(shampoo|serum|moisturi[sz]er|sunscreen|perfume|deodorant|lipstick|face wash|trimmer|razor)\b")


def group_for(query: str) -> str:
    q = query.lower()
    for name, rx in (("maker", MAKER), ("gadget", GADGET), ("fashion", FASHION), ("beauty", BEAUTY)):
        if rx.search(q):
            return name
    return "general"


# --- fetching -------------------------------------------------------------------------------------------------
def fetch(url: str, timeout: int = 12) -> str:
    """A page as text. Uses a real browser's network fingerprint when curl_cffi is installed: several stores turn
    away anything that doesn't look like a browser."""
    try:
        from curl_cffi import requests as cr

        r = cr.get(url, impersonate="chrome", timeout=timeout, headers={"Accept-Language": "en-IN,en;q=0.9"})
        if r.status_code >= 400:
            raise OSError(f"HTTP {r.status_code}")
        return r.text
    except ImportError:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9",
                                                   "Accept": "text/html,application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")


def web_search(query: str, n: int = 4) -> list[dict]:
    try:
        from ddgs import DDGS

        return DDGS().text(query, max_results=n, region="in-en") or []
    except Exception as e:
        log.info("search %r failed: %s", query, e)
        return []


def money(s) -> float | None:
    if s is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s))
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return v if 1 <= v <= 10_000_000 else None


RUPEES = re.compile(r"(?:₹|Rs\.?|INR)\s?(\d[\d,]*(?:\.\d{1,2})?)")


# --- matching -------------------------------------------------------------------------------------------------
STOP = set("a an the for with and of in on to buy online price best cheap cheapest india original new pack "
           "piece pcs set genuine latest".split())
UNIT = r"gb|tb|mb|inch|inches|in|mm|cm|kg|g|w|mah|v|hz|pin|pins|mp|l|litre|liter|ton|star"
ACCESSORY = re.compile(r"\b(case|cover|power supply|adapter|adaptor|charger|cable|backboard|expansion|shield|hat|"
                       r"mount|stand|holder|bracket|remote|screen guard|protector|tempered|sleeve|skin|heatsink|"
                       r"fan|enclosure|strap|replacement|spare|refill|nozzle|for raspberry|for arduino|compatible "
                       r"with|compatible for)\b")
BUNDLE = re.compile(r"\b(kit|combo|bundle|starter|pack of [2-9]|set of [2-9])\b")
SOFT = {"board", "module", "dev", "development", "inch", "cm", "smart", "led", "ultra", "hd", "tv", "model",
        "edition", "version", "wireless", "bluetooth", "wifi", "black", "white", "colour", "color", "original",
        "official", "filament", "printer", "3d", "earbuds", "tws", "headphones", "laptop", "phone", "mobile"}
VARIANT = re.compile(r"^[a-z]{1,2}\d{1,2}[a-z]?$")  # chip/model variants: c3, s3, c5, h2, m1...


def tokens(text: str) -> list[str]:
    t = re.sub(rf"(\d)\s*({UNIT})\b", r"\1\2", (text or "").lower())
    t = re.sub(r"(\d+)inches\b", r"\1inch", t)
    return [w for w in re.findall(r"[a-z0-9+]+", t) if w not in STOP]


MODEL_WORDS = {"plus", "pro", "max", "ultra", "mini", "lite", "fe", "neo", "air", "prime", "se", "edge", "fold", "flip"}


def _spans(w: str, toks: list[str]) -> bool:
    """'devkit' written as 'dev kit', 'esp32' as 'esp 32': consecutive tokens that spell it exactly."""
    for i in range(len(toks)):
        acc = ""
        for x in toks[i:i + 3]:
            acc += x
            if acc == w:
                return True
            if not w.startswith(acc):
                break
    return False


def _family(tok: str) -> str:
    """'8gb' -> 'gb', '55inch' -> 'inch', 'v1' -> 'v', '5' -> '#': tokens that must agree if both mention one."""
    if re.fullmatch(r"\d+(\.\d+)?", tok):
        return "#"
    m = re.fullmatch(rf"\d+(?:\.\d+)?({UNIT})", tok)
    if m:
        return m.group(1)
    m = re.fullmatch(r"(v)\d+", tok)
    return m.group(1) if m else ""


def match(query: str, title: str) -> float:
    """0..1: is this listing the product asked for? Every number/model token must be there; a different variant
    (4GB for 8GB, V2 for V1, ESP32-C5 for ESP32), an accessory, or a bundle you didn't ask for rules it out."""
    q, t_list = tokens(query), tokens(title)
    t = set(t_list)
    if not q:
        return 0.0
    ql = query.lower()
    if ACCESSORY.search(title.lower()) and not ACCESSORY.search(ql):
        return 0.0
    if re.search(r"\b(?:for|compatible with|suitable for|fits)\s+(?:[\w-]+\s+){0,2}" + re.escape(q[0]) + r"\b",
                 re.sub(r"[^a-z0-9+ ]", " ", title.lower())) and not re.search(r"\bfor\b", ql):
        return 0.0  # "relay board for ESP32": made for it, not it
    for i, x in enumerate(t_list[:-1]):  # "iPhone 16 Plus", "Galaxy S24 Ultra", "Pi 5 Pro": a different model
        nxt = t_list[i + 1]
        if x in q and nxt in MODEL_WORDS and nxt not in q and (i + 2 >= len(t_list) or t_list[i + 2] != "hd"):
            return 0.0
    total = got = 0.0
    missing_core = False
    for w in q:
        digit = bool(re.search(r"\d", w))
        weight = 0.5 if re.fullmatch(r"v\d+", w) else 2.0 if digit else 0.5 if w in SOFT else 1.0
        total += weight
        fam = _family(w)
        if w in t or (len(w) >= 4 and _spans(w, t_list)):
            got += weight
            continue
        if fam and fam != "v" and any(_family(x) == fam for x in t):
            return 0.0  # it says 4gb where you asked for 8gb, 43inch where you said 55
        if digit and fam not in ("v", "#"):
            return 0.0  # a model number that isn't in the listing at all
        if fam == "v" and any(_family(x) == "v" for x in t):
            return 0.0
        if not digit and w not in SOFT:
            missing_core = True  # "petg" not in a PLA+ listing: some other product
        if fam == "#" and any(_family(x) == "#" and x != w for x in t_list[:8]):
            return 0.0  # "pi 4" when you asked for "pi 5" (numbers near the start of the name)
    if any(VARIANT.fullmatch(x) and x not in q for x in t_list[:10]) and not any(VARIANT.fullmatch(x) for x in q):
        got *= 0.6  # "ESP32-C5 DevKit": a different chip than plain ESP32
    s = got / total * (0.6 if missing_core else 1.0)
    if BUNDLE.search(title.lower()) and not BUNDLE.search(ql):
        s -= 0.25  # a starter kit costs more than the board: only if nothing else matches
    return max(0.0, s)


# --- per-store lookups ------------------------------------------------------------------------------------------
def _amazon(store: Store, query: str) -> list[dict]:
    page = fetch("https://www.amazon.in/s?k=" + urllib.parse.quote_plus(query))
    out = []
    for block in page.split('data-component-type="s-search-result"')[1:12]:
        # the brand sits in its own heading above the product name on some layouts: the longest heading is the name
        heads = [htmllib.unescape(re.sub(r"<[^>]+>", " ", h)).strip()
                 for h in re.findall(r"<h2[^>]*>(.*?)</h2>", block, re.S)]
        heads += [htmllib.unescape(a) for a in re.findall(r'<h2[^>]*aria-label="([^"]+)"', block)]
        heads = [h for h in dict.fromkeys(heads) if h]
        name = max(heads, key=len, default="")
        brand = next((h for h in heads if h != name and len(h) < 40 and h.lower() not in name.lower()), "")
        title = f"{brand} {name}".strip()
        price = re.search(r'a-price-whole">([\d,]+)', block)
        asin = re.search(r'data-asin="(\w{10})"', block) or re.search(r"/dp/(\w{10})", block)
        if not (title and price and asin):
            continue
        out.append({"title": re.sub(r"\s+", " ", re.sub(r"^Sponsored Ad - ", "", title)),
                    "price": money(price.group(1)), "url": f"https://www.amazon.in/dp/{asin.group(1)}",
                    "sponsored": "Sponsored" in block[:3000], "how": "listing"})
    return out


def _flipkart(store: Store, query: str) -> list[dict]:
    page = fetch("https://www.flipkart.com/search?q=" + urllib.parse.quote_plus(query))
    i = page.find("window.__INITIAL_STATE__")
    if i < 0:
        return []
    raw = page[i:]
    obj, _ = json.JSONDecoder().raw_decode(raw[raw.find("{"):])
    out = []

    def walk(d):
        if isinstance(d, dict):
            if isinstance(d.get("pricing"), dict) and isinstance(d.get("titles"), dict):
                prices = [p.get("value") for p in d["pricing"].get("prices") or [] if not p.get("strikeOff")]
                title = d["titles"].get("title") or ""
                url = d.get("smartUrl") or d.get("baseUrl") or ""
                if prices and title:
                    out.append({"title": title + (f" ({d['titles'].get('subtitle')})" if d["titles"].get("subtitle") else ""),
                                "price": money(prices[0]), "how": "listing",
                                "url": url if url.startswith("http") else "https://www.flipkart.com" + url.split("?")[0]})
                return
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)

    walk(obj)
    return out[:15]


def _shopify(store: Store, query: str) -> list[dict]:
    data = json.loads(fetch(f"https://{store.domain}/search/suggest.json?q={urllib.parse.quote(query)}"
                            "&resources[type]=product&resources[limit]=8"))
    out = []
    for p in data.get("resources", {}).get("results", {}).get("products", []):
        url = p.get("url", "")
        out.append({"title": p.get("title", ""), "price": money(p.get("price")), "how": "store search",
                    "stock": p.get("available"), "url": f"https://{store.domain}{url.split('?')[0]}" if url.startswith("/") else url})
    return out


def page_offer(url: str) -> dict | None:
    """Price, name and stock from a product page's schema.org data (or price meta tags)."""
    page = fetch(url)
    best = None
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack += d
                continue
            if not isinstance(d, dict):
                continue
            types = d.get("@type") if isinstance(d.get("@type"), list) else [d.get("@type")]
            if "Product" in types:
                offers = d.get("offers") or {}
                offers = offers[0] if isinstance(offers, list) and offers else offers
                if isinstance(offers, dict):
                    price = money(offers.get("price") or offers.get("lowPrice") or
                                  (offers.get("priceSpecification") or {}).get("price"))
                    if price:
                        stock = offers.get("availability")
                        best = {"title": htmllib.unescape(str(d.get("name") or "")), "price": price, "how": "store page",
                                "stock": None if stock is None else "InStock" in str(stock), "url": url}
                        break
            stack += [v for v in d.values() if isinstance(v, (dict, list))]
        if best:
            break
    if not best:
        meta = re.search(r'(?:product:price:amount|og:price:amount)"\s+content="([\d.,]+)"', page) or \
            re.search(r'itemprop="price"[^>]*content="([\d.,]+)"', page)
        title = re.search(r'og:title"\s+content="([^"]+)"', page) or re.search(r"<title[^>]*>(.*?)</title>", page, re.S)
        if meta:
            best = {"title": htmllib.unescape(title.group(1).strip()) if title else "", "price": money(meta.group(1)),
                    "how": "store page", "url": url}
    if best and "amazon." in url and not best.get("price"):
        best = None
    if not best and "amazon." in url:  # Amazon product pages: the buy-box price
        core = page.find("corePrice")
        m = re.search(r'a-price-whole">([\d,]+)', page[core:] if core >= 0 else page)
        title = re.search(r'id="productTitle"[^>]*>(.*?)</span>', page, re.S)
        if m:
            best = {"title": htmllib.unescape(title.group(1).strip()) if title else "", "price": money(m.group(1)),
                    "how": "store page", "url": url}
    return best


PRODUCT_URL = re.compile(r"/(?:product|products|p|dp|item|buy|pd)/|/p/itm|-p-\d|\.html?$|/\d{5,}")


def _by_search(store: Store, query: str) -> list[dict]:
    """Find this store's own page for the product with a site: search, then read the price off it."""
    hits = [h for h in web_search(f"{query} site:{store.domain}", 5) if store.domain in h.get("href", "")]
    hits = [h for h in hits if PRODUCT_URL.search(urllib.parse.urlparse(h["href"]).path) or store.kind == "js"]
    hits.sort(key=lambda h: match(query, h.get("title", "") + " " + h.get("href", "")), reverse=True)
    out = []
    for h in hits[:2]:
        offer = None
        if store.kind != "js":
            try:
                offer = page_offer(h["href"])
            except Exception as e:
                log.info("%s page %s: %s", store.name, h["href"], e)
        if not offer:  # a price in the search snippet, or at least the link
            snippet = RUPEES.search(h.get("body", "") + " " + h.get("title", ""))
            offer = {"title": re.sub(r"\s*[-|:]\s*(?:buy|online|" + re.escape(store.name) + r").*$", "",
                                     h.get("title", ""), flags=re.I).strip(),
                     "price": money(snippet.group(1)) if snippet else None,
                     "how": "search snippet" if snippet else "link only", "url": h["href"]}
        if not offer.get("title"):
            offer["title"] = h.get("title", "")
        out.append(offer)
    return out


def lookup(store: Store, query: str) -> dict | None:
    """The best listing for `query` at one store: {store, title, price, url, stock, how, score} or None."""
    try:
        if store.kind == "amazon":
            found = _amazon(store, query)
        elif store.kind == "flipkart":
            found = _flipkart(store, query)
        elif store.kind == "shopify":
            try:
                found = _shopify(store, query)
            except Exception:
                found = []
            found = found or _by_search(store, query)
        else:
            found = _by_search(store, query)
    except Exception as e:
        log.info("%s lookup failed: %s", store.name, e)
        return None
    best = _best(query, found)
    if best is None and store.kind == "shopify":  # its quick search is loose: try the store's own product pages
        try:
            best = _best(query, _by_search(store, query))
        except Exception as e:
            log.info("%s lookup failed: %s", store.name, e)
    if best is None:
        return None
    best["store"] = store.name
    return best


def _best(query: str, found: list[dict]) -> dict | None:
    best = None
    for o in found:
        o["score"] = match(query, o.get("title", ""))
        if o["score"] < 0.7:
            continue
        if o.get("sponsored"):
            o["score"] -= 0.05
        if best is None or (o["score"], -(o.get("price") or 1e12)) > (best["score"], -(best.get("price") or 1e12)) \
                and (o["score"] >= best["score"] - 0.1):
            best = o
    return best


def compare(query: str, stores: list[str] | None = None, max_price: float | None = None) -> list[dict]:
    """Every store's best listing for this product, cheapest first (those without a readable price last)."""
    keys = stores or GROUPS[group_for(query)]
    with ThreadPoolExecutor(len(keys)) as pool:
        results = list(pool.map(lambda k: lookup(STORES[k], query), keys))
    offers = [o for o in results if o]
    if max_price:
        offers = [o for o in offers if not o.get("price") or o["price"] <= max_price * 1.1]
    # in stock and priced first, cheapest first; out of stock after; no readable price last
    return sorted(offers, key=lambda o: (o.get("price") is None, o.get("stock") is False, o.get("price") or 0))


# --- vague requests: which products? --------------------------------------------------------------------------------
PICK_BRIEF = """The user, in India, asked a shopping assistant: "{request}"
Suggest {n} specific products currently on sale in India that fit (use web search for what's current; respect \
any budget). Use the exact model names stores list them under. Reply with JSON only, no prose:
[{{"name": "brand and exact model", "why": "one short reason, under 12 words, no prices"}}]"""


def pick_models(request: str, n: int = 4) -> list[dict]:
    from batsy import claude_code

    reply = claude_code.run(PICK_BRIEF.format(request=request, n=n), "shopping: " + request[:30], timeout=150,
                            args=["--allowedTools", "WebSearch"])
    m = re.search(r"\[.*\]", reply, re.S)
    try:
        items = json.loads(m.group(0)) if m else []
    except ValueError:
        items = []
    return [i for i in items if isinstance(i, dict) and i.get("name")][:n]


def budget(text: str) -> float | None:
    """'under 50k' / 'below 2,000 rupees' / 'within 40 thousand' -> 50000 / 2000 / 40000"""
    m = re.search(r"\b(?:under|below|within|less than|upto|up to|max(?:imum)?|budget(?: of)?|around)\s+(?:rs\.?\s*|₹\s*)?"
                  r"(\d[\d,.]*)\s*(k|thousand|lakh|lakhs|l)?\b", text.lower())
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    mult = {"k": 1e3, "thousand": 1e3, "lakh": 1e5, "lakhs": 1e5, "l": 1e5}.get(m.group(2) or "", 1)
    return v * mult


VAGUE = re.compile(r"\b(best|good|cheap|budget|under|below|within|options?|recommend|suggest|which|some|any|"
                   r"for (?:a|my|the)|decent|top)\b")


SPEC = re.compile(r"^(?:\d+(?:\.\d+)?(?:gb|tb|mb|inch|in|kg|g|mm|cm|w|mah|hz|k|l|mp|v|ton|star)|3d|2d|[2-9]k|[3-6]g|"
                  r"usb\d|wifi\d|ddr\d|hdmi\d?|ip\d\d|1080p|720p|4k|8k)$")


def is_specific(query: str) -> bool:
    """'esp32 devkit v1', 'sony bravia x75l', 'iphone 16 128gb': a product to price. 'a 55 inch tv under 50000',
    'best earbuds for gym', 'some microcontroller for a drone': a choice to make first."""
    q = query.lower()
    toks = tokens(q)
    strong = [w for w in toks if re.search(r"\d", w) and re.search(r"[a-z]", w) and not SPEC.fullmatch(w)]
    vague = VAGUE.search(q) or re.match(r"^(?:a|an|some|any|good|new)\s", q)
    if strong:
        return True  # a model number: x75l, s24, esp32, rtx4060
    if vague:
        return False
    return bool(re.search(r"\b\d+\b", q)) and len(toks) >= 2 or len(toks) >= 3


# --- the report -----------------------------------------------------------------------------------------------------
def rupees(v: float | None) -> str:
    if v is None:
        return "—"
    s = f"{v:,.0f}" if v >= 100 else f"{v:,.2f}".rstrip("0").rstrip(".")
    if v >= 100000:  # Indian grouping: 1,23,456
        whole = f"{int(round(v))}"
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        s = f"{head},{tail}"
    return "₹" + s


def write_report(title: str, sections: list[tuple[str, str, list[dict]]]) -> Path:
    """An HTML page with every listing and link. sections: (heading, note, offers)."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "products"
    path = OUT_DIR / f"{slug}-{dt.date.today().isoformat()}.html"
    rows = []
    for heading, note, offers in sections:
        rows.append(f"<h2>{htmllib.escape(heading)}</h2>" + (f"<p class=note>{htmllib.escape(note)}</p>" if note else ""))
        if not offers:
            rows.append("<p class=none>No store had a matching listing.</p>")
            continue
        cheapest = min((o["price"] for o in offers if o.get("price")), default=None)
        body = "".join(
            f"<tr{' class=best' if o.get('price') and o['price'] == cheapest else ''}><td>{htmllib.escape(o['store'])}</td>"
            f"<td class=p>{rupees(o.get('price'))}{' <small>approx.</small>' if o.get('how') == 'search snippet' else ''}</td>"
            f"<td>{htmllib.escape(o.get('title', '')[:140])}</td>"
            f"<td>{'' if o.get('stock') is None else ('In stock' if o['stock'] else 'Out of stock')}</td>"
            f"<td><a href=\"{htmllib.escape(o['url'])}\">Open</a></td></tr>" for o in offers)
        rows.append(f"<table><tr><th>Store</th><th>Price</th><th>Listing</th><th>Stock</th><th></th></tr>{body}</table>")
    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>{htmllib.escape(title)}: prices</title><style>
:root{{--bg:#0b0d12;--fg:#eee8da;--dim:#8d95a5;--line:#232733;--accent:#ffc20e}}
@media (prefers-color-scheme:light){{:root{{--bg:#fafaf7;--fg:#1d1f24;--dim:#5d6470;--line:#e3e3dd;--accent:#b07d00}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;max-width:1000px;margin:0 auto;padding:24px 16px}}
h1{{font-size:28px;margin:0 0 4px}} h2{{font-size:20px;margin:32px 0 6px}} .sub,.note,.none{{color:var(--dim)}}
table{{width:100%;border-collapse:collapse}} td,th{{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--dim);font-weight:500;font-size:14px}} td.p{{white-space:nowrap;font-variant-numeric:tabular-nums;font-weight:600}}
tr.best td.p{{color:var(--accent)}} a{{color:var(--accent)}} small{{color:var(--dim);font-weight:400}}
@media (max-width:640px){{td:nth-child(4),th:nth-child(4){{display:none}}}}
</style></head><body><h1>{htmllib.escape(title)}</h1>
<p class=sub>Checked {dt.datetime.now().strftime("%-d %b %Y, %-I:%M %p")}. Prices as listed, before delivery; "approx." came from a search
listing, not the store page.</p>{''.join(rows)}</body></html>"""
    path.write_text(page)
    return path


# --- talking --------------------------------------------------------------------------------------------------------
SHOP = [re.compile(p) for p in (
    r"^(?:can you |please )?(?:compare|check) (?:the )?prices? (?:of|for|on) (?:an? |the )?(?P<q>.+)$",
    r"^(?:what(?:'s| is)|whats|find|check|get|tell me|show me) (?:the )?(?:best |lowest |cheapest |current )?"
    r"(?:price|prices|cost) (?:of|for) (?:an? |the )?(?P<q>.+)$",
    r"^how much (?:is|are|does|do|for|would) (?:an? |the |one )?(?P<q>.+?)(?: cost| costs| go for| sell for)?"
    r"(?: online| in india| right now| now)?$",
    r"^where (?:can i|should i|do i|to|could i) (?:buy|get|order) (?:an? |the |some )?(?P<q>.+?)(?: from| online| cheap| cheapest)?$",
    r"^(?:find|search|look) (?:for )?(?:me )?(?:an? |the |some )?(?P<q>.+?) (?:on|in|across|from) (?:amazon|flipkart|robu|"
    r"online|the internet|websites|shopping sites|all (?:the )?sites|every (?:site|website|store)|stores)(?: .*)?$",
    r"^(?:i want to|i wanna|i need to|i'd like to|id like to|help me|i'm looking to|im looking to) (?:buy|get|order|"
    r"shop for|find) (?:an? |some |the |new )?(?P<q>.+)$",
    r"^(?:i'm looking for|im looking for|i am looking for|i need) (?:an? |some |the |new )?(?P<q>.+?(?:under|below|"
    r"within|for less than|budget) .+)$",
    r"^(?:show me|find me|get me|suggest|recommend|search for|look for) (?:some |a few |good |the best |an? )?"
    r"(?P<q>.+?(?:under|below|within|for less than|budget).+)$",
    r"^(?:suggest|recommend) (?:me )?(?:some |a few |good |the best |an? )?(?P<q>.+)$",
    r"^(?:what|which) (?P<q>.+?) (?:should i (?:buy|get)|is the best to buy|are the best to buy|to buy|"
    r"are available(?: online)?|options are there)$",
    r"^(?:best|cheapest|good|budget) (?P<q>.+?) (?:to buy|online|in india|under .+|below .+)$",
    r"^(?:buy|order|shop for) (?:me )?(?:an? |some |the )?(?P<q>.+)$",
)]
NOT_PRODUCT = re.compile(r"^(?:time|it|that|this|ram|memory|battery|disk|storage|space|cpu|data|traffic|"
                         r"claude|my|tokens?)\b|\b(?:left|used|weigh|weight|far|long|times|plus|minus|divided)\b")
OPEN = re.compile(r"^(?:open|show me|take me to|buy|go to) (?:the )?(?P<which>cheapest|first|second|third|"
                  r"[a-z ]+?)(?: one| link| listing| deal| offer| page)?(?: please)?$")
OPEN_REPORT = re.compile(r"^(?:open|show me) (?:the |that )?(?:full )?(?:comparison|price comparison|report|prices|"
                         r"all the prices|table)$")


class ShopTalk:
    """Owned by the app: "how much is X", "compare prices for X", "find me a TV under 50k"."""

    def __init__(self, app):
        self.app, self.cfg = app, app.cfg
        self.last: dict | None = None  # {"offers": [...], "report": path, "t"} for "open the cheapest one"

    def handle(self, n: str, raw: str) -> bool:
        if self.last and time.time() - self.last["t"] < 1800:
            if OPEN_REPORT.match(n):
                self.app.open_result(str(self.last["report"]))
                self.app._reply("Opening the comparison.")
                return True
            m = OPEN.match(n)
            if m and self._open(m.group("which").strip()):
                return True
        for rx in SHOP:
            m = rx.match(n)
            if m:
                q = re.sub(r"\b(?:please|for me|right now|online|in india)\b", "", m.group("q")).strip()
                if len(q) < 2 or NOT_PRODUCT.search(q):
                    return False
                q = _as_said(q, raw)
                self.run(q, raw)
                return True
        return False

    def _open(self, which: str) -> bool:
        offers = [o for o in self.last["offers"] if o.get("url")]
        if not offers:
            return False
        idx = {"cheapest": 0, "first": 0, "second": 1, "third": 2}.get(which)
        o = None
        if idx is not None and idx < len(offers):
            o = offers[idx]
        else:
            o = next((x for x in offers if x["store"].lower().startswith(which) or which in x["store"].lower()), None)
        if not o:
            return False
        self.app._open(o["url"])
        self.app._reply(f"Opening it on {o['store']}.")
        return True

    def run(self, query: str, raw: str) -> None:
        cap = budget(raw)
        if is_specific(query):
            product = re.sub(r"\s*\b(?:under|below|within|for less than)\b.*$", "", query).strip()
            keys = GROUPS[group_for(product)]
            self.app._reply(f"Checking {product} on {_names(keys)}.")
            self.app.overlay.send("status", text="Comparing prices")
            offers = compare(product, keys, cap)
            self._present_one(product, offers)
            return
        self._present_choices(query, raw, cap)

    def _present_one(self, product: str, offers: list[dict]) -> None:
        priced = [o for o in offers if o.get("price")]
        if not offers:
            self.app._reply(f"I couldn't find {product} on any of those stores. Try the exact model name.")
            return
        report = write_report(product, [(product, "", offers)])
        self.last = {"offers": offers, "report": report, "t": time.time()}
        self.app.last_file = report
        if priced:
            available = [o for o in priced if o.get("stock") is not False] or priced
            best = available[0]
            others = [f"{o['store']} {_say_money(o['price'])}" for o in available[1:4]]
            line = f"Cheapest is {best['store']} at {_say_money(best['price'])}"
            line += (", then " + ", ".join(others) + ".") if others else "."
            gone = [o for o in priced if o.get("stock") is False and o["price"] < best["price"]]
            if gone:
                line += f" {gone[0]['store']} has it for {_say_money(gone[0]['price'])}, but it's out of stock."
            elif len(available) > 1 and available[-1]["price"] > best["price"] * 1.15:
                line += f" That's {_say_money(available[-1]['price'] - best['price'])} less than the most expensive."
        else:
            line = f"I found {product} on {_names_of(offers)}, but they don't show prices to me. The links are on screen."
        self.app._reply(line + " Say open the cheapest one, or open the comparison.")
        self.app.show_visual("card", f"{product} | " + "\n".join(_card_line(o) for o in offers[:8]))

    def _present_choices(self, request: str, raw: str, cap: float | None) -> None:
        from batsy import claude_code

        if not __import__("shutil").which("claude") or claude_code.over_budget(self.cfg):
            self.app._reply("To suggest models I need Claude, which isn't available right now. Tell me an exact model "
                            "and I'll compare its prices.")
            return
        self.app._reply("Let me find a few good options, then compare their prices.")
        self.app.overlay.send("status", text="Picking models with Claude")
        picks = pick_models(raw)
        if not picks:
            self.app._reply("I couldn't come up with specific models for that. Try naming a brand or model.")
            return
        self.app.overlay.send("status", text="Comparing prices")
        sections, lines, all_offers = [], [], []
        with ThreadPoolExecutor(len(picks)) as pool:
            results = list(pool.map(lambda p: compare(p["name"], GROUPS[group_for(p["name"] + " " + request)][:4], cap),
                                    picks))
        for p, offers in zip(picks, results):
            sections.append((p["name"], p.get("why", ""), offers))
            priced = [o for o in offers if o.get("price")]
            if priced:
                lines.append((p, priced[0]))
            all_offers += offers
        report = write_report(request[:60], sections)
        self.last = {"offers": sorted([o for _, o in lines], key=lambda o: o["price"]), "report": report, "t": time.time()}
        self.app.last_file = report
        if not lines:
            self.app._reply("I found options but couldn't read their prices. The comparison has the links: say open the comparison.")
        else:
            lines.sort(key=lambda x: x[1]["price"])
            spoken = "; ".join(f"{_short(p['name'])}, {_say_money(o['price'])} on {o['store']}" for p, o in lines[:3])
            more = f" and {len(lines) - 3} more on screen" if len(lines) > 3 else ""
            self.app._reply(f"Cheapest first: {spoken}{more}. Say open the comparison for every store's price.")
        self.app.show_visual("card", "Options | " + "\n".join(
            f"- {p['name']}: {rupees(o['price'])} at {o['store']}" + (f" · {p['why']}" if p.get("why") else "")
            for p, o in lines[:5]))


def _short(name: str) -> str:
    """'Samsung 55 inch Crystal UHD 4K Vision AI Smart TV (2026)' -> 'Samsung 55 inch Crystal UHD' for speaking."""
    name = re.sub(r"\s*\(.*?\)", "", name)
    return " ".join(name.split()[:5])


def _names(keys: list[str]) -> str:
    names = [STORES[k].name for k in keys]
    return ", ".join(names[:3]) + f" and {len(names) - 3} more" if len(names) > 3 else " and ".join(names)


def _names_of(offers: list[dict]) -> str:
    names = [o["store"] for o in offers]
    return ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]


def _say_money(v: float) -> str:
    return f"{v:,.0f} rupees" if v >= 1 else f"{v} rupees"


def _card_line(o: dict) -> str:
    approx = " (approx.)" if o.get("how") == "search snippet" else ""
    stock = "" if o.get("stock") is None else (" · in stock" if o["stock"] else " · out of stock")
    return f"- {o['store']}: {rupees(o.get('price'))}{approx} · {o.get('title', '')[:60]}{stock}"


def _as_said(words: str, text: str) -> str:
    rx = r"[\s,.!?'-]*".join(re.escape(w) for w in words.split())
    m = re.search(rx, text, re.I)
    return m.group(0) if m else words

