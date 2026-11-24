#!/usr/bin/env python3
"""Adds card values for cards the feed keeps listing without one, so more listings can be judged against sold prices.

Each run takes up to GROW_MAX cards that have live listings but no value in values.json (want-list cards first,
then Japanese, then the cards with the most live copies), finds each one's own PriceCharting page and adds it to
values.json in the same form as the hand-made rows:
  - Raw, PSA 7, PSA 8, PSA 9, PSA 10 = median of the last 90 days' sales when there are 3+,
    otherwise PriceCharting's average (that grade is listed in "thin")
  - Japanese 1st Edition and unlimited are separate PriceCharting pages and stay separate here
  - "auto": 1 marks rows added this way; values_refresh.py keeps them current like the others
Safety: a row is only kept when it matches the live listing it was made from (same number, name, set words,
language, 1st Edition / reverse / shadowless), and cards PriceCharting can't match are retried after RETRY_DAYS.
Cards whose live copies later sit far under the value get listed in "Values to recheck" by enrich.py as usual.

Reads   listings.json, stores.json, values.json, values_grow.json (cards already tried)
Writes  values.json, values_grow.json, values_grow.md (what was added / not found)
Read-only towards eBay and shops: it only reads PriceCharting pages, politely spaced.
"""
import json, os, re, sys, time, urllib.parse
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from pc_prices import usd_aud  # noqa: E402
from pc_more import find_card, read_page, value, set_words, jp_set, SELLER_CODE  # noqa: E402
from megacash_prices import card_of  # noqa: E402
from feedlib import grade_of, lang_of, land_aud, norm, REPRINT, fix_spelling  # noqa: E402
from enrich import Values, MODERN, NEWER, SV_MEP, SKIP_CC, FIRST, REV, PRE, ODD, NOSYM  # noqa: E402

GROW_MAX = int(os.environ.get("GROW_MAX", "200"))
RETRY_DAYS = 14
MIN_PRICE = 30                       # A$: cheaper cards aren't worth a lookup
GRADES = [("Raw", "raw"), ("PSA 4", "g4"), ("PSA 5", "g5"), ("PSA 6", "g6"), ("PSA 7", "g7"), ("PSA 8", "g8"), ("PSA 9", "g9"), ("PSA 10", "g10")]
USEFUL = {"Raw", "PSA 7", "PSA 8", "PSA 9", "PSA 10"}   # listings worth a lookup (low grades are read, not hunted)
# words card_of() sometimes takes for the card's name that are really set codes, rarities or packaging
NOT_NAME = set("xy sm bw dp neo sealed promo mc rr rrr hr sr ur ar chr csr sar sir pokemon japanese holo rare lv "
               "nm no near mint dpbp vintage unlimited card old back".split())


def name_of(title):
    """card_of() for a title, with set codes / rarity letters taken out of the name; None when no real name is left."""
    c = card_of(fix_spelling(title))
    js = jp_set(title) if lang_of(title) == "JP" else None
    if lang_of(title) == "JP":
        c["jp"] = True        # titles like "Gengar DPBP 102 Space-Time Creation" are Japanese without saying so
    drop = js[2] if js else set()
    name = [w for w in c["name"] if w not in NOT_NAME and w not in drop and not SELLER_CODE.match(w)]
    if js:
        c["pcsets"], c["setkeys"] = js[0], js[1]
        # search PriceCharting with the card name, number and its Japanese set name, not the seller's wording
        setname = js[0][0].replace("pokemon-japanese-", "").replace("-", " ").replace("%27", "'")
        c["query"] = " ".join(name + [str(c.get("num") or "").replace("#", ""), "japanese", setname]).strip()
    if not any(len(w) >= 3 for w in name):
        return None
    c["name"] = name
    c["query"] = " ".join(w for w in c["query"].split() if w.lower() not in ("sealed",))
    return c


def P(n):
    return os.path.join(HERE, n)


def load(n, d):
    try:
        return json.load(open(P(n), encoding="utf-8"))
    except Exception:
        return d


def candidates(items, V):
    """{ident: {"t": sample title, "nm": bool, "num": .., "lang": .., "n": copies, "want": bool, "top": max A$}}"""
    out = {}
    for x in items:
        t = fix_spelling(x.get("title") or "")
        if (x.get("country") in SKIP_CC or x.get("flag") or x.get("sealed") or x.get("var") or x.get("lot") or REPRINT.search(t)
                or SV_MEP.search(t)):
            continue
        want = str(x.get("query") or "").startswith("+")
        if (MODERN.search(t) or NEWER.search(t)) and not want:
            continue
        lang = lang_of(t)
        if lang not in ("EN", "JP"):
            continue
        nm = x.get("nm") == 1
        g = grade_of(t) or ("Raw" if nm else "")
        if g not in USEFUL or land_aud(x) < MIN_PRICE:
            continue
        if V.find(t, nm):
            continue                      # already valued
        nums = Values.nums(t)
        if len(nums) != 1:
            continue                      # no card number, or several (lots, sets): too vague to look up
        num = next(iter(nums))
        c = name_of(t)
        if not c or c["other"] or c["jp"] != (lang == "JP"):
            continue
        first, rev, sl = bool(FIRST.search(t)), bool(REV.search(t)), "shadowless" in norm(t)
        sw = sorted(set_words(dict(c, keys=[])))
        ident = "|".join([lang, num, " ".join(c["name"]), "1st" if first else "", "rev" if rev else "", "sl" if sl else "", " ".join(sw[:4])])
        e = out.setdefault(ident, {"t": t, "nm": nm, "num": num, "lang": lang, "n": 0, "want": False, "top": 0,
                                   "first": first, "rev": rev, "sl": sl})
        e["n"] += 1
        e["want"] |= want
        e["top"] = max(e["top"], land_aud(x))
    return out


