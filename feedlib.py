"""Shared helpers for the Vintage Slab Finder feed (used by ebay_search.py, enrich.py and build_feed.py).

- categories(): reads the "# ---------- Name ----------" headings in queries.txt
- QueryMatcher: tests a listing title against every search line, so a card found by one search
  is also filed under every other want-list category it fits
- card_key(): "same card" test used for price comparisons (number + set + variant + grade + language + name)
- grade_of(), land_aud(): grade label and landed A$ price for a listing
"""
import re, unicodedata

FX = {'AUD': 1, 'JPY': 0.009002, 'USD': 1.4232, 'HKD': 0.1813, 'GBP': 1.9062, 'KRW': 0.001073,
      'SGD': 1.1138, 'CAD': 1.03, 'NZD': 0.91, 'EUR': 1.65}


def norm(s):
    """Lower case, full-width -> normal, accents dropped (é -> e). Applied to titles and queries alike."""
    s = unicodedata.normalize("NFKC", s or "")
    s = "".join(c for c in unicodedata.normalize("NFD", s) if not unicodedata.combining(c))
    return s.lower().replace("’", "'")


# ---------------------------------------------------------------- categories
HEAD = re.compile(r"^#\s*-{3,}\s*(.+?)\s*-*\s*$")


def categories(path):
    """[(search line, category)] in file order. Category = the nearest "# ---------- X ----------" heading above.
    Text in brackets or after a comma is dropped from the category name ("Mew (every spelling)" -> "Mew")."""
    out, cat = [], "Other"
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            m = HEAD.match(s)
            if m:
                cat = re.sub(r"\s*\(.*?\)\s*", " ", m.group(1)).split(",")[0].strip(" -") or "Other"
            continue
        out.append((s, cat))
    return out


# ---------------------------------------------------------------- eBay-style query matching on titles
IGNORE = {"pokemon", "pokémon", "card", "cards", "tcg"}


def _tokens(q):
    """Split an eBay query into groups. Each group is (negate, [alternatives]); an alternative is a phrase."""
    q = q.lstrip("+").strip()
    toks, i = [], 0
    while i < len(q):
        c = q[i]
        if c.isspace():
            i += 1
            continue
        neg = False
        if c == "-" and i + 1 < len(q) and not q[i + 1].isspace():
            neg, i = True, i + 1
            c = q[i]
        if c == "(":
            j = q.find(")", i)
            j = len(q) if j < 0 else j
            alts = [a.strip().strip('"') for a in re.findall(r'"[^"]*"|[^,]+', q[i + 1:j])]
            toks.append((neg, [a for a in alts if a]))
            i = j + 1
        elif c == '"':
            j = q.find('"', i + 1)
            j = len(q) if j < 0 else j
            toks.append((neg, [q[i + 1:j]]))
            i = j + 1
        else:
            j = i
            while j < len(q) and not q[j].isspace():
                j += 1
            toks.append((neg, [q[i:j]]))
            i = j
    return toks


def _alt_rx(a):
    a = norm(a)
    if not a.isascii():
        return re.compile(re.escape(a))
    a = re.escape(a).replace(r"\ ", r"[\s\-]*")
    return re.compile(r"(?<![a-z0-9])" + a + r"(?![a-z0-9])")


class QueryMatcher:
    def __init__(self, lines):
        self.q = []
        for line in lines:
            groups = []
            for neg, alts in _tokens(line):
                alts = [a for a in alts if norm(a) not in IGNORE]
                if alts:
                    groups.append((neg, [_alt_rx(a) for a in alts]))
            if any(not n for n, _ in groups):
                self.q.append((line, groups))

    def matches(self, title):
        t = norm(title)
        out = []
        for line, groups in self.q:
            ok = True
            for neg, rxs in groups:
                hit = any(r.search(t) for r in rxs)
                if hit == neg:
                    ok = False
                    break
            if ok:
                out.append(line)
        return out


# ---------------------------------------------------------------- same-card key
NUM = re.compile(r"\b(\d{1,3}a?)\s*/\s*(\d{2,3})\b|\b(\d{3})\s*/\s*([A-Z]{1,4})-?P\b|\b(SWSH|SM|XY|BW|DP|HGSS|SVP?)\s?(\d{1,3})\b", re.I)
JPNUM = re.compile(r"(?:#|\bNo\.?\s?)0*(\d{1,3})\b", re.I)
GRADE = re.compile(r"\b(PSA|CGC|BGS|BECKETT|ACE|TAG|SGC)\s*(?:gem\s*(?:mint|mt)|mint|nm-?mt|pristine|black label)?\s*-?\s*(10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b", re.I)

