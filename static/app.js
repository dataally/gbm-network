const $ = (id) => document.getElementById(id);
let chatHistory = [];   // [{role, content}] text only
let lastPmids = [];     // papers last shown, so follow-ups can use their abstracts
let lastSearch = null;  // args of the last pubmed_search (for "next page" etc.)

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function linkify(text) {
  return esc(text).replace(/\[([\dPMID:,;\s]+)\]/g, (m, inner) =>
    "[" + inner.replace(/\d{5,9}/g, (id) =>
      `<a href="https://pubmed.ncbi.nlm.nih.gov/${id}/" target="_blank" rel="noopener">${id}</a>`) + "]");
}

function addMsg(cls, html) {
  const d = document.createElement("div");
  d.className = "msg " + cls;
  d.innerHTML = html;
  $("chatlog").appendChild(d);
  $("chatlog").scrollTop = $("chatlog").scrollHeight;
  return d;
}

// ---- cards ----
function renderString(d) {
  const [g1, g2] = d.genes;
  const S = d.string || {};
  let body;
  if (S.status === "found") {
    const ch = Object.entries(S.channels).filter(([, v]) => v > 0)
      .map(([k, v]) => `<span class="chip">${esc(k)} ${Number(v).toFixed(2)}</span>`).join("");
    body = `<div class="big">Record found <span>score ${Number(S.score).toFixed(3)} · ${esc(S.confidence)} confidence</span></div>
      <div>${ch}</div>
      <div class="links"><a href="${esc(S.url)}" target="_blank" rel="noopener">View in STRING</a></div>`;
  } else if (S.status === "no_record") {
    body = `<div class="big">No STRING record <span>for this pair</span></div>
      <div class="links"><a href="${esc(S.url)}" target="_blank" rel="noopener">Check in STRING</a></div>`;
  } else if (S.status === "gene_not_recognized") {
    body = `<div class="big">Gene not recognized <span>${esc((S.genes || []).join(", "))}</span></div>`;
  } else {
    body = `<div class="meta">STRING unavailable right now.</div>`;
  }
  return `<strong>${esc(g1)} + ${esc(g2)}</strong>
    <div class="sec"><div class="lbl">STRING (human)</div>${body}</div>`;
}

function renderPapers(b) {
  const d = b.data, a = b.args || {};
  const exp = new URLSearchParams();
  Object.entries(a).forEach(([k, v]) => { if (k !== "page" && v) exp.set(k, v); });
  const c = d.counts;
  const counts = c
    ? `<div class="meta">Strict: ${Number(c.strict).toLocaleString()} papers (both genes within ${esc(c.proximity_words)} words + a relationship term).
       Looser: ${Number(c.near).toLocaleString()} within ${esc(c.proximity_words)} words · ${Number(c.mention).toLocaleString()} mention both.</div>` : "";
  const note = d.ranked_pool ? `<div class="meta">Ranked by citations/year among the ${esc(d.ranked_pool)} most relevant hits.</div>` : "";
  const rows = d.results.map((r) => `
    <div class="paper">
      <h3><a href="${esc(r.pubmed_url)}" target="_blank" rel="noopener">${esc(r.title)}</a></h3>
      <div class="meta">${esc(r.authors.slice(0, 6).join(", "))}${r.authors.length > 6 ? ", et al." : ""}</div>
      <div class="meta">${esc(r.journal)} · ${esc(r.year)} · PMID ${esc(r.pmid)}${r.doi_url ? ` · <a href="${esc(r.doi_url)}" target="_blank" rel="noopener">DOI</a>` : ""}</div>
      <div class="cites">${r.citations_per_year == null ? "Citations: n/a" : esc(r.citations) + " citations · " + esc(r.citations_per_year) + " / year"}</div>
      <details><summary>Abstract</summary><div class="abstract">${esc(r.abstract) || "No abstract available."}</div></details>
    </div>`).join("");
  const more = d.pages > d.page ? ` · say "next page" for more` : "";
  return `<strong>PubMed</strong>
    <div class="sec">
      <div class="big">${Number(d.total).toLocaleString()} <span>results — page ${esc(d.page)} of ${esc(d.pages)}${more}</span></div>
      <div class="meta">Query: ${esc(d.query)}</div>${counts}${note}
      <div class="links"><a href="${esc(d.pubmed_url)}" target="_blank" rel="noopener">Open in PubMed</a> ·
        <a href="/api/export?${esc(exp.toString())}">Download CSV</a></div>
      ${rows || '<div class="meta">No papers found.</div>'}
    </div>`;
}

// ---- chat ----
async function send(text) {
  text = text.trim();
  if (!text) return;
  const ex = $("examples");
  if (ex) ex.remove();
  chatHistory.push({ role: "user", content: text });
  addMsg("user", esc(text));
  const wait = addMsg("note", "Thinking…");
  $("chatsend").disabled = true;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chatHistory, pmids: lastPmids, last_search: lastSearch }),
    });
    const data = await res.json();
    wait.remove();
    if (!res.ok) throw new Error(data.error || "Chat failed");
    let firstCard = null;
    for (const b of data.blocks || []) {
      const el = addMsg("card", b.type === "string" ? renderString(b.data) : renderPapers(b));
      firstCard = firstCard || el;
      if (b.type === "papers") lastPmids = b.data.results.map((r) => r.pmid);
    }
    if (data.last_search) lastSearch = data.last_search;
    if (data.reply) {
      chatHistory.push({ role: "assistant", content: data.reply });
      addMsg("bot", linkify(data.reply));
    } else if (data.blocks && data.blocks.length) {
      chatHistory.push({ role: "assistant", content: "(showed results)" });
    }
    // keep the top of the first card (STRING / first results) in view
    if (firstCard) $("chatlog").scrollTop = firstCard.offsetTop - $("chatlog").offsetTop;
  } catch (err) {
    wait.remove();
    chatHistory.pop(); // drop the unanswered user turn so history stays alternating
    addMsg("err", esc(err.message));
  } finally {
    $("chatsend").disabled = false;
    $("chatinput").focus();
  }
}

$("chatform").addEventListener("submit", (e) => {
  e.preventDefault();
  const t = $("chatinput").value;
  $("chatinput").value = "";
  send(t);
});
document.querySelectorAll(".chip-btn").forEach((b) => b.addEventListener("click", () => send(b.textContent)));
$("chatclear").addEventListener("click", () => {
  chatHistory = []; lastPmids = []; lastSearch = null;
  $("chatlog").innerHTML = "";
});
