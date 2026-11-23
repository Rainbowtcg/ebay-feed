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
# money rules (same as enrich.py grade plays)
SELL_KEEP, GRADE_COST = 0.87, 70
SAFE_9_OVER_8 = 1.5    # a 9 must be worth 1.5x an 8 for a safe play ...
SAFE_MIN_GAIN = 40     # ... and make at least A$40 more than an 8
HUNT_10_OVER_9 = 5.0   # a 10 hunt needs a 10 worth 5x a 9 ...
HUNT_MIN_UPSIDE = 300  # ... and at least A$300 profit if it does get the 10
MIN_MAX_RAW = 10       # not worth listing if the most you could pay is under A$10
# politeness / run length
POP_MAX = int(os.environ.get("GEM_POP_MAX", "130"))     # set pages per run
CARD_MAX = int(os.environ.get("GEM_CARD_MAX", "200"))   # card pages per run
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


def classify(v):
    """(kind, max raw A$, note) from A$ values by grade, or (None, None, why)."""
    v8, v9, v10 = v.get("PSA 8"), v.get("PSA 9"), v.get("PSA 10")
    if not v9:
        return None, None, "no PSA 9 price"
    net = lambda x: x * SELL_KEEP - GRADE_COST
    if v8 and v9 >= SAFE_9_OVER_8 * v8 and net(v8) >= MIN_MAX_RAW and (v9 - v8) * SELL_KEEP >= SAFE_MIN_GAIN:
        return "SAFE", round(net(v8)), ""
    if v10 and v10 >= HUNT_10_OVER_9 * v9 and net(v9) >= MIN_MAX_RAW and net(v10) - net(v9) >= HUNT_MIN_UPSIDE:
        return "HUNT", round(net(v9)), ""
    return None, None, "doesn't pay at a 9 after grading and fees"


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
    need.sort(key=lambda c: (data["cards"].get(c[0], {}).get("day", ""), c[3]))
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
    out = {}
    for path, s, r, gem in cands:
        c = data["cards"].get(path) or {}
        v = c.get("v") or {}
        if not v:
            continue
        kind, max_raw, why = classify(v)
        if not kind or gem > MAX_GEM:
            continue
        n = r["n"]; tot = r["total"]
        p = {g: n.get(g, 0) / tot for g in ("10", "9", "8")}
        p_low = max(0.0, 1 - sum(p.values()))
        ev = (p["10"] * v.get("PSA 10", 0) + p["9"] * v.get("PSA 9", 0) + p["8"] * v.get("PSA 8", 0)
              + p_low * v.get("PSA 7", v.get("PSA 8", 0) * 0.6)) * SELL_KEEP - GRADE_COST
        out[path] = {"kind": kind, "max_raw": max_raw, "set": s, "name": c.get("name") or r["name"], "num": card_num(path),
                     "lang": "JP" if "japanese" in s else "EN", "gem": round(gem, 3), "n10": n.get("10", 0), "n9": n.get("9", 0),
                     "n8": n.get("8", 0), "total": tot, "v": v, "thin": c.get("thin", []),
                     "ev_graded": round(ev), "raw_market": v.get("Raw"), "checked": c.get("day")}
    data["targets"] = out
    data["updated"] = T
    data["rules"] = {"min_total": MIN_TOTAL, "min_psa9": MIN_N9, "max_gem": MAX_GEM, "sell_keep": SELL_KEEP,
                     "grade_cost": GRADE_COST, "usd_aud": rate}
    json.dump(data, open(P("gemrates.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    # 5. the readable list
    left_pop = max(0, len(todo) - POP_MAX)
    left_cards = max(0, len(need) - CARD_MAX)
    md = [f"# Gem-rate targets {T}",
          f"{len(out)} targets from {len(cands)} cards with {MIN_TOTAL}+ graded copies and {MIN_N9}+ PSA 9s, across "
          f"{sum(1 for s in sets if data['pop'].get(s, {}).get('rows'))} of {len(sets)} sets. US$1 = A${rate} ({rate_src})."
          + (f" Still to read: {left_pop} set pages, {left_cards} card pages (next runs)." if left_pop or left_cards else ""),
          "", "**Max raw** = the most to pay for a raw NM copy, landed. SAFE: a PSA 8 breaks even there. "
          "HUNT: a PSA 9 breaks even there, so you're playing for the 10. Gem rate = PSA 10s ÷ all graded copies. "
          "Raw now = PriceCharting's raw price: if it's well over max raw, you need a bargain to make it work.", ""]
    for kind, title in (("SAFE", "Safe grade plays"), ("HUNT", "10 hunts")):
        rows = sorted((x for x in out.values() if x["kind"] == kind), key=lambda x: (x["gem"], -x["max_raw"]))
        md += [f"## {title} ({len(rows)})", ""]
        for x in rows:
            v = x["v"]
            q = f"{x['name']} {x['num']}{' japanese' if x['lang'] == 'JP' else ''}".strip()
            thin = f" (few sales: {', '.join(x['thin'])})" if x["thin"] else ""
            md.append(f"- **{x['name']}** #{x['num']} · {x['set'].replace('pokemon-', '').replace('-', ' ')} — "
                      f"gem {x['gem'] * 100:.0f}% ({x['n10']}/{x['total']}), PSA 9 pop {x['n9']} · "
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
