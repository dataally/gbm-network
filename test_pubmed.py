from types import SimpleNamespace as NS

import pytest
import requests as rq

import app as app_module
import chat
import pubmed
import string_db

SAMPLE = """<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>123</PMID>
<Article><Journal><Title>J Test</Title><JournalIssue><PubDate><Year>2024</Year></PubDate></JournalIssue></Journal>
<ArticleTitle>A <i>great</i> study</ArticleTitle>
<Abstract><AbstractText Label="BACKGROUND">Bg.</AbstractText><AbstractText Label="RESULTS">Res.</AbstractText></Abstract>
<AuthorList><Author><LastName>Smith</LastName><Initials>J</Initials></Author></AuthorList></Article></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">10.1/x</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>"""


def paper(pmid, cpy):
    return {"pmid": pmid, "title": f"T{pmid}", "authors": ["A B"], "journal": "J", "year": "2024",
            "abstract": "abs " + pmid, "citations": 10, "citations_per_year": cpy,
            "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/", "doi": "", "doi_url": ""}


# ---- PubMed client ----
def test_parse():
    r = pubmed.parse_articles(SAMPLE)[0]
    assert r["pmid"] == "123" and r["title"] == "A great study"
    assert r["authors"] == ["Smith J"] and r["year"] == "2024"
    assert r["abstract"] == "BACKGROUND: Bg.\n\nRESULTS: Res."
    assert r["doi_url"] == "https://doi.org/10.1/x"


def test_build_query_modes():
    q = pubmed.build_query("immunotherapy", "Melanoma", "2020", "2024", "KRAS", "TP53")
    assert '"KRAS"[tiab] AND "TP53"[tiab]' in q and '"Melanoma"[MeSH Terms]' in q
    assert "(immunotherapy)" in q and "2020:2024[dp]" in q
    strict = pubmed.build_query(gene1="KRAS", gene2="TP53", mode="strict")
    assert strict.startswith('"KRAS TP53"[tiab:~10] AND (interact*')
    assert pubmed.build_query(gene1="KRAS", gene2="TP53", mode="near") == '"KRAS TP53"[tiab:~10]'
    with pytest.raises(pubmed.PubMedError):
        pubmed.build_query(gene1="KRAS", mode="strict")
    with pytest.raises(pubmed.PubMedError):
        pubmed.build_query(gene1='KRAS" OR "x')


def test_add_citations(monkeypatch):
    monkeypatch.setattr(pubmed, "_icite", lambda pmids: {
        "1": {"citation_count": 50, "citations_per_year": 12.345, "relative_citation_ratio": 2.0}})
    recs = pubmed.add_citations([{"pmid": "1"}, {"pmid": "2"}])
    assert recs[0]["citations"] == 50 and recs[0]["citations_per_year"] == 12.3
    assert recs[1]["citations"] is None and recs[1]["citations_per_year"] is None


def test_add_citations_icite_down(monkeypatch):
    monkeypatch.setattr(pubmed, "_icite", lambda pmids: None)
    assert pubmed.add_citations([{"pmid": "1"}])[0]["citations_per_year"] is None


def test_search_citations_sort_and_strict_counts(monkeypatch):
    papers = [paper("1", 5.0), paper("2", 50.0), paper("3", None), paper("4", 20.0)]
    monkeypatch.setattr(pubmed, "_esearch", lambda term, retmax, retstart=0, sort="relevance": (
        (123, [p["pmid"] for p in papers]) if retmax else (999, [])))
    monkeypatch.setattr(pubmed, "fetch", lambda ids: [dict(p) for p in papers])
    monkeypatch.setattr(pubmed, "add_citations", lambda recs: recs)
    r = pubmed.search(gene1="KRAS", gene2="TP53", mode="strict", sort="citations_per_year", page_size=3)
    assert [p["pmid"] for p in r["results"]] == ["2", "4", "1"]  # None sorts last, page_size 3
    assert r["pages"] == 2 and r["ranked_pool"] == 4 and r["total"] == 123
    assert r["counts"] == {"strict": 123, "near": 999, "mention": 999, "proximity_words": 10}
    page2 = pubmed.search(gene1="KRAS", gene2="TP53", sort="citations_per_year", page=2, page_size=3)
    assert [p["pmid"] for p in page2["results"]] == ["3"]


# ---- STRING client ----
def test_string_found_no_record_unknown(monkeypatch):
    string_db._pair_cached.cache_clear()
    edge = {"preferredName_A": "KRAS", "preferredName_B": "TP53", "score": 0.954, "escore": 0.119,
            "dscore": 0, "tscore": 0.95, "ascore": 0, "nscore": 0, "fscore": 0, "pscore": 0}
    monkeypatch.setattr(string_db, "_get", lambda ep, **kw: [edge] if ep == "network" else [{"x": 1}])
    r = string_db.pair("KRAS", "TP53")
    assert r["status"] == "found" and r["confidence"] == "highest" and r["channels"]["text mining"] == 0.95
    monkeypatch.setattr(string_db, "_get", lambda ep, **kw: [] if ep == "network" else [{"x": 1}])
    assert string_db.pair("AAA", "BBB")["status"] == "no_record"
    monkeypatch.setattr(string_db, "_get", lambda ep, **kw: [])
    r = string_db.pair("NOPE1", "NOPE2")
    assert r["status"] == "gene_not_recognized" and r["genes"] == ["NOPE1", "NOPE2"]


