#!/usr/bin/env python3
"""Vintage Slab Finder eBay feed.

Searches eBay with the official Browse API for pre-2018 Pokémon cards located
outside the US (Australia, Japan, UK, NZ, Hong Kong, Singapore, Korea, Canada),
pre-filters the titles, and writes listings.json and listings.csv.

Needs two environment variables from a free eBay developer account
(developer.ebay.com > Application Keys > Production):
  EBAY_CLIENT_ID      the App ID
  EBAY_CLIENT_SECRET  the Cert ID
Read-only: it never bids, buys or contacts anyone.
"""
import base64, csv, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

API = "https://api.ebay.com"
CATEGORY = "183454"  # CCG Individual Cards
COUNTRIES = ["AU", "JP", "GB", "NZ", "HK", "SG", "KR", "CA"]
HERE = os.path.dirname(os.path.abspath(__file__))

REPRINT = re.compile(r"celebrations?|classic collection|25th|30th|anniversary|evolutions|\bcp6\b|reprint|proxy|custom|fan ?art|orica|replica|metal card|gold (plated|metal)|digital|tcg ?live|code card|\bonline code", re.I)
MODERN = re.compile(r"\b(20(1[89]|2\d))\b|sword|shield|scarlet|violet|\bsv\d|\bswsh|vmax|vstar|\bv max\b|pokemon 151|obsidian|paldea|paradox|evolving skies|fusion strike|brilliant stars|astral radiance|lost origin|silver tempest|crown zenith|hidden fates|shining fates|team up|unbroken bonds|unified minds|cosmic eclipse|mega evolution|prismatic|surging sparks|stellar crown|temporal forces|twilight masquerade|journey together|destined rivals|white flare|black bolt", re.I)
JUNK = re.compile(r"\blot\b|bundle|binder|sealed|booster|pack\b|box\b|tin\b|sleeve|playmat|case only|empty|choose|pick your|you pick|mystery|repack|graded card guaranteed", re.I)
GRADE = re.compile(r"\b(PSA|CGC|BGS|BECKETT|ACE|TAG|SGC)\s*-?\s*(10|9\.5|9)\b|gem ?mint|\bNM\b|near mint|mint", re.I)


def token(cid, secret):
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    req = urllib.request.Request(API + "/identity/v1/oauth2/token", data=data, headers={"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def search(tok, q, country, offset=0, limit=200):
    params = {"q": q, "category_ids": CATEGORY, "limit": str(limit), "offset": str(offset),
              "filter": f"itemLocationCountry:{country},buyingOptions:{{FIXED_PRICE|AUCTION|BEST_OFFER}}"}
    req = urllib.request.Request(API + "/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": "Bearer " + tok, "X-EBAY-C-MARKETPLACE-ID": "EBAY_AU",
                                          "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=AU,zip=2447"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def keep(title, modern_ok=False):
    if REPRINT.search(title) or JUNK.search(title):
        return False
    if MODERN.search(title) and not modern_ok:
        return False
    return bool(GRADE.search(title))


def main():
    cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (eBay production keys).")
    queries = [l.strip() for l in open(os.path.join(HERE, "queries.txt"), encoding="utf-8") if l.strip() and not l.startswith("#")]
    tok = token(cid, secret)
    seen, out, calls = set(), [], 0
    for q in queries:
        # A "+" at the start marks a want-list search: modern (2018+) cards are kept for it.
        want = q.startswith("+")
        sq = q.lstrip("+").strip()
        for c in COUNTRIES:
            try:
                res = search(tok, sq, c)
                calls += 1
            except Exception as e:  # keep going on one bad query
                print("query failed:", q, c, e, file=sys.stderr)
                continue
            for it in res.get("itemSummaries", []) or []:
                iid = it.get("legacyItemId") or ((it.get("itemId") or "").split("|") + ["", ""])[1] or None
                title = it.get("title", "")
                if not iid or iid in seen or not keep(title, want):
                    continue
                seen.add(iid)
                price = it.get("currentBidPrice") or it.get("price") or {}
                ship = (it.get("shippingOptions") or [{}])[0].get("shippingCost") or {}
                out.append({
                    "id": iid, "title": title, "price": price.get("value"), "currency": price.get("currency"),
                    "post": ship.get("value"), "postCurrency": ship.get("currency"),
                    "options": it.get("buyingOptions"), "bids": it.get("bidCount"), "ends": it.get("itemEndDate"),
                    "condition": it.get("condition"), "country": (it.get("itemLocation") or {}).get("country"),
                    "seller": (it.get("seller") or {}).get("username"), "feedback": (it.get("seller") or {}).get("feedbackPercentage"),
                    "url": f"https://www.ebay.com.au/itm/{iid}", "query": q,
                })
            time.sleep(0.2)
    out.sort(key=lambda x: (x["country"] != "AU", x["country"] != "JP", x["title"]))
    meta = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "count": len(out), "calls": calls,
            "note": "Pre-filtered by title only. Every listing still needs its item page checked before it goes in the finder."}
    with open(os.path.join(HERE, "listings.json"), "w", encoding="utf-8") as f:
        json.dump({**meta, "items": out}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "listings.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()) if out else ["id"])
        w.writeheader(); w.writerows(out)
    print(f"{len(out)} listings from {calls} searches")


if __name__ == "__main__":
    main()
