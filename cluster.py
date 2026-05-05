"""Cluster classification heuristics. Label-only in v1, does not gate entries."""

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
               "Champions League", "EPL", "La Liga", "Serie A", "Bundesliga"}

WEATHER_TAGS = {"Weather", "Climate"}

GEO_TAGS = {"Geopolitics", "War", "Israel", "Ukraine", "Iran", "Russia",
            "Middle East", "Hamas", "China-Taiwan", "North Korea"}

POLITICS_TAGS = {"Politics", "Elections", "Trump", "Court", "US Politics",
                 "Election", "Congress", "SCOTUS"}

TECH_CORP_TAGS = {"Tech", "Companies", "Earnings", "IPO", "Business",
                  "Technology", "AI", "Stocks"}

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

    # A — Sports (tag-driven, high confidence)
    if tag_set & SPORTS_TAGS or "sports" in cat:
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
