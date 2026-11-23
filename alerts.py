"""Phone alerts for real deals.

Reads digest.json (written by enrich.py) and picks out the highlights worth a ping:
  - cards priced well under their sold value (20%+ under, not auctions, not values flagged as doubtful)
  - grade plays that clear at least A$500 at PSA 9 after grading and fees
  - sealed ETBs / booster boxes at or under RRP
  - gem-rate targets (gemrates.py): raw NM copies at or under the card's max raw price
Each listing is alerted once (alerts_seen.json remembers it for 30 days); it is alerted again only if its
price drops another 10%+. New picks are posted as ONE GitHub issue per run, which the GitHub app pushes to
the phone. Set ALERT_ISSUE=1 (scan.yml / shops.yml do) to post; without it the script only prints.
Read only: it never bids, buys or contacts anyone.
"""
import json, os, re, subprocess, sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
P = lambda f: os.path.join(HERE, f)
OWNER = os.environ.get("ALERT_MENTION", "Rainbowtcg")  # @mention so the issue lands as a direct notification
MIN_UNDER = 20        # % under sold value
MIN_GRADE_NET = 500   # A$ profit at PSA 9 for a grade play
REDROP = 0.90         # alert again if the price falls to 90% or less of the last alerted price
KEEP_DAYS = 30
MAX_ITEMS = 20
SKIP_CC = {"US", "GB", "UK", "CA"}

UNDER_RX = re.compile(r"(\d+)% under A\$[\d,]+ value")
GRADE_RX = re.compile(r"PSA 9 ~A\$[\d,]+ = \+A\$([\d,]+)")


def pick(h):
    """(kind, score) if this highlight deserves an alert, else None."""
    why = h.get("why") or ""
    if h.get("cc") in SKIP_CC:
        return None
    if why.startswith("at or under RRP"):
        return ("Sealed at or under RRP", 0)
    if why.startswith(("10 hunt", "gem target")):     # raw NM copy at or under its weekly gem-rate max price
        return ("Gem-rate target", 0)
    if why.startswith("grade play"):
        m = GRADE_RX.search(why)
        net = int(m.group(1).replace(",", "")) if m else 0
        return ("Grade play", net) if net >= MIN_GRADE_NET else None
    if h.get("auc") or "live copies are" in why:   # auctions climb; "but N of M live copies are…" = doubtful value
        return None
    m = UNDER_RX.search(why)
    if m and int(m.group(1)) >= MIN_UNDER:
        return ("Under sold value", int(m.group(1)))
    return None


def main():
    try:
        d = json.load(open(P("digest.json"), encoding="utf-8"))
    except Exception as e:
        sys.exit(f"alerts: no digest.json ({e})")
    try:
        seen = json.load(open(P("alerts_seen.json"), encoding="utf-8"))
    except Exception:
        seen = {}
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v.get("d", "") >= cutoff}

    picks, ids = [], set()
    for c in d.get("categories", []):
        for h in c.get("hi", []):
            i = h.get("id")
            if not i or i in ids:
                continue
            got = pick(h)
            if not got:
                continue
            pl = float(h.get("pl") or h.get("p") or 0)
            old = seen.get(i)
            if old and not (pl and pl <= REDROP * float(old.get("pl") or 0)):
                continue
            ids.add(i)
            picks.append((not c.get("want"), got[0] != "Under sold value", -got[1], pl, c["cat"], got[0], h))
    picks.sort(key=lambda x: x[:4])
    picks = picks[:MAX_ITEMS]
    if not picks:
        print("alerts: nothing new worth a ping")
        json.dump(seen, open(P("alerts_seen.json"), "w", encoding="utf-8"), indent=1)
        return

    day = d.get("day") or now.date().isoformat()
    lines = [f"@{OWNER} {len(picks)} new deal{'s' if len(picks) != 1 else ''} from the {day} scan "
             f"(each listing is only alerted once; check photos and condition before buying).", ""]
    for *_, cat, kind, h in picks:
        shop = " · shop" if h.get("shop") else ""
        lines.append(f"- **A${float(h.get('pl') or h.get('p') or 0):,.0f}** {h.get('t', '')} "
                     f"({h.get('cc', '')}, {h.get('seller', '')}{shop}) · _{kind}, {cat}_  \n"
                     f"  {h.get('why', '')}  \n  {h.get('url', '')}")
        seen[h["id"]] = {"d": now.date().isoformat(), "pl": h.get("pl") or h.get("p")}
    body = "\n".join(lines)
    title = f"Deals {day}: {len(picks)} new"
    print(title); print(body)

    posted = True
    if os.environ.get("ALERT_ISSUE") == "1":
        r = subprocess.run(["gh", "issue", "create", "--title", title, "--body", body], cwd=HERE,
                           capture_output=True, text=True, timeout=120)
        posted = r.returncode == 0
        print(r.stdout.strip() or r.stderr.strip())
    if posted:  # only remember them once the alert has gone out, so a failed post retries next run
        json.dump(seen, open(P("alerts_seen.json"), "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
