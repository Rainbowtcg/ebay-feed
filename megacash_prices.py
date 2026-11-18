#!/usr/bin/env python3
"""Daily shop pricer: prices every Mega Cash card (and any other shop in SHOPS) against PriceCharting,
so only real deals and close calls need a chat review.

Reads   stores.json, listings.json (live eBay copies to compare), mc_cache.json (earlier PriceCharting reads)
Writes  megacash.json (every card, priced), megacash.md (daily summary, under-value first), mc_cache.json

For each card:
  - the PriceCharting card it matched (check the match link if a number looks off)
  - value at the card's own grade: median of the last 90 days' sales when there are 3+, otherwise
    PriceCharting's average marked THIN; raw cards are treated as NM. PSA 1-6, CGC/SGC/BGS 10 come from
    PriceCharting's full price list; other graders' 9s and below are a marked estimate off the PSA/BGS price
  - landed import cost of the same card from the US: (US price x today's rate + ship) + 10% GST
  - the cheapest live copy of the same card at the same grade in the eBay feed (AU/JP/CN/Asia sellers)
  - the shop's price + postage against all of that, and a signal: UNDER / FAIR / OVER-US / OVER
  - raw cards: net after grading at PSA 8 and PSA 9 (shop price + post + GRADING)
  - eBay links: AU, JP and CN-located listings, and sold (all sellers)
PriceCharting pages are cached for CACHE_DAYS; at most MAX_LOOKUPS new pages are read per run.
"""
import html as H, json, os, re, sys, time, unicodedata, urllib.parse
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from pc_prices import usd_aud  # noqa: E402
from pc_more import find_card, read_page, value, NOISE  # noqa: E402
from feedlib import card_key, grade_of, land_aud  # noqa: E402

SHOPS = ["Mega Cash"]          # shop names as they appear in stores.json "seller"
# Mega Cash's eBay stores: their listings in the daily eBay feed (listings.json) are priced the same way
EBAY_SHOPS = {"mega-cash": "Mega Cash eBay", "megacashselect": "Mega Cash eBay", "megacashsouthwindsor": "Mega Cash Penrith eBay"}
MAX_LOOKUPS = int(os.environ.get("MC_MAX_LOOKUPS", "250"))
CACHE_DAYS = 7
GRADING = 60                   # A$ per card to grade
SHIP_RAW, SHIP_SLAB = 25, 45   # A$ shipping from the US, used for the landed import cost
GST = 1.10
UNDER, FAIR = 0.80, 1.00       # cost <= 80% of value = UNDER, <= 100% = FAIR
# Graders without their own PriceCharting column below 10: value = PSA/BGS price x factor (marked as an estimate)
FACTOR = {"CGC": 0.80, "SGC": 0.80, "BGS": 1.0, "PSA": 1.0}
OTHER_FACTOR = 0.60            # TAG, ACE, AGS, CCIC, CIC, PCA, MNT

GRADERS = r"PSA|CGC|BGS|BECKETT|CCIC|CIC|AGS|TAG|ACE|PCA|MNT|SGC"
GRADE_RE = re.compile(r"\b(%s)\s*(pristine|black(?:\s*label)?|gem\s*mint|mint|nm-?mt)?\s*(10|\d(?:\.5)?)\b(?!\s*of)" % GRADERS, re.I)
JP_RE = re.compile(r"\b(japanese|jpn|jp|japan)\b|\(jpn\)", re.I)


def clean(t):
    t = H.unescape(t)
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode() if re.search(r"[^\x00-\x7f]", t) and "é" in t else t
    return re.sub(r"\s+", " ", t.replace("’", "'").replace("–", "-")).strip()


