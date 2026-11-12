"""Reads the public product catalogues of Australian and NZ card shops (Shopify and
WooCommerce stores publish them as JSON) and saves in-stock Pokemon singles to
stores.json, in the same item format as listings.json so the finder can treat
them alike. Read only: it never adds to a cart, buys or contacts anyone.
Shops are listed in shops.txt."""
import json, re, time, html, http.client, urllib.error, urllib.request, urllib.parse, datetime

UA = {"User-Agent": "Mozilla/5.0 (compatible; vintage-slab-finder/1.0; personal price tracker)",
      "Accept": "application/json"}
MAX_PAGES = 25          # per collection, 250 products per page on Shopify
PAUSE = 1.5             # seconds between requests, to be polite
CUR = {"AU": "AUD", "NZ": "NZD", "JP": "JPY", "UK": "GBP"}
POST = {"AU": "12", "NZ": "25", "JP": "30", "UK": "50"}   # A$ postage estimates to Australia
POKE = re.compile(r"pok[eé]mon|pocket monsters|\bpsa\b|\bcgc\b|\bbgs\b|holo|1st edition|wotc", re.I)
JUNK = re.compile(r"\b(sleeves?|binder|deck box|playmat|toploader|case|figure|plush|booster pack|booster box|bundle|tin|elite trainer|etb|lot of|mystery|repack|proxy|custom|magic|mtg|yu-?gi-?oh|one piece|lorcana|dragon ball|flesh and blood|digimon|sports?|nba|nfl|afl)\b", re.I)


