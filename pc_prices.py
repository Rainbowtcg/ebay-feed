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


def wanted(ref, label):
    """Checks a PriceCharting link fits the card: name word, card number and Japanese/English all match."""
    words = (label if ref.startswith("/game/") else ref).lower()
    name = re.sub(r"[^a-z]", "", (re.findall(r"[a-z][a-z'.]+", re.sub(r"^(japanese|neo|base|set|expedition|aquapolis|skyridge|lost|thunder|"
                                                                        r"firered|leafgreen|plasma|freeze|storm|ancient|origins|wotc|black|star)\s+", "", words)) or [""])[0])
    m = re.search(r"\b0*(\d{1,3})\s*/\s*\d{2,3}\b|no\.\s*0*(\d{1,3})", label.lower())
    num = (m.group(1) or m.group(2)) if m else None
    jp = label.lower().startswith("japanese")

    def ok(path):
        p = path.lower()
        if jp != ("japanese" in p):
            return False
        last = p.rstrip("/").rsplit("/", 1)[-1]
        if name and name not in last.replace("-", ""):
            return False
        return not num or re.search(r"-%s$" % num, last) is not None
    return ok


def find(ref, label):
    """ref is a /game/ path or search words. Returns (url, html) of the card page."""
    ok = wanted(ref, label)
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
            if url.startswith(BASE + "/game/") or ok(urllib.parse.urlparse(final).path):
                return final, page
        links = list(dict.fromkeys(re.findall(r'href="(?:https://www\.pricecharting\.com)?(/game/pokemon[^"?#]+)"', page)))
        pick = next((l for l in links if ok(l)), None)  # first search result that fits the card's name, number and language
        if pick:
            time.sleep(1.5)
            final, page = get(BASE + pick)
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
