"""PriceCharting helpers shared by megacash_prices.py and values_refresh.py.

- find_card(c): finds a card's own PriceCharting page, strict about name, number, set, language,
  reverse holo and 1st Edition (rather a "no match" than the wrong card)
- read_page(page): every price on the page — the six main grades, the full price list (Grade 1-6, CGC 10,
  SGC 10, BGS 10, CGC 10 Pristine...), and the dated sold listings behind each main grade
- value(...): the price to use for one grade, preferring the median of the last 90 days' sales
"""
import html as H, re, statistics, time, urllib.parse
from datetime import date, timedelta

from pc_prices import get, parse, BASE

NOISE = set("pokemon rare foil card tcg english eng the black star wotc promo -".split())
SET_SKIP = NOISE | set("psa cgc bgs tag ace ccic cic ags gem mint pristine japanese jpn edition 1st reverse holo "
                       "ultra secret full art alt illustration special hyper promo rev svp mep pokemon jp".split())

# main price table / sold-listing blocks on a PriceCharting card page -> our grade keys
MAIN = {"used": "raw", "cib": "g7", "new": "g8", "graded": "g9", "box-only": "g95", "manual-only": "g10"}
FULL_LABELS = {"ungraded": "raw", "grade 7": "g7", "grade 8": "g8", "grade 9": "g9", "grade 9.5": "g95", "psa 10": "g10"}
RECENT_DAYS, MIN_SALES = 90, 3
JUNK = re.compile(r"\b(lot|bundle|proxy|custom|reprint|replica|orica|fake|damaged|creased|digital|code card|empty)\b", re.I)


def set_words(c):
    """Title words that should name the set (anything that isn't the card name, number, grade or filler)."""
    name = set(c["name"])
    num = (c.get("num") or "").lower().lstrip("0")
    t = c["title"].lower().replace("s&v", "scarlet violet").replace("'s", "s")
    ws = set(re.findall(r"[a-z0-9&]{3,}", t)) | set(c.get("keys") or [])
    return {w for w in ws - SET_SKIP - name if w == "151" or (not w.isdigit() and w.lstrip("0") != num)}


def score(path, c):
    """0 = wrong card; higher = better fit."""
    p = H.unescape(path).split("?")[0].lower().rstrip("/")
    parts = p.split("/")
    if len(parts) < 4 or not parts[2].startswith("pokemon"):
        return 0
    cset, slug = parts[2], parts[3]
    flat, sflat = slug.replace("-", ""), cset.replace("-", "")
    if c["jp"] != ("japanese" in cset):
        return 0
    long_words = [w.replace("'", "") for w in c["name"] if len(w) > 3]
    if long_words:  # every long name word in the card or set name, and at least one in the card name itself
        if not all(w in flat or w in sflat for w in long_words) or not any(w in flat for w in long_words):
            return 0
    elif c["name"] and c["name"][0] not in flat:
        return 0
    if c.get("num"):
        n = re.sub(r"[^a-z0-9]", "", c["num"].lower()).lstrip("0") or "0"
        tail = re.sub(r"[^a-z0-9]", "", slug.rsplit("-", 1)[-1]).lstrip("0") or "0"
        if tail != n and not flat.endswith(n):
            return 0
    if c["rev"] != ("reverse" in slug):
        return 0
    if c.get("sl") is not None and not c["jp"] and c.get("sl") != ("shadowless" in slug):
        return 0
    bonus = 0.0
    if "1st-edition" in slug or "first-edition" in slug:
        if not c["first"]:
            if not c["jp"]:
                return 0
            bonus -= 0.5      # Japanese: unlimited wanted, keep the 1st Ed page only as a last resort
        else:
            bonus += 0.5
    elif c["first"] and not c["jp"]:
        return 0
    sw = set_words(c)
    hits = sum(w in sflat or w in flat or (len(w) > 5 and w[:5] in sflat) for w in sw)
    if sw and not hits:
        return 0  # the title names a set and this page is from a different one
    return 1 + hits + bonus


