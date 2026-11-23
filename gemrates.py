#!/usr/bin/env python3
"""Gem-rate targets: cards in the hunted eras that are hard to grade a 10 but pay when you do.

Weekly (gemrates.yml). For each set in gem_sets.txt it reads PriceCharting's population page (how many copies got
each grade), keeps cards with enough graded copies to mean something, then reads those cards' own PriceCharting
pages for their sold prices by grade and sorts them into:

  SAFE   a PSA 8 breaks even, a PSA 9 makes money, a 10 is the bonus.     Max raw price = what a PSA 8 nets.
  HUNT   a PSA 9 about breaks even, a PSA 10 is worth 5x+ a 9.            Max raw price = what a PSA 9 nets.
  (cards that lose money even at a 9 are left out)

"Nets" = sale price x 0.87 (eBay fees) minus A$70 grading, the same sums enrich.py uses for grade plays.
Writes gemrates.json (enrich.py reads it: a raw NM copy at or under the max price becomes a grade play / 10 hunt,
which alerts.py can send to the phone) and gemrates.md (the ranked list, with eBay and PriceCharting links).
Population counts include re-submissions and only cards people bothered to grade, so they're a rough guide:
the rules below lean pessimistic. Read only: it never bids, buys or contacts anyone.
"""
import json, os, re, sys, time, urllib.parse
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from pc_prices import get, usd_aud, BASE  # noqa: E402
from pc_more import read_page, value  # noqa: E402

P = lambda f: os.path.join(HERE, f)
# entry rules
MIN_TOTAL = 100        # graded copies in all, or the counts are noise
MIN_N9 = 25            # PSA 9s: the card can reach a 9 and 9s exist to sell
MAX_GEM = 0.35         # gem rate above this: 10s are common, so no special upside (cards are still ranked by gem rate)
# money rules
SELL_KEEP, GRADE_COST = 0.87, 70   # eBay fees kept; base grading cost (same as enrich.py)
# PSA charges by declared value, so a card that comes back worth thousands costs more to grade.
# Rough A$ cost per card by the value it comes back at (submitter + PSA upcharge). Check against current PSA prices.
FEE_TIERS = [(750, 70), (2250, 150), (3750, 260), (7500, 480), (15000, 800)]
FEE_OVER = 0.10        # over the last tier: about 10% of the card's value
HALF_GRADE_WORSE = 0.5 # pop reports are mostly the good copies people chose to grade: assume half of each grade's share
                       # comes back one grade lower for an ordinary raw NM copy
THIN_10_KEEP = 0.6     # a PSA 10 value from under 3 recent sales counts for 60%
THIN_10_CAP = 5        # ... and never more than 5x the PSA 9 price
MAX_BUY = int(os.environ.get("GEM_MAX_BUY", "1000"))   # budget: targets whose max raw is over this are left out
MIN_MAX_RAW = 10       # not worth listing if the most you could pay is under A$10
# focus eras get their card prices read first (e-Card, EX, XY)
FOCUS = re.compile(r"expedition|aquapolis|skyridge|pokemon-ex-|ruby|sapphire|sandstorm|dragon|team-magma|hidden-legends|"
                   r"firered|team-rocket-returns|deoxys|emerald|unseen-forces|delta-species|legend-maker|holon|"
                   r"crystal-guardians|power-keepers|pokemon-xy|flashfire|furious-fists|phantom-forces|primal-clash|"
                   r"roaring-skies|ancient-origins|breakthrough|breakpoint|generations|fates-collide|steam-siege|evolutions")
# politeness / run length
POP_MAX = int(os.environ.get("GEM_POP_MAX", "130"))     # set pages per run
CARD_MAX = int(os.environ.get("GEM_CARD_MAX", "800"))   # card pages per run (~40 min at the pause below)
POP_DAYS, CARD_DAYS = 13, 20                            # re-read after this many days
PAUSE = 2.0


