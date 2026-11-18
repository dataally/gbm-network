# PubMed Chat

A chat-only web app for exploring biomedical literature. Ask in plain English; an LLM decides which of two tools to call:

| Tool | What it does | Data source |
|---|---|---|
| `pubmed_search` | Finds papers by gene(s), disease, topic and year, with abstracts and citation counts. Supports a **strict** mode for gene pairs. | NCBI E-utilities (PubMed), NIH iCite |
| `string_pair` | Checks whether a human gene pair has a record in STRING, with the combined score and per-evidence-channel breakdown. | STRING API |

Results appear as cards in the chat (STRING first, then papers), followed by a short interpretation from the model. The model answers only from tool results and the abstracts shown, and cites papers by PMID.

## Example prompts

- "Papers on KRAS and TP53 in pancreatic cancer since 2020"
- "Is KRAS–TP53 in STRING, and is there literature on the pair?"
- "Strict literature for BRCA1 and PARP1, sorted by citations per year"
- "next page" / "What did the first paper find?" (follow-ups use the papers already shown)

## How it works

- **Tool-calling loop** (`chat.py`): the model (Claude Sonnet 5.5 via the Anthropic API) calls `pubmed_search` and/or `string_pair`; results are returned to it and it writes the final answer. Tool arguments are validated server-side before any API call.
- **Strict literature mode**: both genes within 10 words of each other in the title/abstract **and** a relationship term (`interact*`, co-mutation, co-occurrence, binding, synthetic lethal, ...). It also reports looser counts (within 10 words; mention both). This suggests an interaction study; it does not prove one.
- **Citations per year**: from NIH iCite. Sorting by citations per year re-ranks the 50 most relevant hits (NCBI cannot sort by citations).
- **STRING**: human only (species 9606). "No record" is not evidence that two genes do not interact, and a high score driven by text mining reflects literature co-mention rather than experiments.
- **Stateless server**: the browser sends the chat history, the PMIDs last shown and the last search arguments each turn.

## Setup

Requires Python 3.10+.

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env     # then fill in your keys
./venv/bin/python app.py
```

Open http://localhost:5050 (set `PORT` to change it; port 5000 is often taken by macOS AirPlay).

### Environment variables (`.env`)

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Powers the chat (billed per message). |
| `NCBI_API_KEY` | recommended | Raises the NCBI limit from 3 to 10 requests/second. Free from your NCBI account. |
| `NCBI_EMAIL` | optional | Contact email NCBI asks API clients to send. |

`.env` is git-ignored. Never commit keys.

## Project layout

```
app.py            Flask app: /, /api/chat, /api/export (CSV)
chat.py           System prompt, the two tools, tool-calling loop
pubmed.py         NCBI E-utilities client, query builder, iCite citations
string_db.py      STRING client
templates/        index.html (chat page)
static/           app.js, style.css
test_pubmed.py    Tests (external APIs mocked)
```

## Tests

```bash
./venv/bin/python -m pytest -q
```

The Anthropic, NCBI, iCite and STRING calls are mocked, so tests need no keys or network.

## Limitations

- PubMed holds abstracts only, not full text; the model says so when an abstract does not answer a question.
- Gene matching is by symbol in the title/abstract; aliases (for example HER2 vs ERBB2) are not matched.
- CSV export covers the first 200 hits of a search (50 when sorted by citations per year).
- Each chat message is a billed LLM call.