def card_of(title):
    """Splits a shop title into the parts PriceCharting needs."""
    t = clean(title)
    low = t.lower()
    g = GRADE_RE.search(t)
    grader, grade = (g.group(1).upper().replace("BECKETT", "BGS"), g.group(3)) if g else (None, None)
    label = (g.group(2) or "").lower() if g else ""
    special = "pristine" if "pristine" in label else "black" if "black" in label else ""
    if g:
        low = (low[:g.start()] + " " + low[g.end():])
    jp = bool(JP_RE.search(t))
    rev = bool(re.search(r"\brev(erse)?\b", low))
    first = bool(re.search(r"\b1st\b", low))
    m = (re.search(r"\b([a-z]{0,4}\d{1,3}[a-z]?)\s*/\s*([a-z]{0,4}\d{1,3}|[a-z]{1,4}-?p)\b", low)
         or re.search(r"\b(svp|swsh|mep|xy|sm|bw|sv|dpbp#?|h|rc)\s?#?(\d{1,3})\b", low)
         or re.search(r"\b(\d{1,3})\s+(svp|mep)\b", low)
         or re.search(r"(?:no\.|#)\s*0*(\d{1,3})\b", low))
    if m and m.re.pattern.startswith(r"\b(svp"):
        num, tot = (m.group(1).rstrip("#") + m.group(2)), None
    elif m and m.re.pattern.startswith(r"\b(\d"):
        num, tot = m.group(2) + m.group(1), None
    elif m and m.lastindex and m.lastindex >= 2:
        num, tot = m.group(1), m.group(2)
    else:
        num, tot = (m.group(1) if m else None), None
    if not num and jp:  # old Japanese cards are numbered by Pokedex number, e.g. "Vaporeon 134 Holo Jungle"
        b = re.search(r"[a-z]\s+0*(\d{1,3})\b(?!\s*(of|st|nd|rd|th)\b)", low)
        num = b.group(1) if b else None
    other = re.search(r"\b(portuguese|german|korean|chinese|french|italian|spanish|thai|indonesian|topps|playing cards|box break)\b", low)
    q = re.sub(r"(\b[a-z]{0,4}\d{1,3}[a-z]?)\s*/\s*[a-z]{0,4}\d{1,3}\b", r"\1", t, flags=re.I)
    if g:
        q = q.replace(g.group(0), " ")
    q = re.sub(r"\(?\bjpn\b\)?|\bjp\b", " japanese ", q, flags=re.I)
    q = re.sub(r"\brev\b", "reverse holo", q, flags=re.I)
    q = re.sub(r"(reverse holo)\s+holo\b", r"\1", q, flags=re.I)
    q = re.sub(r"\b1st\s+ed\b\.?", "1st edition", q, flags=re.I)
    q = re.sub(r"[-|()\[\]]", " ", q)
    words = [w for w in q.split() if w.lower() not in NOISE]
    name_part = re.split(r"\s#?\d|\s[a-z]{1,4}\d", " ".join(words), maxsplit=1, flags=re.I)[0]
    name = [w for w in re.findall(r"[a-z']+", name_part.lower()) if len(w) > 1 and w not in
            ("psa", "cgc", "bgs", "japanese", "reverse", "1st", "ed", "edition", "gem", "mint", "pristine", "jpn", "jp", "holo")]
    return {"title": t, "grader": grader, "grade": grade, "jp": jp, "rev": rev, "first": first,
            "num": num, "tot": tot, "name": name, "query": " ".join(words),
            "other": other.group(1) if other else None, "special": special}


def key_of(c):
    return "|".join([" ".join(c["name"]), c["num"] or "", c["tot"] or "", "jp" if c["jp"] else "en",
                     "rev" if c["rev"] else "", "1st" if c["first"] else ""])


