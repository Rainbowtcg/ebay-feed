#!/usr/bin/env python3
"""Adds categories, price history and card values to the day's feed, and writes the daily summary.

Usage: python3 enrich.py            (in the repo folder, after ebay_search.py and store_scan.py)
Reads   listings.json, stores.json, queries.txt, values.json, history.json (yesterday's, optional)
Writes  history.json  30 days of prices per listing (first seen, lowest, previous day's price)
        enrich.json   per-listing extras that build_feed.py adds to feed.js for the finder page
        digest.json   per-category summary (counts + highlights) for the finder page
        digest.md     the same summary as text, for the daily phone summary
Read-only: never bids, buys or contacts anyone.
"""
import hashlib, json, os, re, sys
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
    SYD = ZoneInfo("Australia/Sydney")
except Exception:  # pragma: no cover
    SYD = timezone(timedelta(hours=11))
from feedlib import categories, QueryMatcher, card_key, grade_of, land_aud, lang_of, norm, REPRINT, VARIANTS

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP_DAYS = 30
SKIP_CC = {"US", "GB", "UK", "CA"}
MODERN = re.compile(r'\b(2019|202\d)\b|\b151\b|\bsar\b|\bsir\b|\bsv\d|scarlet|violet|vmax|vstar|master ball'
                    r'|\b(sv\d*[a-z]?|s\d{1,2}[a-z]|m\d[a-z]|sm(1[0-2]|[5-9])[a-z]?|swsh\d*)\b|mega dream|phantasmal|sword|shield', re.I)
SV_MEP = re.compile(r"\b(sv-?p|svp|m-?p|mep)\b|black star promo.*\b(sv|mega)\b", re.I)   # hidden on the finder by default too
PRE = re.compile(r"\b(dark|light|shining|rocket's|team rocket's|gold star|crystal)\b", re.I)
ODD = re.compile(r"premium file|vending|carddass|\bdeck\b|\bpromo\b|topsun|bandai|sticker|jumbo|oversized", re.I)
SLAB = re.compile(r"\b(PSA|CGC|BGS|BECKETT|ACE|TAG|SGC)\b", re.I)
REV = dict(VARIANTS)["rev"]
FIRST = dict(VARIANTS)["1st"]


def P(*a):
    return os.path.join(HERE, *a)


def load(name, default):
    try:
        return json.load(open(P(name), encoding="utf-8"))
    except Exception:
        return default


def day_of(iso):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except Exception:
        t = datetime.now(timezone.utc)
    return t.astimezone(SYD).date().isoformat()


# ---------------------------------------------------------------- card values
class Values:
    def __init__(self, cards):
        self.by = {}
        for c in cards:
            self.by.setdefault(c["num"], []).append(c)

    @staticmethod
    def nums(title):
        t = title.replace(" / ", "/")
        out = set()
        for a, b in re.findall(r"\b(\d{1,3}a?)/(\d{2,3})\b", t):
            out.add(f"{a.lstrip('0') or '0'}/{b.lstrip('0')}")
        for a, b in re.findall(r"\b(\d{3})/([A-Z]{1,4}-?P)\b", t, re.I):
            out.add(f"{int(a)}/{b.upper()}")
        if lang_of(title) == "JP":
            for n in re.findall(r"(?:#|\bno\.?\s*)0*(\d{1,3})\b", t, re.I):
                out.add(f"No.{int(n)}")
        return out

    def find(self, title, nm=False):
        if REPRINT.search(title):
            return None
        g = grade_of(title)
        if not g and nm:
            g = "Raw"
        if not g or g == "graded?":
            return None
        tl, lang = norm(title), lang_of(title)
        first, rev, sl = bool(FIRST.search(title)), bool(REV.search(title)), "shadowless" in tl
        for n in self.nums(title):
            for c in self.by.get(n, []):
                if c["lang"] != lang or g not in c["g"]:
                    continue
                if c["keys"] and not any(k in tl for k in c["keys"]):
                    continue
                if not all(w in tl for w in c["words"]):
                    continue
                if c["first"] != first or c["rev"] != rev or c["sl"] != sl:
                    continue
                if {m.lower() for m in PRE.findall(re.sub(r"light(ly)?[ -]?play(ed)?", "", title, flags=re.I))} != {m.lower() for m in PRE.findall(c["label"])}:
                    continue  # Dark / Light / Shining Houndoom are different cards from Houndoom
                if {m.lower() for m in ODD.findall(title)} != {m.lower() for m in ODD.findall(c["label"])}:
                    continue  # promo / premium file / vending / deck printings are different cards from the set card
                return c, g, c["g"][g]
        return None


