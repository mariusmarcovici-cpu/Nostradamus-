"""Blacklist patterns for filtering out crypto and flagging UMA-dispute markets."""

import re

# Slug substring blacklist (case-insensitive)
CRYPTO_SLUG_TERMS = [
    "btc", "bitcoin", "eth-", "ethereum", "solana", "sol-",
    "crypto", "coinbase", "microstrategy", "mstr", "pump",
    "ada-", "xrp", "doge", "memecoin", "altcoin", "defi",
    "stablecoin", "usdc", "usdt", "binance", "kraken",
    "fartcoin", "wif", "bonk", "pepe", "shib",
]

# Tag blacklist
CRYPTO_TAGS = {
    "Crypto", "Bitcoin", "Ethereum", "Solana", "Memecoins",
    "DeFi", "Cryptocurrency", "Stablecoins",
}

# Regex on question text — word-boundary match for known crypto tickers
CRYPTO_QUESTION_REGEX = re.compile(
    r"\b(btc|eth|sol|xrp|doge|ada|bitcoin|ethereum|solana|crypto|"
    r"microstrategy|coinbase|memecoin|stablecoin)\b",
    re.IGNORECASE,
)

# UMA dispute keyword markers (FLAG ONLY — does not block in dry mode)
UMA_DISPUTE_KEYWORDS = [
    "credible reporting",
    "consensus of credible",
    "credible sources",
    "moderator discretion",
    "resolver discretion",
    "subjective",
    "ambiguity will be resolved",
]


def is_crypto(slug: str, tags: list, question: str) -> bool:
    """Triple-filter crypto detection: slug, tags, question regex."""
    slug_lower = (slug or "").lower()
    for term in CRYPTO_SLUG_TERMS:
        if term in slug_lower:
            return True
    if tags:
        tag_set = set(t.get("label", t) if isinstance(t, dict) else t for t in tags)
        if tag_set & CRYPTO_TAGS:
            return True
    if question and CRYPTO_QUESTION_REGEX.search(question):
        return True
    return False


def has_uma_dispute_marker(resolution_source: str, description: str = "") -> bool:
    """Check if resolutionSource or description contains UMA-risk language."""
    text = " ".join(filter(None, [resolution_source, description])).lower()
    return any(kw in text for kw in UMA_DISPUTE_KEYWORDS)
