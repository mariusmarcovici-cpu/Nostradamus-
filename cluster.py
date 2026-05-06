"""Cluster classification heuristics. Label-only in v1, does not gate entries.

v0.2.0 update (May 6, 2026):
  - Added SPORTS_REGEX to catch sports markets that lack proper tags.
    Polymarket's tagging on esports / J-League / Liga MX / Bolivian football /
    smaller-league markets is unreliable, so they were misclassified as
    H_OTHER. Now we detect by question-text patterns:
      * Per-game match questions: "Game N: ..."
      * Esports terms: pentakill, roshan, baron nashor, barracks, etc.
      * Matchup format: "X vs. Y" (very strong sports signal)
      * Draw markets: "end in a draw"
      * Tournament/league win patterns
"""

import re

CLUSTERS = {
    "A_SPORTS": "Sports",
    "B_MENTION": "Mention markets",
    "C_WEATHER": "Weather/Climate",
    "D_BOXOFFICE": "Box office",
    "E_GEOPOLITICS": "Geopolitics/War",
    "F_POLITICS": "Politics-other",
    "G_TECH_CORP": "Tech/Corporate",
    "H_OTHER": "Other",
}

SPORTS_TAGS = {"NBA", "NHL", "MLB", "NFL", "Soccer", "Football", "Tennis",
               "Golf", "MMA", "UFC", "NASCAR", "Cricket", "WNBA", "Sports",
               "Esports", "Esport", "E-sports",
               "League of Legends", "LoL", "Dota 2", "DOTA2", "Dota",
               "Counter-Strike", "CS:GO", "CS2", "Valorant", "Overwatch",
               "Champions League", "EPL", "La Liga", "Serie A", "Bundesliga",
               "J-League", "MLS", "Liga MX", "Brasileirão", "Bolivia",
               "Premier League", "Ligue 1", "Eredivisie",
               "Boxing", "Cycling", "F1", "Formula 1", "Rugby", "AFL",
               "Hockey", "Baseball", "Basketball"}

WEATHER_TAGS = {"Weather", "Climate"}

GEO_TAGS = {"Geopolitics", "War", "Israel", "Ukraine", "Iran", "Russia",
            "Middle East", "Hamas", "China-Taiwan", "North Korea"}

POLITICS_TAGS = {"Politics", "Elections", "Trump", "Court", "US Politics",
                 "Election", "Congress", "SCOTUS"}

TECH_CORP_TAGS = {"Tech", "Companies", "Earnings", "IPO", "Business",
                  "Technology", "AI", "Stocks"}

# v0.2.0: question-text fallback for sports markets that lack proper tags.
# Each pattern below is a high-confidence sports signal.
SPORTS_REGEX = re.compile(
    # Per-game esports questions: "Game 1:", "Game 2:", etc.
    r"^Game\s+\d+\s*:|"
    # Esports-specific terminology
    r"\b(pentakill|penta\s+kill|quadra\s+kill|ultra\s+kill|"
    r"baron\s+nashor|roshan|barracks|first\s+blood|first\s+tower|"
    r"first\s+dragon|first\s+baron|rampage|aegis|courier|creep\s+score)\b|"
    # Matchup format: "X vs. Y" or "X vs Y" (very strong sports signal)
    r"\bvs\.?\s+[A-Z]|"
    # Draw markets — only sports have these
    r"\bend\s+in\s+a\s+draw\b|"
    # Tournament/league win patterns
    r"\bwin\s+(the\s+)?(LRS|LEC|LCS|LCK|LPL|LCK|MSI|Worlds|TI|"
    r"International|Championship|Cup|League|Split|Season|Playoffs|"
    r"Finals|Series|Title|Tournament|Open|Masters|Grand Slam)\b|"
    # Per-match outcome markets
    r"\b(scored?|goals?|assists?|wickets?|innings?|sets?|aces?|"
    r"red\s+card|yellow\s+card|penalty|own\s+goal|hat-?trick|clean\s+sheet)\b|"
    # Boxing / MMA
    r"\b(KO|TKO|knockout|submission|decision|round\s+\d+)\b|"
    # F1 / NASCAR / racing
    r"\b(pole\s+position|fastest\s+lap|podium|grand\s+prix)\b",
    re.IGNORECASE,
)

MENTION_REGEX = re.compile(
    r"^(what will|will .{1,40}\s(say|tweet|post)|how many (tweets|posts))",
    re.IGNORECASE,
)
WEATHER_REGEX = re.compile(
    r"\b(temperature|high|low|hottest|coldest|rain|snow|hurricane|tornado)\b.*"
    r"\b(by|on|in|reach|exceed)\b",
    re.IGNORECASE,
)
BOXOFFICE_REGEX = re.compile(
    r"\b(opening weekend|box office|debut|gross|3-day|3 day)\b",
    re.IGNORECASE,
)
GEO_REGEX = re.compile(
    r"\b(ceasefire|peace deal|treaty|disarm|sanctions|invasion|airstrike|"
    r"hostages|truce|withdraw)\b",
    re.IGNORECASE,
)


def _tag_set(tags) -> set:
    if not tags:
        return set()
    return set(t.get("label", t) if isinstance(t, dict) else str(t) for t in tags)


def classify(question: str, tags=None, category: str = "") -> str:
    """Return cluster code. Order matters: most specific first."""
    q = question or ""
    tag_set = _tag_set(tags)
    cat = (category or "").lower()

    # A — Sports (tag-driven OR question-pattern fallback for esports / obscure leagues)
    if tag_set & SPORTS_TAGS or "sports" in cat or "esports" in cat:
        return "A_SPORTS"
    if SPORTS_REGEX.search(q):
        return "A_SPORTS"

    # B — Mention markets (regex on question stem)
    if MENTION_REGEX.search(q):
        return "B_MENTION"

    # C — Weather
    if tag_set & WEATHER_TAGS or WEATHER_REGEX.search(q):
        return "C_WEATHER"

    # D — Box office
    if BOXOFFICE_REGEX.search(q):
        return "D_BOXOFFICE"

    # E — Geopolitics / war
    if tag_set & GEO_TAGS or GEO_REGEX.search(q):
        return "E_GEOPOLITICS"

    # F — Politics-other (catches Trump weekly action markets, courts)
    if tag_set & POLITICS_TAGS:
        return "F_POLITICS"

    # G — Tech / corporate
    if tag_set & TECH_CORP_TAGS:
        return "G_TECH_CORP"

    return "H_OTHER"