# set names -> short code. English and Japanese set names; first match wins, so longer names come first.
SETS = [
    ("base set 2", "B2"), ("shadowless", "BS"), ("base set", "BS"), ("jungle", "JU"), ("fossil", "FO"),
    ("team rocket returns", "TRR"), ("rocket gang strikes back", "TRR"), ("team rocket", "TR"), ("rocket gang", "TR"),
    ("gym heroes", "G1"), ("leaders' stadium", "G1"), ("leaders stadium", "G1"), ("gym challenge", "G2"), ("challenge from the darkness", "G2"),
    ("neo genesis", "N1"), ("gold, silver, to a new world", "N1"), ("new world", "N1"), ("neo discovery", "N2"), ("crossing the ruins", "N2"),
    ("neo revelation", "N3"), ("awakening legends", "N3"), ("neo destiny", "N4"), ("darkness, and to light", "N4"), ("darkness and to light", "N4"),
    ("southern island", "SI"), ("legendary collection", "LC"), ("expedition", "EXP"), ("base expansion pack", "EXP"), ("aquapolis", "AQ"),
    ("skyridge", "SK"), ("mysterious mountains", "e4"), ("split earth", "e5"), ("wind from the sea", "e3"), ("town on no map", "e2"),
    ("ruby", "RS"), ("sandstorm", "SS"), ("dragon frontiers", "DF"), ("team magma vs team aqua", "MA"), ("hidden legends", "HL"),
    ("firered", "RG"), ("fire red", "RG"), ("deoxys", "DX"), ("emerald", "EM"), ("unseen forces", "UF"), ("golden sky", "UF"),
    ("delta species", "DS"), ("holon research tower", "DS"), ("legend maker", "LM"), ("holon phantom", "HP"),
    ("crystal guardians", "CG"), ("miracle crystal", "CG"), ("power keepers", "PK"), ("offense and defense", "DF"),
    ("diamond", "DP"), ("mysterious treasures", "MT"), ("secret wonders", "SW"), ("great encounters", "GE"), ("majestic dawn", "MD"),
    ("legends awakened", "LA"), ("stormfront", "SF"), ("rising rivals", "RR"), ("supreme victors", "SV"), ("arceus", "AR"),
    ("heartgold", "HS"), ("unleashed", "UL"), ("undaunted", "UD"), ("triumphant", "TM"), ("call of legends", "CL"),
    ("emerging powers", "EPO"), ("noble victories", "NVI"), ("next destinies", "NXD"), ("dark explorers", "DEX"),
    ("dragons exalted", "DRX"), ("dragon vault", "DRV"), ("boundaries crossed", "BCR"), ("plasma storm", "PLS"),
    ("plasma freeze", "PLF"), ("plasma blast", "PLB"), ("legendary treasures", "LTR"), ("black collection", "BW1"), ("white collection", "BW1"),
    ("red collection", "BW2"), ("cold flare", "BW6"), ("freeze bolt", "BW6"), ("plasma gale", "BW7"), ("spiral force", "BW8"),
    ("thunder knuckle", "BW8"), ("megalo cannon", "BW9"), ("shiny collection", "BWS"), ("ex battle boost", "EBB"),
    ("flashfire", "FLF"), ("furious fists", "FFI"), ("phantom forces", "PHF"), ("primal clash", "PRC"), ("roaring skies", "ROS"),
    ("ancient origins", "AOR"), ("breakthrough", "BKT"), ("breakpoint", "BKP"), ("generations", "GEN"), ("fates collide", "FCO"),
    ("steam siege", "STS"), ("evolutions", "EVO"), ("best of xy", "BXY"), ("rising fist", "XY3"), ("dream shine", "CP5"),
    ("20th anniversary", "CP6"), ("dpbp", "DPBP"), ("shining darkness", "DP3"), ("vs ", "VS"), ("pokemon vs", "VS"), ("web ", "WEB"),
    ("celebrations", "CEL"), ("classic collection", "CEL"), ("151", "151"),
]
VARIANTS = [
    ("1st", re.compile(r"\b1st\b|first ed|1ed\b|初版", re.I)),
    ("shadowless", re.compile(r"shadowless", re.I)),
    ("rev", re.compile(r"reverse|rev\.? ?holo|mirror|master ?ball|poke ?ball|パラレル|ミラー", re.I)),
    ("stamp", re.compile(r"stamp(ed)?\b|staff|prerelease|pre-release|league|winner", re.I)),
    ("goldstar", re.compile(r"gold ?star|☆", re.I)),
    ("nonholo", re.compile(r"non[- ]?holo", re.I)),
]
REPRINT = re.compile(r"celebrations|classic collection|25th|30th|20th anniv|anniversary|\bcp6\b|reprint|復刻", re.I)
STOP = set(("pokemon pokémon card cards tcg japanese japan english psa cgc bgs gem mint mt nm near holo rare promo the 1st edition ed graded jp jpn "
            "of in game base set jungle fossil team rocket gym heroes challenge neo genesis discovery revelation destiny expedition aquapolis skyridge "
            "legendary collection unlimited shadowless generations radiant evolutions celebrations dragon frontiers crystal guardians holon "
            "phantoms delta species pocket monsters wotc vintage black star secret full art reverse first ultra super from with and for new "
            "lp mp hp near-mint condition grade free post shipping ship rank stamp stamped mirror sealed lot vs").split())
