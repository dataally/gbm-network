import csv
import io
import os

from flask import Flask, Response, jsonify, render_template, request

import chat
import pubmed

app = Flask(__name__)

EXPORT_CAP = 200


@app.get("/")
def index():
    # cache-bust static files so edits show up without a hard refresh
    v = int(max(os.path.getmtime(os.path.join(app.static_folder, f))
                for f in os.listdir(app.static_folder)))
    return render_template("index.html", v=v)


@app.post("/api/chat")
def api_chat():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(chat.answer(body.get("messages", []), body.get("pmids", []),
                                   body.get("last_search")))
    except chat.ChatError as e:
        return jsonify(error=str(e)), 400


@app.get("/api/export")
def api_export():
    """CSV of the first EXPORT_CAP hits for a search (same args the chat tool used)."""
    a = request.args
    try:
        kw = chat.sanitize_pubmed_args({**a.to_dict(), "query": a.get("q", ""), "page": 1})
        records = pubmed.search(**{**kw, "page_size": EXPORT_CAP})["results"]
    except pubmed.PubMedError as e:
        return jsonify(error=str(e)), 400
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["PMID", "Title", "Authors", "Journal", "Year", "DOI", "PubMed URL",
                "Citations", "Citations per year", "Abstract"])
    for r in records:
        w.writerow([r["pmid"], r["title"], "; ".join(r["authors"]), r["journal"], r["year"],
                    r["doi"], r["pubmed_url"], r["citations"], r["citations_per_year"], r["abstract"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pubmed.csv"})


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5050)))