# ---------------------------------------------------------------- main
def main():
    L = load("listings.json", {"items": []})
    S = load("stores.json", {"items": []})
    V = Values(load("values.json", {"cards": []}).get("cards", []))
    H = load("history.json", {"ids": {}})
    today = day_of(L.get("generated"))
    cutoff = (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat()

    qcat = categories(P("queries.txt"))
    cat_of_q = dict(qcat)
    cats = list(dict.fromkeys(c for _, c in qcat))
    want_cat = {c: any(q.startswith("+") for q, cc in qcat if cc == c) for c in cats}
    qm = QueryMatcher([q for q, _ in qcat])

    items = [dict(x, shop=0) for x in L.get("items", [])] + [dict(x, shop=1) for x in S.get("items", []) if x.get("available", True)]
    ids = H.get("ids", {})
    had_before = any(r.get("f", today) < today for r in ids.values())

    # 1. update history
    by_hash = {}
    for i, r in ids.items():
        if r.get("l", "") < today and r.get("h"):
            by_hash.setdefault(r["h"], i)
    cur = {}
    for x in items:
        i = str(x.get("id"))
        p = land_aud(x)
        if not p:
            continue
        k = card_key(x.get("title", ""))
        auc = 1 if "AUCTION" in (x.get("options") or []) else 0
        h = hashlib.md5((str(x.get("seller")) + "|" + norm(x.get("title", ""))).encode()).hexdigest()[:10]
        r = ids.get(i)
        if r is None:
            r = {"f": today, "l": today, "p0": p, "pm": p, "p": p, "pp": None, "k": k, "h": h, "a": auc}
            old = by_hash.get(h)
            if old and old != i:
                r["re"] = ids[old].get("f")      # relisted: same seller and title seen before under another listing
                r["p0"] = ids[old].get("p0", p) if not (auc or ids[old].get("a")) else p
            ids[i] = r
        else:
            if r.get("l") != today:
                r["pp"] = r.get("p")              # yesterday's (or the last seen day's) price
            if auc or r.get("a"):
                r["pp"] = None                    # auction bids aren't price drops
            r["l"], r["p"], r["k"], r["a"] = today, p, k, auc
            r["pm"] = min(r.get("pm") or p, p)
        cur[i] = x
    for i in [i for i, r in ids.items() if r.get("l", "") < cutoff]:
        del ids[i]

    # lowest price per card over the last 30 days (all listings seen, including ended ones)
    low, seen_n = {}, {}
    for i, r in ids.items():
        k = r.get("k")
        if k:
            low[k] = min(low.get(k, 1e12), r.get("pm") or 1e12)
            seen_n[k] = seen_n.get(k, 0) + 1
    live_n = {}
    for i in cur:
        k = ids[i].get("k")
        if k:
            live_n[k] = live_n.get(k, 0) + 1

    # 2. per-listing extras + categories
    X, digest = {}, {c: {"cat": c, "want": want_cat[c], "total": 0, "new": 0, "drops": 0, "relisted": 0, "under": 0, "hi": []} for c in cats}
    digest["Other"] = {"cat": "Not in a category", "want": False, "total": 0, "new": 0, "drops": 0, "relisted": 0, "under": 0, "hi": []}
    for i, x in cur.items():
        t = x.get("title", "")
        r = ids[i]
        cs = list(dict.fromkeys(([cat_of_q[x["query"]]] if x.get("query") in cat_of_q else []) + [cat_of_q[q] for q in qm.matches(t)]))
        auc = "AUCTION" in (x.get("options") or [])
        val = V.find(t, x.get("nm") == 1)
        p = r["p"]
        new = had_before and r["f"] == today and not r.get("re")
        drop = r.get("pp") and p <= r["pp"] * 0.95 and r["pp"] - p >= 2
        k = r.get("k")
        lowest = bool(k and not auc and seen_n.get(k, 0) >= 3 and p <= low.get(k, 0) + 0.01)
        ratio = round(p / val[2], 3) if val else None
        tags = "".join(c for c, on in (("n", new), ("d", drop), ("r", bool(r.get("re"))), ("l", lowest)) if on)
        X[i] = [[cats.index(c) for c in cs], r["f"], r.get("pp") if drop else None, round(low[k], 2) if k in low else None,
                live_n.get(k, 0) if k else 0, val[2] if val else None, (val[0]["label"] + " · " + val[1]) if val else "", tags]

        # digest: same rules as the finder's default view
        if x.get("country") in SKIP_CC or x.get("flag"):
            continue
        if (MODERN.search(t) and not str(x.get("query") or "").startswith("+") and not x["shop"]) or SV_MEP.search(t):
            continue
        if p > 1000 and SLAB.search(t):
            continue
        reason, pri, score = None, 9, 0
        if ratio is not None and ratio < 0.4:
            pass  # far too cheap for the card: nearly always a reprint, fake, wrong version or played copy; not a highlight
        elif ratio is not None and ratio <= 0.85 and not auc:
            reason, pri, score = f"{round((1 - ratio) * 100)}% under A${val[2]:,} value ({val[1]}, {val[0]['src'].split('(')[0].strip()})", 0, ratio
        elif drop:
            reason, pri, score = f"price dropped A${r['pp']:,.0f} → A${p:,.0f}", 1, p / r["pp"]
        elif new and lowest:
            reason, pri, score = f"new, cheapest of {seen_n[k]} copies seen in 30 days", 2, 0
        elif r.get("re") and not auc and r.get("p0") and p < r["p0"] * 0.95:
            reason, pri, score = f"relisted cheaper (A${r['p0']:,.0f} → A${p:,.0f})", 1, p / r["p0"]
        for c in (cs or ["Other"]):
            d = digest[c]
            d["total"] += 1
            d["new"] += bool(new)
            d["drops"] += bool(drop)
            d["relisted"] += bool(r.get("re"))
            d["under"] += bool(ratio is not None and 0.4 <= ratio <= 0.85 and not auc)
            if reason:
                d["hi"].append((pri, score, p, {"id": i, "t": t[:90], "p": p, "cc": x.get("country") or "", "seller": x.get("seller") or "",
                                                "url": x.get("url") or f"https://www.ebay.com.au/itm/{i}", "why": reason, "auc": auc}))
    for d in digest.values():
        d["hi"] = [h[3] for h in sorted(d["hi"], key=lambda h: h[:3])[:8]]
    order = [c for c in cats if digest[c]["total"]] + (["Other"] if digest["Other"]["total"] else [])

    H = {"updated": today, "ids": ids}
    json.dump(H, open(P("history.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"day": today, "cats": cats + ["Other"], "x": X}, open(P("enrich.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    tot = {"listings": len(cur), "new": sum(1 for v in X.values() if "n" in v[7]), "drops": sum(1 for v in X.values() if "d" in v[7]),
           "relisted": sum(1 for v in X.values() if "r" in v[7]), "valued": sum(1 for v in X.values() if v[5])}
    json.dump({"day": today, "generated": L.get("generated"), "totals": tot, "first_day": not had_before,
               "categories": [digest[c] for c in order]}, open(P("digest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    md = [f"# Slab Finder summary {today}",
          f"{tot['listings']:,} live listings · {tot['new']:,} new · {tot['drops']:,} price drops · {tot['relisted']:,} relisted · {tot['valued']:,} with a card value",
          "Default view: no US/UK/CA sellers, damaged/reprint/wrong-grader tags hidden, slabs over A$1,000 hidden.", ""]
    for c in order:
        d = digest[c]
        md.append(f"## {c}{' (want list)' if d['want'] else ''} — {d['total']:,} listings, {d['new']} new, {d['drops']} drops, {d['under']} under value")
        for h in d["hi"][:5]:
            md.append(f"- A${h['p']:,.0f} {h['t']} ({h['cc']}, {h['seller']}{', auction' if h['auc'] else ''}): {h['why']} {h['url']}")
        md.append("")
    open(P("digest.md"), "w", encoding="utf-8").write("\n".join(md))
    print(f"enrich: {tot}; {len(ids)} listings in 30-day history; {len(order)} categories")


if __name__ == "__main__":
    main()
