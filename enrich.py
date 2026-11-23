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
from feedlib import categories, QueryMatcher, card_key, grade_of, land_aud, lang_of, norm, set_of, SETS, REPRINT, VARIANTS, fix_spelling
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
GRADE_CAT = "Grade plays"        # Near Mint raw copies where landed cost + grading is well under the PSA 9 price
LOTS_CAT = "Binders & lots"       # heading in queries.txt; @lots listings (never valued as single cards)
SEALED_CAT = "Sealed: ETBs & booster boxes"  # heading in queries.txt; also every shop ETB / booster box
# Australian RRPs (same as the Slab Finder page's NR_RRP): standard Elite Trainer Box, English 36-pack booster box
RRP = {"ETB": 100.0, "Box": 306.0}
GRADE_COST = 70                  # A$ per card to grade (PSA via an AU submitter, value tiers)
SELL_KEEP = 0.87                 # share of the sale price kept after eBay fees
SKIP_CC = {"US", "GB", "UK", "CA"}
AUCTION_HOURS = 24               # auctions ending within this many hours are shown when the bid is well under value
AUCTION_RATIO = 0.7              # ... at or under 70% of the card's value (landed, with GST)
NEO_NAME = {"1": " neo genesis", "2": " neo discovery", "3": " neo revelation", "4": " neo destiny"}
# Deal rules. Sold values come first; asking prices of other live copies are the second opinion,
# used on their own only when a card has no sold value (or one resting on few sales).
ASK_MIN = 3                      # other sellers with a live copy before asking prices mean anything
ASK_DEAL = 0.80                  # no sold value: at or under 80% of the next-cheapest other seller's copy is a highlight ...
ASK_FLOOR = 0.35                 # ... unless it's under 35% of the typical asking price (usually a different card, fake or damaged)
ASK_MIN_PRICE = 20               # asking-price-only highlights start at A$20 (cheaper cards are noise)
GRADE_MIN_NET = 40               # grade play: at least A$40 profit at PSA 9 after grading and fees ...
GRADE_MIN_SHARE = 0.30           # ... and at least 30% of the landed cost
GRADE_MAX_LOSS = 0.25            # ... and one grade lower loses no more than 25% of the landed cost
VINTAGE = {"BS", "B2", "JU", "FO", "TR", "G1", "G2", "N1", "N2", "N3", "N4", "SI", "LC", "EXP", "AQ", "SK", "e2", "e3", "e4", "e5", "VS", "WEB"}
OTHER_SLAB = re.compile(r"\b(ICG|AGS|CCIC|CIC|PCA|MNT|GMA|HGA|KSA|BCCG|AP grading|slabbed|graded)\b", re.I)
LOT_OLD_E = re.compile(r"wotc|wizards|199\d|base set|jungle|fossil|team rocket|gym (?:heroes|challenge)|\bneo\b|e-?card|expedition|"
                       r"aquapolis|skyridge|old back|1st edition|first edition|shadowless|gold star|lv\.?x|legendary collection|"
                       r"southern islands|\bholos?\b|binder|childhood|original", re.I)
NMWORD = re.compile(r"gem ?mint|\bNM\b|near mint|\bmint\b", re.I)
PLAYED_T = re.compile(r"\[(HP|MP|LP|DMG)\]|\b(HP|MP|LP|DMG)\b(?!\s?\d)|heavily|moderately|lightly played|\bplayed\b|damaged|creased?", re.I)
NEWER = re.compile(r"terastal|stellar|mega (evolution|dream)|\bm\d[a-z]?\b|mc battle|\b(mur|sar|sir|ar)\b|\b(2019|202\d)\b|\bex\b.*\b\d{3}/\d{3}\b", re.I)


def key_title(t):
    """"Neo 3" / "neo3" titles get the set's name, so the same-card key keeps Neo 2 and Neo 3 Houndoom No.229 apart.
    Misspelt and romanised Japanese names are corrected first, so "Charzard" and "Lizardon" are Charizard here too."""
    t = fix_spelling(t)
    m = re.search(r"\bneo ([1-4])\b", neo_n(norm(t)))
    return t + NEO_NAME[m.group(1)] if m and not set_of(t) else t


OTHER_TCG = re.compile(r"yu-?gi-?oh|magic|\bmtg\b|one piece|lorcana|digimon|dragon ball|flesh and blood|weiss|union arena|"
                       r"star wars|gundam|grand archive|sorcery|altered|riftbound|naruto|disney", re.I)