def text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).replace("&amp;", "&").replace("&#39;", "'").strip()


def grade_of_head(h):
    h = h.lower()
    if "total" in h:
        return "total"
    m = re.search(r"(\d+(?:\.5)?)\s*$", h) or re.search(r"(?:grade|psa|gem|mint)\D*(\d+(?:\.5)?)", h)
    return m.group(1) if m else None


LINK = re.compile(r'href="(?:https?://www\.pricecharting\.com)?/?(?:pop/item|pop/game|game)/([^"/?#]+)/([^"/?#]+)"')
NUM = re.compile(r"^\d{1,3}(?:,\d{3})*$|^\d+$")


def parse_pop(page):
    """{card path: {"name", "n": {"10": .., "9": ..}, "total": ..}} from a PriceCharting set population page.
    Card links look like /pop/item/<set>/<card>; the price page is /game/<set>/<card>.
    First tries table rows; if the page isn't a plain table, reads each card's block of numbers in the order
    of the page's "Card Grade 6 ... Grade 10 Total" header."""
    out, heads = {}, []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<t([dh])[^>]*>(.*?)</t[dh]>", row, re.S)
        labels = [text(c) for _, c in cells]
        if (cells and all(k == "h" for k, _ in cells)) or sum(1 for l in labels if re.search(r"grade|total|psa", l, re.I)) >= 3:
            heads = [grade_of_head(l) for l in labels]
            continue
        link = LINK.search(row)
        if not link or not heads:
            continue
        n, total = {}, None
        for h, l in zip(heads, labels):
            l = l.replace(" ", "")
            if not h or not NUM.match(l):
                continue
            v = int(l.replace(",", ""))
            if h == "total":
                total = v
            else:
                n[h] = v
        if not n:
            continue
        name = re.sub(r"\s*#\s*\w+$", "", labels[0]) if labels and labels[0] else link.group(2)
        out[f"/game/{link.group(1)}/{link.group(2)}"] = {"name": name, "n": n, "total": total or sum(n.values())}
    if out:
        return out
    # fallback: not a table. Column order from the header text, then the numbers after each card's name link.
    flat = text(page)
    m = re.search(r"\bCard\s+((?:Grade\s+\d+(?:\.5)?\s+)+)Total\b", flat, re.I)
    order = (re.findall(r"Grade\s+(\d+(?:\.5)?)", m.group(1), re.I) + ["total"]) if m else ["6", "7", "8", "9", "10", "total"]
    hits = [(x.start(), x.group(1), x.group(2)) for x in LINK.finditer(page)]
    for i, (pos, st, slug) in enumerate(hits):
        path = f"/game/{st}/{slug}"
        if path in out:
            continue
        nxt = next((p for p, s2, g2 in hits[i + 1:] if (s2, g2) != (st, slug)), len(page))
        a = re.search(r">(.*?)</a>", page[pos:nxt], re.S)
        name = text(a.group(1)) if a else ""
        after = text(page[pos + (a.end() if a else 0):nxt])
        nums = [t for t in after.split() if NUM.match(t)][:len(order)]
        if not name or len(nums) < len(order):
            continue
        vals = dict(zip(order, (int(t.replace(",", "")) for t in nums)))
        total = vals.pop("total", None)
        out[path] = {"name": re.sub(r"\s*#\s*\w+$", "", name), "n": vals, "total": total or sum(vals.values())}
    return out


def load(name, default):
    try:
        return json.load(open(P(name), encoding="utf-8"))
    except Exception:
        return default


def sets_to_read():
    out = []
    for line in open(P("gem_sets.txt"), encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.split()[0])
    return list(dict.fromkeys(out))


def card_num(path):
    m = re.search(r"-(\d+[a-z]?)$", path)
    return m.group(1) if m else ""


def fee(v):
    for top, cost in FEE_TIERS:
        if v <= top:
            return cost
    return v * FEE_OVER


