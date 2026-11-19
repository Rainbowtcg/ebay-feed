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
from feedlib import categories, QueryMatcher, card_key, grade_of, land_aud, lang_of, norm, set_of, SETS, REPRINT, VARIANTS
try:
    from feedlib import landed_aud
except Exception:  # older feedlib: no GST
    def landed_aud(it, p=None):
        return land_aud(it) if p is None else p

HERE = os.path.dirname(os.path.abspath(__file__))
KEEP_DAYS = 30
PAWN_CAT = "Pawn shops"          # heading in queries.txt; also every listing from a pawn_sellers seller
TRUSTED_CAT = "Trusted sellers"  # heading in queries.txt; also every listing from a trusted_sellers.txt seller
SLAB_CAT = "Slab plays"          # played raw copies of cards whose low-grade slabs sell for more than raw + grading
GRADE_COST = 70                  # A$ per card to grade (PSA via an AU submitter, value tiers)
SELL_KEEP = 0.87                 # share of the sale price kept after eBay fees
SKIP_CC = {"US", "GB", "UK", "CA"}
AUCTION_HOURS = 24               # auctions ending within this many hours are shown when the bid is well under value
AUCTION_RATIO = 0.7              # ... at or under 70% of the card's value (landed, with GST)
NEO_NAME = {"1": " neo genesis", "2": " neo discovery", "3": " neo revelation", "4": " neo destiny"}


def key_title(t):
    """"Neo 3" / "neo3" titles get the set's name, so the same-card key keeps Neo 2 and Neo 3 Houndoom No.229 apart."""
    m = re.search(r"\bneo ([1-4])\b", neo_n(norm(t)))
    return t + NEO_NAME[m.group(1)] if m and not set_of(t) else t


def search_words(c):
    """Broad eBay search for a valued card: its name words plus number variants, e.g. houndoom (229,no.229)."""
    n = str(c.get("num") or "")
    if n.lower().startswith("no."):
        v = [str(int(n[3:])) if n[3:].isdigit() else n[3:], "no." + n[3:]]
    elif "/" in n:
        a, b = n.split("/", 1)
        v = [n]
        if a.isdigit() and b.isdigit():
            v += [f"{int(a)}/{int(b)}", f"{a.zfill(3)}/{b.zfill(3)}"]
    else:
        v = [n] if n else []
    v = list(dict.fromkeys(x for x in v if x))
    num = (v[0] if len(v) == 1 else "(" + ",".join(v) + ")") if v else ""
    return " ".join(list(c.get("words") or []) + ([num] if num else [])).strip()


NEO_N = [("neo 1 ", "N1"), ("neo 2 ", "N2"), ("neo 3 ", "N3"), ("neo 4 ", "N4")]  # Japanese Neo sets as PSA labels name them


def neo_n(t):
    """"neo2" / "neo-2" -> "neo 2", so PSA-label style set names count."""
    return re.sub(r"\bneo\s*-?\s*([1-4])\b", r"neo \1", t)


POKEDEX_SETS = {"BS", "JU", "FO", "TR", "G1", "G2", "N1", "N2", "N3", "N4", "SI", "VS", "WEB"}  # numbered No.xxx in Japan
CN_RAW_MIN = 0.6                 # raw cards from China under 60% of value: usually fakes, not highlighted
SUSPECT_MIN = 3                  # a value is "suspect" when 3+ live copies sit 15%+ under it ...
SUSPECT_MED = 0.75               # ... and the typical (median) live copy is under 75% of it
MODERN = re.compile(r'\b(2019|202\d)\b|\b151\b|\bsar\b|\bsir\b|\bsv\d|scarlet|violet|vmax|vstar|master ball'
                    r'|\b(sv\d*[a-z]?|s\d{1,2}[a-z]|m\d[a-z]|sm(1[0-2]|[5-9])[a-z]?|swsh\d*)\b|mega dream|phantasmal|sword|shield', re.I)