def find_card(c, ref=None):
    """(url, html) of the card's PriceCharting page. ref: a known /game/ path to try first."""
    if ref and ref.startswith("/game/"):
        try:
            final, page = get(BASE + ref)
            if 'id="price_data"' in page or "used_price" in page:
                return final, page
        except Exception:
            pass
        time.sleep(1.5)
    url = BASE + "/search-products?type=prices&q=" + urllib.parse.quote_plus(c["query"])
    final, page = get(url)
    path = urllib.parse.urlparse(final).path
    if path.startswith("/game/") and ('id="price_data"' in page or "used_price" in page):
        if score(path, c):
            return final, page
    links = list(dict.fromkeys(H.unescape(l) for l in re.findall(
        r'href="(?:https://www\.pricecharting\.com)?(/game/pokemon[^"?#]+)"', page)))
    best = max(links, key=lambda l: score(l, c), default=None)
    if not best or not score(best, c):
        raise RuntimeError("no PriceCharting card fit" + (f" (top result {links[0]})" if links else ""))
    time.sleep(1.5)
    final, page = get(BASE + urllib.parse.quote(best, safe="/&-:"))
    return final, page


def _money(s):
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def _text(s):
    return H.unescape(re.sub(r"<[^>]+>", " ", s or ""))


def full_prices(page):
    """The 'Full Price Guide' list: {'raw':.., 'grade 1':.., ..., 'cgc 10':.., 'sgc 10':.., 'bgs 10':..} in US$."""
    out = {}
    m = re.search(r'id="full[-_]prices".*?</table>', page, re.S) or re.search(r"Full Price Guide.*?</table>", page, re.S)
    if not m:
        return out
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S):
        cells = [re.sub(r"\s+", " ", _text(x)).strip() for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) >= 2:
            label = cells[0].lower()
            out[FULL_LABELS.get(label, label)] = _money(cells[1])
    return out


def sales(page):
    """Dated sold listings PriceCharting shows for each main grade: {'raw': [(date, US$, title)], 'g9': [...]}."""
    out = {}
    blocks = list(re.finditer(r'class="[^"]*completed-auctions-([a-z-]+)[^"]*"', page))
    for i, b in enumerate(blocks):
        key = MAIN.get(b.group(1))
        if not key:
            continue
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(page)
        chunk = page[b.end():end]
        rows = []
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", chunk, re.S):
            d = re.search(r"(20\d\d-\d\d-\d\d)", row)
            p = re.search(r'class="[^"]*js-price[^"]*"[^>]*>(.*?)<', row, re.S)
            if not d or not p or _money(p.group(1)) is None:
                continue
            t = re.search(r'class="[^"]*title[^"]*"[^>]*>(.*?)</td>', row, re.S)
            title = re.sub(r"\s+", " ", _text(t.group(1))).strip() if t else ""
            rows.append((d.group(1), _money(p.group(1)), title))
        out.setdefault(key, []).extend(rows)
    return out


def read_page(page, keep_days=180):
    main, name = parse(page)
    cut = (date.today() - timedelta(days=keep_days)).isoformat()
    sold = {k: [r for r in v if r[0] >= cut] for k, v in sales(page).items()}
    return {"name": name, "main": main, "full": full_prices(page), "sales": sold}


def value(info, key, today=None):
    """(US$ value, note) for one grade key. Median of the last 90 days' sales when there are 3+,
    otherwise PriceCharting's own average (marked thin)."""
    today = today or date.today()
    cut = (today - timedelta(days=RECENT_DAYS)).isoformat()
    avg = info["main"].get(key) if key in info["main"] else info["full"].get(key)
    rows = [r for r in info["sales"].get(key, []) if r[0] >= cut and not JUNK.search(r[2])]
    n = len(rows)
    last = max((r[0] for r in info["sales"].get(key, [])), default=None)
    if n >= MIN_SALES:
        med = statistics.median(r[1] for r in rows)
        return med, {"n90": n, "last": last, "basis": "90-day median", "pc_avg": avg}
    if not info["sales"]:
        return avg, {"n90": None, "last": None, "basis": "PriceCharting average (sales list not read)", "thin": avg is not None and key in MAIN.values()}
    return avg, {"n90": n, "last": last, "basis": "PriceCharting average", "thin": True}
