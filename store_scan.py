"""Reads the public product catalogues of Australian and NZ card shops (Shopify and
WooCommerce stores publish them as JSON) and saves in-stock Pokemon singles to
stores.json, in the same item format as listings.json so the finder can treat
them alike. Read only: it never adds to a cart, buys or contacts anyone.
Shops are listed in shops.txt."""
import json, re, time, html, urllib.request, urllib.parse, datetime

UA = {"User-Agent": "Mozilla/5.0 (compatible; vintage-slab-finder/1.0; personal price tracker)",
      "Accept": "application/json"}
MAX_PAGES = 25          # per collection, 250 products per page on Shopify
PAUSE = 1.5             # seconds between requests, to be polite
CUR = {"AU": "AUD", "NZ": "NZD", "JP": "JPY", "UK": "GBP"}
POST = {"AU": "12", "NZ": "25", "JP": "30", "UK": "50"}   # A$ postage estimates to Australia
POKE = re.compile(r"pok[eé]mon|pocket monsters|\bpsa\b|\bcgc\b|\bbgs\b|holo|1st edition|wotc", re.I)
JUNK = re.compile(r"\b(sleeves?|binder|deck box|playmat|toploader|case|figure|plush|booster pack|booster box|bundle|tin|elite trainer|etb|lot of|mystery|repack|proxy|custom|magic|mtg|yu-?gi-?oh|one piece|lorcana|dragon ball|flesh and blood|digimon|sports?|nba|nfl|afl)\b", re.I)


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def item(shop, pid, title, price, cur, ok, url, listed):
    return {"id": f"s-{shop['domain']}-{pid}", "title": html.unescape(title).strip(), "price": f"{price:.2f}",
            "currency": cur, "post": POST.get(shop["country"], "20"), "postCurrency": "AUD",
            "options": ["FIXED_PRICE"], "bids": 0, "ends": "", "condition": "", "country": shop["country"],
            "seller": shop["name"], "feedback": "", "url": url, "query": "store", "listed": listed or "",
            "available": ok}


def shopify(shop, coll):
    base = f"https://{shop['domain']}" + (f"/collections/{coll}" if coll and coll != "all" else "")
    out = []
    for page in range(1, MAX_PAGES + 1):
        try:
            data = get(f"{base}/products.json?limit=250&page={page}")
        except Exception:
            if page == 1:
                raise
            break
        prods = data.get("products") or []
        if not prods:
            break
        for p in prods:
            vs = [v for v in p.get("variants", []) if v.get("available")]
            if not vs:
                continue
            text = " ".join([p.get("title", ""), p.get("product_type", ""), " ".join(p.get("tags", []) if isinstance(p.get("tags"), list) else [str(p.get("tags", ""))])])
            if coll in ("", "all") and not POKE.search(text):
                continue
            if JUNK.search(p.get("title", "")):
                continue
            price = min(float(v.get("price") or 0) for v in vs)
            if price <= 0:
                continue
            url = f"https://{shop['domain']}/products/{p.get('handle')}"
            out.append(item(shop, p["id"], p.get("title", ""), price, shop["currency"], True, url,
                            (p.get("published_at") or p.get("created_at") or "")[:10]))
        time.sleep(PAUSE)
    return out


def woo(shop, coll):
    base = f"https://{shop['domain']}/wp-json/wc/store/v1"
    cat = ""
    if coll and coll != "all":
        cats = get(f"{base}/products/categories?per_page=100")
        ids = [str(c["id"]) for c in cats if c.get("slug") == coll]
        if not ids:
            raise RuntimeError(f"category {coll} not found")
        cat = "&category=" + ids[0]
    out = []
    for page in range(1, MAX_PAGES + 1):
        try:
            prods = get(f"{base}/products?per_page=100&page={page}{cat}")
        except Exception:
            if page == 1:
                raise
            break
        if not prods:
            break
        for p in prods:
            if not p.get("is_in_stock"):
                continue
            title = p.get("name", "")
            if coll in ("", "all") and not POKE.search(title):
                continue
            if JUNK.search(title):
                continue
            pr = p.get("prices") or {}
            unit = int(pr.get("currency_minor_unit") or 2)
            price = int(pr.get("price") or 0) / (10 ** unit)
            if price <= 0:
                continue
            out.append(item(shop, p["id"], title, price, pr.get("currency_code") or shop["currency"], True,
                            p.get("permalink") or "", ""))
        time.sleep(PAUSE)
    return out


def main():
    shops = []
    for line in open("shops.txt", encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        f = [x.strip() for x in line.split("|")]
        domain, name, country, platform = f[0], f[1], f[2], f[3]
        colls = [c.strip() for c in (f[4] if len(f) > 4 else "all").split(",") if c.strip()]
        cur = f[5] if len(f) > 5 and f[5] else CUR.get(country, "AUD")
        shops.append({"domain": domain, "name": name, "country": country, "platform": platform,
                      "collections": colls, "currency": cur})
    items, status, seen = [], [], set()
    for s in shops:
        got, err = 0, ""
        for c in s["collections"]:
            try:
                rows = (shopify if s["platform"] == "shopify" else woo)(s, c)
            except Exception as e:  # one broken shop must not stop the rest
                err = f"{c}: {type(e).__name__} {str(e)[:120]}"
                continue
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"]); items.append(r); got += 1
        status.append({"shop": s["name"], "domain": s["domain"], "items": got, "error": err})
        print(f"{s['name']}: {got} items {err}")
    out = {"generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M+00:00"),
           "count": len(items), "shops": status, "items": items}
    json.dump(out, open("stores.json", "w", encoding="utf-8"), ensure_ascii=False)
    print(len(items), "items from", len(shops), "shops")


if __name__ == "__main__":
    main()
