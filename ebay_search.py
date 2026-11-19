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

REPRINT = re.compile(r"celebrations?|classic collection|25th|30th|anniversary|evolutions|\bcp6\b|reprint|proxy|custom|fan ?art|orica|replica|metal card|gold (plated|metal)|digital|tcg ?live|code card|\bonline code"
                     r"|fan[- ]?made|unofficial|hand[- ]?made|acrylic (card|stand|block|display|art)|key ?(chain|ring)|novelty|reproduction|\brepro\b|bootleg|\bfake\b(?! ?free)", re.I)
# "fake" is skipped when it reads "not fake" / "no fakes" (sellers often promise that); see not_fake() below.
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
# eBay allows 5,000 calls a day. Searches stop at SEARCH_MAX so the page check always has calls left;
# searches that didn't run keep yesterday's results (like a quota stop), and the summary says so.
DAILY_CALLS = int(os.environ.get("DAILY_CALLS", "5000"))
SEARCH_MAX = int(os.environ.get("SEARCH_MAX", str(DAILY_CALLS - DEEP_MAX - 50)))
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


# Pawn shops: a search line containing "@pawn" is run once per page against these eBay sellers only (AU),
# so badly titled pawn-shop listings ("Pokemon card holo rare") are still found. The list is pawn_sellers.txt
# plus any seller whose username looks like a pawn shop / Cash Converters store seen in earlier feeds.
PAWN_RX = re.compile(r"(?<!res)pawn|cash.?conv|cashies|mega.?cash|cash.?stop|cash.?town|cash.?brok|hock.?shop", re.I)
PAWN_PAGES = 3
# Pawn-shop listings are only kept when they fit the hunted eras (pre-2018, WotC to XY), not every modern card they stock.
PAWN_ERA = re.compile(r"\b(199[6-9]|200\d|201[0-7])\b|wotc|wizards|old back|vintage|base set|jungle|fossil|team rocket|gym (heroes|challenge)|"
                      r"\bneo\b|e-?series|e-?card|expedition|aquapolis|skyridge|legendary collection|\bex\b.*(ruby|sapphire|sandstorm|dragon|"
                      r"magma|aqua|hidden legends|firered|leafgreen|rocket returns|deoxys|emerald|unseen forces|delta|legend maker|holon|crystal guardians|"
                      r"frontiers|power keepers)|gold star|\bstar\b.*holo|☆|shining|crystal|lv\.? ?x|\blegend\b|prime\b|heartgold|soulsilver|hgss|"
                      r"undaunted|unleashed|triumphant|call of legends|diamond|pearl|platinum|majestic dawn|great encounters|stormfront|"
                      r"rising rivals|supreme victors|arceus|black star|promo|pcg|generations|evolutions|xy\b|black.?white|\bbw\b|plasma|"
                      r"boundaries crossed|dragons exalted|legendary treasures|\bpsa\b|\bcgc\b|\bbgs\b", re.I)


def trusted_sellers():
    """trusted_sellers.txt: one eBay username per line (# comments allowed). Used by "@trusted" search lines."""
    names = []
    try:
        for l in open(os.path.join(HERE, "trusted_sellers.txt"), encoding="utf-8"):
            l = l.split("#")[0].strip()
            if l:
                names.append(l.lower())
    except FileNotFoundError:
        pass
    return sorted(set(names))[:250]


def pawn_sellers():
    names = []
    try:
        for l in open(os.path.join(HERE, "pawn_sellers.txt"), encoding="utf-8"):
            l = l.split("#")[0].strip()
            if l:
                names.append(l.lower())
    except FileNotFoundError:
        pass
    try:
        prev = json.load(open(os.path.join(HERE, "listings.json"), encoding="utf-8"))
        names += [str(n).lower() for n in prev.get("pawn_sellers", [])]
        names += [str(x.get("seller")).lower() for x in prev.get("items", []) if x.get("seller") and PAWN_RX.search(str(x.get("seller")))]
    except Exception:
        pass
    return sorted(set(n for n in names if n and n != "none"))[:250]