def value_for(info, c):
    """(US$, note) at this card's grade, or (None, note)."""
    gr, g, sp = c["grader"], c["grade"], c["special"]
    if not g:
        v, n = value(info, "raw")
        return v, dict(n, grade="raw (NM)")
    full = info["full"]
    own = {"CGC": "cgc 10 pristine" if sp == "pristine" else "cgc 10", "SGC": "sgc 10",
           "BGS": "bgs 10 black" if sp == "black" else "bgs 10", "TAG": "tag 10", "ACE": "ace 10"}.get(gr)
    if g == "10" and gr != "PSA" and own:
        for col in (own, own.replace(" pristine", "").replace(" black", "")):
            if full.get(col):
                extra = {} if col == own else {"estimate": f"valued as a plain {col.upper()} (no {own.upper()} price)"}
                return full[col], dict({"grade": f"{gr} 10", "basis": "PriceCharting full price list", "n90": None, "thin": True}, **extra)
    key = {"10": "g10", "9.5": "g95", "9": "g9", "8": "g8", "7": "g7"}.get(g)
    if not key:  # PSA 1-6 (and half grades below 7) only appear in the full price list
        v = full.get(f"grade {g.split('.')[0]}")
        note = {"grade": f"{gr} {g}", "basis": "PriceCharting full price list", "n90": None, "thin": True}
    else:
        if g == "9.5" and gr == "PSA":
            key = "g9"
        v, note = value(info, key)
        note = dict(note, grade=f"{gr} {g}")
    if v is None:
        return None, note
    f = FACTOR.get(gr, OTHER_FACTOR)
    if gr == "BGS" and g == "10":
        f = 1.0
    if f != 1.0 or (gr != "PSA" and g == "10"):
        note = dict(note, estimate=f"{gr} valued at {f:.0%} of the PSA/BGS price (no {gr} {g} column)")
        v = v * f
    return v, note


def ebay_links(c):
    n = " ".join(c["name"][:3]) or c["query"]
    if c["num"] and c["tot"]:
        v = dict.fromkeys([f'{c["num"]}/{c["tot"]}', f'{c["num"].lstrip("0")}/{c["tot"]}', c["num"].lstrip("0")])
        q = f'{n} ({",".join(v)})'
    elif c["num"]:
        q = f'{n} {c["num"]}'
    else:
        q = c["query"]
    if c["jp"]:
        q += " japanese"
    e = urllib.parse.quote_plus(q)
    base = "https://www.ebay.com.au/sch/i.html?_nkw=" + e
    return {"au": base + "&LH_PrefLoc=1", "jp": base + "&LH_LocatedIn=1&_salic=104",
            "cn": base + "&LH_LocatedIn=1&_salic=45", "sold": base + "&LH_Sold=1&LH_Complete=1&LH_PrefLoc=2"}


def grade_class(title):
    g = grade_of(title)
    return "raw" if g in ("", "Raw", "raw?") else g.upper()


def live_index():
    """Live eBay copies by card (number|variant|language|name word): [(set, grade, A$ landed, country, url)].
    Only near-mint raw copies and slabs; damaged-flagged listings are left out."""
    path = os.path.join(HERE, "listings.json")
    if not os.path.exists(path):
        return {}
    idx = {}
    for it in json.load(open(path, encoding="utf-8")).get("items", []):
        if it.get("flag"):
            continue
        k = card_key(it.get("title", ""))
        if not k:
            continue
        num, st, var, g, lang, nm = k.split("|")
        if g in ("", "raw?", "graded?"):
            continue  # raw copies without a stated NM condition are often played
        idx.setdefault((num, var, lang, nm), []).append((st, "raw" if g == "Raw" else g.upper(), land_aud(it), it.get("country"), it.get("url")))
    return idx


def cheapest_live(idx, title):
    k = card_key(title)
    if not k:
        return None
    num, st, var, g, lang, nm = k.split("|")
    gc = grade_class(title)
    rows = [r for r in idx.get((num, var, lang, nm), []) if r[1] == gc and (not st or not r[0] or r[0] == st) and r[2] > 0]
    if not rows:
        return None
    r = min(rows, key=lambda r: r[2])
    return {"aud": r[2], "country": r[3], "url": r[4], "copies": len(rows)}


