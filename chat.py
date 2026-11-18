"""Chat-only research assistant with two tools the model chooses between:
  - pubmed_search: literature search (NCBI E-utilities + NIH iCite citations)
  - string_pair:   does the STRING database have a record for a gene pair?
All facts come from tool results; the model only decides what to call and explains."""
import json
import os
import re

import anthropic

import pubmed
import string_db

MODEL = "claude-sonnet-5-5"
MAX_TURNS = 10
MAX_PMIDS = 20
MAX_ROUNDS = 4
ABSTRACT_CHARS = 1500  # per paper sent to the model

SYSTEM = """You are a research assistant for biomedical literature, inside a small app whose only interface is this chat. You have two tools:
- pubmed_search: search PubMed (with citation counts). Use it for any request to find, list, count or narrow papers, and for "next page" (same arguments, page+1; see LAST SEARCH). Use mode "strict" when asked whether literature really studies a gene pair / interaction.
- string_pair: check whether the STRING database has a record for a human gene pair. Use it when STRING, protein interaction or association evidence is asked about.
Call whichever tool(s) the question needs, both when it asks for both; call none when a question can be answered from papers already shown (see PAPERS). Never answer a factual question about papers or genes from memory.

The app shows the user a card for every tool result, with the papers or the STRING numbers, so do NOT repeat lists or every number: write a short (2-5 sentence) interpretation.

Rules:
- Use ONLY tool results and the papers under PAPERS. Never invent papers, numbers or citations.
- Cite each paper claim with its PMID in square brackets, e.g. [12345678].
- If the abstracts don't contain the answer (methods details, cohort sizes, full text), say so. PubMed gives abstracts only.
- Gene symbols only go in gene1/gene2 (aliases are not matched; mention that when results look thin). Put other topics in `query`.
- Strict mode means both genes within 10 words plus a relationship term in the title/abstract: it suggests an interaction study, it does not prove one.
- "No STRING record" is not evidence the genes don't interact. If STRING's score comes mostly from text mining, it reflects literature co-mention, not experiments.
- If a tool errors or a source is unavailable, say so plainly. Ask a brief clarifying question if the request is too vague to search."""

PUBMED_TOOL = {
    "name": "pubmed_search",
    "description": "Search PubMed. Returns the total hit count, the query used, and one page of 10 "
                   "papers (title, authors, journal, year, abstract, citation count and citations "
                   "per year from NIH iCite). All given terms are combined with AND.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Free-text topic terms, e.g. immunotherapy resistance"},
            "gene1": {"type": "string", "description": "Gene symbol, e.g. KRAS (searched in title/abstract)"},
            "gene2": {"type": "string", "description": "Second gene symbol, e.g. TP53"},
            "disease": {"type": "string", "description": "Disease, ideally a MeSH heading, e.g. Pancreatic Neoplasms"},
            "year_from": {"type": "string", "description": "4-digit start year"},
            "year_to": {"type": "string", "description": "4-digit end year"},
            "sort": {"type": "string", "enum": ["relevance", "date", "citations_per_year"],
                     "description": "relevance (default), date (newest first), or citations_per_year "
                                    "(re-ranks the 50 most relevant hits)"},
            "page": {"type": "integer", "description": "Page number, default 1"},
            "mode": {"type": "string", "enum": ["any", "strict"],
                     "description": "any (default): each gene appears in title/abstract. strict: needs "
                                    "gene1 and gene2; both within 10 words AND a relationship term "
                                    "(interact*, co-mutation, binding...). Also returns looser counts."},
        },
    },
}

STRING_TOOL = {
    "name": "string_pair",
    "description": "Check whether the STRING database (human) has a record for a gene pair. Returns "
                   "status (found / no_record / gene_not_recognized / unavailable), combined score, "
                   "confidence label and per-evidence-channel scores.",
    "input_schema": {
        "type": "object",
        "properties": {
            "gene1": {"type": "string", "description": "Gene symbol, e.g. KRAS"},
            "gene2": {"type": "string", "description": "Gene symbol, e.g. TP53"},
        },
        "required": ["gene1", "gene2"],
    },
}


class ChatError(Exception):
    pass


# ---- argument sanitizing -------------------------------------------------
def sanitize_pubmed_args(args):
    """Validate model-supplied args; returns kwargs for pubmed.search (raises PubMedError)."""
    args = args or {}
    out = {}
    q = str(args.get("query") or "").strip()[:200]
    if q:
        out["q"] = q
    for k in ("gene1", "gene2"):
        v = str(args.get(k) or "").strip()
        if v:
            out[k] = pubmed.clean_gene(v)
    d = str(args.get("disease") or "").strip().replace('"', "")[:100]
    if d:
        out["disease"] = d
    for k in ("year_from", "year_to"):
        v = str(args.get(k) or "").strip()
        if v:
            if not re.fullmatch(r"\d{4}", v):
                raise pubmed.PubMedError(f"{k} must be a 4-digit year.")
            out[k] = v
    sort = args.get("sort") or "relevance"
    out["sort"] = sort if sort in ("relevance", "date", "citations_per_year") else "relevance"
    mode = args.get("mode") or "any"
    out["mode"] = mode if mode in ("any", "strict") else "any"
    try:
        out["page"] = min(max(int(args.get("page") or 1), 1), 200)
    except (TypeError, ValueError):
        out["page"] = 1
    return out