def search(tok, q, country, offset=0, limit=200, aspect=None, sellers=None):
    params = {"q": q, "category_ids": CATEGORY, "limit": str(limit), "offset": str(offset),
              "filter": f"itemLocationCountry:{country},buyingOptions:{{FIXED_PRICE|AUCTION|BEST_OFFER}}"
                        + (",sellers:{" + "|".join(sellers) + "}" if sellers else "")}
    if aspect:
        params["aspect_filter"] = aspect
    req = urllib.request.Request(API + "/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": "Bearer " + tok, "X-EBAY-C-MARKETPLACE-ID": "EBAY_AU",
                                          "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=AU,zip=2447"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


NOT_FAKE = re.compile(r"\b(not|no|never|non)[- ]?(a )?fakes?\b|\bfakes? free|100% (genuine|authentic|real)", re.I)


def reprint_hit(t):
    """Reprint / fake / novelty words, ignoring sellers' "not fake" promises."""
    return REPRINT.search(NOT_FAKE.sub(" ", t))


def keep(title, modern_ok=False):
    # "+" want-list searches may keep XY Evolutions cards; every other reprint is still dropped.
    check = re.sub(r"\bevolutions\b", "", title, flags=re.I) if modern_ok else title
    # "+" want-list searches keep sealed single promos (still-sealed movie/campaign cards); boosters, boxes and lots stay out.
    junk_title = re.sub(r"\bsealed\b", "", title, flags=re.I) if modern_ok else title
    if reprint_hit(check) or JUNK.search(junk_title):
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
    if reprint_hit(check) or JUNK.search(junk_title):
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


try:  # better "same card" test (number + set + variant + grade + language + name), shared with enrich.py
    from feedlib import card_key as _card_key2
except Exception:
    _card_key2 = None
try:
    from feedlib import line_options
except Exception:  # older feedlib: only @pawn is understood
    def line_options(line):
        return line.lstrip("+").replace("@pawn", "").strip(), ({"pawn": True} if "@pawn" in line else {})


def card_key(title):
    """Same card = same card number + same set + same variant + same grade + same language + first name word."""
    if _card_key2:
        return _card_key2(title)
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


def deep_check(tok, out, limit=None):
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
    for *_, x in cands[:DEEP_MAX if limit is None else limit]:
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
    # If the last run was cut short by eBay's daily limit, the searches it never reached go first this time,
    # so the same end of queries.txt doesn't keep missing out (new lines count as not reached).
    try:
        last = json.load(open(os.path.join(HERE, "listings.json"), encoding="utf-8")).get("query_stats") or {}
        if last:
            missed = [q for q in queries if not (last.get(q) or {}).get("ran")]
            if missed and len(missed) < len(queries):
                queries = missed + [q for q in queries if q not in set(missed)]
                print(f"{len(missed)} searches not reached last run go first")
    except Exception as e:
        print("query order: last run's stats not used:", e)
    tok = token(cid, secret)
    pawns = pawn_sellers()
    trusted = trusted_sellers()
    print(f"pawn-shop sellers: {len(pawns)}; trusted sellers: {len(trusted)}; search budget {SEARCH_MAX} calls")
    seen, out, calls = set(), [], 0
    stats = {}  # per search: [eBay returned, kept as new, already found by an earlier search, filtered out]
    cstats = {}  # per search and country: new listings kept
    budget_hit = False
    nm_fail, nm_found = 0, 0
    done, quota_hit, fails_in_row = set(), False, 0
    for q in queries:
        if quota_hit or budget_hit:
            break
        # A "+" at the start marks a want-list search: modern (2018+) cards are kept for it.
        want = q.startswith("+")
        sq, opts = line_options(q)
        is_pawn, is_trusted = bool(opts.get("pawn")), bool(opts.get("trusted"))
        sellers = pawns if is_pawn else trusted if is_trusted else None
        if (is_pawn or is_trusted) and not sellers:
            continue
        if is_pawn:
            countries = ["AU"]
        elif is_trusted and not isinstance(opts.get("c"), list):
            countries = ["JP"]  # the trusted sellers are Japan-based
        elif isinstance(opts.get("c"), list):
            countries = [c for c in opts["c"] if c in COUNTRIES] or COUNTRIES
        else:
            countries = COUNTRIES
        for c in countries:
          # NM pass first, so listings the seller marked "Near Mint or Better" are recorded with nm=1
          passes = ([NM_ASPECT] if NM_PASS and nm_fail < 5 and not opts.get("nonm") and not is_trusted else []) + [None]
          pages = [(asp, off) for asp in passes for off in ([o * 200 for o in range(PAWN_PAGES)] if sellers else [0])]
          for asp, off in pages:
            if calls >= SEARCH_MAX:
                budget_hit = True
                break
            try:
                res = search(tok, sq, c, offset=off, aspect=asp, sellers=sellers)
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
                if is_trusted:  # trusted sellers' stock: no grade word needed, but hunted eras and not played
                    ok = keep_nm(title, want) and bool(PAWN_ERA.search(title))
                else:
                    ok = keep_nm(title, want) if asp else keep(title, want)
                if ok and is_pawn and not PAWN_ERA.search(title):
                    ok = False  # pawn shops: hunted eras only
                st = stats.setdefault(q, [0, 0, 0, 0])
                st[0] += 1
                if iid and iid in seen:
                    st[2] += 1
                    continue
                if not iid or not ok:
                    st[3] += 1
                    continue
                st[1] += 1
                cs_ = cstats.setdefault(q, {})
                cs_[c] = cs_.get(c, 0) + 1
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
                    "flag": flag_for(title, asp or is_trusted),
                })
            time.sleep(0.2)
          if quota_hit or budget_hit:
              break
        if not quota_hit and not budget_hit:
            done.add(q)
    if budget_hit:
        print(f"search budget of {SEARCH_MAX} calls reached: {len(queries) - len(done)} searches keep yesterday's results", file=sys.stderr)
    carried = 0
    if quota_hit or budget_hit:
        # don't wipe the feed with a half run: carry over the last full results for searches that didn't finish
        try:
            prev = json.load(open(os.path.join(HERE, "listings.json"), encoding="utf-8")).get("items", [])
        except Exception:
            prev = []
        for x in prev:
            if x.get("query") not in done and x.get("id") not in seen:
                seen.add(x.get("id")); out.append(dict(x, stale=1)); carried += 1
    if DEEP_MAX > 0 and not quota_hit:
        out, deep_calls, _ = deep_check(tok, out, limit=max(0, min(DEEP_MAX, DAILY_CALLS - calls - 20)))
        calls += deep_calls
    out.sort(key=lambda x: (x["country"] != "AU", x["country"] != "JP", x["country"] != "CN", x["title"]))
    meta = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"), "count": len(out), "calls": calls,
            "damaged_flagged": sum(1 for x in out if str(x.get("flag", "")).startswith("damage")),
            "unverified": sum(1 for x in out if x.get("flag") == "unverified"),
            "searches_done": f"{len(done)}/{len(queries)}", "carried_over": carried,
            "budget_stop": budget_hit,
            "pawn_sellers": sorted(set(pawns) | {str(x.get("seller")).lower() for x in out if x.get("seller") and PAWN_RX.search(str(x.get("seller")))}),
            "query_stats": {q: dict(zip(("returned", "new", "dupes", "filtered"), stats.get(q, [0, 0, 0, 0])), by_country=cstats.get(q, {}),
                                ran=q in done) for q in queries},
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
