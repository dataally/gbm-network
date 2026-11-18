"""Tiny client for the STRING protein-association database (human only)."""
from functools import lru_cache
from urllib.parse import quote

import requests

BASE = "https://string-db.org/api/json"
SPECIES = 9606  # Homo sapiens
CALLER = "pubmed-search-app"
CHANNELS = {"escore": "experiments", "dscore": "databases", "tscore": "text mining",
            "ascore": "co-expression", "nscore": "neighborhood",
            "fscore": "gene fusion", "pscore": "co-occurrence"}


def confidence(score):
    if score >= 0.9:
        return "highest"
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    return "low"


def _get(endpoint, **params):
    r = requests.get(f"{BASE}/{endpoint}",
                     params={**params, "species": SPECIES, "caller_identity": CALLER},
                     timeout=15)
    r.raise_for_status()
    return r.json()


def link(g1, g2):
    return f"https://string-db.org/cgi/network?identifiers={quote(g1)}%0d{quote(g2)}&species={SPECIES}"


@lru_cache(maxsize=256)
def _pair_cached(g1, g2):  # raises on failure, so failures are never cached
    edges = _get("network", identifiers=f"{g1}\r{g2}", required_score=0)
    for e in edges:
        names = {e.get("preferredName_A", "").upper(), e.get("preferredName_B", "").upper()}
        if names == {g1.upper(), g2.upper()}:
            score = float(e["score"])
            return {
                "status": "found",
                "score": score,
                "confidence": confidence(score),
                "channels": {name: float(e.get(key, 0)) for key, name in CHANNELS.items()},
                "url": link(g1, g2),
            }
    unknown = [g for g in (g1, g2) if not _get("get_string_ids", identifiers=g, limit=1)]
    if unknown:
        return {"status": "gene_not_recognized", "genes": unknown}
    return {"status": "no_record", "url": link(g1, g2)}


def pair(g1, g2):
    """Never raises. status: found | no_record | gene_not_recognized | unavailable."""
    try:
        return _pair_cached(g1, g2)
    except (requests.RequestException, ValueError, KeyError):
        return {"status": "unavailable"}
