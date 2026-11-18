#!/usr/bin/env python3
"""Keeps values.json (the card values behind the feed's "under value" flags) current.

Each run re-reads the PriceCharting page of up to MAX_REFRESH cards whose values are older than STALE_DAYS,
and rewrites their A$ values at today's exchange rate:
  - Raw, PSA 7, PSA 8, PSA 9, PSA 10 = median of the last 90 days' sales when there are 3+,
    otherwise PriceCharting's average (that grade is listed in the card's "thin" field)
  - the card's PriceCharting page is saved in "pc" so later runs go straight to it
Safety: only rows that came from PriceCharting or the Finder's benchmarks are refreshed (hand-researched rows,
e.g. "eBay sold" or "Scrydex", are left alone), and a new value more than 3x or under a third of the old one
is not written: it is listed in values_check.md instead, since that usually means a wrong match.
"""
import json, os, re, sys, time, urllib.parse
from datetime import date, datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from pc_prices import usd_aud  # noqa: E402
from pc_more import find_card, read_page, value  # noqa: E402

MAX_REFRESH = int(os.environ.get("VALUES_MAX", "120"))
STALE_DAYS = 6
AUTO_SRC = re.compile(r"^(Finder benchmark|PriceCharting)", re.I)
GRADES = [("Raw", "raw"), ("PSA 7", "g7"), ("PSA 8", "g8"), ("PSA 9", "g9"), ("PSA 10", "g10")]


def card_of(e):
    keys = [w for k in e.get("keys", []) for w in k.lower().split()]
    words = [w.lower() for w in e.get("words", [])]
    name = [w for w in words if w not in keys] or words
    n = e["num"]
    if n.lower().startswith("no."):
        num, tot = n[3:], None
    elif "/" in n:
        num, tot = n.split("/", 1)
    else:
        num, tot = n, None
    label = e["label"]
    q = re.sub(r"\b(\w+)\s*/\s*[\w-]+\b", r"\1", label)
    q = re.sub(r"\b1st\s+ed\b\.?", "1st edition", q, flags=re.I)
    q = re.sub(r"\bno\.\s*", "", q, flags=re.I)
    if e.get("rev") and "reverse" not in q.lower():
        q += " reverse holo"
    return {"title": label, "name": name, "num": num, "tot": tot, "jp": e.get("lang") == "JP",
            "rev": bool(e.get("rev")), "first": bool(e.get("first")), "sl": bool(e.get("sl")),
            "keys": keys, "query": q}


def main():
    path = os.path.join(HERE, "values.json")
    data = json.load(open(path, encoding="utf-8"))
    rate, rate_src = usd_aud()
    today = date.today().isoformat()
    stale = (date.today() - timedelta(days=STALE_DAYS)).isoformat()
    # values the last summary flagged as probably wrong (most live copies far under them) go first, even if recently checked
    try:
        sus = {c.get("id") for c in json.load(open(os.path.join(HERE, "values_suspect.json"), encoding="utf-8")).get("cards", [])}
    except Exception:
        sus = set()
    todo = [e for e in data["cards"] if e.get("lang") in ("EN", "JP") and AUTO_SRC.search(e.get("src", ""))
            and (e.get("checked", "") < stale or (e.get("id") in sus and e.get("checked", "") < today))]
    todo.sort(key=lambda e: (e.get("id") not in sus, e.get("checked", "")))  # suspect first, then oldest first
    hand = [e["label"] for e in data["cards"] if e.get("id") in sus and not AUTO_SRC.search(e.get("src", ""))]
    done, errors, checks = 0, [], []
    for e in todo[:MAX_REFRESH]:
        c = card_of(e)
        try:
            url, page = find_card(c, ref=e.get("pc"))
            info = read_page(page)
        except Exception as ex:
            errors.append(f"- {e['label']}: {str(ex)[:160]}")
            e["checked"] = today  # try again after STALE_DAYS, not every run
            time.sleep(1.5)
            continue
        thin, new = [], {}
        for g, k in GRADES:
            usd, note = value(info, k)
            if not usd:
                continue
            aud = round(usd * rate)
            old = e["g"].get(g)
            if old and (aud > old * 3 or aud < old / 3):
                checks.append(f"- {e['label']} {g}: was A${old}, PriceCharting now A${aud} — kept the old value; "
                              f"check the match [{info['name']}]({url})")
                continue
            new[g] = aud
            if note.get("thin"):
                thin.append(g)
        e["g"].update(new)
        e["pc"] = urllib.parse.urlparse(url).path
        e["checked"] = today
        e["thin"] = thin
        e["src"] = f"PriceCharting {today} (90-day sales median where 3+ sales)"
        done += 1
        time.sleep(1.5)
    data["updated"] = today
    data["rate"] = {"usd_aud": rate, "source": rate_src}
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    left = max(0, len(todo) - MAX_REFRESH)
    md = [f"# Card value refresh {today}", f"{done} cards refreshed at US$1 = A${rate}; {left} still due.", ""]
    if checks:
        md += ["## Big jumps not written (check the match)"] + checks + [""]
    if hand:
        md += ["## Flagged as probably wrong, but hand-researched (not refreshed automatically)"] + [f"- {h}" for h in hand] + [""]
    if errors:
        md += ["## Not found on PriceCharting"] + errors + [""]
    open(os.path.join(HERE, "values_check.md"), "w", encoding="utf-8").write("\n".join(md))
    print(f"values refresh: {done} refreshed, {len(checks)} big jumps held back, {len(errors)} not found, {left} still due")


if __name__ == "__main__":
    main()
