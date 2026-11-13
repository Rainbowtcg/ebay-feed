#!/usr/bin/env python3
"""Daily price guide: reads guide.txt, looks each card up on PriceCharting, writes guide.json.

Each card gets PriceCharting's current sold-price averages (Ungraded, Grade 7, 8, 9, 9.5, PSA 10) in US$ and A$,
the PriceCharting card it matched (so a wrong match is easy to spot), and today's US$→A$ rate.
Read-only: one page per card, politely spaced. Failures are listed in guide.json "errors" rather than stopping the run.
"""
import html as H, json, os, re, time, urllib.parse, urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://www.pricecharting.com"
UA = {"User-Agent": "Mozilla/5.0 (personal collection price guide; github.com/Rainbowtcg/ebay-feed)", "Accept-Language": "en"}
# PriceCharting's trading-card price table: element id -> grade
IDS = [("used_price", "raw"), ("complete_price", "g7"), ("new_price", "g8"), ("graded_price", "g9"),
       ("box_only_price", "g95"), ("manual_only_price", "g10")]
HEAD = {"ungraded": "raw", "grade 7": "g7", "grade 8": "g8", "grade 9": "g9", "grade 9.5": "g95", "psa 10": "g10"}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def money(s):
    m = re.search(r"\$\s*([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse(page):
    out = {}
    for el, key in IDS:
        m = re.search(r'id="%s"[^>]*>(.*?)</td>' % el, page, re.S)
        if m:
            out[key] = money(m.group(1))
    if not any(out.values()):  # fallback: read the table by its column headings
        t = re.search(r'<table[^>]*id="price_data".*?</table>', page, re.S)
        if t:
            heads = [re.sub(r"<[^>]+>|\s+", " ", h).strip().lower() for h in re.findall(r"<th[^>]*>(.*?)</th>", t.group(0), re.S)]
            row = re.search(r"<tr[^>]*>\s*(<td.*?)</tr>", t.group(0), re.S)
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S) if row else []
            for h, c in zip(heads, cells):
                if h in HEAD:
                    out[HEAD[h]] = money(c)
    name = re.search(r'<h1[^>]*id="product_name"[^>]*>(.*?)</h1>', page, re.S)
    name = H.unescape(re.sub(r"<[^>]+>|\s+", " ", name.group(1)).strip()) if name else ""
    return out, name


SKIP = set("japanese neo base set holo reverse promo black star wotc nintendo crystal full art gold the and with of unlimited 1st edition svp swsh xy sm".split())


def words_of(t):
    return [w for w in re.findall(r"[a-z]+", t.lower()) if len(w) >= 3]


def wanted(ref, label):
    """Scores a PriceCharting link for the card: 0 = wrong card (name, number or Japanese/English don't fit),
    higher = more of the label's set words appear in the link's set."""
    text = label + " " + ("" if ref.startswith("/game/") else ref)
    low = label.lower()
    m = (re.search(r"\b0*(\d{1,3})\s*/\s*(?:\d{2,3}|bw-?p)\b", low) or re.search(r"no\.\s*0*(\d{1,3})", low)
         or re.search(r"\b((?:rc|xy|sm|swsh|svp|h|sh|bw)\s?\d{1,3})\b", low) or re.search(r"#\s*0*(\d{1,3})\b", low))
    num = re.sub(r"\s", "", m.group(1)) if m else None
    if num and num.startswith("svp"):
        num = num[3:]
    jp = label.lower().startswith("japanese") or "bw-p" in label.lower()
    lw = set(words_of(text)) - SKIP
    want_set = ref.split("/")[2] if ref.startswith("/game/") else None

    def score(path):
        p = H.unescape(path).split("?")[0].lower().rstrip("/")
        parts = p.split("/")
        if len(parts) < 4 or jp != ("japanese" in parts[2]):
            return 0
        card, cset = parts[3], parts[2]
        if want_set and re.sub(r"[^a-z0-9]", "", cset) != re.sub(r"[^a-z0-9]", "", want_set):
            return 0
        if not any(w in card for w in lw):
            return 0
        if num and not re.search(r"-0*%s(?:[a-z-]*)$" % re.escape(num.lstrip("0") or "0"), card):
            return 0
        setw = [w for w in lw if w not in card]
        hits = sum(w in cset.replace("-", "") for w in setw)
        if setw and not hits and not want_set and not jp:
            return 0  # label names a set but this link is from a different one
        return 1 + hits
    return score


def find(ref, label):
    """ref is a /game/ path or search words. Returns (url, html) of the card page."""
    score = wanted(ref, label)
    tries = []
    if ref.startswith("/game/"):
        tries.append(BASE + ref)
    tries.append(BASE + "/search-products?type=prices&q=" + urllib.parse.quote_plus(ref if not ref.startswith("/game/") else label))
    last = None
    for url in tries:
        try:
            final, page = get(url)
        except Exception as e:
            last = e
            continue
        if "/game/" in final and ('id="price_data"' in page or "used_price" in page):
            if url.startswith(BASE + "/game/") or score(urllib.parse.urlparse(final).path):
                return final, page
        links = list(dict.fromkeys(H.unescape(l) for l in re.findall(r'href="(?:https://www\.pricecharting\.com)?(/game/pokemon[^"?#]+)"', page)))
        best = max(links, key=score, default=None)  # the result that fits the card and shares the most set words
        if best and score(best):
            time.sleep(1.5)
            final, page = get(BASE + urllib.parse.quote(best, safe="/&-:"))
            if 'id="price_data"' in page or "used_price" in page:
                return final, page
        last = "no PriceCharting card matched the name/number" + (f" (top result was {links[0]})" if links else "")
    raise RuntimeError(str(last))


def usd_aud():
    try:
        _, page = get("https://open.er-api.com/v6/latest/USD")
        return round(json.loads(page)["rates"]["AUD"], 4), "open.er-api.com"
    except Exception:
        return 1.52, "fallback (rate lookup failed)"


def main():
    rate, rate_src = usd_aud()
    cats, errors, seen = {}, [], {}
    for line in open(os.path.join(HERE, "guide.txt"), encoding="utf-8"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        cat, label, ref = parts[:3]
        try:
            if ref in seen:
                url, prices, name = seen[ref]
            else:
                url, page = find(ref, label)
                prices, name = parse(page)
                seen[ref] = (url, prices, name)
                time.sleep(1.5)
            if not any(prices.values()):
                raise RuntimeError("page had no prices")
            cats.setdefault(cat, []).append({
                "label": label, "matched": name, "url": url,
                "usd": prices, "aud": {k: (round(v * rate) if v else None) for k, v in prices.items()}})
        except Exception as e:
            errors.append(f"{cat} | {label}: {e}")
            cats.setdefault(cat, []).append({"label": label, "error": str(e)})
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "usd_aud": rate, "rate_source": rate_src,
           "source": "PriceCharting sold-price averages (Ungraded, Grade 7, 8, 9, 9.5, PSA 10)", "cats": cats, "errors": errors}
    json.dump(out, open(os.path.join(HERE, "guide.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n = sum(len(v) for v in cats.values())
    print(f"price guide: {n - len(errors)}/{n} cards priced, US$1 = A${rate} ({rate_src})")
    for e in errors:
        print("  ", e)


if __name__ == "__main__":
    main()