def sealed_kind(x):
    """"ETB" / "Box" for sealed listings (eBay @sealed searches and shop sealed scans), else None."""
    k = x.get("sealed")
    if k in RRP and not OTHER_TCG.search(x.get("title", "")):
        return k
    return None


def rrp_value(x):
    """The RRP a sealed listing is judged against, as a card-style value (card, grade, A$), or None when no RRP applies:
    Japanese product, Pokemon Center ETBs, half/mini/enhanced boxes and anything that isn't a standard ETB or 36-pack box."""
    k = sealed_kind(x)
    t = x.get("title", "")
    if not k or OTHER_TCG.search(t) or re.search(r"japan|\bjp\b|\bjpn\b|korean|chinese|pok[eé]mon center|\bpc\b|half|mini|enhanced|18 packs|bundle", t, re.I):
        return None
    name = "Elite Trainer Box" if k == "ETB" else "Booster box (36 packs)"
    return ({"label": name, "id": "rrp-" + k, "thin": [], "src": "Australian RRP", "words": [], "num": ""}, "RRP", RRP[k])


def search_words(c):
    """eBay search for a valued card: name words plus the number as sellers write it, e.g.
    hypno (8/62,008/062,fossil) 1st. English cards also add one set word as an alternative and the variant
    (1st Edition, shadowless, reverse holo, or leave those out when it's the plain card)."""
    n = str(c.get("num") or "")
    jp = c.get("lang") == "JP"
    if not (c.get("words") or n):
        return ""                             # nothing to search on: no link rather than a search for just "-reverse"
    if n.lower().startswith("no."):
        v = [str(int(n[3:])) if n[3:].isdigit() else n[3:], "no." + n[3:]]
    elif "/" in n:
        a, b = n.split("/", 1)
        v = [n]
        if a.isdigit() and b.isdigit():
            v += [f"{int(a)}/{int(b)}", f"{a.zfill(3)}/{b.zfill(3)}"]
    else:
        v = [n] if n else []
    keys = [k for k in (c.get("keys") or []) if k and " " not in k]
    if not jp and keys and "/" in n:
        v.append(keys[0])                     # sellers who write the set name but not the number
    v = list(dict.fromkeys(x for x in v if x))
    num = (v[0] if len(v) == 1 else "(" + ",".join(v) + ")") if v else ""
    extra = []
    if c.get("first"):
        extra.append("1st")
    if c.get("sl"):
        extra.append("shadowless")
    if c.get("rev"):
        extra.append("(reverse,rev)")
    elif not jp:
        extra.append("-reverse")
    if not jp and not c.get("first") and WOTC_KEYS.search(" ".join(c.get("keys") or []) + " " + c.get("label", "")):
        extra.append("-1st")                  # unlimited WotC: 1st Edition copies are a dearer, different card
    return " ".join(list(c.get("words") or []) + ([num] if num else []) + extra).strip()


WOTC_KEYS = re.compile(r"\b(base set|jungle|fossil|team rocket|gym|heroes|challenge|neo|genesis|discovery|revelation|destiny)\b", re.I)


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


PLAYED_EX = re.compile(r"\bexcellent\b|\bexc\b|\bEX\s?[-/+]\s?(NM|near mint)|\bEX\+|\bEXMT\b|\bEX-MT\b", re.I)
PLAYED_LP = re.compile(r"lightly played|\bLP\b|\[LP\]|light play|light wear|\bLP\+", re.I)
PLAYED_BAD = re.compile(r"moderately|heavily|\bMP\b|\[MP\]|\bHP\b(?!\s?\d)|\[HP\]|damaged?|\bdmg\b|creas|\bdent|\bbent\b|\bbend|\btear\b|"
                        r"\btorn\b|water|stain|\bpoor\b|\bas[- ]is\b|heavy wear|\bink\b|marked|writing|peel|ripped|\bcut\b|miscut|scratch", re.I)


def played_grade(title, flag):
    """(grade to judge at, one grade lower, label) for an Excellent or Lightly Played raw listing, else None."""
    t = re.sub(r"\bHP\s?\d+|\d+\s?HP\b", "", title)
    if PLAYED_BAD.search(t) or (flag.startswith("damage:") and not re.search(r"\b(lp|lightly|played)$", flag)):
        return None
    if PLAYED_EX.search(t):
        return ("PSA 6", "PSA 5", "Excellent")
    if PLAYED_LP.search(t) or re.search(r"\b(lp|lightly)$", flag):
        return ("PSA 5", "PSA 4", "Lightly Played")
    return None


def gst_of(pl, p):
    return " incl. GST" if pl > p else ""