SV_MEP = re.compile(r"\b(sv-?p|svp|m-?p|mep)\b|black star promo.*\b(sv|mega)\b", re.I)   # hidden on the finder by default too
PRE = re.compile(r"\b(dark|light|shining|rocket's|team rocket's|gold star|crystal)\b", re.I)
NOSYM = re.compile(r"no[- ]?(set[- ]?)?symbol|no[- ]?rarity|without (set )?symbol|missing (set )?symbol", re.I)  # Jungle no-symbol, JP no-rarity
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
            # bare Pokedex number ("Houndoom Holo 229 Neo 2"), only when the title has no other card number:
            # 3 digits, not part of 12/34, 9.5, a year or "pop 441"
            for n in [] if out else re.findall(r"(?<![/\d.])(?<!pop )(?<!pop: )\b(\d{3})\b(?![/\d.])", t, re.I):
                out.add(f"No.{int(n)}")
        return out

    @staticmethod
    def _fits_set(title, c):
        """Japanese vintage cards are numbered by Pokédex number (No.229), so the same number is in several sets.
        False when such a title names one of those sets but not the value card's set (Neo Discovery vs Neo Revelation,
        Erika's Ivysaur Gym Challenge vs Base Set Ivysaur). Cards numbered 4/102 etc. are left alone."""
        if not str(c.get("num", "")).startswith("No."):
            return True
        want = set_of(c["label"])
        t = neo_n(norm(title)) + " "
        named = {code for name, code in SETS + NEO_N if name in t and code in POKEDEX_SETS}
        return not (want and named and want not in named)

    @staticmethod
    def _score(tl, c):
        """How specifically the title names this card's set: longest matching key wins."""
        tl = neo_n(tl)
        return max([len(k) for k in c["keys"] if k in tl] or [0])

    def find_slab(self, title):
        """Slab-play entry for a raw copy in any condition: the card's low-grade and PSA 7 sale prices."""
        if REPRINT.search(title) or SLAB.search(re.sub(r"\b(psa|cgc|bgs)\s*\d*\s*(contender|candidate|worthy|ready|potential)\b", "", title, flags=re.I)):
            return None
        tl, lang = norm(title), lang_of(title)
        first, rev, sl = bool(FIRST.search(title)), bool(REV.search(title)), "shadowless" in tl
        best = []
        for n in self.nums(title):
            for c in self.by.get(n, []):
                if not c.get("slab") or c["lang"] != lang:
                    continue
                if c["keys"] and not any(k in neo_n(tl) for k in c["keys"]):
                    continue
                if not all(w in tl for w in c["words"]) or c["first"] != first or c["rev"] != rev or c["sl"] != sl:
                    continue
                if {m.lower() for m in PRE.findall(re.sub(r"light(ly)?[ -]?play(ed)?", "", title, flags=re.I))} != {m.lower() for m in PRE.findall(c["label"])}:
                    continue
                if {m.lower() for m in ODD.findall(title)} != {m.lower() for m in ODD.findall(c["label"])}:
                    continue
                if bool(NOSYM.search(title)) != bool(NOSYM.search(c["label"])):
                    continue
                if not self._fits_set(title, c):
                    continue
                best.append((self._score(tl, c), c))
        return max(best, key=lambda b: b[0])[1] if best else None

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
        best = []
        for n in self.nums(title):
            for c in self.by.get(n, []):
                if c["lang"] != lang or g not in c["g"]:
                    continue
                if c["keys"] and not any(k in neo_n(tl) for k in c["keys"]):
                    continue
                if not all(w in tl for w in c["words"]):
                    continue
                if c["first"] != first or c["rev"] != rev or c["sl"] != sl:
                    continue
                if {m.lower() for m in PRE.findall(re.sub(r"light(ly)?[ -]?play(ed)?", "", title, flags=re.I))} != {m.lower() for m in PRE.findall(c["label"])}:
                    continue  # Dark / Light / Shining Houndoom are different cards from Houndoom
                if {m.lower() for m in ODD.findall(title)} != {m.lower() for m in ODD.findall(c["label"])}:
                    continue  # promo / premium file / vending / deck printings are different cards from the set card
                if bool(NOSYM.search(title)) != bool(NOSYM.search(c["label"])):
                    continue  # no-symbol / no-rarity printings are different cards (and values) from the normal one
                if not self._fits_set(title, c):
                    continue  # same number, different set (Japanese No.229 Houndoom is in Neo 2 and Neo 3)
                best.append((self._score(tl, c), c))
        if not best:
            return None
        c = max(best, key=lambda b: b[0])[1]
        return c, g, c["g"][g]


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
    cats = [SLAB_CAT] + [c for c in dict.fromkeys(c for _, c in qcat) if c != SLAB_CAT]
    want_cat = {c: any(q.startswith("+") for q, cc in qcat if cc == c) for c in cats}
    want_cat[SLAB_CAT] = True
    qm = QueryMatcher([q for q, _ in qcat if "@pawn" not in q and "@trusted" not in q])  # seller-only lines match any title
    pawn_set = {str(n).lower() for n in L.get("pawn_sellers", [])}
    try:
        trusted_set = {l.split("#")[0].strip().lower() for l in open(P("trusted_sellers.txt"), encoding="utf-8")} - {""}
    except Exception:
        trusted_set = set()

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
        k = card_key(key_title(x.get("title", "")))
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
            if not r.get("a"):  # auction bids (often A$1-10 starts) aren't asking prices
                low[k] = min(low.get(k, 1e12), r.get("pm") or 1e12)
            seen_n[k] = seen_n.get(k, 0) + 1
    live_n = {}
    for i in cur:
        k = ids[i].get("k")
        if k:
            live_n[k] = live_n.get(k, 0) + 1

    # 1b. card values for every live listing, and values that look wrong
    # (many live copies far "under" a value usually means the value or the match is off, not that they're all bargains)
    gen = L.get("generated")
    try:
        now = datetime.fromisoformat(str(gen).replace("Z", "+00:00"))
    except Exception:
        now = datetime.now(timezone.utc)
    vals, groups = {}, {}
    for i, x in cur.items():
        val = V.find(x.get("title", ""), x.get("nm") == 1)
        vals[i] = val
        if val and "AUCTION" not in (x.get("options") or []) and x.get("country") not in SKIP_CC and not x.get("flag"):
            groups.setdefault((val[0].get("id") or val[0]["label"], val[1]), []).append(
                (landed_aud(x, ids[i]["p"]) / val[2], str(x.get("seller") or ""), i))
    suspect = {}
    for (vid, g), rs in groups.items():
        under = [r for r in rs if r[0] <= 0.85]
        med = sorted(r[0] for r in rs)[len(rs) // 2]
        if len(under) >= SUSPECT_MIN and med <= SUSPECT_MED:
            c = vals[rs[0][2]][0]
            suspect[(vid, g)] = {"label": c["label"], "grade": g, "value": c["g"][g], "copies": len(rs), "under": len(under),
                                 "sellers": len({r[1] for r in rs}), "median_ratio": round(med, 2), "src": c.get("src", ""),
                                 "pc": c.get("pc", ""), "id": vid}

    # 2. per-listing extras + categories
    X, digest = {}, {c: {"cat": c, "want": want_cat[c], "total": 0, "new": 0, "drops": 0, "relisted": 0, "under": 0, "hi": []} for c in cats}
    digest["Other"] = {"cat": "Not in a category", "want": False, "total": 0, "new": 0, "drops": 0, "relisted": 0, "under": 0, "hi": []}
    for i, x in cur.items():
        t = x.get("title", "")
        r = ids[i]
        cs = list(dict.fromkeys(([cat_of_q[x["query"]]] if x.get("query") in cat_of_q else []) + [cat_of_q[q] for q in qm.matches(t)]))
        if PAWN_CAT in cats and str(x.get("seller") or "").lower() in pawn_set:
            cs = list(dict.fromkeys(cs + [PAWN_CAT]))   # anything a pawn shop lists, whichever search found it
        if TRUSTED_CAT in cats and str(x.get("seller") or "").lower() in trusted_set:
            cs = list(dict.fromkeys(cs + [TRUSTED_CAT]))
        auc = "AUCTION" in (x.get("options") or [])
        val = vals.get(i)
        p = r["p"]
        pl = landed_aud(x, p)        # with GST on overseas items: what it really costs delivered
        new = had_before and r["f"] == today and not r.get("re")
        drop = r.get("pp") and p <= r["pp"] * 0.95 and r["pp"] - p >= 2
        k = r.get("k")
        lowest = bool(k and not auc and seen_n.get(k, 0) >= 3 and p <= low.get(k, 0) + 0.01)
        ratio = round(pl / val[2], 3) if val else None
        sus = suspect.get((val[0].get("id") or val[0]["label"], val[1])) if val else None
        hours = None
        if auc and x.get("ends"):
            try:
                hours = (datetime.fromisoformat(str(x["ends"]).replace("Z", "+00:00")) - now).total_seconds() / 3600
            except Exception:
                hours = None
        slab, slab_why = None, ""
        flag = str(x.get("flag") or "")
        if not auc and x.get("country") not in SKIP_CC and not flag.startswith(("reprint", "grader")):
            slab = V.find_slab(t)
            if slab:
                sv = slab["slab"]
                floor = sv["low"] * SELL_KEEP - GRADE_COST - p      # if it comes back a low grade
                at7 = sv["mid"] * SELL_KEEP - GRADE_COST - p        # if it comes back a PSA 7
                if at7 >= 100 and floor >= -30:
                    slab_why = (f"slab play: low grade ({sv['lowg']}) ~A${sv['low']:,} → {'+' if floor >= 0 else '−'}A${abs(floor):,.0f}; "
                                f"PSA 7 ~A${sv['mid']:,} → +A${at7:,.0f} after grading and fees ({sv['src']})")
                    cs = [SLAB_CAT] + [c for c in cs if c != SLAB_CAT]
                else:
                    slab = None
        tags = "".join(c for c, on in (("n", new), ("d", drop), ("r", bool(r.get("re"))), ("l", lowest), ("s", bool(slab))) if on)
        X[i] = [[cats.index(c) for c in cs], r["f"], r.get("pp") if drop else None, round(low[k], 2) if k in low else None,
                live_n.get(k, 0) if k else 0, val[2] if val else None, (val[0]["label"] + " · " + val[1]) if val else "", tags,
                (1 if val[1] in (val[0].get("thin") or []) else 0) if val else 0,   # value rests on few sales
                search_words(val[0]) if val else "", (val[0].get("checked") or "") if val else ""]

        # digest: same rules as the finder's default view
        if slab:
            d = digest[SLAB_CAT]
            d["total"] += 1
            d["new"] += bool(new)
            d["drops"] += bool(drop)
            d["hi"].append((0, -(at7 + floor), p, {"id": i, "t": t[:90], "p": p, "cc": x.get("country") or "", "seller": x.get("seller") or "",
                                                    "url": x.get("url") or f"https://www.ebay.com.au/itm/{i}", "why": slab_why, "auc": auc,
                                                    "cond": flag.replace("damage: ", "") or ("NM" if x.get("nm") == 1 else "")}))
        if x.get("country") in SKIP_CC or x.get("flag"):
            continue
        if (MODERN.search(t) and not str(x.get("query") or "").startswith("+") and not x["shop"]) or SV_MEP.search(t):
            continue
        if p > 1000 and SLAB.search(t):
            continue
        reason, pri, score = None, 9, 0
        gst = " incl. GST" if pl > p else ""
        if ratio is not None and ratio < 0.4:
            pass  # far too cheap for the card: nearly always a reprint, fake, wrong version or played copy; not a highlight
        elif ratio is not None and ratio < CN_RAW_MIN and x.get("country") == "CN" and val[1] == "Raw":
            pass  # raw cards from China far under value: usually fakes
        elif ratio is not None and ratio <= 0.85 and not auc and sus:
            reason, pri, score = (f"{round((1 - ratio) * 100)}% under A${val[2]:,} value{gst} — but {sus['under']} of {sus['copies']} live copies are "
                                  f"too, so the value may be wrong ({val[1]})"), 3, ratio
        elif ratio is not None and ratio <= 0.85 and not auc:
            reason, pri, score = f"{round((1 - ratio) * 100)}% under A${val[2]:,} value{gst} ({val[1]}, {val[0]['src'].split('(')[0].strip()})", 0, ratio
        elif (ratio is not None and auc and hours is not None and 0 < hours <= AUCTION_HOURS and ratio <= AUCTION_RATIO
              and not sus and not (x.get("country") == "CN" and val[1] == "Raw" and ratio < CN_RAW_MIN)):
            reason, pri, score = (f"auction ends in {hours:.0f}h: A${pl:,.0f}{gst} now, {round((1 - ratio) * 100)}% under A${val[2]:,} value "
                                  f"({val[1]}; {x.get('bids') or 0} bids)"), 1, ratio
        elif drop:
            reason, pri, score = f"price dropped A${r['pp']:,.0f} → A${p:,.0f}", 1, p / r["pp"]
        elif new and lowest:
            reason, pri, score = f"new, cheapest of {seen_n[k]} copies seen in 30 days", 2, 0
        elif r.get("re") and not auc and r.get("p0") and p < r["p0"] * 0.95:
            reason, pri, score = f"relisted cheaper (A${r['p0']:,.0f} → A${p:,.0f})", 1, p / r["p0"]
        for c in ([c for c in cs if c != SLAB_CAT] or ["Other"]):
            d = digest[c]
            d["total"] += 1
            d["new"] += bool(new)
            d["drops"] += bool(drop)
            d["relisted"] += bool(r.get("re"))
            d["under"] += bool(ratio is not None and 0.4 <= ratio <= 0.85 and not auc and not sus)
            if reason:
                d["hi"].append((pri, score, p, {"id": i, "t": t[:90], "p": p, "cc": x.get("country") or "", "seller": x.get("seller") or "",
                                                "url": x.get("url") or f"https://www.ebay.com.au/itm/{i}", "why": reason, "auc": auc}))
    for d in digest.values():
        d["hi"] = [h[3] for h in sorted(d["hi"], key=lambda h: h[:3])[:12 if d["cat"] == SLAB_CAT else 8]]
    order = [c for c in cats if digest[c]["total"]] + (["Other"] if digest["Other"]["total"] else [])

    H = {"updated": today, "ids": ids}
    json.dump(H, open(P("history.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"day": today, "cats": cats + ["Other"], "x": X}, open(P("enrich.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    tot = {"listings": len(cur), "new": sum(1 for v in X.values() if "n" in v[7]), "drops": sum(1 for v in X.values() if "d" in v[7]),
           "relisted": sum(1 for v in X.values() if "r" in v[7]), "valued": sum(1 for v in X.values() if v[5])}
    sus_list = sorted(suspect.values(), key=lambda v: -v["value"])
    json.dump({"day": today, "generated": L.get("generated"), "totals": tot, "first_day": not had_before,
               "categories": [digest[c] for c in order], "suspect_values": sus_list},
              open(P("digest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # values_refresh.py re-reads these cards' PriceCharting pages first on the next run
    json.dump({"day": today, "cards": sus_list}, open(P("values_suspect.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    md = [f"# Slab Finder summary {today}",
          f"{tot['listings']:,} live listings · {tot['new']:,} new · {tot['drops']:,} price drops · {tot['relisted']:,} relisted · {tot['valued']:,} with a card value",
          "Default view: no US/UK/CA sellers, damaged/reprint/wrong-grader tags hidden, slabs over A$1,000 hidden.", ""]
    for c in order:
        d = digest[c]
        md.append(f"## {c}{' (want list)' if d['want'] else ''} — {d['total']:,} listings, {d['new']} new, {d['drops']} drops, {d['under']} under value")
        for h in d["hi"][:5]:
            md.append(f"- A${h['p']:,.0f} {h['t']} ({h['cc']}, {h['seller']}{', auction' if h['auc'] else ''}): {h['why']} {h['url']}")
        md.append("")
    if sus_list:
        md.append(f"## Values to recheck — {len(sus_list)}")
        md.append("Most live copies sit well under these values, so the value or the card match is probably off. Not counted as under value.")
        for v in sus_list[:15]:
            md.append(f"- {v['label']} {v['grade']}: A${v['value']:,} value; {v['under']} of {v['copies']} copies under it "
                      f"({v['sellers']} seller{'s' if v['sellers'] != 1 else ''}, typical copy {round(v['median_ratio'] * 100)}% of value)")
        md.append("")
    open(P("digest.md"), "w", encoding="utf-8").write("\n".join(md))
    print(f"enrich: {tot}; {len(ids)} listings in 30-day history; {len(order)} categories")


if __name__ == "__main__":
    main()