def net(v):
    return v * SELL_KEEP - fee(v) if v else None


def classify(v, n, total, thin):
    """(kind, max raw A$, expected net A$, why). Max raw = what an ordinary raw NM copy is expected to net once graded
    and sold, using the population's grade mix shifted half a grade worse. SAFE when a PSA 8 alone still covers it."""
    v7, v8, v9, v10, raw = (v.get(k) for k in ("PSA 7", "PSA 8", "PSA 9", "PSA 10", "Raw"))
    if not v8 or not v9:
        return None, None, None, "no PSA 8 or 9 price"
    if "PSA 8" in thin or "PSA 9" in thin:
        return None, None, None, "too few PSA 8/9 sales to trust"
    if v9 < v8 or (v10 and v10 < v9) or (v7 and v8 < v7):
        return None, None, None, "prices out of order (a higher grade priced lower): bad data"
    if not total:
        return None, None, None, "no population"
    if v10 and "PSA 10" in thin:
        v10 = min(v10 * THIN_10_KEEP, v9 * THIN_10_CAP)   # one big outlier sale (often a 1st Ed or mislabelled) can't carry it
    p = {g: n.get(str(g), 0) / total for g in (10, 9, 8, 7)}
    h = HALF_GRADE_WORSE
    q = {10: p[10] * (1 - h), 9: p[9] * (1 - h) + p[10] * h, 8: p[8] * (1 - h) + p[9] * h, 7: p[7] * (1 - h) + p[8] * h}
    q_low = max(0.0, 1 - sum(q.values()))
    low = net(raw or (v7 or v8) * 0.5) or 0          # a low-grade slab sells for about raw
    ev = (q[10] * (net(v10) if v10 else net(v9)) + q[9] * net(v9) + q[8] * net(v8)
          + q[7] * (net(v7) if v7 else net(v8 * 0.6)) + q_low * low)
    max_raw = round(ev)
    if max_raw < MIN_MAX_RAW:
        return None, None, None, "doesn't pay on the usual grade mix"
    if max_raw > MAX_BUY:
        return None, None, None, f"over the A${MAX_BUY:,} budget"
    return ("SAFE" if net(v8) >= max_raw else "HUNT"), max_raw, round(ev), ""