def hi_entry(i, x, t, p, pl, why, auc):
    """One digest highlight. p = price + postage; pl = what it costs delivered (with GST on overseas items)."""
    return {"id": i, "t": t[:90], "p": p, "pl": pl, "cc": x.get("country") or "", "seller": x.get("seller") or "",
            "url": x.get("url") or f"https://www.ebay.com.au/itm/{i}", "why": why, "auc": auc, "shop": 1 if x.get("shop") else 0,
            "post?": 1 if (x.get("post") in (None, "") and not x.get("shop")) else 0,
            "ck": "" if x.get("lot") else (card_key(key_title(t)) or "")}


def ebay_warnings(L):
    """Lines for the top of the summary when the day's eBay search didn't fully run."""
    out = []
    done, _, total = str(L.get("searches_done") or "").partition("/")
    try:
        done, total = int(done), int(total)
    except ValueError:
        done = total = None
    carried = int(L.get("carried_over") or 0)
    if done == 0 or L.get("calls") == 0:
        out.append(f"⚠ eBay search did not run: 0 of {total} searches. Every eBay listing below is carried over from an earlier day; "
                   "check the Search eBay step in GitHub Actions.")
    elif done is not None and total and done < total:
        out.append(f"⚠ eBay search incomplete: {done} of {total} searches ran; {carried:,} listings are carried over from an earlier day.")
    bad = L.get("bad_lines") or []
    if bad:
        out.append(f"⚠ eBay rejected {len(bad)} search line{'s' if len(bad) != 1 else ''} in queries.txt (fix or remove): " + " | ".join(bad[:5]))
    return out + ([""] if out else [])


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
        title = fix_spelling(title)
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
    # gem-rate targets from gemrates.py (weekly): PriceCharting card path -> kind (SAFE / HUNT), max raw price, gem rate
    GEM = load("gemrates.json", {}).get("targets") or {}
    H = load("history.json", {"ids": {}})
    today = day_of(L.get("generated"))
    cutoff = (datetime.fromisoformat(today) - timedelta(days=KEEP_DAYS)).date().isoformat()

    qcat = categories(P("queries.txt"))
    cat_of_q = dict(qcat)
    cats = [GRADE_CAT, SLAB_CAT] + [c for c in dict.fromkeys(c for _, c in qcat) if c not in (SLAB_CAT, GRADE_CAT)]
    want_cat = {c: any(q.startswith("+") for q, cc in qcat if cc == c) for c in cats}
    want_cat[SLAB_CAT] = want_cat[GRADE_CAT] = True
    shop_seen = ""
    try:
        shop_seen = datetime.fromisoformat(str(S.get("generated")).replace("Z", "+00:00")).astimezone(SYD).strftime("%-d %b %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    except Exception:
        pass
    qm = QueryMatcher([q for q, _ in qcat if "@pawn" not in q and "@trusted" not in q and "@sealed" not in q])  # seller-only lines match any title
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
        k = None if x.get("lot") else card_key(key_title(x.get("title", "")))   # lots aren't one card
        auc = 1 if "AUCTION" in (x.get("options") or []) else 0
        h = hashlib.md5((str(x.get("seller")) + "|" + norm(x.get("title", ""))).encode()).hexdigest()[:10]
        r = ids.get(i)
        if r is None:
            r = {"f": today, "l": today, "p0": p, "pm": p, "p": p, "pp": None, "k": k, "h": h, "a": auc, "v": 1 if x.get("var") else 0}
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
            if x.get("var"):
                r["pp"] = None                    # multi-option listings: the price may be a different option each day
            r["l"], r["p"], r["k"], r["a"], r["v"] = today, p, k, auc, 1 if x.get("var") else 0
            r["pm"] = min(r.get("pm") or p, p)
        cur[i] = x
    for i in [i for i, r in ids.items() if r.get("l", "") < cutoff]:
        del ids[i]

    # lowest price per card over the last 30 days (all listings seen, including ended ones)
    low, seen_n = {}, {}
    for i, r in ids.items():
        k = r.get("k")
        if k:
            if not r.get("a") and not r.get("v"):  # auction bids (often A$1-10 starts) and multi-option prices aren't asking prices
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
        val = None if x.get("lot") else V.find(x.get("title", ""), x.get("nm") == 1)
        vals[i] = val
        if val and "AUCTION" not in (x.get("options") or []) and x.get("country") not in SKIP_CC and not x.get("flag"):
            groups.setdefault((val[0].get("id") or val[0]["label"], val[1]), []).append(
                (landed_aud(x, ids[i]["p"]) / val[2], str(x.get("seller") or ""), i))
    for i, x in cur.items():  # sealed ETBs / boxes are judged against RRP, not a card value
        if sealed_kind(x):
            vals[i] = rrp_value(x)
    suspect = {}
    for (vid, g), rs in groups.items():
        under = [r for r in rs if r[0] <= 0.85]
        med = sorted(r[0] for r in rs)[len(rs) // 2]
        if len(under) >= SUSPECT_MIN and med <= SUSPECT_MED:
            c = vals[rs[0][2]][0]
            suspect[(vid, g)] = {"label": c["label"], "grade": g, "value": c["g"][g], "copies": len(rs), "under": len(under),
                                 "sellers": len({r[1] for r in rs}), "median_ratio": round(med, 2), "src": c.get("src", ""),
                                 "pc": c.get("pc", ""), "id": vid}

    # 1c. asking prices of the card's other live copies (landed, Buy It Now only): the second opinion when sold data is thin.
    # Compared with the next-cheapest copy from another seller rather than the average, since overpriced listings
    # drag averages up and make ordinary prices look like bargains.
    asks = {}
    for i, x in cur.items():
        k = ids[i].get("k")
        if (k and "AUCTION" not in (x.get("options") or []) and not x.get("flag") and not x.get("var")
                and x.get("country") not in SKIP_CC):
            asks.setdefault(k, []).append((landed_aud(x, ids[i]["p"]), i, str(x.get("seller") or "").lower()))

    def ask_of(i, k, seller):
        """{"med", "n", "sellers", "next"} for the card's other sellers' live copies, or None when fewer than ASK_MIN sellers."""
        seller = str(seller or "").lower()
        best = {}
        for p, j, s_ in asks.get(k, []):
            if j != i and s_ != seller and p > 0:
                best[s_] = min(best.get(s_, 1e12), p)   # one price per seller: their cheapest copy
        ps = sorted(best.values())
        if len(ps) < ASK_MIN:
            return None
        n = len(ps)
        med = ps[n // 2] if n % 2 else (ps[n // 2 - 1] + ps[n // 2]) / 2
        return {"med": med, "n": n, "next": ps[0]}

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
        if SEALED_CAT in cats and sealed_kind(x):
            cs = list(dict.fromkeys([SEALED_CAT] + cs))
        if x.get("lot") and LOTS_CAT in cats:
            cs = [LOTS_CAT]                  # binders and lots stay in their own category ...
        elif LOTS_CAT in cs:
            cs = [c for c in cs if c != LOTS_CAT]   # ... and single cards never go in it
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
        # slab play for any valued card: an Excellent or Lightly Played raw copy, judged at the grade that condition
        # usually gets (Excellent PSA 6, Lightly Played PSA 5); it has to pay at that grade and lose little one grade lower.
        # Moderately / heavily played and damaged copies stay out (only the slab-play want-list cards above take those).
        if (not slab and not auc and x.get("country") not in SKIP_CC | {"CN"} and not x.get("var") and not x.get("lot")
                and not SLAB.search(t) and not OTHER_SLAB.search(t) and not flag.startswith(("reprint", "grader"))):
            cond = played_grade(t, flag)
            if cond:
                pv = V.find(t, True)
                if pv and pv[1] == "Raw" and pv[0].get("src", "").startswith("PriceCharting"):
                    gv = pv[0]["g"]
                    want_g, low_g, label = cond
                    pw, plow = gv.get(want_g), gv.get(low_g)
                    if pw:
                        net = pw * SELL_KEEP - GRADE_COST - pl
                        net_low = plow * SELL_KEEP - GRADE_COST - pl if plow else None
                        if net >= max(GRADE_MIN_NET, GRADE_MIN_SHARE * pl) and (net_low is None or net_low >= -GRADE_MAX_LOSS * pl):
                            slab = {"slab": {}}
                            thin = [g for g in (low_g, want_g) if g in (pv[0].get("thin") or [])]
                            slab_why = (f"slab play ({label}, judged at {want_g}): A${pl:,.0f} landed{gst_of(pl, p)} → {want_g} ~A${pw:,} = "
                                        f"+A${net:,.0f} after A${GRADE_COST} grading and fees"
                                        + (f"; {low_g} ~A${plow:,} = {'+' if net_low >= 0 else '−'}A${abs(net_low):,.0f}" if net_low is not None else "")
                                        + (f"; PSA 7 ~A${gv['PSA 7']:,}" if gv.get("PSA 7") else "")
                                        + (" (low-grade prices are PriceCharting averages with few sales)" if thin else "")
                                        + f" — {pv[0]['label']}. Check the photos: the condition is the seller's word")
                            at7, floor = net, (net_low if net_low is not None else net)
                            cs = [SLAB_CAT] + [c for c in cs if c != SLAB_CAT]
        # grade play: Near Mint raw copy that pays after grading at the grade it's likely to get (PSA 8 for WotC/e-Card-era
        # and old Japanese cards, PSA 9 for later ones), without a big loss one grade lower
        gp, gp_why, gp_net = False, "", 0
        nm_raw_ok = bool(val and val[1] == "Raw" and not auc and not flag and not x.get("var") and x.get("country") not in SKIP_CC | {"CN"}
                         and (x.get("nm") == 1 or NMWORD.search(t)) and not OTHER_SLAB.search(t) and not sus and ratio is not None and ratio >= 0.4)
        gem = GEM.get(val[0].get("pc")) if val and val[0].get("pc") else None
        if nm_raw_ok:
            gv = val[0]["g"]
            old = set_of(val[0]["label"]) in VINTAGE or str(val[0].get("num", "")).startswith("No.")
            want_g, low_g = ("PSA 8", "PSA 7") if old else ("PSA 9", "PSA 8")
            pw, plow = gv.get(want_g), gv.get(low_g)
            pr = gv.get("Raw")
            if pw and not val[0].get("src", "").startswith("PriceCharting"):
                pw = None   # hand-entered benchmarks aren't trusted for grading maths (they get re-read from PriceCharting over time)
            if pw and pr and pw > 15 * pr:
                pw = None   # graded price over 15x raw: usually a mismatched value, not a real spread
            if pw:
                gp_net = pw * SELL_KEEP - GRADE_COST - pl
                net_low = plow * SELL_KEEP - GRADE_COST - pl if plow else None
                if gp_net >= max(GRADE_MIN_NET, GRADE_MIN_SHARE * pl) and (net_low is None or net_low >= -GRADE_MAX_LOSS * pl):
                    gp = True
                    up = [g for g in (("PSA 9", "PSA 10") if old else ("PSA 10",)) if gv.get(g)]
                    thin = [g for g in (low_g, want_g) + tuple(up) if g in (val[0].get("thin") or [])]
                    gp_why = (f"grade play{' (vintage: judged at PSA 8)' if old else ''}: A${pl:,.0f} landed raw{gst_of(pl, p)} → "
                              f"{want_g} ~A${pw:,} = +A${gp_net:,.0f} after A${GRADE_COST} grading and fees"
                              + (f"; {low_g} ~A${plow:,} = {'+' if net_low >= 0 else '−'}A${abs(net_low):,.0f}" if net_low is not None else "")
                              + "".join(f"; {g} ~A${gv[g]:,}" for g in up)
                              + (f" ({', '.join(thin)} rest on few sales)" if thin else "")
                              + f" — {val[0]['src'].split('(')[0].strip()}. Check the photos: raw 'NM' is the seller's word")
                    cs = [GRADE_CAT] + [c for c in cs if c != GRADE_CAT]
        if gem and nm_raw_ok:
            gem_txt = (f"gem rate {gem['gem'] * 100:.0f}% ({gem['n10']:,} PSA 10s of {gem['total']:,} graded); "
                       f"{'safe' if gem['kind'] == 'SAFE' else '10 hunt'} max raw A${gem['max_raw']:,}")
            if gp:
                gp_why += f" · {gem_txt}"
            elif pl <= gem["max_raw"]:
                gv = gem["v"]
                gp = True
                gp_net = (gv.get("PSA 10", 0) * SELL_KEEP - GRADE_COST - pl) * gem["gem"]   # ranked by the 10's expected share
                gp_why = (f"{'10 hunt' if gem['kind'] == 'HUNT' else 'gem target'}: A${pl:,.0f} landed raw{gst_of(pl, p)}, at or under "
                          f"the A${gem['max_raw']:,} max → PSA 8 ~A${gv.get('PSA 8', 0):,} · PSA 9 ~A${gv.get('PSA 9', 0):,} · "
                          f"PSA 10 ~A${gv.get('PSA 10', 0):,}; {gem_txt.split(';')[0]} — weekly gem-rate list "
                          f"({gem.get('checked', '')}). Only buy a copy that looks clean in the photos")
                cs = [GRADE_CAT] + [c for c in cs if c != GRADE_CAT]
        tags = "".join(c for c, on in (("n", new), ("d", drop), ("r", bool(r.get("re"))), ("l", lowest), ("s", bool(slab)), ("g", gp), ("m", bool(x.get("var")))) if on)
        X[i] = [[cats.index(c) for c in cs], r["f"], r.get("pp") if drop else None, round(low[k], 2) if k in low else None,
                live_n.get(k, 0) if k else 0, val[2] if val else None, (val[0]["label"] + " · " + val[1]) if val else "", tags,
                (1 if val[1] in (val[0].get("thin") or []) else 0) if val else 0,   # value rests on few sales
                search_words(val[0]) if val else "", (val[0].get("checked") or "") if val else ""]

        # binders & lots: no values, so the summary shows what's fresh or about to end, cheapest per card first
        if x.get("lot"):
            if x.get("country") in SKIP_CC or LOTS_CAT not in digest:
                continue
            d = digest[LOTS_CAT]
            d["total"] += 1
            d["new"] += bool(new)
            d["drops"] += bool(drop)
            n_cards = None
            for m in re.finditer(r"\b(\d{1,4})\s?\+?\s?(?:[x×]\s)?(?:pokemon\s|pokémon\s|vintage\s|holo\s|assorted\s|mixed\s)*(?:cards|pcs|pieces|card lot)\b|(?:\bx|×)\s?(\d{1,4})\b|\blot of (\d{1,4})\b", t, re.I):
                n = int(m.group(1) or m.group(2) or m.group(3))
                if not 1995 <= n <= 2030:        # years ("2000 Pokemon cards") aren't card counts
                    n_cards = n
                    break
            score_old = len(LOT_OLD_E.findall(t))   # more named old sets / signs of age = higher up the list
            each = f"; {n_cards} cards ≈ A${pl / n_cards:,.2f} each" if n_cards and n_cards >= 10 else ""
            why, pri = "", 9
            if auc and hours is not None and 0 < hours <= AUCTION_HOURS:
                why, pri = f"auction ends in {hours:.0f}h: A${pl:,.0f}{gst_of(pl, p)} now, {x.get('bids') or 0} bids — the bid will rise", 1
            elif new or (r["f"] == today and not had_before):
                why, pri = f"new: A${pl:,.0f}{gst_of(pl, p)}{' (auction)' if auc else ''}", 0
            elif drop:
                why, pri = f"price dropped A${r['pp']:,.0f} → A${p:,.0f}", 2
            elif r.get("re") and not auc and r.get("p0") and p < r["p0"] * 0.95:
                why, pri = f"relisted cheaper (A${r['p0']:,.0f} → A${p:,.0f})", 2
            if why:
                d["hi"].append((pri, -score_old, pl,
                                hi_entry(i, x, t, p, pl, why + each + ". Check the photos for holos and condition", auc)))
            continue

        # digest: same rules as the finder's default view
        if slab:
            d = digest[SLAB_CAT]
            d["total"] += 1
            d["new"] += bool(new)
            d["drops"] += bool(drop)
            d["hi"].append((0, -(at7 + floor), p, dict(hi_entry(i, x, t, p, pl, slab_why, auc),
                                                       cond=flag.replace("damage: ", "") or ("NM" if x.get("nm") == 1 else ""))))
        if gp:
            d = digest[GRADE_CAT]
            d["total"] += 1
            d["new"] += bool(new)
            d["drops"] += bool(drop)
            d["hi"].append((0, -gp_net, pl, hi_entry(i, x, t, p, pl, gp_why, auc)))
        if x.get("country") in SKIP_CC or x.get("flag"):
            continue
        sealed = bool(sealed_kind(x))
        if not sealed and ((MODERN.search(t) and not str(x.get("query") or "").startswith("+") and not x["shop"]) or SV_MEP.search(t)):
            continue
        if p > 1000 and SLAB.search(t):
            continue
        played = bool(PLAYED_T.search(re.sub(r"\bHP\s?\d+|\d+\s?HP\b", "", t))) and not SLAB.search(t)
        reason, pri, score = None, 9, 0
        gst = gst_of(pl, p)
        ask = None if (auc or not k) else ask_of(i, k, x.get("seller"))
        vs_next = round(pl / ask["next"], 3) if ask else None
        ask_ok = bool(ask and pl >= ASK_FLOOR * ask["med"])          # not so cheap it's probably a different card
        ask_txt = (f"next-cheapest other seller A${ask['next']:,.0f}, typical asking A${ask['med']:,.0f} across {ask['n']} sellers" if ask else "")
        thin_val = bool(val and val[1] in (val[0].get("thin") or []))
        src = val[0]["src"].split("(")[0].strip() if val else ""
        relist_cheaper = bool(r.get("re") and not auc and r.get("p0") and p < r["p0"] * 0.95)
        if sealed:
            if ratio is not None and ratio < 0.6 and not auc:
                reason, pri, score = (f"A${pl:,.0f}, far under the A${val[2]:,.0f} RRP: check it's the English product and not a pre-order "
                                      f"deposit, a different size or another language"), 3, ratio
            elif ratio is not None and ratio <= 1.0 and not auc:
                reason, pri, score = (f"at or under RRP: A${pl:,.0f}{gst} vs A${val[2]:,.0f} {val[0]['label']} RRP"
                                      + (f" ({round((1 - ratio) * 100)}% under)" if ratio < 0.995 else "")), 0, ratio
            elif drop:
                reason, pri, score = f"price dropped A${r['pp']:,.0f} → A${p:,.0f}", 1, p / r["pp"]
        elif played:
            pass  # played copy (shop titles carry [HP] / [MP] etc.; eBay ones are already flagged)
        elif x.get("var"):
            pass  # multi-option listing: the price eBay reports may belong to a different option than the title's card
        elif ratio is not None and ratio < 0.4:
            pass  # far too cheap for the card: nearly always a reprint, fake, wrong version or played copy; not a highlight
        elif ratio is not None and ratio < CN_RAW_MIN and x.get("country") == "CN" and val[1] == "Raw":
            pass  # raw cards from China far under value: usually fakes
        elif ratio is not None and ratio <= 0.85 and not auc and sus:
            reason, pri, score = (f"{round((1 - ratio) * 100)}% under A${val[2]:,} value{gst} — but {sus['under']} of {sus['copies']} live copies are "
                                  f"too, so the value may be wrong ({val[1]})"), 3, ratio
        elif ratio is not None and ratio <= 0.85 and not auc:
            reason, pri, score = (f"A${pl:,.0f} landed{gst}, {round((1 - ratio) * 100)}% under A${val[2]:,} value ({val[1]}, {src}"
                                  + (", few sales" if thin_val else "") + ")"
                                  + (f"; {ask_txt}" if ask else "")), 0, ratio
        elif auc:
            ends = ""
            if hours is not None:
                try:
                    ends = datetime.fromisoformat(str(x["ends"]).replace("Z", "+00:00")).astimezone(SYD).strftime(" (%a %-I:%M%p)").replace("AM", "am").replace("PM", "pm")
                except Exception:
                    ends = ""
            if (ratio is not None and hours is not None and 0 < hours <= AUCTION_HOURS and ratio <= AUCTION_RATIO
                    and not sus and not (x.get("country") == "CN" and val[1] == "Raw" and ratio < CN_RAW_MIN)):
                reason, pri, score = (f"auction ends in {hours:.0f}h{ends}: A${pl:,.0f}{gst} now, {round((1 - ratio) * 100)}% under A${val[2]:,} value "
                                      f"({val[1]}; {x.get('bids') or 0} bids) — the bid will rise"), 1, ratio
            elif ratio is None and k and hours is not None and 0 < hours <= AUCTION_HOURS and not MODERN.search(t) and not NEWER.search(t):
                aa = ask_of(i, k, x.get("seller"))
                if aa and ASK_FLOOR * aa["med"] <= pl <= 0.6 * aa["next"]:
                    reason, pri, score = (f"auction ends in {hours:.0f}h{ends}: A${pl:,.0f}{gst} now, no sold value yet; "
                                          f"cheapest Buy It Now from another seller A${aa['next']:,.0f} ({x.get('bids') or 0} bids) — the bid will rise"), 3, pl / aa["next"]
        else:
            signal = (f"price dropped A${r['pp']:,.0f} → A${p:,.0f}" if drop else
                      f"relisted cheaper (A${r['p0']:,.0f} → A${p:,.0f})" if relist_cheaper else
                      f"new, cheapest of {seen_n[k]} copies seen in 30 days" if (new and lowest) else "")
            under_next = vs_next is not None and vs_next <= ASK_DEAL and ask_ok
            if ratio is not None:
                if ratio <= 1.0 and (signal or under_next):
                    reason, pri, score = (f"{signal or f'{round((1 - vs_next) * 100)}% under the next-cheapest other seller'}; "
                                          f"A${pl:,.0f} landed{gst} is {round(ratio * 100)}% of A${val[2]:,} value ({val[1]}, {src}"
                                          + (", few sales" if thin_val else "") + ")"), 2, ratio
                elif thin_val and under_next:
                    reason, pri, score = (f"{signal + '; ' if signal else ''}A${pl:,.0f} landed{gst} is over the A${val[2]:,} value ({val[1]}), but that "
                                          f"rests on few sales, and it's {round((1 - vs_next) * 100)}% under the {ask_txt}"), 3, vs_next
                # otherwise it's over what the card sells for: not a highlight, however it compares with other listings
            elif pl >= ASK_MIN_PRICE and not MODERN.search(t) and not NEWER.search(t):
                if under_next:
                    reason, pri, score = ((signal + "; " if signal else "") + f"no sold value yet — A${pl:,.0f} landed{gst}, "
                                          f"{round((1 - vs_next) * 100)}% under the {ask_txt}"), 2, vs_next
                elif signal and vs_next is not None and vs_next <= 1.0 and ask_ok:
                    reason, pri, score = (f"{signal}; no sold value yet — A${pl:,.0f} landed{gst}, the cheapest live copy "
                                          f"({ask_txt})"), 3, vs_next
        for c in ([c for c in cs if c not in (SLAB_CAT, GRADE_CAT)] or ["Other"]):
            d = digest[c]
            d["total"] += 1
            d["new"] += bool(new)
            d["drops"] += bool(drop)
            d["relisted"] += bool(r.get("re"))
            d["under"] += bool(ratio is not None and ((0.6 <= ratio <= 1.0) if sealed else 0.4 <= ratio <= 0.85) and not auc and not sus)
            if reason:
                d["hi"].append((pri, score, pl, hi_entry(i, x, t, p, pl, reason + (f" · shop listing, last checked {shop_seen}" if x["shop"] and shop_seen else ""), auc)))
    for d in digest.values():
        # one line per card: the best copy, with a count of the other highlighted copies of the same card
        kept, seen_k = [], {}
        for h in sorted(d["hi"], key=lambda h: h[:3]):
            ck = h[3].get("ck")
            if ck and ck in seen_k:
                seen_k[ck]["more"] = seen_k[ck].get("more", 0) + 1
                continue
            if ck:
                seen_k[ck] = h[3]
            kept.append(h[3])
        for h in kept:
            if h.get("more"):
                h["why"] += f" (+{h['more']} more cop{'ies' if h['more'] > 1 else 'y'} also flagged)"
        d["hi"] = kept[:15 if d["cat"] == LOTS_CAT else 12 if d["cat"] in (SLAB_CAT, GRADE_CAT) else 8]
    order = [c for c in cats if digest[c]["total"]] + (["Other"] if digest["Other"]["total"] else [])

    H = {"updated": today, "ids": ids}
    json.dump(H, open(P("history.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    json.dump({"day": today, "cats": cats + ["Other"], "x": X}, open(P("enrich.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    tot = {"listings": len(cur), "new": sum(1 for v in X.values() if "n" in v[7]), "drops": sum(1 for v in X.values() if "d" in v[7]),
           "relisted": sum(1 for v in X.values() if "r" in v[7]), "valued": sum(1 for v in X.values() if v[5])}
    sus_list = sorted(suspect.values(), key=lambda v: -v["value"])
    json.dump({"day": today, "generated": L.get("generated"), "totals": tot, "first_day": not had_before, "warnings": ebay_warnings(L),
               "categories": [digest[c] for c in order], "suspect_values": sus_list},
              open(P("digest.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    # values_refresh.py re-reads these cards' PriceCharting pages first on the next run
    json.dump({"day": today, "cards": sus_list}, open(P("values_suspect.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    md = [f"# Slab Finder summary {today}"] + ebay_warnings(L) + [
          f"{tot['listings']:,} live listings · {tot['new']:,} new · {tot['drops']:,} price drops · {tot['relisted']:,} relisted · {tot['valued']:,} with a card value",
          "Default view: no US/UK/CA sellers, damaged/reprint/wrong-grader tags hidden, slabs over A$1,000 hidden. "
          "Prices are landed (postage, plus GST on overseas items). Highlights need a sold value at or under the price, "
          "or, where there's no sold value yet, a price well under the other live copies.", ""]
    for c in order:
        d = digest[c]
        md.append(f"## {c}{' (want list)' if d['want'] else ''} — {d['total']:,} listings, {d['new']} new, {d['drops']} drops, {d['under']} under value")
        for h in d["hi"][:5]:
            md.append(f"- A${h.get('pl', h['p']):,.0f} {h['t']} ({h['cc']}, {h['seller']}{', auction' if h['auc'] else ''}"
                      f"{', + postage' if h.get('post?') else ''}): {h['why']} {h['url']}")
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
