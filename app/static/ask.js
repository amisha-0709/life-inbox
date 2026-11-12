/* Question/answer recovery is per owner; restores never call Ask or organization. */
const askState = {owner: undefined, busy: false, request: 0, answer: null, stale: false};
function askCacheKey() { return askState.owner == null ? null : `life-inbox-answer-${askState.owner}`; }
function persistAskState() {
  const key = askCacheKey(); if (!key) return;
  try {
    const cached = JSON.stringify({owner: askState.owner, draft: document.getElementById('askQuestion').value, answer: askState.answer, stale: askState.stale});
    if (cached.length < 100000) sessionStorage.setItem(key, cached);
  } catch { /* The experience still works when browser storage is unavailable. */ }
}
function restoreAskState() {
  if (!askCacheKey()) return;
  try {
    const raw = sessionStorage.getItem(askCacheKey());
    if (!raw || raw.length > 100000) return;
    const cached = JSON.parse(raw);
    if (cached.owner !== askState.owner || typeof cached.draft !== 'string') return;
    document.getElementById('askQuestion').value = cached.draft.slice(0, 500);
    if (cached.answer && typeof cached.answer.answer === 'string' && Array.isArray(cached.answer.citations)) {
      askState.answer = cached.answer; askState.stale = !!cached.stale;
      renderGroundedAnswer(cached.answer);
      setStatus('askStatus', 'Saved answer restored. Ask again to check for newer information.');
    }
  } catch { /* Ignore invalid or unavailable session state. */ }
}
function citationLink(cite, label) {
  const link = document.createElement('a');
  const url = new URL(location.href); url.searchParams.set('view', 'ask'); url.searchParams.set('item', cite.item_id); url.searchParams.delete('task');
  link.href = `${url.pathname}${url.search}`; link.textContent = label; link.dataset.citationItem = cite.item_id;
  link.onclick = event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button) return;
    event.preventDefault(); openSavedItem(cite.item_id, {trigger: link});
  };
  return link;
}
function appendAnswerText(parent, text, citations) {
  const pattern = /\[([^\]]+)\]/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const index = citations.findIndex(cite => cite.id === match[1]);
    parent.append(index < 0 ? document.createTextNode(match[0]) : citationLink(citations[index], `[${index + 1}]`));
    offset = match.index + match[0].length;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}
function renderGroundedAnswer(data) {
  const root = document.getElementById('askOutput'); root.replaceChildren();
  const article = document.createElement('article'); article.className = 'answer-card';
  const question = document.createElement('h2'); question.className = 'answer-question'; question.textContent = data.question;
  const evidence = document.createElement('p'); evidence.className = 'answer-evidence';
  evidence.textContent = data.confidence === 'insufficient' ? 'No reliable answer found in your saved items.' : `Based on ${new Set(data.citations.map(cite => cite.item_id)).size} saved source${new Set(data.citations.map(cite => cite.item_id)).size === 1 ? '' : 's'}.`;
  const answer = document.createElement('div'); answer.className = 'answer-body';
  for (const paragraph of data.answer.split(/\n\s*\n/)) { const p = document.createElement('p'); appendAnswerText(p, paragraph, data.citations); answer.append(p); }
  article.append(question, evidence, answer);
  if (data.citations.length) {
    const heading = document.createElement('h3'); heading.textContent = 'Explore the sources'; article.append(heading);
    const sources = document.createElement('ol'); sources.className = 'answer-sources';
    data.citations.forEach((cite, index) => {
      const row = document.createElement('li');
      const link = citationLink(cite, cite.title || 'Saved capture'); link.className = 'citation-link'; link.setAttribute('aria-label', `Source ${index + 1}: ${cite.title || 'Saved capture'}`);
      const excerpt = document.createElement('p'); excerpt.textContent = cite.excerpt; excerpt.className = 'citation-excerpt';
      const type = document.createElement('span'); type.className = 'item-meta'; type.textContent = captureTypeLabel(cite.item_type);
      row.append(link, type, excerpt); sources.append(row);
    });
    article.append(sources);
  } else {
    const help = document.createElement('p'); help.className = 'muted'; help.textContent = 'Try a more specific question, or save the missing information in Inbox first.'; article.append(help);
  }
  root.append(article);
}
async function submitInboxQuestion(event) {
  event?.preventDefault(); if (askState.busy) return;
  const question = document.getElementById('askQuestion').value.trim(); if (question.length < 3) { setStatus('askStatus', 'Write a question with at least 3 characters.', true); return; }
  if (askState.owner == null) { setStatus('askStatus', 'Check your account before asking.', true); return; }
  const request = ++askState.request; askState.busy = true;
  setButtonBusy('askButton', true, 'Looking through your Inbox…', 'Ask'); setStatus('askStatus', 'Finding relevant saved items…'); persistAskState();
  try {
    const data = await fetchJson('/api/ask', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({question})}, 'Could not answer right now. Your question is still here; try again.');
    if (request !== askState.request) return;
    askState.answer = data; askState.stale = false; renderGroundedAnswer(data); persistAskState();
    setStatus('askStatus', data.confidence === 'insufficient' ? 'Try a more specific question or inspect your saved information.' : 'Answer ready. Open a source to inspect the original.');
  } catch (error) { if (request === askState.request) { setStatus('askStatus', error.message, true); persistAskState(); } }
  finally { if (request === askState.request) { askState.busy = false; setButtonBusy('askButton', false, 'Looking through your Inbox…', 'Ask'); } }
}
document.getElementById('askQuestion').addEventListener('input', persistAskState);
window.addEventListener('lifeinbox:owner', event => {
  const owner = event.detail?.id ?? null; if (owner === askState.owner) return;
  const initial = askState.owner === undefined;
  askState.request++; askState.busy = false; askState.owner = owner; askState.answer = null; askState.stale = false;
  if (!initial) { document.getElementById('askQuestion').value = ''; document.getElementById('askOutput').replaceChildren(); setStatus('askStatus', ''); }
  setButtonBusy('askButton', false, 'Looking through your Inbox…', 'Ask'); restoreAskState();
});
window.addEventListener('lifeinbox:organized', event => {
  if (!askState.answer || !event.detail?.changed) return;
  askState.stale = true; persistAskState(); setStatus('askStatus', 'Your Inbox changed. Ask again to refresh this saved answer.');
});