SETS = {  # English set slug -> (printed total, one distinctive title word or "")
 "base-set": (102, "base"), "jungle": (64, "jungle"), "fossil": (62, "fossil"), "team-rocket": (82, "rocket"),
 "gym-heroes": (132, "heroes"), "gym-challenge": (132, "challenge"), "neo-genesis": (111, "genesis"),
 "neo-discovery": (75, "discovery"), "neo-revelation": (64, "revelation"), "neo-destiny": (105, "destiny"),
 "legendary-collection": (110, "legendary"), "expedition": (165, "expedition"), "aquapolis": (147, "aquapolis"),
 "skyridge": (144, "skyridge"), "ruby-&-sapphire": (109, "sapphire"), "sandstorm": (100, "sandstorm"),
 "dragon": (97, ""), "team-magma-&-team-aqua": (95, "magma"), "hidden-legends": (101, "hidden"),
 "fire-red-&-leaf-green": (112, "firered"), "team-rocket-returns": (109, "returns"), "deoxys": (107, ""),
 "emerald": (106, "emerald"), "unseen-forces": (115, "unseen"), "delta-species": (113, "delta"),
 "legend-maker": (92, "maker"), "holon-phantoms": (110, "holon"), "crystal-guardians": (100, "guardians"),
 "dragon-frontiers": (101, "frontiers"), "power-keepers": (108, "keepers"), "diamond-&-pearl": (130, ""),
 "mysterious-treasures": (123, "mysterious"), "secret-wonders": (132, "wonders"), "great-encounters": (106, "encounters"),
 "majestic-dawn": (100, "majestic"), "legends-awakened": (146, "awakened"), "stormfront": (100, "stormfront"),
 "platinum": (127, ""), "rising-rivals": (111, "rivals"), "supreme-victors": (147, "victors"), "arceus": (99, ""),
 "heartgold-&-soulsilver": (123, "heartgold"), "unleashed": (95, "unleashed"), "undaunted": (90, "undaunted"),
 "triumphant": (102, "triumphant"), "call-of-legends": (95, "call"), "black-&-white": (114, ""),
 "emerging-powers": (98, "emerging"), "noble-victories": (101, "noble"), "next-destinies": (99, "destinies"),
 "dark-explorers": (108, "explorers"), "dragons-exalted": (124, "exalted"), "dragon-vault": (20, "vault"),
 "boundaries-crossed": (149, "boundaries"), "plasma-storm": (135, ""), "plasma-freeze": (116, ""),
 "plasma-blast": (101, ""), "legendary-treasures": (113, ""), "xy": (146, ""), "flashfire": (106, "flashfire"),
 "furious-fists": (111, "furious"), "phantom-forces": (119, "phantom"), "primal-clash": (160, "primal"),
 "roaring-skies": (108, "roaring"), "ancient-origins": (98, "ancient"), "breakthrough": (162, "breakthrough"),
 "breakpoint": (122, "breakpoint"), "generations": (83, "generations"), "fates-collide": (124, "fates"),
 "steam-siege": (114, "steam"), "evolutions": (108, "evolutions"),
}
VARIANT = {"Reverse Holo": "(reverse,rev)", "Shadowless": "shadowless", "1st Edition": "1st", "Cosmos Holo": "cosmos",
           "Prerelease": "prerelease", "Prerelease Staff": "staff", "Toys R Us": "(toys,tru)", "Gold Border": "gold",
           "City Championships": "championship"}


def ebay_query(name, num, set_slug, lang):
    """Tight but not narrow: the card name plus its number written the ways sellers write it (56/144, 056/144)
    or one distinctive set word, and the variant (reverse holo, shadowless...) when there is one."""
    card = (name or "").split("#")[0]
    variants = re.findall(r"\[([^\]]+)\]", card)
    plain = re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", card)).strip()
    slug = (set_slug or "").replace("pokemon-", "")
    if lang == "JP":
        words = [w for w in slug.replace("japanese-", "").replace("%27s", "").split("-") if len(w) > 3]
        word = max(words, key=len) if words else ""
        alts = [x for x in (word, num if num and num.isdigit() and int(num) > 0 else "") if x]
        q = f"{plain} japanese" + (f" ({','.join(alts)})" if len(alts) > 1 else f" {alts[0]}" if alts else "")
    else:
        total, word = SETS.get(slug, (None, ""))
        alts = []
        if num and num.isdigit() and total:
            alts = [f"{int(num)}/{total}", f"{int(num):03d}/{total}"]
        elif num:
            alts = [num]
        if word:
            alts.append(word)
        alts = list(dict.fromkeys(alts))
        q = plain + (f" ({','.join(alts)})" if len(alts) > 1 else f" {alts[0]}" if alts else "")
    extra = [VARIANT[v] for v in variants if v in VARIANT]
    if lang != "JP" and "Reverse Holo" not in variants:
        extra.append("-reverse")
    if lang != "JP" and "1st Edition" not in variants and re.match(r"(base-set|jungle|fossil|team-rocket|gym-|neo-)", slug) \
            and slug != "team-rocket-returns":
        extra.append("-1st")      # 1st Edition copies are a different (dearer) card
    return " ".join([q] + extra)


def ebay(q, sold=False):
    u = "https://www.ebay.com.au/sch/i.html?_nkw=" + urllib.parse.quote_plus(q)
    return u + ("&LH_Sold=1&LH_Complete=1" if sold else "&_sop=15")


