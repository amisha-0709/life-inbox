/* One collection and one owner-scoped item detail, shared by source links. */
let detailRequest = 0;
let detailReturn = null;
let currentDetail = null;
let detailOwner = undefined;
function updateFiltersLabel() { const count=Object.entries(itemFilterFields).filter(([key,id])=>key!=='q' && document.getElementById(id).value).length;const summary=document.getElementById('toggleFilters');if(summary)summary.textContent=count?`Filters (${count})`:'Filters'; }
function humanDate(instant) {
  try { return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium'}).format(new Date(instant)); }
  catch { return ''; }
}
function savedItemTitle(item) { return item.effective_title || item.title || item.original_filename || (item.raw_text || item.source_url || 'Saved item').slice(0, 100); }
function fillTopicChoices(select, topics, all = false) {
  if (!select) return;
  const selected = all ? new URLSearchParams(location.search).get('topic_id') || select.value : select.value;
  select.replaceChildren();
  const empty = new Option(all ? 'All topics' : 'Choose a topic', ''); empty.disabled = !all; select.append(empty);
  for (const topic of topics) select.append(new Option(topic.name, String(topic.id)));
  select.value = selected || '';
}
function renderCollectionItem(item) {
  const row = document.createElement('article'); row.className = 'collection-row';
  const button = document.createElement('button'); button.type = 'button'; button.className = 'item-open'; button.dataset.itemId = item.id;
  const type = document.createElement('span'); type.className = 'source-icon'; type.textContent = ({text: '✎', url: '↗', pdf: 'PDF', image: '▧', voice: '♫'})[item.item_type] || '•'; type.setAttribute('aria-hidden', 'true');
  const content = document.createElement('span'); content.className = 'item-summary';
  const title = document.createElement('strong'); title.textContent = savedItemTitle(item);
  const excerpt = document.createElement('span'); excerpt.className = 'item-excerpt'; excerpt.textContent = (item.extracted_text || item.raw_text || item.source_url || item.original_filename || '').replace(/\s+/g, ' ').slice(0, 150);
  excerpt.hidden = excerpt.textContent.trim().toLocaleLowerCase() === title.textContent.trim().toLocaleLowerCase();
  const meta = document.createElement('span'); meta.className = 'item-meta';
  meta.textContent = [({text:'Note',url:'Link',pdf:'PDF',image:'Image',voice:'Voice'})[item.item_type], item.effective_topic_name, humanDate(item.created_at), item.effective_priority === 'High' ? 'High priority' : ''].filter(Boolean).join(' · ');
  content.append(title, excerpt, meta); button.append(type, content);
  if (item.processing_status === 'failed') { const badge = document.createElement('span'); badge.className = 'state-badge warning'; badge.textContent = 'Needs attention'; button.append(badge); }
  else if (['captured','processing'].includes(item.processing_status)) { const badge = document.createElement('span'); badge.className = 'state-badge'; badge.textContent = 'Processing'; button.append(badge); }
  button.addEventListener('click', () => openSavedItem(item.id, {trigger: button})); row.append(button); return row;
}

async function openSavedItem(id, {trigger = null, history = true} = {}) {
  id = Number(id); if (!Number.isInteger(id) || id <= 0) return;
  const dialog = document.getElementById('itemDetail');
  if (!dialog.open) detailReturn = {scroll: history ? scrollY : window.history.state?.itemDetailScroll ?? scrollY, trigger, id: window.history.state?.itemDetailReturnId || id};
  if (history) {
    const url = new URL(location.href); url.searchParams.set('item', id);
    const state = {...window.history.state, itemDetailEntry: true, ownerId: window.lifeInboxOwnerId, itemDetailScroll: detailReturn.scroll, itemDetailReturnId: detailReturn.id};
    if (dialog.open) window.history.replaceState(state, '', url); else window.history.pushState(state, '', url);
  }
  if (!dialog.open) dialog.showModal();
  document.documentElement.classList.add('detail-open');
  const request = ++detailRequest;
  currentDetail = null;
  document.getElementById('detailHeading').textContent = 'Loading saved item…';
  const body = document.getElementById('itemDetailBody'); body.innerHTML = '<p class="muted" role="status">Loading…</p>';
  document.getElementById('detailHeading').focus({preventScroll: true});
  try {
    const [detail, topics] = await Promise.all([fetchJson(`/api/items/${id}/detail`, {}, 'This item is unavailable or belongs to another account.'), fetchJson('/api/topics', {}, 'Could not load topics.')]);
    if (request !== detailRequest || !dialog.open) return;
    currentDetail = detail;
    renderSavedItemDetail(detail, topics);
  } catch (error) {
    if (request !== detailRequest) return;
    document.getElementById('detailHeading').textContent = 'Item unavailable'; body.replaceChildren();
    const message = document.createElement('p'); message.textContent = error.message; body.append(message);
  }
}
function closeItemDetail() {
  if (new URLSearchParams(location.search).has('item') && window.history.state?.itemDetailEntry) { window.history.back(); return; }
  const url = new URL(location.href); url.searchParams.delete('item'); window.history.replaceState(window.history.state, '', url); finishClosingItem();
}
function finishClosingItem() {
  detailRequest++; currentDetail = null;
  const dialog = document.getElementById('itemDetail'); if (dialog.open) dialog.close();
  document.documentElement.classList.toggle('detail-open', !!document.querySelector('dialog[open]'));
  const returning = detailReturn; detailReturn = null;
  if (returning) {
    const trigger = returning.trigger?.isConnected ? returning.trigger : document.querySelector(`[data-item-id="${returning.id}"]`);
    trigger?.focus({preventScroll: true}); window.scrollTo(0, returning.scroll);
  }
}
function syncItemDetailFromUrl() {
  const id = new URLSearchParams(location.search).get('item');
  if (id) openSavedItem(id, {history: false}); else if (document.getElementById('itemDetail').open) finishClosingItem();
}
function detailSection(title, id) {
  const section = document.createElement('section'); section.className = 'detail-section'; if (id) section.id = id;
  const heading = document.createElement('h3'); heading.textContent = title; section.append(heading); return section;
}
function renderSavedItemDetail(detail, topics) {
  const {item, insight, tasks, related_items: related} = detail;
  const body = document.getElementById('itemDetailBody'); body.replaceChildren();
  document.getElementById('detailHeading').textContent = savedItemTitle(item);
  const meta = document.createElement('p'); meta.className = 'muted'; meta.textContent = `${captureTypeLabel(item.item_type)} · Saved ${humanDate(item.created_at)}`; body.append(meta);
  if (item.processing_status !== 'ready' && item.item_type !== 'text') {
    const notice = document.createElement('div'); notice.className = 'notice';
    const text = document.createElement('p'); text.textContent = item.processing_status === 'failed' ? 'The original is saved, but its content could not be read. Retry or correct the extracted text below.' : 'The original is saved. Its content is still processing. If processing was interrupted, retry it.';
    const retry = actionButton('Retry processing', 'secondary', async () => {
      retry.disabled = true;
      try { const updated = await fetchJson(`/api/items/${item.id}/retry`, {method:'POST'}, 'Could not retry processing.'); await openSavedItem(item.id, {history:false}); if (updated.processing_status === 'ready') organizeCapturedItems(); }
      catch(error) { setStatus('detailStatus', error.message, true); retry.disabled = false; }
    }); retry.id = 'retryItemButton'; notice.append(text, retry); body.append(notice);
  }
  const status = document.createElement('p'); status.id = 'detailStatus'; status.className = 'status'; status.setAttribute('role','status'); status.setAttribute('aria-live','polite'); body.append(status);
  const corrections = detailSection('Make it yours');
  const form = document.createElement('form'); form.className = 'correction-form';
  form.innerHTML = '<label>Title<input id="detailTitle" maxlength="200" required /></label><div class="field-pair"><label>Topic<select id="detailTopic"></select></label><label>Priority<select id="detailPriority"><option>Low</option><option>Medium</option><option>High</option></select></label></div><div class="task-actions"><button id="saveItemCorrections" type="submit">Save corrections</button><button id="resetItemCorrections" type="button" class="quiet">Reset all corrections</button></div>';
  form.querySelector('#detailTitle').value = (insight.effective_title || savedItemTitle(item)).slice(0,200);
  fillTopicChoices(form.querySelector('#detailTopic'), topics); form.querySelector('#detailTopic').value = insight.effective_topic_id || '';
  form.querySelector('#detailPriority').value = insight.effective_priority || 'Medium';
  const save = async clear => {
    const button = form.querySelector('#saveItemCorrections'); button.disabled = true;
    const payload = clear ? {clear_overrides:true} : {user_title:form.querySelector('#detailTitle').value.trim(), user_priority:form.querySelector('#detailPriority').value};
    if (!clear && form.querySelector('#detailTopic').value) payload.user_topic_id = Number(form.querySelector('#detailTopic').value);
    try { await fetchJson(`/api/items/${item.id}/insight`, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}, 'Could not save corrections. Your entries are still here.'); await loadNotes(); await openSavedItem(item.id,{history:false}); setStatus('detailStatus', clear ? 'Corrections reset to suggestions.' : 'Your corrections are saved. Re-analysis will preserve them.'); }
    catch(error) { setStatus('detailStatus', error.message, true); button.disabled = false; }
  };
  form.addEventListener('submit', event => {event.preventDefault(); save(false);}); form.querySelector('#resetItemCorrections').onclick = () => save(true); corrections.append(form); body.append(corrections);
  const original = detailSection('Original capture');
  if (item.source_url) { const link=document.createElement('a'); link.href=item.source_url; link.target='_blank'; link.rel='noopener noreferrer'; link.textContent=item.source_url; original.append(link); }
  if (item.storage_path) {
    const link=document.createElement('a'); link.href=`/api/items/${item.id}/file`; link.target='_blank'; link.rel='noopener'; link.textContent=`Open ${item.original_filename || 'original file'}`; original.append(link);
    if (item.item_type === 'image') { const image=document.createElement('img'); image.src=link.href; image.alt=item.original_filename || 'Saved image'; image.className='detail-media'; original.append(image); }
    if (item.item_type === 'voice') { const audio=document.createElement('audio'); audio.controls=true; audio.preload='none'; audio.src=link.href; audio.className='detail-media'; original.append(audio); }
  }
  const raw=document.createElement('div'); raw.id='detailOriginal'; raw.className='source-text'; raw.textContent=item.item_type==='text' ? item.raw_text : (item.original_filename || item.source_url || 'Original file saved.'); original.append(raw);
  if (item.item_type === 'text') {
    const edit=actionButton('Edit original text','quiet',()=>{
      edit.hidden=true; const editor=document.createElement('form'); editor.innerHTML='<label>Original text<textarea id="detailSourceText" rows="6" required></textarea></label><div class="task-actions"><button id="saveSourceButton" type="submit">Save text</button><button type="button" class="quiet">Cancel</button></div>';
      editor.querySelector('textarea').value=item.raw_text; editor.querySelector('button.quiet').onclick=()=>{editor.remove();edit.hidden=false;edit.focus();};
      editor.onsubmit=async event=>{event.preventDefault();const submit=editor.querySelector('button');submit.disabled=true;try{await fetchJson(`/api/items/${item.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({raw_text:editor.querySelector('textarea').value})},'Could not save original. Your edit is still here.');await loadNotes();await openSavedItem(item.id,{history:false});organizeCapturedItems();}catch(error){setStatus('detailStatus',error.message,true);submit.disabled=false;}};
      original.append(editor);editor.querySelector('textarea').focus();
    }); edit.id='editSourceButton'; original.append(edit);
  }
  body.append(original);
  if (item.item_type === 'url') { const section=detailSection('Extracted article');const text=document.createElement('div');text.className='source-text';text.textContent=item.extracted_text || item.raw_text || 'No article text was extracted.';section.append(text);body.append(section); }
  else if (item.item_type !== 'text') {
    const extracted=detailSection(item.item_type==='voice' ? 'Transcript' : 'Extracted text');
    const label=document.createElement('label');label.htmlFor='detailExtracted';label.textContent=item.item_type==='voice'?'Correct or add the transcript':'Correct the extracted text';
    const input=document.createElement('textarea');input.id='detailExtracted';input.rows=8;input.value=item.extracted_text || '';
    const save=actionButton('Save extracted text','secondary',async()=>{save.disabled=true;try{await fetchJson(`/api/items/${item.id}/extracted-text`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({extracted_text:input.value})},'Could not save extracted text. Your edit is still here.');await loadNotes();setStatus('detailStatus','Extracted text saved. Organizing the change…');organizeCapturedItems();}catch(error){setStatus('detailStatus',error.message,true);}finally{save.disabled=false;}});save.id='saveExtractedText';extracted.append(label,input,save);body.append(extracted);
  }
  const linked=detailSection('Linked tasks','detailLinkedTasks');
  if (!tasks.length) {const empty=document.createElement('p');empty.className='muted';empty.textContent='No tasks linked to this capture.';linked.append(empty);}
  for(const task of tasks){const button=actionButton(`${task.status==='completed'?'✓ ':''}${task.text}${task.due_on?' · '+task.due_on:''}`,'detail-link',()=>openLinkedTask(task));linked.append(button);}body.append(linked);
  if (related.length) {const section=detailSection('Related captures','detailRelatedItems');for(const row of related){const button=actionButton(savedItemTitle(row.item),'detail-link',()=>openSavedItem(row.item.id));section.append(button);}body.append(section);}
  const menu=document.createElement('details');menu.className='secondary-menu';const summary=document.createElement('summary');summary.textContent='More actions';menu.append(summary);
  const reanalyze=actionButton('Re-analyse this item','quiet',async()=>{reanalyze.disabled=true;try{await fetchJson(`/api/items/${item.id}/invalidate-analysis`,{method:'POST'},'Could not queue this item.');setStatus('detailStatus','Queued for organization.');await organizeCapturedItems();await openSavedItem(item.id,{history:false});}catch(error){setStatus('detailStatus',error.message,true);}finally{reanalyze.disabled=false;}});reanalyze.id='reanalyzeItemButton';menu.append(reanalyze);
  let confirming=false;const remove=actionButton('Delete capture','quiet danger-text',()=>{if(!confirming){confirming=true;remove.textContent='Confirm delete';cancel.hidden=false;return;}remove.disabled=true;fetchJson(`/api/items/${item.id}`,{method:'DELETE'},'Could not delete capture.').then(()=>{closeItemDetail();loadNotes();}).catch(error=>{setStatus('detailStatus',error.message,true);remove.disabled=false;});});
  const cancel=actionButton('Keep capture','quiet',()=>{confirming=false;remove.textContent='Delete capture';cancel.hidden=true;});cancel.hidden=true;menu.append(remove,cancel);body.append(menu);
}
async function openLinkedTask(task) {
  document.getElementById('itemDetail').close(); document.documentElement.classList.toggle('detail-open', !!document.querySelector('dialog[open]')); detailReturn=null;
  updateUrl({item:'',view:'tasks',task:String(task.id),task_status:'',task_view:'all',task_filter:task.status==='open'?'all':task.status});syncControlsFromUrl();showDemoView('tasks',{history:false});
  await loadTasks();if(typeof openTaskDetail==='function')openTaskDetail(task.id,{history:false});else document.querySelector(`[data-task-id="${task.id}"]`)?.scrollIntoView({block:'center'});
}
document.getElementById('itemDetail').addEventListener('cancel',event=>{event.preventDefault();closeItemDetail();});
window.addEventListener('popstate',syncItemDetailFromUrl);
window.addEventListener('DOMContentLoaded',syncItemDetailFromUrl);
window.addEventListener('lifeinbox:owner',event=>{const owner=event.detail?.id ?? null;if(detailOwner===undefined){detailOwner=owner;if(!new URLSearchParams(location.search).has('item')&&document.getElementById('itemDetail').open)finishClosingItem();return;}if(owner===detailOwner)return;detailOwner=owner;if(document.getElementById('itemDetail').open){detailRequest++;document.getElementById('itemDetail').close();document.documentElement.classList.toggle('detail-open', !!document.querySelector('dialog[open]'));detailReturn=null;}const url=new URL(location.href);url.searchParams.delete('item');window.history.replaceState({},'',url);});
