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
COUNTRIES = ["AU", "JP", "CN", "NZ", "HK", "SG", "KR"]
HERE = os.path.dirname(os.path.abspath(__file__))

REPRINT = re.compile(r"celebrations?|classic collection|25th|30th|anniversary|evolutions|\bcp6\b|reprint|proxy|custom|fan ?art|orica|replica|metal card|gold (plated|metal)|digital|tcg ?live|code card|\bonline code", re.I)
MODERN = re.compile(r"\b(20(1[89]|2\d))\b|sword|shield|scarlet|violet|\bsv\d|\bswsh|vmax|vstar|\bv max\b|pokemon 151|obsidian|paldea|paradox|evolving skies|fusion strike|brilliant stars|astral radiance|lost origin|silver tempest|crown zenith|hidden fates|shining fates|team up|unbroken bonds|unified minds|cosmic eclipse|mega evolution|prismatic|surging sparks|stellar crown|temporal forces|twilight masquerade|journey together|destined rivals|white flare|black bolt", re.I)
JUNK = re.compile(r"\blot\b|bundle|binder|sealed|booster|pack\b|box\b|tin\b|sleeve|playmat|case only|empty|choose|pick your|you pick|mystery|repack|graded card guaranteed", re.I)
GRADE = re.compile(r"\b(PSA|CGC|BGS|BECKETT|ACE|TAG|SGC)\s*-?\s*(10|9\.5|9)\b|gem ?mint|\bNM\b|near mint|mint", re.I)
# Second pass per search: raw cards the seller marked "Near Mint or Better" in eBay's Card Condition field,
# so un-graded cards with plain titles (no "NM"/"PSA" words) are found too. Played/damaged copies stay out.
NM_ASPECT = "categoryId:183454,Card Condition:{Near Mint or Better}"
NM_PASS = os.environ.get("NM_PASS", "1") != "0"
PLAYED = re.compile(r"lightly|moderately|heavily|\bplayed\b|\bLP\b|\bMP\b|\[HP\]|\bHP\b(?!\s?\d)|damaged?|\bdmg\b|creas|\bdent|\bbent\b|\bbend|\btear\b|\btorn\b|water ?(damage|mark|stain)|\bstain|scratch|scuff|\bpoor\b|\bas[- ]is\b|heavy wear|\bwear\b|\bink\b|marked|writing|peel|ripped|\bcut\b|miscut", re.I)
# Nothing on a want-list search is deleted for condition. Each listing gets a "flag":
#   "damage: <word>"  title or item page mentions damage  -> hidden on the finder by default, one tap to show
#   "unverified"      raw, no condition word, seller didn't mark Near Mint or Better -> shown with a grey tag
NMWORD = re.compile(r"gem ?mint|\bNM\b|near mint|\bmint\b", re.I)
# Page check: raw listings priced far under the same card's other listings get their item page read
# (seller condition notes, Card Condition field, description) and are tagged "damage" if it is mentioned.
DEEP_MAX = int(os.environ.get("DEEP_MAX", "300"))   # item-page calls per run, keeps under eBay's 5,000/day
DEEP_RATIO = float(os.environ.get("DEEP_RATIO", "0.6"))  # "suspiciously cheap" = under 60% of the card's median
SLAB = re.compile(r"\b(PSA|CGC|BGS|BECKETT|ACE|TAG|SGC)\b", re.I)
STOP = set(("pokemon pokémon card cards tcg japanese japan english psa cgc bgs gem mint mt nm near holo rare promo the 1st edition ed graded jp jpn of in game "
             "base set jungle fossil team rocket gym heroes challenge neo genesis discovery revelation destiny expedition aquapolis skyridge "
             "legendary collection unlimited shadowless generations radiant evolutions celebrations dragon frontiers crystal guardians "
             "holon phantoms delta species pocket monsters wotc vintage black star secret full art reverse first ultra").split())


