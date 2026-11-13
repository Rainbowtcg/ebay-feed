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


def find(ref, label):
    """ref is a /game/ path or search words. Returns (url, html) of the card page."""
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
        if "/game/" in final and 'id="price_data"' in page:
            return final, page
        m = re.search(r'href="(?:https://www\.pricecharting\.com)?(/game/pokemon[^"?#]+)"', page)  # first search result
        if m:
            time.sleep(1.5)
            final, page = get(BASE + m.group(1))
            if 'id="price_data"' in page or "used_price" in page:
                return final, page
        last = "no matching card"
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