def entry_for(cand, url, info, rate, today):
    """A values.json row for the card, or (None, why)."""
    t = cand["t"]
    c = name_of(t)
    path = urllib.parse.urlparse(url).path
    parts = path.strip("/").split("/")
    if len(parts) < 3:
        return None, f"odd PriceCharting link {path}"
    cset = parts[1]
    flat = cset.replace("-", "")
    if c.get("setkeys"):
        keys = list(c["setkeys"])
    else:
        keys = sorted(w for w in set_words(dict(c, keys=[])) if w in flat or (len(w) > 5 and w[:5] in flat))
    if cand["num"].startswith("No.") and not keys:
        return None, "Japanese No. card with no set named in the title (the same number is in several sets)"
    g, thin = {}, []
    for G, k in GRADES:
        usd, note = value(info, k)
        if usd:
            g[G] = round(usd * rate)
            if note.get("thin"):
                thin.append(G)
    if not g:
        return None, "PriceCharting page has no prices"
    set_name = re.sub(r"^pokemon ", "", cset.replace("-", " ")).title()
    if c.get("setkeys"):      # Japanese set found by its English name: label it that way, so later listings match it
        set_name = "Japanese " + c["setkeys"][0].title()
    label = f"{set_name} {info.get('name') or ' '.join(c['name']).title()}".strip()
    if cand["num"] not in label:
        label += f" {cand['num']}"
    # the title's printing words (promo / premium file / Dark / Shining / no symbol...) belong to the card it was matched to
    extra = [w for rx in (PRE, ODD) for w in dict.fromkeys(m.lower() for m in rx.findall(t)) if w not in label.lower()]
    if NOSYM.search(t) and not NOSYM.search(label):
        extra.append(NOSYM.search(t).group(0).lower())
    if extra:
        label += " (" + ", ".join(extra) + ")"
    row = {"id": "auto:" + cand["lang"] + ":" + path, "label": label, "lang": cand["lang"], "num": cand["num"],
           "words": list(c["name"]), "keys": keys, "first": cand["first"], "sl": cand["sl"], "rev": cand["rev"], "g": g,
           "src": f"PriceCharting {today} (90-day sales median where 3+ sales; auto-added from a live listing)",
           "pc": path, "checked": today, "thin": thin, "auto": 1}
    hit = Values([dict(row, keys=row["keys"])]).find(t, cand["nm"])
    if not hit:
        return None, "the value wouldn't match its own listing (name/set words differ from PriceCharting's)"
    return row, ""


def main():
    today = date.today().isoformat()
    L = load("listings.json", {"items": []})
    S = load("stores.json", {"items": []})
    data = load("values.json", {"cards": []})
    tried = load("values_grow.json", {"tried": {}})
    known_pc = {e.get("pc") for e in data["cards"] if e.get("pc")}
    V = Values(data["cards"])
    items = list(L.get("items", [])) + [x for x in S.get("items", []) if x.get("available", True)]
    cands = candidates(items, V)
    retry = (date.today() - timedelta(days=RETRY_DAYS)).isoformat()
    todo = [(i, c) for i, c in cands.items() if tried["tried"].get(i, {}).get("day", "") < retry]
    todo.sort(key=lambda ic: (not ic[1]["want"], ic[1]["lang"] != "JP", -ic[1]["n"], -ic[1]["top"]))
    rate, rate_src = usd_aud()
    added, missed = [], []
    for ident, cand in todo[:GROW_MAX]:
        c = name_of(cand["t"])
        c["first"], c["rev"], c["sl"] = cand["first"], cand["rev"], cand["sl"]
        try:
            url, page = find_card(c)
            info = read_page(page)
            path = urllib.parse.urlparse(url).path
            if path in known_pc:
                row, why = None, f"PriceCharting page {path} is already in values.json under another name"
            else:
                row, why = entry_for(cand, url, info, rate, today)
        except Exception as ex:
            row, why = None, str(ex)[:160]
        tried["tried"][ident] = {"day": today, "ok": bool(row), "why": why}
        if row:
            data["cards"].append(row)
            known_pc.add(row["pc"])
            V = Values(data["cards"])
            added.append(f"- {row['label']} ({row['lang']}): " + ", ".join(f"{k} A${v:,}" for k, v in row["g"].items())
                         + f" — from “{cand['t'][:70]}” · https://www.pricecharting.com{row['pc']}")
        else:
            missed.append(f"- {cand['t'][:80]}: {why}")
        time.sleep(1.5)
    # forget tries older than 60 days so the file doesn't grow forever
    old = (date.today() - timedelta(days=60)).isoformat()
    tried["tried"] = {k: v for k, v in tried["tried"].items() if v.get("day", "") >= old}
    json.dump(data, open(P("values.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(tried, open(P("values_grow.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    left = max(0, len(todo) - GROW_MAX)
    md = [f"# New card values {today}",
          f"{len(added)} cards added from PriceCharting at US$1 = A${rate} ({rate_src}); {len(missed)} not matched; "
          f"{left} unvalued cards still waiting; {len(data['cards'])} cards valued in all.", ""]
    if added:
        md += ["## Added"] + added + [""]
    if missed:
        md += ["## Not matched (retried after %d days)" % RETRY_DAYS] + missed[:80] + [""]
    open(P("values_grow.md"), "w", encoding="utf-8").write("\n".join(md))
    print(f"values grow: {len(added)} added, {len(missed)} not matched, {left} still waiting")


if __name__ == "__main__":
    main()