def token(cid, secret):
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    req = urllib.request.Request(API + "/identity/v1/oauth2/token", data=data, headers={"Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["access_token"]


def search(tok, q, country, offset=0, limit=200, aspect=None):
    params = {"q": q, "category_ids": CATEGORY, "limit": str(limit), "offset": str(offset),
              "filter": f"itemLocationCountry:{country},buyingOptions:{{FIXED_PRICE|AUCTION|BEST_OFFER}}"}
    if aspect:
        params["aspect_filter"] = aspect
    req = urllib.request.Request(API + "/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": "Bearer " + tok, "X-EBAY-C-MARKETPLACE-ID": "EBAY_AU",
                                          "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=AU,zip=2447"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def keep(title, modern_ok=False):
    # "+" want-list searches may keep XY Evolutions cards; every other reprint is still dropped.
    check = re.sub(r"\bevolutions\b", "", title, flags=re.I) if modern_ok else title
    # "+" want-list searches keep sealed single promos (still-sealed movie/campaign cards); boosters, boxes and lots stay out.
    junk_title = re.sub(r"\bsealed\b", "", title, flags=re.I) if modern_ok else title
    if REPRINT.search(check) or JUNK.search(junk_title):
        return False

    if MODERN.search(title) and not modern_ok:
        return False
    if modern_ok:  # "+" want-list searches keep everything that isn't junk; condition is tagged, not filtered
        return True
    if PLAYED.search(title) and not SLAB.search(title):
        return False  # general (non-want) searches still skip played raw copies
    return bool(GRADE.search(title))


def keep_nm(title, modern_ok=False):
    """For listings eBay already filtered to Card Condition: Near Mint or Better. No grade word needed in the title."""
    check = re.sub(r"\bevolutions\b", "", title, flags=re.I) if modern_ok else title
    junk_title = re.sub(r"\bsealed\b", "", title, flags=re.I) if modern_ok else title
    if REPRINT.search(check) or JUNK.search(junk_title):
        return False
    if PLAYED.search(title) and not modern_ok:
        return False
    if MODERN.search(title) and not modern_ok:
        return False
    return True


def flag_for(title, nm_pass):
    if SLAB.search(title):
        return ""
    hit = PLAYED.search(title)
    if hit:
        return "damage: " + hit.group(0).lower().strip()
    if not nm_pass and not NMWORD.search(title):
        return "unverified"
    return ""


def num(x):
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def card_key(title):
    """Same card = same card number + same grade + same language + first name word."""
    n = re.search(r"\b(\d{1,3}a?/\d{2,3}|\d{3}/[A-Z]{1,4}-?P|[A-Z]{2,4}\d{2,3}|SWSH\d+|SM\d+)\b", title, re.I)
    if not n:  # Japanese vintage cards are numbered "No.094" / "#94" with no set total
        n = re.search(r"(?:#|\bNo\.?\s?)0*(\d{1,3})\b", title, re.I) if re.search("japan|\\bjpn?\\b", title, re.I) else None
    if not n:
        return None
    g = re.search(r"\b(PSA|CGC|BGS|ACE)\s?-?(\d{1,2}(\.5)?)\b", title, re.I)
    words = [w.lower() for w in re.findall(r"[^\W\d_][^\W\d_'.]+", title) if w.lower() not in STOP and len(w) > 2]
    return (n.group(1).upper(), (g.group(1).upper() + g.group(2)) if g else "RAW",
            "JP" if re.search("japan", title, re.I) else "EN", words[0] if words else "")


def get_item(tok, iid):
    req = urllib.request.Request(API + "/buy/browse/v1/item/get_item_by_legacy_id?" + urllib.parse.urlencode({"legacy_item_id": iid}),
                                 headers={"Authorization": "Bearer " + tok, "X-EBAY-C-MARKETPLACE-ID": "EBAY_AU"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def page_text(item):
    """Seller condition notes + Card Condition field + short and full description, tags stripped."""
    parts = [item.get("conditionDescription") or "", item.get("shortDescription") or "", item.get("description") or ""]
    for d in item.get("conditionDescriptors") or []:
        parts += [v.get("content", "") for v in d.get("values") or []]
    txt = re.sub(r"<[^>]+>", " ", " ".join(parts))
    # sellers' boilerplate like "no damage", "no creases", "free of scratches" is not damage
    return re.sub(r"\b(no|not|without|free of|zero|never)\s+(any\s+)?\w+(\s+(or|and|/)\s+\w+)*", " ", txt, flags=re.I)


GRADERS = {"PSA": r"\bpsa\b|professional sports authenticator", "CGC": r"\bcgc\b|certified guaranty",
           "BGS": r"\bbgs\b|beckett", "SGC": r"\bsgc\b", "ACE": r"\bace\b", "TAG": r"\btag\b"}
REPRINT_SET = re.compile(r"celebrations|classic collection|25th|30th|20th anniv|anniversary|evolutions|\bcp6\b|reprint|復刻", re.I)


def aspects(item):
    """eBay item specifics + condition descriptors as {name: value} (names lower-cased)."""
    out = {}
    for a in item.get("localizedAspects") or []:
        out[str(a.get("name", "")).lower()] = str(a.get("value", ""))
    for d in item.get("conditionDescriptors") or []:
        out.setdefault(str(d.get("name", "")).lower(), ", ".join(v.get("content", "") for v in d.get("values") or []))
    return out


def deep_check(tok, out):
    """Read the item page of listings far cheaper than the same card's other listings (raw and graded).
    Tags: damage (raw only), reprint (item specifics name a reprint set the title hides),
    grader (slab is from a different company than the title says, e.g. MNT 9 sold as "PSA 9")."""
    groups = {}
    for x in out:
        k = card_key(x["title"])
        if k:
            groups.setdefault(k, []).append(x)
    cands = []
    for k, L in groups.items():
        if len(L) < 3:
            continue
        ps = sorted(num(x["price"]) + num(x["post"]) for x in L)
        med = ps[len(ps) // 2]
        for x in L:
            p = num(x["price"]) + num(x["post"])
            if 0 < p < DEEP_RATIO * med and med >= 40 and not str(x.get("flag", "")).startswith("damage"):
                auc = "AUCTION" in (x.get("options") or [])
                cands.append((0 if str(x.get("query", "")).startswith("+") else 1, 1 if auc else 0, p / med, x))
    cands.sort(key=lambda c: c[:3])  # want-list first, Buy It Now before auctions, then cheapest against peers
    tagged, checked, calls = 0, 0, 0
    for *_, x in cands[:DEEP_MAX]:
        try:
            item = get_item(tok, x["id"])
            calls += 1
        except Exception as e:
            print("page check failed:", x["id"], e, file=sys.stderr)
            if getattr(e, "code", None) == 429:
                break
            continue
        checked += 1
        asp = aspects(item)
        txt = page_text(item)
        x["checked"] = 1
        slab = SLAB.search(x["title"])
        cc = asp.get("card condition", "")
        if cc:
            x["cardCondition"] = cc
        # 1. reprint set hidden by the title (e.g. Celebrations Venusaur titled "Base Set 15/102")
        setname = asp.get("set", "") + " " + asp.get("year manufactured", "")
        if REPRINT_SET.search(setname) and not REPRINT_SET.search(x["title"]):
            x["flag"] = "reprint: " + asp.get("set", "").strip()[:30]
        # 2. grader in the item specifics differs from the one in the title
        elif slab:
            g = (asp.get("professional grader", "") + " " + asp.get("grade", "")).strip()
            tg = slab.group(1).upper()
            if g and tg in GRADERS and not re.search(GRADERS[tg], g, re.I):
                x["flag"] = "grader: " + g[:30] + " (title says " + tg + ")"
            if g:
                x["grader"] = g[:60]
        # 3. damage mentioned on the page (raw cards only; slab notes are about the case)
        elif PLAYED.search(txt + " " + cc):
            x["flag"] = "damage: " + PLAYED.search(txt + " " + cc).group(0).lower().strip()
        elif x.get("flag") == "unverified" and re.search(r"near mint|\bNM\b|mint", cc + " " + txt, re.I):
            x["flag"] = ""  # the page says NM after all
        if x.get("flag"):
            tagged += 1
        time.sleep(0.2)
    print(f"page check: {checked} cheap listings read, {tagged} tagged (damage / reprint / grader)")
    return out, calls, tagged


def main():
    cid, secret = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET (eBay production keys).")
    queries = [l.strip() for l in open(os.path.join(HERE, "queries.txt"), encoding="utf-8") if l.strip() and not l.startswith("#")]
    tok = token(cid, secret)
    seen, out, calls = set(), [], 0
    stats = {}  # per search: [eBay returned, kept as new, already found by an earlier search, filtered out]
    nm_fail, nm_found = 0, 0
    done, quota_hit, fails_in_row = set(), False, 0
    for q in queries:
        if quota_hit:
            break
        # A "+" at the start marks a want-list search: modern (2018+) cards are kept for it.
        want = q.startswith("+")
        sq = q.lstrip("+").strip()
        for c in COUNTRIES:
          # NM pass first, so listings the seller marked "Near Mint or Better" are recorded with nm=1
          passes = ([NM_ASPECT] if NM_PASS and nm_fail < 5 else []) + [None]
          for asp in passes:
            try:
                res = search(tok, sq, c, aspect=asp)
                calls += 1
                fails_in_row = 0
            except Exception as e:  # keep going on one bad query
                print("query failed:", q, c, "(NM pass)" if asp else "", e, file=sys.stderr)
                fails_in_row += 1
                if getattr(e, "code", None) == 429 or fails_in_row >= 8:
                    quota_hit = True  # eBay's 5,000 calls/day used up (e.g. a second run the same day)
                    print("eBay call limit reached: stopping; unfinished searches keep yesterday's results", file=sys.stderr)
                    break
                if asp:
                    nm_fail += 1  # stop the NM pass for the rest of the run if eBay keeps refusing it
                continue
            for it in res.get("itemSummaries", []) or []:
                iid = it.get("legacyItemId") or ((it.get("itemId") or "").split("|") + ["", ""])[1] or None
                title = it.get("title", "")
                ok = keep_nm(title, want) if asp else keep(title, want)
                st = stats.setdefault(q, [0, 0, 0, 0])
                st[0] += 1
                if iid and iid in seen:
                    st[2] += 1
                    continue
                if not iid or not ok:
                    st[3] += 1
                    continue
                st[1] += 1
                if asp:
                    nm_found += 1
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
                    "nm": 1 if asp else 0,
                    "flag": flag_for(title, asp),
                })
            time.sleep(0.2)
          if quota_hit:
              break
        if not quota_hit:
            done.add(q)
    carried = 0
    if quota_hit:
        # don't wipe the feed with a half run: carry over the last full results for searches that didn't finish
        try:
            prev = json.load(open(os.path.join(HERE, "listings.json"), encoding="utf-8")).get("items", [])
        except Exception:
            prev = []
        for x in prev:
            if x.get("query") not in done and x.get("id") not in seen:
                seen.add(x.get("id")); out.append(dict(x, stale=1)); carried += 1
    if DEEP_MAX > 0 and not quota_hit:
        out, deep_calls, _ = deep_check(tok, out)
        calls += deep_calls
    out.sort(key=lambda x: (x["country"] != "AU", x["country"] != "JP", x["country"] != "CN", x["title"]))
    meta = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "count": len(out), "calls": calls,
            "damaged_flagged": sum(1 for x in out if str(x.get("flag", "")).startswith("damage")),
            "unverified": sum(1 for x in out if x.get("flag") == "unverified"),
            "searches_done": f"{len(done)}/{len(queries)}", "carried_over": carried,
            "query_stats": {q: dict(zip(("returned", "new", "dupes", "filtered"), stats.get(q, [0, 0, 0, 0]))) for q in queries},
            "note": "Pre-filtered by title and eBay Card Condition. Cheap raw outliers had their item page read (checked=1); others still need a look before buying."}
    with open(os.path.join(HERE, "listings.json"), "w", encoding="utf-8") as f:
        json.dump({**meta, "items": out}, f, ensure_ascii=False, indent=1)
    with open(os.path.join(HERE, "listings.csv"), "w", newline="", encoding="utf-8") as f:
        fields = list(dict.fromkeys(k for x in out for k in x)) or ["id"]
        w = csv.DictWriter(f, fieldnames=fields, restval="")
        w.writeheader(); w.writerows(out)
    print(f"{len(out)} listings from {calls} searches ({nm_found} extra raw Near Mint or Better listings from the NM pass)")


if __name__ == "__main__":
    main()