def main():
    today = date.today()
    T = today.isoformat()
    data = load("gemrates.json", {"pop": {}, "cards": {}})
    data.setdefault("pop", {}); data.setdefault("cards", {})
    rate, rate_src = usd_aud()
    sets = sets_to_read()
    log = []

    # 1. population pages (oldest first)
    pop_cut = (today - timedelta(days=POP_DAYS)).isoformat()
    todo = sorted((s for s in sets if data["pop"].get(s, {}).get("day", "") < pop_cut or not data["pop"].get(s, {}).get("rows")),
                  key=lambda s: data["pop"].get(s, {}).get("day", ""))
    for s in todo[:POP_MAX]:
        try:
            _, page = get(f"{BASE}/pop/set/{s}")
            rows = parse_pop(page)
            if rows:
                data["pop"][s] = {"day": T, "rows": rows}
            else:
                log.append(f"- {s}: population page read but no card rows understood (page layout may have changed)")
        except Exception as e:
            data["pop"][s] = {"day": data["pop"].get(s, {}).get("day", ""), "rows": data["pop"].get(s, {}).get("rows", {}), "error": str(e)[:120]}
            log.append(f"- {s}: population page failed ({str(e)[:80]})")
        time.sleep(PAUSE)

    # 2. cards that pass the population rules
    cands = []
    for s in sets:
        for path, r in (data["pop"].get(s, {}).get("rows") or {}).items():
            n10, n9, tot = r["n"].get("10", 0), r["n"].get("9", 0), r["total"] or 0
            if tot >= MIN_TOTAL and n9 >= MIN_N9:
                cands.append((path, s, r, n10 / tot if tot else 0))

    # 3. prices for those cards (stalest first, lowest gem rate first)
    card_cut = (today - timedelta(days=CARD_DAYS)).isoformat()
    need = [c for c in cands if data["cards"].get(c[0], {}).get("day", "") < card_cut]
    need.sort(key=lambda c: (data["cards"].get(c[0], {}).get("day", ""), not FOCUS.search(c[1]), c[3]))
    for path, s, r, gem in need[:CARD_MAX]:
        try:
            _, page = get(BASE + path)
            info = read_page(page)
            v, thin = {}, []
            for G, k in (("Raw", "raw"), ("PSA 7", "g7"), ("PSA 8", "g8"), ("PSA 9", "g9"), ("PSA 10", "g10")):
                usd, note = value(info, k)
                if usd:
                    v[G] = round(usd * rate)
                    if note.get("thin"):
                        thin.append(G)
            data["cards"][path] = {"day": T, "v": v, "thin": thin, "name": info.get("name") or r["name"]}
        except Exception as e:
            data["cards"][path] = {"day": T, "v": {}, "error": str(e)[:120]}
        time.sleep(PAUSE)

    # 4. sort into safe plays and 10 hunts
    out, skipped = {}, {}
    for path, s, r, gem in cands:
        c = data["cards"].get(path) or {}
        v = c.get("v") or {}
        if not v or gem > MAX_GEM:
            continue
        n = r["n"]; tot = r["total"]
        kind, max_raw, ev, why = classify(v, n, tot, c.get("thin", []))
        if not kind:
            skipped[why.split(" (")[0].split(":")[0]] = skipped.get(why.split(" (")[0].split(":")[0], 0) + 1
            continue
        out[path] = {"kind": kind, "max_raw": max_raw, "set": s, "name": c.get("name") or r["name"], "num": card_num(path),
                     "lang": "JP" if "japanese" in s else "EN", "gem": round(gem, 3), "n10": n.get("10", 0), "n9": n.get("9", 0),
                     "n8": n.get("8", 0), "total": tot, "v": v, "thin": c.get("thin", []),
                     "ev_graded": ev, "raw_market": v.get("Raw"), "checked": c.get("day"),
                     "q": ebay_query(c.get("name") or r["name"], card_num(path), s, "JP" if "japanese" in s else "EN")}
    data["targets"] = out
    data["updated"] = T
    data["rules"] = {"min_total": MIN_TOTAL, "min_psa9": MIN_N9, "max_gem": MAX_GEM, "sell_keep": SELL_KEEP,
                     "grade_cost": GRADE_COST, "fee_tiers": FEE_TIERS, "half_grade_worse": HALF_GRADE_WORSE,
                     "max_buy": MAX_BUY, "usd_aud": rate}
    json.dump(data, open(P("gemrates.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    # 5. the readable list
    left_pop = max(0, len(todo) - POP_MAX)
    left_cards = max(0, len(need) - CARD_MAX)
    md = [f"# Gem-rate targets {T}",
          f"{len(out)} targets from {len(cands)} cards with {MIN_TOTAL}+ graded copies and {MIN_N9}+ PSA 9s, across "
          f"{sum(1 for s in sets if data['pop'].get(s, {}).get('rows'))} of {len(sets)} sets. US$1 = A${rate} ({rate_src})."
          + (f" Still to read: {left_pop} set pages, {left_cards} card pages (next runs)." if left_pop or left_cards else ""),
          "", "**Max raw** = the most to pay for a raw NM copy, landed: what a copy is expected to net after grading "
          "(fees by value tier) and eBay fees, using the card's real grade mix from the pop report, shifted half a grade "
          "worse because pop reports are mostly the best copies. SAFE: a PSA 8 alone still covers max raw. "
          "HUNT: you need a 9 or better. Gem rate = PSA 10s ÷ all graded copies. Cards need 3+ recent PSA 8 and 9 sales; "
          f"a thin PSA 10 price counts {THIN_10_KEEP:.0%}, capped at {THIN_10_CAP}x the PSA 9. Over A${MAX_BUY:,} max raw is left out (budget). "
          "Raw now = PriceCharting's raw price, which includes played copies.", ""]
    if skipped:
        md += ["Left out: " + "; ".join(f"{k} ({v})" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])), ""]
    for kind, title in (("SAFE", "Safe grade plays"), ("HUNT", "10 hunts")):
        rows = sorted((x for x in out.values() if x["kind"] == kind), key=lambda x: (x["gem"], -x["max_raw"]))
        md += [f"## {title} ({len(rows)})", ""]
        for x in rows:
            v = x["v"]
            q = x["q"]
            thin = f" (few sales: {', '.join(x['thin'])})" if x["thin"] else ""
            md.append(f"- **{x['name']}** #{x['num']} · {x['set'].replace('pokemon-', '').replace('-', ' ')} — "
                      f"gem {x['gem'] * 100:.{1 if x['gem'] < 0.1 else 0}f}% ({x['n10']}/{x['total']}), PSA 9 pop {x['n9']} · "
                      f"PSA 8 A${v.get('PSA 8', 0):,} · 9 A${v.get('PSA 9', 0):,} · 10 A${v.get('PSA 10', 0):,} · "
                      f"raw now A${v.get('Raw', 0):,} → **max raw A${x['max_raw']:,}**{thin} · "
                      f"[eBay]({ebay(q)}) · [sold]({ebay(q, True)}) · [PriceCharting]({BASE}{next(p for p, y in out.items() if y is x)})")
        md.append("")
    if log:
        md += ["## Problems this run", ""] + log + [""]
    open(P("gemrates.md"), "w", encoding="utf-8").write("\n".join(md))
    print(f"gem rates: {len(out)} targets ({sum(1 for x in out.values() if x['kind'] == 'SAFE')} safe, "
          f"{sum(1 for x in out.values() if x['kind'] == 'HUNT')} hunts); {len(todo[:POP_MAX])} set pages and "
          f"{len(need[:CARD_MAX])} card pages read; {left_pop} sets and {left_cards} cards left for next run")


if __name__ == "__main__":
    main()