def ebay_items():
    """Mega Cash's eBay listings from today's feed, shaped like stores.json shop items. "listed" = the day the feed
    first saw the listing (history.json), so new eBay stock counts as new in the summary."""
    path = os.path.join(HERE, "listings.json")
    if not os.path.exists(path):
        return []
    hp = os.path.join(HERE, "history.json")
    seen = json.load(open(hp, encoding="utf-8")).get("ids", {}) if os.path.exists(hp) else {}
    out = []
    for it in json.load(open(path, encoding="utf-8")).get("items", []):
        shop = EBAY_SHOPS.get(str(it.get("seller", "")).lower())
        if not shop or it.get("currency", "AUD") != "AUD":
            continue
        post = it.get("post") if (it.get("postCurrency") or "AUD") == "AUD" else None
        out.append({"title": it.get("title", ""), "seller": shop, "url": it.get("url"), "price": it.get("price"),
                    "post": post or 0, "listed": (seen.get(str(it.get("id"))) or {}).get("f", ""),
                    "auction": "AUCTION" in (it.get("options") or []), "offers": "BEST_OFFER" in (it.get("options") or [])})
    return out


def main():
    stores = json.load(open(os.path.join(HERE, "stores.json"), encoding="utf-8"))
    cpath = os.path.join(HERE, "mc_cache.json")
    cache = json.load(open(cpath, encoding="utf-8")) if os.path.exists(cpath) else {}
    rate, rate_src = usd_aud()
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(days=CACHE_DAYS)).isoformat()
    items = [i for i in stores.get("items", []) if i.get("seller") in SHOPS and i.get("available", True)]
    items += ebay_items()
    cards = [(i, card_of(i["title"])) for i in items]
    cards.sort(key=lambda ic: str(ic[0].get("listed", "")), reverse=True)  # today's stock first
    looked = 0
    for i, c in cards:
        if c["other"]:
            continue
        k = key_of(c)
        hit = cache.get(k)
        if hit and hit.get("checked", "") > stale:
            continue
        if looked >= MAX_LOOKUPS:
            continue
        looked += 1
        try:
            url, page = find_card(c, ref=(hit or {}).get("path"))
            info = read_page(page)
            if not any(info["main"].values()) and not any(info["full"].values()):
                raise RuntimeError("page had no prices")
            cache[k] = {"url": url, "path": urllib.parse.urlparse(url).path, "info": info, "checked": now.isoformat()}
        except Exception as e:
            cache[k] = {"error": str(e)[:200], "checked": now.isoformat()}
        time.sleep(1.5)

    live = live_index()
    out = []
    for i, c in cards:
        price, post = float(i.get("price") or 0), float(i.get("post") or 0)
        cost = price + post
        hit = {"error": f"{c['other']} card: not on PriceCharting's English/Japanese lists"} if c["other"] else cache.get(key_of(c), {})
        row = {"title": c["title"], "shop": i["seller"], "url": i["url"], "listed": i.get("listed"),
               "price": price, "post": post, "cost": cost, "grader": c["grader"], "grade": c["grade"],
               "jp": c["jp"], "links": ebay_links(c), "live": cheapest_live(live, c["title"]),
               "auction": i.get("auction", False), "offers": i.get("offers", False)}
        if "info" not in hit:
            row.update(signal="UNPRICED", why=hit.get("error", "not looked up yet"))
            out.append(row)
            continue
        info = hit["info"]
        usd, note = value_for(info, c)
        aud_all = {k: round(v * rate) for k, v in info["main"].items() if v}
        row.update(pc=hit["url"], matched=info["name"], aud=aud_all, basis=note)
        if not usd:
            row.update(signal="UNPRICED", why=f"no PriceCharting value at {note.get('grade')}")
            out.append(row)
            continue
        val = round(usd * rate)
        landed = round((usd * rate + (SHIP_SLAB if c["grade"] else SHIP_RAW)) * GST)
        row.update(value=val, landed=landed, gap=round(cost / val - 1, 2), thin=bool(note.get("thin")))
        row["signal"] = ("UNDER" if cost <= val * UNDER else "FAIR" if cost <= val * FAIR
                         else "OVER-US" if cost <= landed else "OVER")
        if not c["grade"]:
            for gk, lab in (("g8", "psa8_net"), ("g9", "psa9_net")):
                gv, _ = value(info, gk)
                row[lab] = round(gv * rate - cost - GRADING) if gv else None
        out.append(row)

    json.dump({"generated": now.isoformat(timespec="minutes"), "usd_aud": rate, "rate_source": rate_src,
               "lookups_this_run": looked, "cards": out}, open(os.path.join(HERE, "megacash.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(cache, open(cpath, "w", encoding="utf-8"), ensure_ascii=False)
    write_md(out, rate, now)
    n = {s: sum(r["signal"] == s for r in out) for s in ("UNDER", "FAIR", "OVER-US", "OVER", "UNPRICED")}
    print(f"shop pricer: {len(out)} cards, {looked} PriceCharting lookups, US$1 = A${rate} | " +
          ", ".join(f"{k} {v}" for k, v in n.items()))


def line(r, full):
    g = f"{r['grader']} {r['grade']}" if r["grade"] else "raw"
    s = f"- **A${r['cost']:.0f}** [{r['title']}]({r['url']}) · {g}"
    if r.get("shop", "Mega Cash") != "Mega Cash":
        s += f" · {r['shop']}" + (" (auction, current bid)" if r.get("auction") else " (Best Offer)" if r.get("offers") else "")
    if "value" not in r:
        return s + f" · {r.get('why', '')}"
    b = r["basis"]
    s += f" · value A${r['value']} ({r['gap']:+.0%})"
    if r.get("thin"):
        s += " · ⚠️THIN"
    if not full:
        return s
    s += f" · {b.get('basis')}" + (f", {b['n90']} sales/90d" if b.get("n90") is not None else "") + (f", last {b['last']}" if b.get("last") else "")
    if b.get("estimate"):
        s += f" · {b['estimate']}"
    s += f" · import A${r['landed']}"
    if r.get("live"):
        lv = r["live"]
        s += f" · cheapest live [{lv['country']} A${lv['aud']:.0f}]({lv['url']}) ({lv['copies']} copies)"
    if r.get("psa9_net") is not None:
        s += f" · graded PSA 8 {r['psa8_net']:+.0f} / PSA 9 {r['psa9_net']:+.0f}"
    s += f" · [PC: {r['matched']}]({r['pc']})"
    L = r["links"]
    return s + f" · [AU]({L['au']}) [JP]({L['jp']}) [CN]({L['cn']}) [Sold]({L['sold']})"


def write_md(out, rate, now):
    new_cut = (now - timedelta(days=2)).date().isoformat()
    L = [f"# Shop pricer {now.date().isoformat()}",
         f"US$1 = A${rate}. Value = median of the last 90 days' sales at the card's grade (raw = NM), or PriceCharting's "
         f"average marked ⚠️THIN when there were under 3 sales. Cost = shop price + post. Graded net assumes A${GRADING} grading.", ""]
    groups = [("UNDER", "Under value (cost 20%+ below value)", True),
              ("FAIR", "Fair (within value)", True),
              ("OVER-US", "Above US value, below import cost", False),
              ("OVER", "Over (dearer than importing)", False),
              ("UNPRICED", "Unpriced (check by hand)", False)]
    for sig, head, full in groups:
        rows = sorted((r for r in out if r["signal"] == sig), key=lambda r: (r.get("thin", False), r.get("gap", 0)))
        fresh = [r for r in rows if str(r.get("listed", "")) >= new_cut]
        L.append(f"## {head}: {len(rows)} ({len(fresh)} new)")
        for r in (rows if full else fresh):
            L.append(line(r, full))
        if not full and len(rows) > len(fresh):
            L.append(f"- …{len(rows) - len(fresh)} older, in megacash.json")
        L.append("")
    open(os.path.join(HERE, "megacash.md"), "w", encoding="utf-8").write("\n".join(L))


if __name__ == "__main__":
    main()
