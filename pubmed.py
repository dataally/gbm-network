"""Minimal NCBI E-utilities client for PubMed."""
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from functools import lru_cache

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
API_KEY = os.getenv("NCBI_API_KEY") or None
EMAIL = os.getenv("NCBI_EMAIL") or None
TOOL = "pubmed-search-app"
PAGE_SIZE = 10
CPY_POOL = 50  # citations/yr sort re-ranks this many top-relevance hits
REL_TERMS = ('(interact*[tiab] OR "co-mutation*"[tiab] OR "co-mutated"[tiab] OR '
             '"co-occurr*"[tiab] OR "co-express*"[tiab] OR "synthetic lethal*"[tiab] OR '
             'cooperat*[tiab] OR binds[tiab] OR binding[tiab] OR "physically associat*"[tiab])')
PROXIMITY = 10  # words

_MIN_INTERVAL = 0.11 if API_KEY else 0.34  # 10 req/s with key, 3 without
_lock = threading.Lock()
_last = 0.0

class PubMedError(Exception):
    pass


def _get(endpoint, params):
    global _last
    params = {**params, "tool": TOOL}
    if EMAIL:
        params["email"] = EMAIL
    if API_KEY:
        params["api_key"] = API_KEY
    with _lock:
        wait = _MIN_INTERVAL - (time.time() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.time()
    try:
        r = requests.get(f"{BASE}/{endpoint}", params=params, timeout=20)
    except requests.RequestException as e:
        raise PubMedError(f"Could not reach NCBI: {e}") from e
    if r.status_code == 429:
        raise PubMedError("NCBI rate limit hit; try again in a moment.")
    if not r.ok:
        raise PubMedError(f"NCBI returned HTTP {r.status_code}.")
    return r


def clean_gene(g):
    g = (g or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,20}", g):
        raise PubMedError(f"'{g}' doesn't look like a gene symbol.")
    return g


def pair_queries(g1, g2):
    g1, g2 = clean_gene(g1), clean_gene(g2)
    near = f'"{g1} {g2}"[tiab:~{PROXIMITY}]'
    return {"mention": f'"{g1}"[tiab] AND "{g2}"[tiab]', "near": near,
            "strict": f"{near} AND {REL_TERMS}"}


def build_query(q="", disease="", year_from="", year_to="", gene1="", gene2="", mode="any"):
    """mode: any = each given gene appears in title/abstract; near = both genes within
    PROXIMITY words; strict = near AND a relationship term."""
    if mode in ("near", "strict"):
        if not (gene1.strip() and gene2.strip()):
            raise PubMedError(f"'{mode}' mode needs two genes.")
        parts = [pair_queries(gene1, gene2)[mode]]
    else:
        parts = [f'"{clean_gene(g)}"[tiab]' for g in (gene1, gene2) if g.strip()]
    if q.strip():
        parts.append(f"({q.strip()})")
    if disease.strip():
        d = disease.strip().replace('"', "")
        parts.append(f'("{d}"[MeSH Terms] OR "{d}"[Title/Abstract])')
    if year_from or year_to:
        parts.append(f"({year_from or '1000'}:{year_to or '3000'}[dp])")
    return " AND ".join(parts)


def _esearch(term, retmax, retstart=0, sort="relevance"):
    data = _get("esearch.fcgi", {
        "db": "pubmed", "term": term, "retmode": "json", "retmax": retmax,
        "retstart": retstart, "sort": "pub_date" if sort == "date" else "relevance",
    }).json().get("esearchresult", {})
    if "ERROR" in data:
        raise PubMedError(f"NCBI rejected the query: {data['ERROR']}")
    return int(data.get("count", 0)), data.get("idlist", [])


def search(q="", disease="", year_from="", year_to="", sort="relevance", page=1,
           page_size=PAGE_SIZE, gene1="", gene2="", mode="any"):
    kw = dict(q=q, disease=disease, year_from=year_from, year_to=year_to,
              gene1=gene1, gene2=gene2)
    term = build_query(mode=mode, **kw)
    if not term:
        raise PubMedError("Give at least one gene, a disease or search terms.")
    page = max(1, page)
    out = {"query": term, "mode": mode, "sort": sort, "page": page,
           "pubmed_url": "https://pubmed.ncbi.nlm.nih.gov/?term=" + requests.utils.quote(term)}
    if sort == "citations_per_year":
        total, ids = _esearch(term, CPY_POOL)
        papers = add_citations(fetch(tuple(ids))) if ids else []
        papers.sort(key=lambda r: -(r["citations_per_year"]
                                    if r["citations_per_year"] is not None else -1))
        start = (page - 1) * page_size
        out.update(total=total, pages=max(1, -(-len(papers) // page_size)),
                   results=papers[start:start + page_size], ranked_pool=len(papers))
    else:
        total, ids = _esearch(term, page_size, (page - 1) * page_size, sort)
        out.update(total=total, pages=max(1, -(-total // page_size)),
                   results=add_citations(fetch(tuple(ids))) if ids else [])
    if mode == "strict":  # looser counts for context, with the same extra filters
        out["counts"] = {
            "strict": total,
            "near": _esearch(build_query(mode="near", **kw), 0)[0],
            "mention": _esearch(build_query(mode="any", **kw), 0)[0],
            "proximity_words": PROXIMITY,
        }
    return out


ICITE_URL = "https://icite.od.nih.gov/api/pubs"


@lru_cache(maxsize=256)
def _icite_cached(pmids):  # raises on failure, so failures are never cached
    r = requests.get(ICITE_URL, params={"pmids": ",".join(pmids), "format": "json"},
                     timeout=20)
    r.raise_for_status()
    return {str(d["pmid"]): d for d in r.json().get("data", [])}


def _icite(pmids):
    try:
        return _icite_cached(pmids)
    except (requests.RequestException, ValueError, KeyError):
        return None  # citations shown as n/a


def add_citations(records):
    """Merge NIH iCite citation data into records. Never raises."""
    info = _icite(tuple(r["pmid"] for r in records)) or {}
    for r in records:
        d = info.get(r["pmid"])
        cpy = d.get("citations_per_year") if d else None
        r["citations"] = d.get("citation_count") if d else None
        r["citations_per_year"] = round(cpy, 1) if cpy is not None else None
        r["rcr"] = d.get("relative_citation_ratio") if d else None
    return records


@lru_cache(maxsize=256)
def _fetch_xml(pmids):
    return _get("efetch.fcgi", {"db": "pubmed", "id": ",".join(pmids),
                                "retmode": "xml"}).text


def fetch(pmids):
    return parse_articles(_fetch_xml(tuple(pmids)))


def _text(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_articles(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for art in root.findall(".//PubmedArticle"):
        pmid = _text(art.find(".//MedlineCitation/PMID"))
        authors = []
        for a in art.findall(".//AuthorList/Author"):
            last, init = _text(a.find("LastName")), _text(a.find("Initials"))
            name = f"{last} {init}".strip() or _text(a.find("CollectiveName"))
            if name:
                authors.append(name)
        abstract_parts = []
        for ab in art.findall(".//Abstract/AbstractText"):
            label = ab.get("Label")
            txt = _text(ab)
            abstract_parts.append(f"{label}: {txt}" if label else txt)
        year = (_text(art.find(".//JournalIssue/PubDate/Year"))
                or _text(art.find(".//JournalIssue/PubDate/MedlineDate"))[:4])
        doi = ""
        for aid in art.findall(".//ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = _text(aid)
        out.append({
            "pmid": pmid,
            "title": _text(art.find(".//ArticleTitle")),
            "authors": authors,
            "journal": _text(art.find(".//Journal/Title")),
            "year": year,
            "abstract": "\n\n".join(abstract_parts),
            "doi": doi,
            "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "doi_url": f"https://doi.org/{doi}" if doi else "",
        })
    return out