def get(url, tries=3):
    """Polite GET: retries dropped connections and server errors, gives up at once on a clear refusal."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError, TimeoutError):
            if attempt == tries - 1:
                raise
        time.sleep(5 * (attempt + 1))


AUTO_RX = re.compile(r"graded|slab|psa|cgc", re.I)
POKE_RX = re.compile(r"pok[eé]mon", re.I)
SEALED_Q = ["elite trainer box", "pokemon elite trainer box", "pokemon booster box", "japanese booster box"]
SEALED_SKIP = re.compile(r"\bcase\b|empty|sleeve|binder|deck box|playmat|code card|\bpromo\b|opened|damaged|pre-?owned|\bsingle pack\b|\bbundle\b|\btin\b|\bmini\b|half box", re.I)


PHOTOS = {}   # shop name -> image hints from its products
CAMERA_RX = re.compile(r"(^|[/_-])(img|dsc|dscn|dcim|pxl|photo|scan|pic|front|back)[-_ ]?\d*|\b(front|back)\b", re.I)
STOCK_RX = re.compile(r"_hires|tcgplayer|product_\d+|pokemontcg|_in_\d+x\d+|stock|\b(swsh|sv|sm|xy|bw)\d+[-_]\d+", re.I)


def note_photos(shop, srcs):
    srcs = [u for u in srcs if u]
    p = PHOTOS.setdefault(shop["name"], {"n": 0, "multi": 0, "camera": 0, "stock": 0, "samples": []})
    p["n"] += 1
    if not srcs:
        return
    name = srcs[0].split("?")[0].rsplit("/", 1)[-1]
    p["multi"] += len(srcs) >= 2
    p["camera"] += bool(CAMERA_RX.search(name))
    p["stock"] += bool(STOCK_RX.search(name))
    if len(p["samples"]) < 6 and p["n"] % 7 == 1:
        p["samples"].append(srcs[0].split("?")[0])


def photo_label(name):
    """A best guess from image counts and file names. Nothing here looks at the pictures themselves."""
    p = PHOTOS.get(name)
    if not p or p["n"] < 5:
        return ""
    multi, cam, stock = p["multi"] / p["n"], p["camera"] / p["n"], p["stock"] / p["n"]
    if stock >= 0.25:
        return "stock"
    if multi >= 0.5 or cam >= 0.25:
        return "real"
    if p["n"] >= 500 and multi < 0.1:
        return "stock"
    return "unclear"


def auto_collections(shop):
    """Finds a Shopify shop's graded Pokemon collections plus its raw singles / secret rare collections.
    Raw collections come back as "raw:<handle>" so their rows can be trimmed to older cards (keep_raw)."""
    cols = get(f"https://{shop['domain']}/collections.json?limit=250").get("collections") or []
    named = [(c.get("handle") or "", c.get("title") or "") for c in cols]
    both = [h for h, t in named if POKE_RX.search(h + " " + t) and AUTO_RX.search(h + " " + t)]
    raw = [h for h, t in named if h not in both and (
             (POKE_RX.search(h + " " + t) and re.search(r"single|card|secret|vintage|japanese|black.?white|plasma", h + " " + t, re.I))
             or re.search(r"secret.?rare|pokemon.?singles|vintage.?pokemon", h + " " + t, re.I))
           and not re.search(r"seal|box|pack|bulk|lot|accessor|supplies|magic|mtg|yu-?gi|one.?piece|lorcana", h + " " + t, re.I)]
    picked = both[:4] + ["raw:" + h for h in raw[:4]]
    return picked or ["raw:all"]


# Raw singles collections are big and mostly modern: keep graded copies, and raw cards from older eras only.
GRADED_RX = re.compile(r"\b(PSA|CGC|BGS|BECKETT|ACE|TAG|SGC|ARK)\s*\d", re.I)
VINTAGE_RX = re.compile(r"base set|jungle|fossil|team rocket|gym (heroes|challenge)|\bneo\b|expedition|aquapolis|skyridge|"
    r"legendary collection|e-?card|\bex (ruby|sandstorm|dragon|team|hidden|firered|deoxys|emerald|unseen|delta|legend|holon|crystal|dragon|power)|"
    r"delta species|gold star|diamond|pearl|platinum|rising rivals|supreme victors|arceus|heartgold|soulsilver|hgss|unleashed|undaunted|triumphant|"
    r"call of legends|\bprime\b|lv\.?\s?x|black (&|and) white|emerging powers|noble victories|next destinies|dark explorers|dragons exalted|"
    r"dragon vault|boundaries crossed|plasma (storm|freeze|blast)|legendary treasures|radiant collection|\bxy\b|flashfire|furious fists|"
    r"phantom forces|primal clash|roaring skies|ancient origins|breakthrough|breakpoint|generations|fates collide|steam siege|evolutions|"
    r"double crisis|kalos|\bbw\d|\bbw-?p\b|cold flare|freeze bolt|plasma gale|spiral force|thunder knuckle|megalo cannon|dark rush|"
    r"psycho drive|hail blizzard|dragon blast|dragon blade|shiny collection|battle boost|red collection|1st edition|shining|"
    r"black star promo|wotc|\b(199[6-9]|200\d|201[0-7])\b", re.I)
MODERN_RX = re.compile(r"vmax|vstar|\bgx\b|scarlet|violet|sword|shield|\bswsh|\bsv\d|sun (&|and) moon|\bsm\d|mega evolution|"
    r"\b151\b|paldea|obsidian|paradox|temporal|twilight|shrouded|stellar|surging|prismatic|journey together|destined rivals|"
    r"black bolt|white flare|evolving skies|brilliant stars|astral|lost origin|silver tempest|crown zenith|celebrations|"
    r"hidden fates|shining fates|champion.?s path|vivid voltage|cosmic eclipse|unified minds|unbroken bonds|team up|lost thunder|"
    r"celestial storm|ultra prism|burning shadows|guardians rising|crimson invasion|forbidden light|dragon majesty|"
    r"\b20(1[89]|2\d)\b|reprint|25th|classic collection", re.I)


def keep_raw(title):
    if GRADED_RX.search(title):
        return True
    return bool(VINTAGE_RX.search(title)) and not MODERN_RX.search(title)


def sealed_scan(shop):
    """Reads ETBs and booster boxes through a Shopify shop's public search suggestions."""
    out, seen = [], set()
    for q in SEALED_Q:
        url = (f"https://{shop['domain']}/search/suggest.json?q={urllib.parse.quote(q)}"
               "&resources%5Btype%5D=product&resources%5Blimit%5D=10")
        data = get(url, tries=2)
        for p in ((data.get("resources") or {}).get("results") or {}).get("products") or []:
            title = html.unescape(p.get("title") or "").strip()
            key = p.get("handle") or title
            if key in seen or not p.get("available"):
                continue
            seen.add(key)
            if not POKE_RX.search(title + " " + str(p.get("vendor") or "") + " " + str(p.get("type") or "")) or SEALED_SKIP.search(title):
                continue
            kind = "ETB" if re.search(r"elite trainer|\betb\b", title, re.I) else "Box" if re.search(r"booster box|\bbooster display\b", title, re.I) else ""
            try:
                price = float(p.get("price") or p.get("price_min") or 0)
            except (TypeError, ValueError):
                continue
            if kind and price > 0:
                link = p.get("url") or ""
                out.append({"title": title, "price": round(price, 2), "shop": shop["name"], "kind": kind,
                            "lang": "JP" if re.search(r"japanese|\bjp\b", title, re.I) else "EN",
                            "url": f"https://{shop['domain']}{link.split('?')[0]}"})
        time.sleep(PAUSE)
    return out


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
            note_photos(shop, [i.get("src") for i in p.get("images") or []])
            url = f"https://{shop['domain']}/products/{p.get('handle')}"
            listed = (p.get("published_at") or p.get("created_at") or "")[:10]
            named = [v for v in vs if (v.get("title") or "").strip().lower() not in ("", "default title", "default")]
            if len(vs) > 1 and named:
                # one row per in-stock option (usually condition: Near Mint, Lightly Played...), so a played copy's price
                # is never shown as if it were Near Mint
                for v in named:
                    price = float(v.get("price") or 0)
                    if price > 0:
                        r = item(shop, f"{p['id']}-{v.get('id')}", f"{p.get('title', '')} [{v.get('title')}]", price,
                                 shop["currency"], True, f"{url}?variant={v.get('id')}", listed)
                        out.append(r)
                continue
            price = min(float(v.get("price") or 0) for v in vs)
            if price <= 0:
                continue
            vt = (vs[0].get("title") or "").strip()
            title = p.get("title", "") + (f" [{vt}]" if vt.lower() not in ("", "default title", "default") else "")
            out.append(item(shop, p["id"], title, price, shop["currency"], True, url, listed))
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
            note_photos(shop, [i.get("src") for i in p.get("images") or []])
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
    items, status, seen, sealed = [], [], set(), []
    for s in shops:
        got, err = 0, ""
        colls = s["collections"]
        if s["platform"] == "shopify" and "sealed-only" not in colls:
            if "auto" in colls:   # "auto" alone, or added after named collections to pick up raw singles too
                try:
                    named_c = [c for c in colls if c != "auto"]
                    colls = named_c + [c for c in auto_collections(s) if c not in named_c and c[4:] not in named_c]
                except Exception as e:
                    err = f"auto: {type(e).__name__} {str(e)[:100]}"
                    colls = []
        if "sealed-only" in colls:
            colls = []
        for c in colls:
            raw = c.startswith("raw:")
            c = c[4:] if raw else c
            try:
                rows = (shopify if s["platform"] == "shopify" else woo)(s, c)
            except Exception as e:  # one broken shop must not stop the rest
                err = f"{c}: {type(e).__name__} {str(e)[:120]}"
                continue
            if raw:
                rows = [r for r in rows if keep_raw(r["title"])]
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"]); items.append(r); got += 1
        # A listed collection that comes back empty has usually been renamed: look it up once.
        if got == 0 and s["platform"] == "shopify" and colls and "auto" not in s["collections"]:
            try:
                for c in auto_collections(s):
                    raw = c.startswith("raw:")
                    c = c[4:] if raw else c
                    if c in colls:
                        continue
                    for r in shopify(s, c):
                        if raw and not keep_raw(r["title"]):
                            continue
                        if r["id"] not in seen:
                            seen.add(r["id"]); items.append(r); got += 1
                if got:
                    err = ""
            except Exception as e:
                err = err or f"auto: {type(e).__name__} {str(e)[:100]}"
        n_sealed = 0
        if s["platform"] == "shopify" and s["country"] in ("AU", "NZ"):
            try:
                found = sealed_scan(s)
                sealed += found
                n_sealed = len(found)
            except Exception as e:
                print(f"{s['name']}: sealed search failed: {type(e).__name__} {str(e)[:100]}")
        status.append({"shop": s["name"], "domain": s["domain"], "items": got, "sealed": n_sealed, "error": err,
                       "photos": photo_label(s["name"]), "photo_hints": PHOTOS.get(s["name"], {})})
        print(f"{s['name']}: {got} items, {n_sealed} sealed, photos {photo_label(s['name']) or 'n/a'} {err}")
    out = {"generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M+00:00"),
           "count": len(items), "shops": status, "items": items}
    json.dump(out, open("stores.json", "w", encoding="utf-8"), ensure_ascii=False)
    json.dump({"generated": out["generated"], "items": sealed}, open("sealed.json", "w", encoding="utf-8"), ensure_ascii=False)
    print(len(items), "items from", len(shops), "shops")


if __name__ == "__main__":
    main()
