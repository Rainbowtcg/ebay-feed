"""Keep the listings that matter most current between full eBay scans.

Run by the shop scans (late morning and late afternoon), before enrich.py rebuilds the summary:
  1. Free cleanup: auctions whose end time has passed are dropped from listings.json (no eBay calls).
  2. Live check (one eBay call each, capped by RECHECK_MAX): today's gem buys, grade plays, slab plays,
     want-list highlights, and everything from pawn shops / general sellers. Sold or ended listings are
     dropped; price, postage, bids and eBay's Card Condition are updated.

Needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (same keys as ebay_search.py). Without them only step 1 runs.
"""
import json
import os
import sys
import urllib.error
from datetime import datetime, timezone

from ebay_search import get_item, token, aspects, num, PLAYED

HERE = os.path.dirname(os.path.abspath(__file__))
RECHECK_MAX = int(os.environ.get("RECHECK_MAX", "100"))


def P(f):
    return os.path.join(HERE, f)


def parse_end(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def priority_ids(L, D):
    """Ids in check order: gem buys, grade plays, slab plays, pawn-seller listings, want-list highlights."""
    ids = []
    add = lambda i: ids.append(str(i)) if i and str(i) not in ids else None
    for b in D.get("gem_buys", []):
        add(b.get("id"))
    cats = D.get("categories", [])
    for name in ("Grade plays", "Slab plays"):
        for c in cats:
            if c.get("cat") == name:
                for h in c.get("hi", []):
                    add(h.get("id"))
    pawn = {str(n).lower() for n in L.get("pawn_sellers", [])}
    for x in L.get("items", []):
        if str(x.get("seller") or "").lower() in pawn:
            add(x.get("id"))
    for c in cats:
        if c.get("want"):
            for h in c.get("hi", [])[:8]:
                add(h.get("id"))
    return [i for i in ids if not i.startswith("s-")]   # shop items are refreshed by store_scan.py


def main():
    L = json.load(open(P("listings.json"), encoding="utf-8"))
    try:
        D = json.load(open(P("digest.json"), encoding="utf-8"))
    except FileNotFoundError:
        D = {}
    items = L.get("items", [])
    now = datetime.now(timezone.utc)

    # 1. ended auctions (free)
    keep, ended = [], 0
    for x in items:
        e = parse_end(x.get("ends")) if "AUCTION" in (x.get("options") or []) else None
        if e and e < now:
            ended += 1
            continue
        keep.append(x)
    items = keep

    # 2. live check of the listings that matter most
    cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    gone, changed, checked = 0, 0, 0
    if cid and secret:
        try:
            tok = token(cid, secret)
        except Exception as e:
            print("recheck: no eBay token:", e, file=sys.stderr)
            tok = None
        by_id = {str(x.get("id")): x for x in items}
        drop = set()
        for i in priority_ids(L, D)[:RECHECK_MAX] if tok else []:
            x = by_id.get(i)
            if not x:
                continue
            try:
                it = get_item(tok, i)
            except urllib.error.HTTPError as e:
                if e.code == 404:          # ended, sold or pulled
                    drop.add(i)
                    gone += 1
                    checked += 1
                    continue
                if e.code == 429:
                    print("recheck: eBay call limit reached, stopping", file=sys.stderr)
                    break
                continue
            except Exception:
                continue
            checked += 1
            avail = [a.get("estimatedAvailabilityStatus") for a in it.get("estimatedAvailabilities") or []]
            end = parse_end(it.get("itemEndDate"))
            if "OUT_OF_STOCK" in avail or (end and end < now):
                drop.add(i)
                gone += 1
                continue
            before = (x.get("price"), x.get("post"), x.get("bids"))
            pr = (it.get("currentBidPrice") or it.get("price") or {})
            if pr.get("value"):
                x["price"], x["currency"] = pr["value"], pr.get("currency", x.get("currency"))
            ship = (it.get("shippingOptions") or [{}])[0].get("shippingCost") or {}
            if ship.get("value") is not None:
                x["post"], x["postCurrency"] = ship["value"], ship.get("currency", x.get("postCurrency"))
            if it.get("bidCount") is not None:
                x["bids"] = it["bidCount"]
            asp = aspects(it)
            cc = asp.get("card condition") or next((d.get("values", [{}])[0].get("content") for d in it.get("conditionDescriptors") or []
                                                     if "condition" in str(d.get("name", "")).lower()), None)
            if cc:
                x["cardCondition"] = cc
                if PLAYED.search(cc) and not str(x.get("flag") or ""):
                    x["flag"] = "damage: " + cc.lower()
            x["imgs"] = (1 if it.get("image") else 0) + len(it.get("additionalImages") or [])
            x["rechecked"] = now.strftime("%Y-%m-%dT%H:%MZ")
            if (x.get("price"), x.get("post"), x.get("bids")) != before:
                changed += 1
        items = [x for x in items if str(x.get("id")) not in drop]

    L["items"] = items
    L["count"] = len(items)
    L["rechecked"] = {"at": now.strftime("%Y-%m-%dT%H:%MZ"), "checked": checked, "gone": gone,
                      "changed": changed, "ended_auctions": ended}
    tmp = P("listings.json.tmp")
    json.dump(L, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, P("listings.json"))
    print(f"recheck: {ended} ended auctions dropped; {checked} live-checked, {gone} sold/ended dropped, {changed} price or bid changes")


if __name__ == "__main__":
    main()