# ---- tool runners: each returns (payload_for_model, ui_block_or_None, is_error) ----
def run_pubmed(args):
    try:
        kw = sanitize_pubmed_args(args)
        res = pubmed.search(**kw)
    except pubmed.PubMedError as e:
        return {"error": str(e)}, None, True
    papers = [{
        "pmid": p["pmid"], "title": p["title"], "authors": p["authors"][:5],
        "journal": p["journal"], "year": p["year"],
        "abstract": (p["abstract"] or "No abstract available.")[:ABSTRACT_CHARS],
        "citations": p["citations"], "citations_per_year": p["citations_per_year"],
    } for p in res["results"]]
    payload = {k: res[k] for k in ("query", "mode", "sort", "total", "page", "pages") if k in res}
    payload.update(papers=papers)
    for k in ("counts", "ranked_pool"):
        if k in res:
            payload[k] = res[k]
    block = {"type": "papers", "data": res, "args": kw}
    return payload, block, False


def run_string(args):
    args = args or {}
    try:
        g1, g2 = pubmed.clean_gene(args.get("gene1")), pubmed.clean_gene(args.get("gene2"))
    except pubmed.PubMedError as e:
        return {"error": str(e)}, None, True
    res = string_db.pair(g1, g2)
    return res, {"type": "string", "data": {"genes": [g1, g2], "string": res}}, False


TOOL_RUNNERS = {"pubmed_search": run_pubmed, "string_pair": run_string}


# ---- context / messages --------------------------------------------------
def build_context(papers, last_search):
    lines = ["LAST SEARCH: " + (json.dumps(last_search) if last_search else "(none)"),
             "", "PAPERS (last shown to the user):"]
    if not papers:
        lines.append("(none)")
    for p in papers:
        cites = (f"{p['citations']} citations, {p['citations_per_year']}/yr"
                 if p.get("citations_per_year") is not None else "citations n/a")
        lines.append(f"[{p['pmid']}] {p['title']} — {p['journal']}, {p['year']} ({cites})\n"
                     f"Abstract: {(p['abstract'] or 'No abstract available.')[:ABSTRACT_CHARS]}\n")
    return "\n".join(lines)


def clean_messages(messages):
    out = []
    for m in (messages or [])[-MAX_TURNS * 2:]:
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip():
            out.append({"role": m["role"], "content": str(m["content"])[:4000]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    if not out or out[-1]["role"] != "user":
        raise ChatError("Send a message first.")
    return out


def _call(client, system, msgs):
    try:
        return client.beta.messages.create(
            model=MODEL,
            max_tokens=3000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=system,
            tools=[PUBMED_TOOL, STRING_TOOL],
            tool_choice={"type": "auto"},
            messages=msgs,
        )
    except anthropic.AuthenticationError as e:
        raise ChatError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env.") from e
    except anthropic.RateLimitError as e:
        raise ChatError("Rate limited by the Anthropic API; try again in a moment.") from e
    except anthropic.APIConnectionError as e:
        raise ChatError("Could not reach the Anthropic API.") from e
    except anthropic.APIStatusError as e:
        raise ChatError(f"Anthropic API error ({e.status_code}).") from e


def answer(messages, pmids, last_search=None, client=None):
    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise ChatError("Chat is not configured: set ANTHROPIC_API_KEY in .env and restart.")
        client = anthropic.Anthropic()
    msgs = clean_messages(messages)
    pmids = [p for p in (str(x) for x in (pmids or [])) if p.isdigit()][:MAX_PMIDS]
    try:
        papers = pubmed.add_citations(pubmed.fetch(tuple(pmids))) if pmids else []
    except pubmed.PubMedError as e:
        raise ChatError(str(e)) from e
    system = [{"type": "text", "text": SYSTEM},
              {"type": "text", "text": build_context(papers, last_search if isinstance(last_search, dict) else None)}]

    blocks, new_last, reply = [], None, ""
    resp = None
    for _ in range(MAX_ROUNDS):
        resp = _call(client, system, msgs)
        if resp.stop_reason == "refusal":
            return {"reply": "I can't help with that request.", "blocks": blocks, "last_search": new_last}
        reply = "".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            break
        results = []
        for b in calls:
            runner = TOOL_RUNNERS.get(b.name)
            if runner is None:
                payload, block, is_err = {"error": f"Unknown tool {b.name}"}, None, True
            else:
                payload, block, is_err = runner(b.input or {})
            if block:
                blocks.append(block)
                if block["type"] == "papers":
                    new_last = block["args"]
            item = {"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(payload)}
            if is_err:
                item["is_error"] = True
            results.append(item)
        msgs = msgs + [{"role": "assistant", "content": resp.content},
                       {"role": "user", "content": results}]
    else:
        reply = reply or "I ran out of steps for that request; try a simpler question."
    if resp is not None and resp.stop_reason == "max_tokens" and not reply and not blocks:
        reply = "The answer was cut off; try a narrower question."
    blocks.sort(key=lambda b: 0 if b["type"] == "string" else 1)  # STRING card first
    return {"reply": reply, "blocks": blocks, "last_search": new_last}
