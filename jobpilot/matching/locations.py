"""Location normalisation: Indian city aliases, country and work-mode detection."""

from __future__ import annotations

import re
from functools import cache

# canonical → aliases (lowercase)
INDIAN_CITIES: dict[str, list[str]] = {
    "Bengaluru": ["bengaluru", "bangalore", "blr", "bengaluru urban"],
    "Hyderabad": ["hyderabad", "secunderabad", "hyd", "hitec city", "hitech city", "gachibowli"],
    "Mumbai": ["mumbai", "bombay", "navi mumbai", "thane", "powai"],
    "Pune": ["pune", "pimpri", "hinjewadi"],
    "Chennai": ["chennai", "madras"],
    "Delhi NCR": [
        "delhi",
        "new delhi",
        "ncr",
        "delhi ncr",
        "gurugram",
        "gurgaon",
        "noida",
        "greater noida",
        "faridabad",
        "ghaziabad",
    ],
    "Kolkata": ["kolkata", "calcutta"],
    "Ahmedabad": ["ahmedabad", "gandhinagar"],
    "Kochi": ["kochi", "cochin", "ernakulam"],
    "Thiruvananthapuram": ["thiruvananthapuram", "trivandrum", "technopark"],
    "Jaipur": ["jaipur"],
    "Chandigarh": ["chandigarh", "mohali", "panchkula"],
    "Indore": ["indore"],
    "Coimbatore": ["coimbatore"],
    "Visakhapatnam": ["visakhapatnam", "vizag"],
    "Vijayawada": ["vijayawada"],
    "Bhubaneswar": ["bhubaneswar"],
    "Nagpur": ["nagpur"],
    "Lucknow": ["lucknow"],
    "Mysuru": ["mysuru", "mysore"],
    "Mangaluru": ["mangaluru", "mangalore"],
    "Warangal": ["warangal"],
}

COUNTRY_ALIASES: dict[str, list[str]] = {
    "India": ["india", "ind", "in"],
    "United States": ["united states", "usa", "us", "u.s.", "america"],
    "United Kingdom": ["united kingdom", "uk", "england", "london"],
    "Canada": ["canada"],
    "Germany": ["germany", "berlin", "munich"],
    "Singapore": ["singapore"],
    "United Arab Emirates": ["uae", "dubai", "abu dhabi", "united arab emirates"],
    "Australia": ["australia", "sydney", "melbourne"],
    "Netherlands": ["netherlands", "amsterdam"],
    "Ireland": ["ireland", "dublin"],
    "France": ["france", "paris"],
}

_WORLDWIDE = re.compile(r"\b(worldwide|anywhere|global(ly)?|international|any location)\b", re.I)
_REMOTE = re.compile(r"\b(remote|work from home|wfh|anywhere)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\b(on-?site|in-?office|office based|work from office|wfo)\b", re.I)


@cache
def _city_patterns() -> list[tuple[re.Pattern[str], str]]:
    pats = []
    for name, aliases in INDIAN_CITIES.items():
        for alias in aliases:
            pats.append((re.compile(rf"\b{re.escape(alias)}\b", re.I), name))
    pats.sort(key=lambda p: -len(p[0].pattern))
    return pats


def find_city(text: str | None) -> str | None:
    """Canonical Indian city mentioned in text (first match), else None."""
    if not text:
        return None
    best: tuple[int, str] | None = None
    for pattern, name in _city_patterns():
        m = pattern.search(text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), name)
    return best[1] if best else None


def normalize_city(text: str) -> str:
    """Canonical city for a user-typed location, or the trimmed text title-cased."""
    return find_city(text) or text.strip().title()


def find_country(text: str | None) -> str | None:
    if not text:
        return None
    if find_city(text):
        return "India"
    lowered = text.lower()
    for country, aliases in COUNTRY_ALIASES.items():
        for alias in aliases:
            if len(alias) <= 3:
                if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered) and (
                    alias != "in" or "india" in lowered
                ):
                    return country
            elif alias in lowered:
                return country
    return None


def is_worldwide(text: str | None) -> bool:
    return bool(text and _WORLDWIDE.search(text))


def detect_work_mode(*texts: str | None) -> str:
    """remote | hybrid | onsite | unknown, from location/title/description snippets."""
    joined = " ".join(t for t in texts if t)
    if _HYBRID.search(joined):
        return "hybrid"
    if _REMOTE.search(joined):
        return "remote"
    if _ONSITE.search(joined):
        return "onsite"
    return "unknown"


def remote_allows_country(region_text: str | None, home_country: str) -> bool:
    """For remote jobs: does the stated region ("USA only", "Worldwide", "APAC") include home?"""
    if not region_text or is_worldwide(region_text):
        return True
    lowered = region_text.lower()
    if home_country.lower() in lowered:
        return True
    if home_country == "India" and re.search(r"\b(apac|asia|south asia|emea and apac)\b", lowered):
        return True
    return find_country(region_text) in (None, home_country) and not re.search(
        r"\b(only|must be (based|located) in|residents? of)\b", lowered
    )