def test_string_unavailable(monkeypatch):
    string_db._pair_cached.cache_clear()

    def boom(*a, **k):
        raise rq.ConnectionError("down")
    monkeypatch.setattr(string_db, "_get", boom)
    assert string_db.pair("KRAS", "TP53") == {"status": "unavailable"}


# ---- chat: argument sanitizing ----
def test_sanitize_pubmed_args():
    kw = chat.sanitize_pubmed_args({"query": " immuno ", "gene1": "KRAS", "year_from": "2020",
                                    "sort": "bogus", "mode": "weird", "page": "3", "evil": "x"})
    assert kw == {"q": "immuno", "gene1": "KRAS", "year_from": "2020", "sort": "relevance",
                  "mode": "any", "page": 3}
    assert chat.sanitize_pubmed_args({"page": -5})["page"] == 1
    assert chat.sanitize_pubmed_args({"page": "abc"})["page"] == 1
    with pytest.raises(pubmed.PubMedError):
        chat.sanitize_pubmed_args({"year_from": "20"})
    with pytest.raises(pubmed.PubMedError):
        chat.sanitize_pubmed_args({"gene1": 'bad gene"'})


def test_run_tools_errors():
    payload, block, is_err = chat.run_pubmed({"gene1": "bad gene!"})
    assert is_err and block is None and "gene symbol" in payload["error"]
    payload, block, is_err = chat.run_string({"gene1": "KRAS"})
    assert is_err and block is None


def test_build_context():
    ctx = chat.build_context([paper("1", 2.5)], {"gene1": "KRAS"})
    assert "[1] T1" in ctx and "abs 1" in ctx and '"gene1": "KRAS"' in ctx
    assert "(none)" in chat.build_context([], None)


def test_missing_key_and_messages(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(chat.ChatError, match="ANTHROPIC_API_KEY"):
        chat.answer([{"role": "user", "content": "hi"}], [])
    with pytest.raises(chat.ChatError):
        chat.clean_messages([{"role": "assistant", "content": "hi"}])


# ---- chat: tool loop ----
def fake_client(responses, seen):
    it = iter(responses)

    def create(**kw):
        seen.append(kw["messages"])
        return next(it)
    return NS(beta=NS(messages=NS(create=create)))


def test_answer_calls_both_tools_in_one_round(monkeypatch):
    monkeypatch.setattr(pubmed, "fetch", lambda p: [])
    res = {"query": "q", "mode": "any", "sort": "relevance", "total": 1, "page": 1, "pages": 1,
           "results": [paper("1", 3.0)], "pubmed_url": "u"}
    monkeypatch.setattr(pubmed, "search", lambda **kw: res)
    monkeypatch.setattr(string_db, "pair", lambda a, b: {"status": "no_record", "url": "u"})
    first = NS(stop_reason="tool_use", content=[
        NS(type="tool_use", id="t1", name="pubmed_search", input={"gene1": "KRAS", "gene2": "TP53"}),
        NS(type="tool_use", id="t2", name="string_pair", input={"gene1": "KRAS", "gene2": "TP53"})])
    second = NS(stop_reason="end_turn", content=[NS(type="text", text="Summary [1].")])
    seen = []
    out = chat.answer([{"role": "user", "content": "papers and STRING?"}], [], None,
                      client=fake_client([first, second], seen))
    assert out["reply"] == "Summary [1]."
    assert [b["type"] for b in out["blocks"]] == ["string", "papers"]  # STRING card first
    assert out["last_search"]["gene1"] == "KRAS" and out["last_search"]["page"] == 1
    last = seen[1][-1]
    assert last["role"] == "user" and [c["tool_use_id"] for c in last["content"]] == ["t1", "t2"]


def test_answer_no_tool_and_tool_error(monkeypatch):
    seen = []
    plain = NS(stop_reason="end_turn", content=[NS(type="text", text="From abstracts [1].")])
    out = chat.answer([{"role": "user", "content": "summarize"}], [], None, client=fake_client([plain], seen))
    assert out == {"reply": "From abstracts [1].", "blocks": [], "last_search": None}

    bad = NS(stop_reason="tool_use", content=[
        NS(type="tool_use", id="t1", name="pubmed_search", input={"year_from": "x"})])
    fin = NS(stop_reason="end_turn", content=[NS(type="text", text="Bad year.")])
    seen = []
    out = chat.answer([{"role": "user", "content": "find"}], [], None, client=fake_client([bad, fin], seen))
    item = seen[1][-1]["content"][0]
    assert item["is_error"] is True and out["blocks"] == [] and out["reply"] == "Bad year."


# ---- Flask app ----
def test_routes():
    c = app_module.app.test_client()
    assert c.get("/").status_code == 200
    assert c.get("/api/search").status_code == 404 and c.get("/api/diseases").status_code == 404
    html = c.get("/").get_data(as_text=True)
    assert 'id="chatinput"' in html and 'id="gene1"' not in html and 'id="disease"' not in html


def test_export_csv(monkeypatch):
    seen = {}

    def fake_search(**kw):
        seen.update(kw)
        return {"results": [paper("1", 2.0)]}
    monkeypatch.setattr(pubmed, "search", fake_search)
    r = app_module.app.test_client().get("/api/export?gene1=KRAS&gene2=TP53&sort=date&q=immuno")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    lines = r.get_data(as_text=True).splitlines()
    assert lines[0].startswith("PMID,Title") and lines[1].startswith("1,T1")
    assert seen["gene1"] == "KRAS" and seen["q"] == "immuno" and seen["page_size"] == 200
    assert app_module.app.test_client().get("/api/export?year_from=20").status_code == 400
