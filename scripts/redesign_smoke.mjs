// Native CDP walkthrough; the Python runner owns all disposable processes/data.
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';

const [debugPort, appPort, artifacts, stage = 'shell', viewport = 'both'] = process.argv.slice(2);
assert(['shell','inbox','tasks','ask','all'].includes(stage),'supported acceptance stage');
const base = `http://127.0.0.1:${appPort}`;
const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
const socket = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
let sequence = 0;
const pending = new Map();
const browserErrors = [];
const passedChecks = [];
const requestCounts={analyze:0,ask:0};
socket.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.method === 'Runtime.exceptionThrown') browserErrors.push(message.params.exceptionDetails.text);
  if(message.method==='Network.requestWillBeSent' && message.params.request.method==='POST') {
    const pathname=new URL(message.params.request.url).pathname;
    if(pathname==='/api/analyze') requestCounts.analyze++;
    if(pathname==='/api/ask') requestCounts.ask++;
  }
  if (message.id && pending.has(message.id)) {
    const {resolve, reject} = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result);
  }
};
function command(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    pending.set(id, {resolve, reject});
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const result = await command('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function until(expression, label, milliseconds = 20000) {
  const started = Date.now();
  while (Date.now() - started < milliseconds) {
    try { if (await evaluate(expression)) return; }
    catch (error) {
      if (!/context|navigation|Inspected target/i.test(error.message)) throw error;
    }
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${label}`);
}
async function api(endpoint, options = {}) {
  return evaluate(`(async()=>{
    const response=await fetch(${JSON.stringify(endpoint)},${JSON.stringify(options)});
    if(!response.ok) throw new Error(await response.text());
    return response.status===204 ? null : response.json();
  })()`);
}
async function allTasks() {
  const tasks=[],seen=new Set();
  for(let offset=0;offset<10000;offset+=100) {
    const page=await api(`/api/tasks?status=all&limit=100&offset=${offset}`);
    const extra=page.filter(task=>!seen.has(task.id));
    tasks.push(...extra);extra.forEach(task=>seen.add(task.id));
    if(page.length!==100 || !extra.length) break;
  }
  return tasks;
}
async function navigate(query = '?view=inbox', before = false) {
  const pathname = before ? '/before' : '/';
  await command('Page.navigate', {url: `${base}${pathname}${query}`});
  await until(`document.readyState==='complete' && location.pathname===${JSON.stringify(pathname)} && !!document.querySelector('#noteText')`, 'page loaded');
  await until(`!!document.querySelector('#authUser')?.textContent.includes('Local Owner')`, 'local owner loaded');
  if (!before) await until(`!!document.querySelector('#captureComposer')`, 'unified composer loaded');
}
async function screenshot(name) {
  await new Promise(resolve => setTimeout(resolve, 350));
  const result = await command('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  fs.writeFileSync(path.join(artifacts, `${name}.png`), Buffer.from(result.data, 'base64'));
}
async function click(selector) {
  await evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
}
async function destination(name) {
  await click(`.nav-link[data-view="${name}"]`);
  await until(`new URLSearchParams(location.search).get('view')===${JSON.stringify(name)} && !document.querySelector('#view-${name}').hidden`, `${name} navigation`);
  assert.equal(await evaluate(`document.querySelectorAll('.view:not([hidden])').length`), 1);
  assert.equal(await evaluate(`document.querySelector('.nav-link[aria-current="page"]')?.dataset.view`), name);
}
async function organized() {
  await until(`!organizationRunning && /Organized\.|Everything is up to date/.test(document.querySelector('#backgroundStatus').textContent)`, 'automatic organization', 45000);
}
async function save() {
  await click('#addNoteButton');
  await until(`!document.querySelector('#addNoteButton').disabled && document.querySelector('#noteText').value==='' && document.querySelector('#attachmentPreviews').children.length===0`, 'capture saved');
  await organized();
}
async function write(text) {
  await evaluate(`(()=>{const field=document.querySelector('#noteText');field.value=${JSON.stringify(text)};field.dispatchEvent(new Event('input',{bubbles:true}))})()`);
}
async function fileInput(files) {
  const {root} = await command('DOM.getDocument');
  const {nodeId} = await command('DOM.querySelector', {nodeId: root.nodeId, selector: '#captureFiles'});
  await command('DOM.setFileInputFiles', {nodeId, files: files.map(file => path.join(artifacts, file))});
}
async function putImage(kind) {
  const bytes = fs.readFileSync(path.join(artifacts, 'workshop.png')).toString('base64');
  await evaluate(`(()=>{
    const file=new File([Uint8Array.from(atob(${JSON.stringify(bytes)}),c=>c.charCodeAt(0))], '${kind}-workshop.png', {type:'image/png'});
    const transfer=new DataTransfer();transfer.items.add(file);
    const event=${kind === 'drop' ? "new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer})" : "new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:transfer})"};
    document.querySelector('${kind === 'drop' ? '#captureComposer' : '#noteText'}').dispatchEvent(event);
  })()`);
}
async function layout(label) {
  const dimensions = await evaluate(`({width:innerWidth,scroll:document.documentElement.scrollWidth})`);
  assert(dimensions.scroll <= dimensions.width, `${label}: horizontal overflow ${JSON.stringify(dimensions)}`);
  for (const selector of ['.nav-link', '#addNoteButton', '#attachButton', '#recordVoiceButton']) {
    const rectangles = await evaluate(`Array.from(document.querySelectorAll(${JSON.stringify(selector)})).map(element=>{const r=element.getBoundingClientRect();return {width:r.width,height:r.height}})`);
    for (const rect of rectangles.filter(rect=>rect.width || rect.height)) assert(rect.height >= 40 && rect.width >= 40, `${label}: small control ${selector} ${JSON.stringify(rect)}`);
  }
  await namedInputs();
}
async function namedInputs(selector='body') {
  const missing=await evaluate(`Array.from(document.querySelector(${JSON.stringify(selector)}).querySelectorAll('input,textarea,select')).filter(element=>{
    const r=element.getBoundingClientRect();
    if(!r.width || !r.height || element.type==='hidden') return false;
    const labels=[...(element.labels||[])].map(label=>label.textContent.trim()).join('');
    return !labels && !element.getAttribute('aria-label') && !element.getAttribute('aria-labelledby');
  }).map(element=>element.id||element.tagName)`);
  assert.deepEqual(missing,[],'visible input controls have labels');
}

async function navigationChecks() {
  const before = (await api('/api/analyze/latest')).analysis_run_id;
  await destination('tasks');
  assert.equal(await evaluate(`document.activeElement.id`), 'mainContent');
  await destination('ask');
  await evaluate(`history.back()`);
  await until(`new URLSearchParams(location.search).get('view')==='tasks' && !document.querySelector('#view-tasks').hidden`, 'browser Back');
  await evaluate(`history.forward()`);
  await until(`new URLSearchParams(location.search).get('view')==='ask' && !document.querySelector('#view-ask').hidden`, 'browser Forward');
  await command('Page.reload');
  await until(`document.readyState==='complete' && document.querySelector('.nav-link[aria-current="page"]')?.dataset.view==='ask'`, 'reload retains destination');
  await click('#accountMenu summary');
  await click('#accountMenu a');
  await until(`new URLSearchParams(location.search).get('view')==='settings' && !document.querySelector('#view-settings').hidden`, 'settings in account menu');
  assert.equal(await evaluate(`document.querySelectorAll('.view:not([hidden])').length`), 1);
  await command('Page.reload');
  await until(`document.readyState==='complete' && document.querySelector('#view-settings')?.hidden===false`, 'settings reload');
  await destination('inbox');
  assert.equal((await api('/api/analyze/latest')).analysis_run_id, before, 'navigation must not organize');
}

async function shellChecks(label) {
  await navigationChecks();
  // URL-only input is a URL; prose containing a URL remains original note text.
  const text = `Remember the ${label} workshop: https://smoke.example/article`;
  await write(text);
  await evaluate(`document.querySelector('#noteText').focus()`);
  await command('Input.dispatchKeyEvent', {type:'keyDown',key:'Enter',code:'Enter',modifiers:2});
  await command('Input.dispatchKeyEvent', {type:'keyUp',key:'Enter',code:'Enter',modifiers:2});
  await organized();
  const note = (await api(`/api/items?q=${encodeURIComponent(text)}&limit=500`)).find(row => row.raw_text === text);
  assert(note && note.item_type === 'text');
  await write('https://smoke.example/article');
  await save();
  const urls = await api('/api/items?item_type=url&limit=500');
  assert(urls.some(row => row.source_url === 'https://smoke.example/article' && row.processing_status === 'ready'));

  // A failed save preserves both text and a staged attachment; retry uses Save.
  const failedText = `Preserve my ${label} draft after a failed save`;
  await fileInput(['workshop.png']);
  await write(failedText);
  await evaluate(`window.__smokeFetch=window.fetch;window.fetch=async(...args)=>{
    if(new URL(String(args[0]),location.href).pathname==='/api/items' && args[1]?.method==='POST') {
      window.fetch=window.__smokeFetch;
      return new Response(JSON.stringify({detail:'Fixture: temporary save failure'}),{status:503,headers:{'Content-Type':'application/json'}});
    }
    return window.__smokeFetch(...args);
  }`);
  await click('#addNoteButton');
  await until(`!document.querySelector('#addNoteButton').disabled && document.querySelector('#noteStatus').classList.contains('error')`, 'failed save feedback');
  assert.equal(await evaluate(`document.querySelector('#noteText').value`), failedText);
  assert.equal(await evaluate(`document.querySelector('#attachmentPreviews').children.length`), 1);
  await screenshot(`after-${label}-capture-retry`);
  await evaluate(`window.fetch=window.__smokeFetch`);
  await save();

  await fileInput(['workshop.pdf', 'workshop.wav']);
  await putImage('drop');
  await putImage('paste');
  assert.equal(await evaluate(`document.querySelector('#attachmentPreviews').children.length`), 4);
  await evaluate(`Array.from(document.querySelectorAll('#attachmentPreviews button')).find(button=>button.getAttribute('aria-label')==='Remove paste-workshop.png').click()`);
  assert.equal(await evaluate(`document.querySelector('#attachmentPreviews').children.length`), 3);
  await screenshot(`after-${label}-composer-attachments`);
  await save();
  for (const kind of ['pdf', 'image', 'voice']) {
    const rows = await api(`/api/items?item_type=${kind}&limit=500`);
    assert(rows.some(row => row.processing_status === 'ready'), `saved ${kind}`);
  }

  await click('#recordVoiceButton');
  await until(`!document.querySelector('#stopRecordingButton').hidden`, 'voice started');
  await new Promise(resolve => setTimeout(resolve, 600));
  await click('#stopRecordingButton');
  await until(`document.querySelector('#attachmentPreviews audio') && !document.querySelector('#addNoteButton').disabled`, 'voice review');
  assert.match(await evaluate(`document.querySelector('#voiceStatus').textContent`), /ready/i);
  await screenshot(`after-${label}-voice-review`);
  await save();
  await click('#recordVoiceButton');
  await until(`!document.querySelector('#cancelRecordingButton').hidden`, 'voice restart');
  await click('#cancelRecordingButton');
  await until(`document.querySelector('#voiceStatus').textContent.includes('cancelled')`, 'voice cancel');
  assert.equal(await evaluate(`document.querySelector('#attachmentPreviews').children.length`), 0);
  await layout(label);
}

async function itemDialog(itemId) {
  await click(`[data-item-id="${itemId}"]`);
  await until(`document.querySelector('#itemDetail')?.open && document.querySelector('#detailOriginal') && new URLSearchParams(location.search).get('item')===${JSON.stringify(String(itemId))}`, 'item details');
}
async function closeItem() {
  await click('#closeItemDetail');
  await until(`!document.querySelector('#itemDetail').open && !new URLSearchParams(location.search).has('item')`, 'close item details');
}
async function filterItems(values) {
  if(Object.keys(values).some(selector=>selector!=='#itemSearch')) {
    if(await evaluate(`!!document.querySelector('#extraFilters') && !document.querySelector('#extraFilters').open`)) await click('#toggleFilters');
  }
  await evaluate(`(()=>{
    const values=${JSON.stringify(values)};
    for(const [selector,value] of Object.entries(values)) document.querySelector(selector).value=value;
    document.querySelector('#itemSearchForm').requestSubmit();
  })()`);
  await until(`!itemPagination.listRequestInFlight`, 'filtered collection');
}
async function inboxChecks(label) {
  await destination('inbox');
  const runBeforeBrowsing = (await api('/api/analyze/latest')).analysis_run_id;
  await filterItems({'#itemSearch':'colleague','#itemTypeFilter':'text'});
  await until(`document.querySelectorAll('#notes [data-item-id]').length>0 && new URLSearchParams(location.search).get('q')==='colleague'`, 'compact filtered rows');
  assert.equal(await evaluate(`document.querySelector('#dashboard')?.offsetHeight || 0`), 0, 'Inbox has one collection, not repeated dashboard lists');
  const chosen = await evaluate(`(()=>{
    const rows=[...document.querySelectorAll('#notes [data-item-id]')];
    const selected=rows[Math.min(12,rows.length-1)];
    selected.scrollIntoView({block:'center'});
    return Number(selected.dataset.itemId);
  })()`);
  const position = await evaluate(`scrollY`);
  await itemDialog(chosen);
  assert.equal(await evaluate(`document.querySelector('#detailOriginal').textContent.includes('Call colleague')`), true);
  assert.equal(await evaluate(`document.querySelector('#itemDetail').contains(document.activeElement)`), true, 'focus enters details');
  await namedInputs('#itemDetail');
  const bounds = await evaluate(`(()=>{const r=document.querySelector('#itemDetail').getBoundingClientRect();return {x:r.x,width:r.width,height:r.height,viewportWidth:innerWidth,viewportHeight:innerHeight}})()`);
  assert(bounds.x>=-1 && bounds.x+bounds.width<=bounds.viewportWidth+1, 'details fit viewport');
  if(label==='mobile') assert(bounds.width >= bounds.viewportWidth-20, 'mobile details use available screen width');

  // Native dialog focus stays within details when tabbing through its controls.
  for(let press=0;press<14;press++) {
    await command('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
    await command('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
    const focus=await evaluate(`({inside:document.querySelector('#itemDetail').contains(document.activeElement),tag:document.activeElement.tagName,id:document.activeElement.id,text:document.activeElement.textContent.slice(0,80)})`);
    assert.equal(focus.inside,true,`dialog keyboard focus is contained: ${JSON.stringify(focus)}`);
  }
  await evaluate(`history.back()`);
  await until(`!document.querySelector('#itemDetail').open && !new URLSearchParams(location.search).has('item')`, 'Back closes detail');
  assert.equal(await evaluate(`document.querySelector('#itemSearch').value`),'colleague');
  assert(Math.abs((await evaluate(`scrollY`))-position)<10,'Back preserves list position');
  await evaluate(`history.forward()`);
  await until(`document.querySelector('#itemDetail').open`, 'Forward reopens detail');
  await command('Page.reload');
  await until(`document.readyState==='complete' && document.querySelector('#itemDetail')?.open && document.querySelector('#detailTitle')?.value`, 'detail deep link reload');
  assert.equal((await api('/api/analyze/latest')).analysis_run_id,runBeforeBrowsing,'browsing details does not organize');

  const originalTasks = (await allTasks()).filter(task=>task.source_item_ids.includes(chosen));
  assert(originalTasks.length>0,'fixture obligation has a durable linked task');
  assert.equal(await evaluate(`document.querySelector('#detailLinkedTasks').textContent.includes(${JSON.stringify(originalTasks[0].text)})`),true,'details show saved linked task');
  await click('#detailLinkedTasks button');
  await until(`new URLSearchParams(location.search).get('view')==='tasks' && new URLSearchParams(location.search).get('task')===${JSON.stringify(String(originalTasks[0].id))} && !document.querySelector('#view-tasks').hidden`, 'linked task navigation');
  await evaluate(`history.back()`);
  await until(`document.querySelector('#itemDetail')?.open && !!document.querySelector('#detailTitle')`, 'return from linked task');
  assert.equal((await api('/api/analyze/latest')).analysis_run_id,runBeforeBrowsing,'source navigation does not organize');
  const selectedTopic = await evaluate(`(()=>{
    const select=document.querySelector('#detailTopic');
    const option=[...select.options].find(option=>option.value && option.value!==select.value);
    select.value=option.value;return Number(option.value);
  })()`);
  const correctedTitle = `Workshop follow-up — ${label}`;
  await evaluate(`document.querySelector('#detailTitle').value=${JSON.stringify(correctedTitle)};document.querySelector('#detailPriority').value='Low'`);
  await click('#saveItemCorrections');
  await until(`(async()=>{const response=await fetch('/api/items/${chosen}/insight');const value=await response.json();return value.user_title===${JSON.stringify(correctedTitle)} && value.user_priority==='Low' && value.effective_topic_id===${selectedTopic}})()`, 'durable correction saved');
  await api(`/api/items/${chosen}/invalidate-analysis`,{method:'POST'});
  const delta = await api('/api/analyze',{method:'POST'});
  assert.deepEqual(delta.metrics.target_ids,[chosen]);
  const insight = await api(`/api/items/${chosen}/insight`);
  assert.equal(insight.effective_title,correctedTitle);
  assert.equal(insight.effective_topic_id,selectedTopic);
  assert.equal(insight.effective_priority,'Low');
  const retainedTasks = (await allTasks()).filter(task=>task.source_item_ids.includes(chosen));
  assert.deepEqual(retainedTasks.map(task=>[task.id,task.status,task.due_on]),originalTasks.map(task=>[task.id,task.status,task.due_on]));
  await command('Page.reload');
  await until(`document.querySelector('#itemDetail')?.open && document.querySelector('#detailTitle')?.value===${JSON.stringify(correctedTitle)}`, 'corrections persist after reload and re-analysis');
  assert.equal(await evaluate(`document.querySelector('#detailPriority').value`),'Low');
  assert.equal(await evaluate(`document.querySelector('#detailTopic').value`),String(selectedTopic));
  await screenshot(`after-${label}-item-details`);

  // Editing the original is a distinct action from correcting presentation.
  await click('#editSourceButton');
  await until(`!!document.querySelector('#detailSourceText')`, 'source editor');
  const changedSource = await evaluate(`document.querySelector('#detailSourceText').value`)+ '!';
  await evaluate(`document.querySelector('#detailSourceText').value=${JSON.stringify(changedSource)}`);
  await click('#saveSourceButton');
  await until(`(async()=>{const item=await(await fetch('/api/items/${chosen}')).json();return item.raw_text===${JSON.stringify(changedSource)}})()`, 'source correction saved');
  await organized();
  assert.equal((await api(`/api/items/${chosen}/insight`)).effective_title,correctedTitle);
  const afterSourceEdit = (await allTasks()).filter(task=>task.source_item_ids.includes(chosen));
  assert.deepEqual(afterSourceEdit.map(task=>[task.id,task.status,task.due_on]),originalTasks.map(task=>[task.id,task.status,task.due_on]));
  await closeItem();
  assert.equal(await evaluate(`document.querySelector('#itemSearch').value`),'colleague');
  await filterItems({'#itemSearch':'','#itemTypeFilter':'','#topicFilter':String(selectedTopic),'#priorityFilter':'Low'});
  await until(`!!document.querySelector('[data-item-id="${chosen}"]')`, 'corrected topic and priority filters');
  await screenshot(`after-${label}-inbox-filtered`);

  // Files and media retain an original source and separate editable extraction.
  for(const kind of ['pdf','image','voice']) {
    await filterItems({'#itemSearch':'','#itemTypeFilter':kind,'#topicFilter':'','#priorityFilter':''});
    const file = (await api(`/api/items?item_type=${kind}&limit=500`)).at(-1);
    await until(`!!document.querySelector('[data-item-id="${file.id}"]')`, `${kind} row`);
    await itemDialog(file.id);
    assert.equal(await evaluate(`!!document.querySelector('#itemDetail a[href*="/api/items/${file.id}/file"]')`),true,`${kind} original link`);
    if(kind==='image') assert.equal(await evaluate(`!!document.querySelector('#itemDetail img')`),true,'image preview');
    if(kind==='voice') assert.equal(await evaluate(`!!document.querySelector('#itemDetail audio[controls]')`),true,'voice playback');
    await until(`!!document.querySelector('#detailExtracted')`, `${kind} extraction control`);
    if(kind==='pdf') {
      const corrected = `Workshop reference corrected for ${label}. The Cedar room is upstairs.`;
      await evaluate(`document.querySelector('#detailExtracted').value=${JSON.stringify(corrected)}`);
      await click('#saveExtractedText');
      await until(`(async()=>{const item=await(await fetch('/api/items/${file.id}')).json();return item.extracted_text===${JSON.stringify(corrected)}})()`, 'extracted-text correction persists');
      await organized();
    }
    await closeItem();
  }

  await filterItems({'#itemSearch':'Call the dentist','#itemTypeFilter':'text','#topicFilter':'','#priorityFilter':''});
  const duplicates=await api('/api/items?q=Call%20the%20dentist&limit=500');
  const relatedSource=duplicates.find(item=>item.raw_text==='Call the dentist');
  await until(`!!document.querySelector('[data-item-id="${relatedSource.id}"]')`, 'related-source fixture row');
  await itemDialog(relatedSource.id);
  assert.equal(await evaluate(`!!document.querySelector('#detailRelatedItems button')`),true,'related captures remain discoverable');
  await click('#detailRelatedItems button');
  await until(`new URLSearchParams(location.search).get('item')!==${JSON.stringify(String(relatedSource.id))} && document.querySelector('#detailOriginal')`, 'related capture navigation');
  await closeItem();

  // The fixture initially fails extraction and succeeds when explicitly retried.
  const failureUrl=`https://smoke.example/fail-${label}`;
  await write(failureUrl);
  await save();
  await filterItems({'#itemSearch':'','#itemTypeFilter':'','#processingStatusFilter':'failed'});
  const failures = await api('/api/items?processing_status=failed&limit=500');
  const failed = failures.find(item=>item.source_url===failureUrl);
  assert(failed,'failed capture remains visible and saved');
  {
    await until(`!!document.querySelector('[data-item-id="${failed.id}"]')`, 'failed capture indicator');
    await itemDialog(failed.id);
    await click('#retryItemButton');
    await until(`(async()=>{const item=await(await fetch('/api/items/${failed.id}')).json();return item.processing_status==='ready'})()`, 'processing retry recovers original');
    await organized();
    assert.equal((await api(`/api/items/${failed.id}`)).source_url,failed.source_url);
    await closeItem();
  }
  await filterItems({'#itemTypeFilter':'','#processingStatusFilter':''});
  await layout(label);
}

async function visibleTaskIds() {
  return evaluate(`Array.from(document.querySelectorAll('#tasks [data-task-id]')).map(row=>Number(row.dataset.taskId))`);
}
async function taskView(view) {
  await click(`#taskViewNav [data-task-view="${view}"]`);
  await until(`(()=>{const button=document.querySelector('#taskViewNav [data-task-view="${view}"]');return button.getAttribute('aria-pressed')==='true' || button.getAttribute('aria-selected')==='true' || button.hasAttribute('aria-current')})()`, `${view} task view selected`);
}
async function taskFilter(value, search='') {
  await evaluate(`document.querySelector('#taskFilter').value=${JSON.stringify(value)};document.querySelector('#taskSearch').value=${JSON.stringify(search)};document.querySelector('#taskSearch').form.requestSubmit()`);
}
async function waitTask(id, condition, label) {
  for(let attempt=0;attempt<100;attempt++) {
    const task=(await allTasks()).find(task=>task.id===id);
    if(task && condition(task)) return task;
    await new Promise(resolve=>setTimeout(resolve,100));
  }
  throw new Error(`Timed out: ${label}`);
}
async function openTask(id) {
  await click(`[data-open-task="${id}"]`);
  await until(`document.querySelector('#taskDetail')?.open && !!document.querySelector('#taskEditText')`, 'task details');
}
async function closeTask() {
  if(await evaluate(`document.querySelector('#taskDetail')?.open`)) {
    await click('#closeTaskDetail');
    await until(`!document.querySelector('#taskDetail').open`, 'close task detail');
  }
}
async function setTaskDate(value) {
  if(!await evaluate(`!!document.querySelector('#taskDetail .deadline-editor')`)) await click('#editTaskDeadline');
  await until(`!!document.querySelector('#taskDetail .deadline-editor input')`, 'deadline editor');
  await evaluate(`(()=>{const form=document.querySelector('#taskDetail .deadline-editor');form.querySelector('input').value=${JSON.stringify(value)};form.requestSubmit()})()`);
}
async function tasksChecks(label) {
  // Each viewport gets an unresolved-date/failure fixture even after the prior
  // viewport deliberately corrected those records. These are explicit captures,
  // not organization triggered by browsing.
  if(!(await allTasks()).some(task=>task.status==='open' && task.deadline_needs_review)) {
    await api('/api/items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({raw_text:`Book the ${label} inspection tomorrow at 10am`})});
    await api('/api/analyze',{method:'POST'});
  }
  if(!(await api('/api/items?processing_status=failed&limit=500')).length) {
    await api('/api/items/url',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:`https://smoke.example/fail-tasks-${label}`})});
  }
  await navigate('?view=tasks');
  const initialRecords=await allTasks();
  const initial=initialRecords.filter(task=>task.status==='open');
  const initialIds=initialRecords.map(task=>task.id).sort((a,b)=>a-b);
  const initialSourceIds=new Set(initialRecords.flatMap(task=>task.source_item_ids));
  const reference=(await api('/api/attention')).reference_date;
  const today=initial.find(task=>task.due_on===reference && !task.deadline_needs_review);
  const upcoming=initial.find(task=>task.due_on>reference && !task.deadline_needs_review);
  const overdue=initial.find(task=>task.due_on && task.due_on<reference);
  const undated=initial.find(task=>!task.due_on && !task.deadline_needs_review);
  const uncertain=initial.find(task=>task.deadline_needs_review);
  assert(today && upcoming && overdue && undated && uncertain,'representative task fixture categories');
  const sourceTaskIds=initial.filter(task=>task.source_item_ids.some(id=>today.source_item_ids.includes(id))).map(task=>task.id).sort((a,b)=>a-b);
  const beforeReads=(await api('/api/analyze/latest')).analysis_run_id;

  await taskView('all');
  await taskFilter('all');
  await until(`document.querySelectorAll('#tasks [data-task-id]').length===${Math.min(40,initial.length)}`, 'first task page');
  const firstCount=(await visibleTaskIds()).length;
  for(let page=0;page<30;page++) {
    const more=await evaluate(`!!document.querySelector('#taskLoadMore') && !document.querySelector('#taskLoadMore').hidden`);
    if(!more) break;
    const count=(await visibleTaskIds()).length;
    await click('#taskLoadMore');
    await until(`document.querySelectorAll('#tasks [data-task-id]').length>${count}`, 'load another task page');
  }
  const allVisible=await visibleTaskIds();
  assert.equal(new Set(allVisible).size,allVisible.length,'task pages do not duplicate rows');
  assert(initial.every(task=>allVisible.includes(task.id)),'All tasks are reachable beyond the first page');
  await screenshot(`after-${label}-tasks-all`);
  console.log(JSON.stringify({viewport:label,taskPagination:{firstPage:firstCount,reachable:allVisible.length,savedOpenTasks:initial.length}}));

  await taskView('today');
  await taskFilter('all');
  await until(`!!document.querySelector('#tasks [data-task-id="${today.id}"]') && !document.querySelector('#tasks [data-task-id="${upcoming.id}"]')`, 'Today uses saved dates');
  await screenshot(`after-${label}-tasks-today`);
  await taskView('upcoming');
  await until(`!!document.querySelector('#tasks [data-task-id="${upcoming.id}"]') && !document.querySelector('#tasks [data-task-id="${today.id}"]') && !document.querySelector('#tasks [data-task-id="${overdue.id}"]')`, 'Upcoming keeps future occurrences separate');
  await taskView('all');
  for(const [filter,required,allowed] of [
    ['overdue',overdue,initial.filter(task=>task.due_on && task.due_on<reference)],
    ['undated',undated,initial.filter(task=>!task.due_on)],
    ['review',uncertain,initial.filter(task=>task.deadline_needs_review || task.source_changed)],
  ]) {
    await taskFilter(filter);
    const ids=allowed.map(task=>task.id);
    await until(`!!document.querySelector('#tasks [data-task-id="${required.id}"]') && Array.from(document.querySelectorAll('#tasks [data-task-id]')).every(row=>${JSON.stringify(ids)}.includes(Number(row.dataset.taskId)))`, `${filter} task filter`);
  }
  assert.equal((await api('/api/analyze/latest')).analysis_run_id,beforeReads,'task browsing and filtering do not organize');

  await taskFilter('all',today.text);
  await until(`!!document.querySelector('[data-task-complete="${today.id}"]') && !document.querySelector('[data-task-complete="${today.id}"]').disabled`, 'completion control');
  await click(`[data-task-complete="${today.id}"]`);
  await waitTask(today.id,task=>task.status==='completed','task completed');
  await until(`!!document.querySelector('#undoTaskAction')?.onclick && !document.querySelector('#taskActionNotice').hidden`, 'completion undo offered');
  await click('#undoTaskAction');
  await waitTask(today.id,task=>task.status==='open','completion undone');
  await until(`!!document.querySelector('[data-task-complete="${today.id}"]') && !document.querySelector('[data-task-complete="${today.id}"]').disabled && !document.querySelector('[data-task-complete="${today.id}"]').checked`, 'task returned after undo');
  await click(`[data-task-complete="${today.id}"]`);
  await waitTask(today.id,task=>task.status==='completed','completed task persisted');
  await taskFilter('completed',today.text);
  await until(`!!document.querySelector('[data-task-complete="${today.id}"]') && !document.querySelector('[data-task-complete="${today.id}"]').disabled`, 'completed task view');
  await click(`[data-task-complete="${today.id}"]`);
  await waitTask(today.id,task=>task.status==='open','completed task reopened');
  await taskFilter('all',today.text);
  await until(`!!document.querySelector('[data-open-task="${today.id}"]')`, 'task editing reachable');
  await openTask(today.id);
  await namedInputs('#taskDetail');
  for(let press=0;press<16;press++) {
    await command('Input.dispatchKeyEvent',{type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
    await command('Input.dispatchKeyEvent',{type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
    assert.equal(await evaluate(`document.querySelector('#taskDetail').contains(document.activeElement)`),true,'task modal keyboard focus stays inside');
  }
  const title=`Follow up on the ${label} workshop`;
  await evaluate(`document.querySelector('#taskEditText').value=${JSON.stringify(title)};document.querySelector('#taskEditPriority').value='High'`);
  await click('#saveTaskCorrections');
  await waitTask(today.id,task=>task.text===title && task.priority==='High','task corrections saved');
  await api(`/api/items/${today.source_item_ids[0]}/invalidate-analysis`,{method:'POST'});
  await api('/api/analyze',{method:'POST'});
  let corrected=(await allTasks()).find(task=>task.id===today.id);
  assert.equal(corrected.text,title);
  assert.equal(corrected.priority,'High');
  assert.equal(corrected.due_on,today.due_on);
  const afterReanalysis=await allTasks();
  assert(initialIds.every(id=>afterReanalysis.some(task=>task.id===id)),'re-analysis preserves every existing task identity');
  for(const task of afterReanalysis.filter(task=>!initialIds.includes(task.id))) {
    assert(task.source_item_ids.length && task.source_item_ids.every(id=>!initialSourceIds.has(id)),'new task identities must belong to a genuinely new source, not duplicate an already processed source');
  }
  assert.deepEqual(afterReanalysis.filter(task=>task.source_item_ids.some(id=>today.source_item_ids.includes(id))).map(task=>task.id).sort((a,b)=>a-b),sourceTaskIds,'re-analysis does not duplicate the corrected obligation');

  await setTaskDate('2026-11-08');
  await waitTask(today.id,task=>task.due_on==='2026-11-08' && task.deadline_status==='edited','explicit deadline saved');
  await api(`/api/items/${today.source_item_ids[0]}/invalidate-analysis`,{method:'POST'});
  await api('/api/analyze',{method:'POST'});
  corrected=(await allTasks()).find(task=>task.id===today.id);
  assert.equal(corrected.due_on,'2026-11-08');
  const preview=await api('/api/calendar/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:today.id})});
  assert.equal(preview.start_date,'2026-11-08','calendar uses corrected saved deadline');
  await evaluate(`Array.from(document.querySelector('#taskDetail').querySelectorAll('button')).find(button=>button.textContent==='Preview calendar').click()`);
  await until(`document.querySelector('#calendarDialog').open && document.querySelector('#calendarPreview').textContent.includes('2026-11-08')`, 'calendar preview from task detail');
  await screenshot(`after-${label}-task-calendar`);
  await evaluate(`Array.from(document.querySelector('#calendarDialog').querySelectorAll('button')).find(button=>button.textContent==='Close').click()`);
  await until(`!document.querySelector('#calendarDialog').open`, 'return from calendar preview');
  await screenshot(`after-${label}-task-details`);
  if(!await evaluate(`!!document.querySelector('#taskDetail .deadline-editor')`)) await click('#editTaskDeadline');
  await until(`!!document.querySelector('#taskDetail .deadline-editor')`, 'clear deadline control');
  await evaluate(`Array.from(document.querySelector('#taskDetail .deadline-editor').querySelectorAll('button')).find(button=>button.textContent==='Clear deadline').click()`);
  await waitTask(today.id,task=>task.due_on===null && task.deadline_status==='cleared','deadline cleared');
  await api(`/api/items/${today.source_item_ids[0]}/invalidate-analysis`,{method:'POST'});
  await api('/api/analyze',{method:'POST'});
  corrected=(await allTasks()).find(task=>task.id===today.id);
  assert.equal(corrected.due_on,null);
  assert.equal(corrected.deadline_status,'cleared');
  assert.equal(corrected.text,title);

  await click('#taskSources button[data-item-id]');
  await until(`document.querySelector('#itemDetail')?.open && !!document.querySelector('#detailOriginal')`, 'inspect original task source');
  assert.equal(await evaluate(`document.querySelector('#itemDetail').contains(document.activeElement)`),true,'source inspection focuses the top modal');
  await evaluate(`history.back()`);
  await until(`document.querySelector('#taskDetail')?.open && !document.querySelector('#itemDetail').open && !!document.querySelector('#taskEditText')`, 'Back returns to task details');
  await closeTask();

  await taskFilter('review',uncertain.text);
  await until(`!!document.querySelector('[data-open-task="${uncertain.id}"]')`, 'uncertain date remains reviewable');
  await openTask(uncertain.id);
  assert.match(await evaluate(`document.querySelector('#taskDetail').textContent`),/review|uncertain|exact time/i);
  await setTaskDate('2026-11-09');
  await waitTask(uncertain.id,task=>task.due_on==='2026-11-09' && !task.deadline_needs_review,'uncertain date deliberately corrected');
  await closeTask();
  await taskFilter('all',upcoming.text);
  await until(`!!document.querySelector('[data-open-task="${upcoming.id}"]')`, 'task dismissal is discoverable');
  await openTask(upcoming.id);
  await evaluate(`Array.from(document.querySelector('#taskDetail').querySelectorAll('button')).find(button=>button.textContent==='Dismiss task').click()`);
  await waitTask(upcoming.id,task=>task.status==='dismissed','task dismissed');
  await until(`document.querySelector('#taskActionMessage').textContent.includes('dismissed') && !document.querySelector('#taskActionNotice').hidden`, 'dismissal undo offered');
  await click('#undoTaskAction');
  await waitTask(upcoming.id,task=>task.status==='open','dismissal undone');
  await until(`!!document.querySelector('#taskDetail button') && !pendingTaskActions.has(${upcoming.id})`, 'dismissal undo finished');
  await evaluate(`Array.from(document.querySelector('#taskDetail').querySelectorAll('button')).find(button=>button.textContent==='Dismiss task').click()`);
  await waitTask(upcoming.id,task=>task.status==='dismissed','dismissed state saved');
  await closeTask();
  await taskFilter('dismissed',upcoming.text);
  await until(`!!document.querySelector('[data-open-task="${upcoming.id}"]')`, 'dismissed filter');
  await openTask(upcoming.id);
  await evaluate(`Array.from(document.querySelector('#taskDetail').querySelectorAll('button')).find(button=>button.textContent==='Restore task').click()`);
  await waitTask(upcoming.id,task=>task.status==='open' && task.due_on===upcoming.due_on,'dismissed task restored with saved date');
  await closeTask();
  await taskFilter('all','');
  for(let index=0;index<2;index++) await api('/api/items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({raw_text:`Call the ${label} reviewer`})});
  await api('/api/analyze',{method:'POST'});
  // Direct API fixtures bypass the composer's normal post-organization refresh.
  await evaluate(`loadFocusedReviews()`);
  await click('#taskReviewButton');
  await until(`document.querySelector('#reviewDialog').open && !!document.querySelector('#focusedReviews [data-review-item]')`, 'focused review');
  const reviews=await api('/api/analysis/reviews');
  const duplicate=reviews.find(review=>review.status==='pending' && review.kind==='possible_duplicate');
  assert(duplicate,'duplicate fixture needs a review decision');
  await until(`!!document.querySelector('#focusedReviews [data-review-item="${duplicate.item_id}"]')`, 'duplicate review shows source context');
  const beforeReview=(await allTasks()).map(task=>task.id).sort((a,b)=>a-b);
  await evaluate(`(()=>{const card=document.querySelector('#focusedReviews [data-review-item="${duplicate.item_id}"]');Array.from(card.querySelectorAll('button')).find(button=>/Keep separate|Keep both/i.test(button.textContent)).click()})()`);
  await until(`(async()=>{const rows=await(await fetch('/api/analysis/reviews')).json();return rows.find(row=>row.id===${duplicate.id}).status!=='pending'})()`, 'duplicate review decision saved');
  assert.deepEqual((await allTasks()).map(task=>task.id).sort((a,b)=>a-b),beforeReview,'review never deletes or merges historical task identities');
  const failed=(await api('/api/items?processing_status=failed&limit=500'))[0];
  if(failed) {
    await until(`!!document.querySelector('#focusedReviews [data-review-item="${failed.id}"]')`, 'processing failure in focused review');
    await evaluate(`(()=>{const card=document.querySelector('#focusedReviews [data-review-item="${failed.id}"]');Array.from(card.querySelectorAll('button')).find(button=>/Retry/i.test(button.textContent)).click()})()`);
    await until(`(async()=>{const item=await(await fetch('/api/items/${failed.id}')).json();return item.processing_status==='ready'})()`, 'focused processing retry recovers original');
  }
  await screenshot(`after-${label}-focused-review`);
  await evaluate(`Array.from(document.querySelector('#reviewDialog').querySelectorAll('button')).find(button=>button.textContent==='Close').click()`);
  await until(`!document.querySelector('#reviewDialog').open`, 'close focused review');
  await taskFilter('all','');
  await layout(label);
}

async function askQuestion(question) {
  await evaluate(`document.querySelector('#askQuestion').value=${JSON.stringify(question)};document.querySelector('#askQuestion').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#askForm').requestSubmit()`);
  await until(`!document.querySelector('#askButton').disabled && !!document.querySelector('#askOutput .answer-body') && !document.querySelector('#askStatus').classList.contains('error')`, 'answer completed');
}
async function askChecks(label) {
  await destination('ask');
  const question='What did I save about the Cedar room?';
  await askQuestion(question);
  await until(`document.querySelector('#askOutput .answer-body').textContent.includes('Cedar') && !!document.querySelector('#askOutput [data-citation-item]')`, 'grounded readable answer and citations');
  const answer=await evaluate(`document.querySelector('#askOutput .answer-body').textContent`);
  const paragraphs=await evaluate(`Array.from(document.querySelectorAll('#askOutput .answer-body p')).map(node=>node.textContent.trim()).filter(Boolean)`);
  assert.equal(new Set(paragraphs).size,paragraphs.length,'readable answer does not repeat identical passages');
  if(stage==='ask') assert.equal((answer.match(/Cedar room is on the second floor/g)||[]).length,1,'duplicate captures contribute provenance without repeating the same answer sentence');
  const citation=await evaluate(`Number(document.querySelector('#askOutput [data-citation-item]').dataset.citationItem)`);
  const reads={...requestCounts};
  await screenshot(`after-${label}-ask`);
  await click('#askOutput [data-citation-item]');
  await until(`document.querySelector('#itemDetail')?.open && document.querySelector('#detailOriginal') && new URLSearchParams(location.search).get('item')===${JSON.stringify(String(citation))}`, 'answer citation opens matching saved item');
  assert.equal(await evaluate(`document.querySelector('#itemDetail').contains(document.activeElement)`),true);
  await screenshot(`after-${label}-ask-source`);
  await evaluate(`history.back()`);
  await until(`!document.querySelector('#itemDetail').open && !document.querySelector('#view-ask').hidden`, 'Back returns to answer');
  assert.equal(await evaluate(`document.querySelector('#askQuestion').value`),question);
  assert.equal(await evaluate(`document.querySelector('#askOutput .answer-body').textContent`),answer);
  await command('Page.reload');
  await until(`document.readyState==='complete' && document.querySelector('#askOutput .answer-body')?.textContent.includes('Cedar') && document.querySelector('#authUser').textContent.includes('Local Owner')`, 'owner-matched cached answer reload');
  assert.equal(await evaluate(`document.querySelector('#askQuestion').value`),question);
  assert.deepEqual(requestCounts,reads,'citation/history/reload do not call Ask or organization');

  const failedQuestion=`What else should I remember about the ${label} workshop?`;
  await evaluate(`window.__smokeAskFetch=window.fetch;window.fetch=async(...args)=>{
    if(new URL(String(args[0]),location.href).pathname==='/api/ask' && args[1]?.method==='POST') {
      window.fetch=window.__smokeAskFetch;
      return new Response(JSON.stringify({detail:'Fixture: temporary answer failure'}),{status:503,headers:{'Content-Type':'application/json'}});
    }
    return window.__smokeAskFetch(...args);
  }`);
  await evaluate(`document.querySelector('#askQuestion').value=${JSON.stringify(failedQuestion)};document.querySelector('#askQuestion').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#askForm').requestSubmit()`);
  await until(`!document.querySelector('#askButton').disabled && document.querySelector('#askStatus').classList.contains('error')`, 'Ask failure feedback');
  assert.equal(await evaluate(`document.querySelector('#askQuestion').value`),failedQuestion,'failed Ask keeps question draft');
  assert.equal(await evaluate(`document.querySelector('#askOutput .answer-body').textContent`),answer,'failed Ask retains previous answer');
  await evaluate(`window.fetch=window.__smokeAskFetch`);
  await screenshot(`after-${label}-ask-retry`);
  await askQuestion('ZXQ2147QZ lunarpassphrase?');
  await until(`!document.querySelector('#askOutput [data-citation-item]')`, 'insufficient evidence has no invented sources');
  assert.match(await evaluate(`document.querySelector('#askOutput').textContent`),/No reliable answer|not enough|couldn.t find|insufficient/i);
  await askQuestion(question);
  await until(`!!document.querySelector('#askOutput [data-citation-item]')`, 'explicit Ask retry succeeds');
  await layout(label);
}
async function settings() {
  await click('#accountMenu summary');
  await click('#accountMenu a');
  await until(`!document.querySelector('#view-settings').hidden`, 'account settings');
}
async function ownerIsolationChecks() {
  // Runs last so switching to an empty owner cannot invalidate later fixtures.
  const before={...requestCounts};
  await settings();
  assert.equal(await evaluate(`document.querySelector('#devLoginPanel').hidden`),false,'local smoke switcher enabled only for all stage');
  await evaluate(`document.querySelector('#devLoginEmail').value='redesign-other@example.com';document.querySelector('#devLoginForm').requestSubmit()`);
  await until(`document.querySelector('#authUser').textContent.includes('redesign-other')`, 'switch to second owner');
  await destination('ask');
  assert.equal(await evaluate(`document.querySelector('#askQuestion').value`),'','second owner does not inherit question draft');
  assert.equal(await evaluate(`!!document.querySelector('#askOutput [data-citation-item]')`),false,'second owner does not inherit citations');
  assert(!await evaluate(`document.querySelector('#askOutput').textContent.includes('Cedar')`),'second owner does not inherit answer');
  assert.deepEqual(requestCounts,before,'account/navigation changes do not query a model');
  await askQuestion('What did I save about the Cedar room?');
  assert.equal(await evaluate(`!!document.querySelector('#askOutput [data-citation-item]')`),false,'other owner cannot retrieve Local Owner sources');
  const afterExplicitQuestion={...requestCounts};
  await settings();
  await evaluate(`Array.from(document.querySelector('#view-settings').querySelectorAll('button')).find(button=>button.textContent==='Sign out').click()`);
  await until(`document.querySelector('#authUser').textContent.includes('Local Owner')`, 'restore local owner');
  await destination('ask');
  await until(`document.querySelector('#askOutput .answer-body')?.textContent.includes('Cedar')`, 'matching owner restores its own cached answer');
  assert.deepEqual(requestCounts,afterExplicitQuestion,'restoring owner cache makes no Ask/organizer call');
  passedChecks.push({viewport:'final',stage:'owner-isolation',checks:['owner-specific question/answer/citation cache','no automatic Ask on owner restore','retrieval isolation']});
}

try {
  await command('Runtime.enable');
  await command('Page.enable');
  await command('Browser.grantPermissions', {origin:base,permissions:['audioCapture']});
  await command('Network.enable');
  await command('Network.setBlockedURLs', {urls:['*fonts.googleapis.com*','*fonts.gstatic.com*']});
  await command('Emulation.setTimezoneOverride', {timezoneId:'Asia/Kolkata'});
  await command('Emulation.setEmulatedMedia', {features:[{name:'prefers-reduced-motion',value:'reduce'}]});
  if(stage==='ask') {
    await navigate();
    for(let index=0;index<3;index++) await api('/api/items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({raw_text:'Workshop location: the Cedar room is on the second floor.'})});
  }
  for (const [label,width,height,mobile] of [['desktop',1440,1000,false],['mobile',390,844,true]]) {
    if(viewport!=='both' && viewport!==label) continue;
    await command('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile});
    await navigate('?view=inbox', true);
    await screenshot(`before-${label}-inbox`);
    if(stage==='all') {
      await navigate('?view=tasks',true);
      await until(`!!document.querySelector('#tasks [data-task-id]')`, 'baseline saved tasks');
      await screenshot(`before-${label}-tasks`);
    }
    if(['ask','all'].includes(stage)) {
      await navigate('?view=ask',true);
      await evaluate(`document.querySelector('#askQuestion').value='What did I save about the Cedar room?';document.querySelector('#askForm').requestSubmit()`);
      await until(`!document.querySelector('#askButton').disabled && document.querySelector('#askOutput').textContent.includes('Cedar')`, 'baseline Ask answer');
      await screenshot(`before-${label}-ask`);
    }
    await navigate();
    await screenshot(`after-${label}-inbox`);
    if(['tasks','ask'].includes(stage)) await navigationChecks(); else await shellChecks(label);
    if(['inbox','all'].includes(stage)) await inboxChecks(label);
    if(['tasks','all'].includes(stage)) await tasksChecks(label);
    if(['ask','all'].includes(stage)) await askChecks(label);
    const checks=[
      ...(!['tasks','ask'].includes(stage)?['navigation/history/reload','capture text/URL/files/drop/paste/audio','record/review/cancel',
        'failed-save draft preservation','automatic incremental organization']:[]),
      ...(['inbox','all'].includes(stage)?['item detail/focus/history/scroll','durable corrections/reanalysis',
        'source/extraction editing','original files/media','failed extraction/retry']:[]),
      ...(['tasks','all'].includes(stage)?['task views/filters/pagination','completion/undo',
        'task/deadline corrections/reanalysis','source/history','dismiss/restore/undo','focused review/recovery']:[]),
      ...(['ask','all'].includes(stage)?['Ask/citations/history/cache','Ask failure draft/answer preservation','insufficient evidence','distinct readable answer paragraphs']:[]),
      'keyboard/layout'
    ];
    passedChecks.push({viewport:label,stage,checks});
    console.log(`PASS ${label} ${stage}: ${checks.join(', ')}`);
  }
  if(stage==='all') await ownerIsolationChecks();
  fs.writeFileSync(path.join(artifacts,'browser-results.json'),JSON.stringify({stage,passedChecks,requestCounts,browserErrors},null,2));
  assert.deepEqual(browserErrors, [], 'uncaught browser exceptions');
} catch (error) {
  await screenshot('failure');
  fs.writeFileSync(path.join(artifacts,'failure.json'),JSON.stringify({
    message:error.message, browserErrors,
    page:await evaluate(`({url:location.href,text:document.body.innerText.slice(0,16000),activeElement:{tag:document.activeElement.tagName,id:document.activeElement.id}})`)
  },null,2));
  throw error;
} finally {
  socket.close();
}