JPRX = re.compile(r"japan|\bjpn?\b|日本|ポケモン", re.I)
LANGS = [("XX", re.compile(r"\b(spanish|espa[nñ]ol|german|deutsch|french|fran[cç]ais|\bvf\b|italian|italiano|portuguese|portugu[eê]s|brazilian|dutch|polish|thai|indonesian)\b", re.I)),
         ("KR", re.compile(r"korean|\bkor\b|한국|포켓몬", re.I)), ("CN", re.compile(r"chinese|\bs-chinese|\bt-chinese|中文|简体|繁體", re.I)),
         ("JP", JPRX)]


def lang_of(title):
    for code, rx in LANGS:
        if rx.search(title):
            return code
    return "EN"


def grade_of(title):
    """'PSA 10', 'CGC 9.5', 'Raw' (NM raw), or '' (raw, condition unknown/played)."""
    g = GRADE.search(title)
    if g:
        co = g.group(1).upper().replace("BECKETT", "BGS")
        return f"{co} {g.group(2)}"
    if re.search(r"\b(graded|slab)\b", title, re.I):
        return "graded?"
    if re.search(r"near mint|\bnm\b|\bmint\b|美品", title, re.I):
        return "Raw"
    return ""


def set_of(title):
    t = norm(title) + " "
    for name, code in SETS:
        if name in t:
            return code
    return ""


def card_number(title):
    m = NUM.search(title)
    if m:
        if m.group(1):
            return f"{m.group(1).lstrip('0').upper() or '0'}/{m.group(2).lstrip('0')}"
        if m.group(3):
            return f"{int(m.group(3))}/{m.group(4).upper()}-P"
        return f"{m.group(5).upper()}{int(m.group(6))}"
    if JPRX.search(title):
        m = JPNUM.search(title)
        if m:
            return f"No.{int(m.group(1))}"
    return ""


def name_word(title):
    words = [w.lower() for w in re.findall(r"[^\W\d_][^\W\d_'.]+", title) if w.lower() not in STOP and len(w) > 2]
    return words[0] if words else ""


def card_key(title):
    """Same card = same number + same set + same variant + same grade + same language + first name word.
    None when there's no card number (too vague to compare)."""
    n = card_number(title)
    if not n:
        return None
    var = "+".join(v for v, rx in VARIANTS if rx.search(title))
    if REPRINT.search(title):
        var = (var + "+reprint").strip("+")
    g = grade_of(title) or "raw?"
    return "|".join((n, set_of(title), var, g, lang_of(title), name_word(title)))


def land_aud(it):
    """Price + postage in A$ (shops without a postage figure count as 0)."""
    try:
        p = float(it.get("price") or 0) * FX.get(it.get("currency") or "AUD", 0)
        post = float(it.get("post") or 0) * FX.get(it.get("postCurrency") or it.get("currency") or "AUD", 0)
    except (TypeError, ValueError):
        return 0.0
    return round(p + post, 2)
