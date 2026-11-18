#!/usr/bin/env python3
"""Which searches in queries.txt actually find anything, so dead ones can be dropped and their eBay calls reused.

Usage: python query_report.py      (after ebay_search.py; reads listings.json and queries.txt)
Keeps   query_history.json  the last KEEP_RUNS days of results per search line (new listings, per country)
Writes  queries_report.md   searches that found nothing new for a while, countries that never find anything for a
                            line, and the day's eBay call count against the 5,000 a day limit
Read-only: it only reports; it never edits queries.txt.
"""
import json, os, sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
try:
    from feedlib import line_options
except Exception:
    def line_options(line):
        return line.lstrip("+").strip(), {}

KEEP_RUNS = 14
MIN_RUNS = 7          # judge a line only after this many runs
COUNTRIES = ["AU", "JP", "CN", "NZ", "HK", "SG", "KR"]


def P(*a):
    return os.path.join(HERE, *a)


def calls_per_run(line):
    """eBay calls one line costs per daily run (NM pass doubles a normal line)."""
    _, o = line_options(line)
    if o.get("pawn"):
        return 3 * 2
    if o.get("trusted"):
        return 3 * (len(o["c"]) if isinstance(o.get("c"), list) else 1)
    n = len([c for c in o["c"] if c in COUNTRIES]) if isinstance(o.get("c"), list) else len(COUNTRIES)
    return n * (1 if o.get("nonm") else 2)


def main():
    L = json.load(open(P("listings.json"), encoding="utf-8"))
    qs = L.get("query_stats", {})
    lines = [l.strip() for l in open(P("queries.txt"), encoding="utf-8") if l.strip() and not l.startswith("#")]
    try:
        H = json.load(open(P("query_history.json"), encoding="utf-8"))
    except Exception:
        H = {"runs": []}
    day = str(L.get("generated", ""))[:10] or datetime.now(timezone.utc).date().isoformat()
    run = {"day": day, "q": {q: [s.get("new", 0), s.get("returned", 0), s.get("by_country", {})] for q, s in qs.items()
                             if s.get("ran", True)}}
    H["runs"] = [r for r in H["runs"] if r["day"] != day] + [run]
    H["runs"] = H["runs"][-KEEP_RUNS:]
    json.dump(H, open(P("query_history.json"), "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))

    est = sum(calls_per_run(q) for q in lines)
    md = [f"# Search report {day}",
          f"Today's run used {L.get('calls', '?')} eBay calls of 5,000 (searches {L.get('searches_done', '?')}"
          f"{', stopped at the search budget' if L.get('budget_stop') else ''}). queries.txt as it stands costs about {est:,} search calls"
          f" + up to 300 page checks.", ""]
    dead, deadwant, quiet = [], [], []
    for q in lines:
        runs = [r["q"][q] for r in H["runs"] if q in r["q"]]
        if len(runs) < MIN_RUNS:
            continue
        new = sum(r[0] for r in runs)
        if new == 0:
            (deadwant if q.startswith("+") else dead).append((sum(r[1] for r in runs), q))
            continue
        _, o = line_options(q)
        if o.get("pawn") or o.get("trusted"):
            continue
        cs = o["c"] if isinstance(o.get("c"), list) else COUNTRIES
        have = {c for r in runs for c, n in (r[2] or {}).items() if n}
        none = [c for c in cs if c not in have]
        if none and any(r[2] for r in runs):
            quiet.append((q, none, new))
    n = len(H["runs"])
    md.append(f"## Found nothing new in {n} runs — {len(dead)} searches (a normal search costs about 14 calls a day)")
    md.append("Safe to delete, or reword. Want-list lines are listed separately below and kept on purpose.")
    md += [f"- `{q}` (eBay returned {ret:,} results in total, all filtered out or already found by other searches)" for ret, q in sorted(dead)]
    md.append("")
    if deadwant:
        md.append(f"## Want-list searches with nothing in {n} runs — {len(deadwant)} (kept; rare cards)")
        md += [f"- `{q}`" for _, q in sorted(deadwant)]
        md.append("")
    if quiet:
        md.append("## Countries that never found anything for a search")
        md.append("Adding `@c=` with the countries that do find things (e.g. `@c=AU,JP`) saves 2 calls a day per country dropped.")
        saved = 0
        for q, none, new in sorted(quiet, key=lambda x: -len(x[1])):
            md.append(f"- `{q}` — nothing from {', '.join(none)} ({new} new listings from the others)")
            saved += 2 * len(none)
        md.append(f"\nDropping all of these would save about {saved:,} calls a day.")
        md.append("")
    if n < MIN_RUNS:
        md.append(f"_Only {n} run(s) recorded so far; searches are judged after {MIN_RUNS}._")
    open(P("queries_report.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print(f"query report: {len(dead)} dead searches, {len(deadwant)} quiet want-list searches, "
          f"{len(quiet)} searches with dead countries; queries.txt ~{est} calls")


if __name__ == "__main__":
    main()
