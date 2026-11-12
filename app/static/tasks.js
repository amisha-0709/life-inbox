/* Saved task facts drive this collection; no date phrase is parsed in the UI. */
let savedTasks = [];
let taskReference = '';
let taskPageLimit = 40;
let taskReadRevision = 0;
let selectedTaskId = null;
let taskReturn = null;
let taskOwner = undefined;
let taskDataReady = false;
function dateLabel(iso) { if (!iso) return 'No date'; try { return new Intl.DateTimeFormat(undefined,{month:'short',day:'numeric',year:'numeric',timeZone:'UTC'}).format(new Date(`${iso}T12:00:00Z`)); } catch { return iso; } }
function taskNeedsReview(task) { return task.deadline_needs_review || task.source_changed || (task.deadline_status==='legacy' && !task.timing_confirmed); }
function taskIsSnoozed(task) { return task.snoozed_until && task.snoozed_until>taskReference; }
function taskControlsFromUrl() {
  const params=new URLSearchParams(location.search);
  const view=['today','upcoming','all'].includes(params.get('task_view'))?params.get('task_view'):'today';
  document.querySelectorAll('[data-task-view]').forEach(button=>{const active=button.dataset.taskView===view;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
  const filter=params.get('task_filter') || (['completed','dismissed'].includes(params.get('task_status'))?params.get('task_status'):'all');
  document.getElementById('taskFilter').value=['all','overdue','undated','review','completed','dismissed','snoozed'].includes(filter)?filter:'all';
  document.getElementById('taskSearch').value=params.get('task_q') || '';
  return {view,filter:document.getElementById('taskFilter').value,query:document.getElementById('taskSearch').value.toLocaleLowerCase()};
}
function setTaskView(view) { updateUrl({view:'tasks',task_view:view,task_filter:'',task_status:'',task:''});taskPageLimit=40;renderTaskCollection(); }
function applyTaskFilters(event) {event?.preventDefault();const filter=document.getElementById('taskFilter').value;updateUrl({task_view:filter==='all'?new URLSearchParams(location.search).get('task_view') || 'today':'all',task_filter:filter==='all'?'':filter,task_q:document.getElementById('taskSearch').value.trim(),task_status:''});taskPageLimit=40;renderTaskCollection();}
async function loadSavedTasks() {
  const revision=++taskReadRevision;
  if(!taskDataReady)setStatus('taskStatus','Loading tasks…');
  try {
    const [tasks,board]=await Promise.all([fetchJson('/api/tasks?status=all',{},'Could not load tasks.'),fetchJson('/api/attention',{},'Could not load saved task dates.')]);
    if(revision!==taskReadRevision)return false;
    document.getElementById('retryTasks').hidden=true;if(document.getElementById('taskStatus').textContent==='Loading tasks…')setStatus('taskStatus','');
    savedTasks=tasks;taskDataReady=true;taskReference=board.reference_date;renderTaskCollection();
    if(new URLSearchParams(location.search).has('task')&&!new URLSearchParams(location.search).has('item'))syncTaskDetailFromUrl();
    return true;
  } catch(error){if(revision===taskReadRevision){setStatus('taskStatus',error.message,true);document.getElementById('retryTasks').hidden=false;}return false;}
}
function matchingTasks() {
  const {view,filter,query}=taskControlsFromUrl();
  return savedTasks.filter(task=>{
    if(query&&!task.text.toLocaleLowerCase().includes(query))return false;
    if(filter==='completed')return task.status==='completed';
    if(filter==='dismissed')return task.status==='dismissed';
    if(task.status!=='open')return view==='all'&&filter==='all'&&task.status==='completed';
    if(filter==='overdue')return task.due_on && task.due_on<taskReference;
    if(filter==='undated')return !task.due_on;
    if(filter==='review')return taskNeedsReview(task);
    if(filter==='snoozed')return taskIsSnoozed(task);
    if(view==='all')return true;
    if(taskIsSnoozed(task))return false;
    return view==='upcoming' ? task.due_on>taskReference : !!task.due_on && task.due_on<=taskReference;
  }).sort((a,b)=>(a.status==='open'?0:1)-(b.status==='open'?0:1)||(a.due_on||'9999').localeCompare(b.due_on||'9999')||a.id-b.id);
}
function renderTaskCollection() {
  const rows=matchingTasks();const root=document.getElementById('tasks');root.replaceChildren();
  document.getElementById('taskViewDate').textContent=`${dateLabel(taskReference)} · ${rows.length} task${rows.length===1?'':'s'}`;
  for(const task of rows.slice(0,taskPageLimit)) {
    const row=document.createElement('article');row.className=`task-row ${task.status==='completed'?'is-completed':''}`;row.dataset.taskId=task.id;
    const label=document.createElement('label');label.className='completion-target';
    const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.checked=task.status==='completed';checkbox.dataset.taskComplete=task.id;checkbox.setAttribute('aria-label',`${checkbox.checked?'Reopen':'Complete'} ${task.text}`);checkbox.disabled=pendingTaskActions.has(task.id);checkbox.onchange=()=>changeSavedTask(task,task.status==='completed'?'reopen':'complete');label.append(checkbox);
    const button=document.createElement('button');button.type='button';button.className='task-open';button.dataset.openTask=task.id;
    const text=document.createElement('strong');text.textContent=task.text;
    const meta=document.createElement('span');meta.className='item-meta';meta.textContent=[task.due_on?dateLabel(task.due_on):'No date',task.due_on<taskReference&&task.status==='open'?'Overdue':'',taskNeedsReview(task)?'Needs review':'',task.priority==='High'?'High priority':'',taskIsSnoozed(task)?'Snoozed':'',task.status==='dismissed'?'Dismissed':''].filter(Boolean).join(' · ');
    button.append(text,meta);button.onclick=()=>openTaskDetail(task.id,{trigger:button});row.append(label,button);root.append(row);
  }
  if(!rows.length){const empty=document.createElement('div');empty.className='empty-state';empty.textContent='No tasks in this view. Try All or another filter.';root.append(empty);}
  const more=document.getElementById('taskLoadMore');more.hidden=rows.length<=taskPageLimit;more.textContent=`Show more tasks (${Math.min(taskPageLimit,rows.length)} of ${rows.length})`;
}
function showMoreTasks(){taskPageLimit+=40;renderTaskCollection();}
async function changeSavedTask(task,action) {
  if(pendingTaskActions.has(task.id))return;
  pendingTaskActions.add(task.id);
  const ownerEpoch=window.lifeInboxOwnerEpoch;
  const hadDialogFocus=document.getElementById('taskDetail').contains(document.activeElement);
  const hadRowFocus=document.getElementById('tasks').contains(document.activeElement)||document.activeElement===document.getElementById('undoTaskAction');
  const oldIndex=[...document.querySelectorAll('#tasks [data-task-id]')].findIndex(row=>Number(row.dataset.taskId)===task.id);
  const position=scrollY;
  try {
    await fetchJson(`/api/tasks/${task.id}/${action}`,{method:'POST'},'Could not update this task.');invalidateCalendarPreview();
    await loadSavedTasks();await loadFocusedReviews();
    if(ownerEpoch!==window.lifeInboxOwnerEpoch)return false;
    const notice=document.getElementById('taskActionNotice');notice.hidden=false;
    document.getElementById('taskActionMessage').textContent=action==='complete'?'Task completed.':action==='dismiss'?'Task dismissed.':'Task reopened.';
    document.getElementById('undoTaskAction').onclick=async()=>{if(await changeSavedTask(savedTasks.find(row=>row.id===task.id)||task,action==='reopen'?'complete':'reopen'))notice.hidden=true;};
    if(document.getElementById('taskDetail').open&&selectedTaskId===task.id)renderTaskDetail(savedTasks.find(row=>row.id===task.id));
    return true;
  }catch(error){if(ownerEpoch===window.lifeInboxOwnerEpoch)setStatus('taskStatus',error.message,true);return false;}
  finally{pendingTaskActions.delete(task.id);if(ownerEpoch===window.lifeInboxOwnerEpoch){renderTaskCollection();if(hadDialogFocus&&document.getElementById('taskDetail').open)document.querySelector('#taskDetailActions button')?.focus({preventScroll:true});else if(hadRowFocus){const rows=[...document.querySelectorAll('#tasks [data-task-complete]')];const focus=document.querySelector(`[data-task-complete="${task.id}"]`)||rows[Math.max(0,Math.min(oldIndex,rows.length-1))]||document.getElementById('undoTaskAction');focus?.focus({preventScroll:true});window.scrollTo(0,position);}}}
}
async function openTaskDetail(id,{trigger=null,history=true}={}) {
  id=Number(id);if(!taskDataReady){await loadSavedTasks();}
  const task=savedTasks.find(task=>task.id===id);const dialog=document.getElementById('taskDetail');
  if(!task){setStatus('taskStatus','This task is unavailable in your account.',true);return;}
  if(!dialog.open)taskReturn={scroll:scrollY,trigger,id};
  if(history){const url=new URL(location.href);url.searchParams.set('task',id);const state={...window.history.state,taskDetailEntry:true,ownerId:window.lifeInboxOwnerId};dialog.open?window.history.replaceState(state,'',url):window.history.pushState(state,'',url);}
  selectedTaskId=id;if(!dialog.open)dialog.showModal();document.documentElement.classList.add('detail-open');renderTaskDetail(task);document.getElementById('taskDetailHeading').focus({preventScroll:true});
}
function closeTaskDetail(){if(window.history.state?.taskDetailEntry){window.history.back();return;}const url=new URL(location.href);url.searchParams.delete('task');window.history.replaceState({},'',url);finishClosingTask();}
function finishClosingTask(){document.getElementById('taskDetail').close();document.getElementById('calendarDialog').close();selectedTaskId=null;document.documentElement.classList.toggle('detail-open',!!document.querySelector('dialog[open]'));if(taskReturn){const trigger=taskReturn.trigger?.isConnected?taskReturn.trigger:document.querySelector(`[data-open-task="${taskReturn.id}"]`);trigger?.focus({preventScroll:true});window.scrollTo(0,taskReturn.scroll);}taskReturn=null;}
function syncTaskDetailFromUrl(){const params=new URLSearchParams(location.search);if(params.has('item'))return;const id=Number(params.get('task'));if(id){if(!document.getElementById('taskDetail').open||selectedTaskId!==id)openTaskDetail(id,{history:false});}else if(document.getElementById('taskDetail').open)finishClosingTask();}
function refreshOpenTaskDetail(){if(selectedTaskId&&document.getElementById('taskDetail').open){const task=savedTasks.find(row=>row.id===selectedTaskId);if(task)renderTaskDetail(task);}}
function renderTaskDetail(task) {
  if(!task)return;const root=document.getElementById('taskDetailBody');root.replaceChildren();document.getElementById('taskDetailHeading').textContent=task.text;
  const status=document.createElement('p');status.id='taskDetailStatus';status.className='status';status.setAttribute('role','status');root.append(status);
  const form=document.createElement('form');form.className='correction-form';form.innerHTML='<label>Task title<textarea id="taskEditText" rows="3" maxlength="2000" required></textarea></label><label>Priority<select id="taskEditPriority"><option>Low</option><option>Medium</option><option>High</option></select></label><p class="provenance">Changing the title keeps the saved deadline. Edit the date separately.</p><button id="saveTaskCorrections" type="submit">Save task</button>';
  form.querySelector('textarea').value=task.text;form.querySelector('select').value=task.priority;
  form.onsubmit=async event=>{event.preventDefault();if(form.querySelector('textarea').value.trim()===task.text && form.querySelector('select').value===task.priority){setStatus('taskDetailStatus','No changes to save.');return;}const button=form.querySelector('button');button.disabled=true;try{await fetchJson(`/api/tasks/${task.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({...((form.querySelector('textarea').value.trim()!==task.text)?{text:form.querySelector('textarea').value}:{}),...((form.querySelector('select').value!==task.priority)?{priority:form.querySelector('select').value}:{})})},'Could not save. Your edits are still here.');invalidateCalendarPreview();await loadSavedTasks();refreshOpenTaskDetail();setStatus('taskDetailStatus','Your task corrections are saved.');}catch(error){setStatus('taskDetailStatus',error.message,true);button.disabled=false;}};root.append(form);
  const date=detailSection('Deadline');const summary=document.createElement('p');summary.className='deadline-value';summary.textContent=`${dateLabel(task.due_on)} · ${deadlineStatusLabel(task)}`;date.append(summary);
  if(taskNeedsReview(task)){const warning=document.createElement('p');warning.className='notice';warning.textContent=task.deadline_note || 'The source or timing needs review. Choose the intended date; your saved deadline stays unchanged until you do.';date.append(warning);}
  const edit=actionButton('Edit deadline','secondary',()=>showDeadlineEditor(task,date,edit));edit.id='editTaskDeadline';date.append(edit);
  if(task.due_on&&!task.timing_confirmed&&!task.deadline_needs_review&&!hasUnreviewedSourceChange(task))date.append(actionButton('Confirm saved date','quiet',()=>confirmSavedDeadline(task)));
  if(task.due_on&&!task.deadline_needs_review&&!hasUnreviewedSourceChange(task))date.append(actionButton('Preview calendar','quiet',()=>{document.getElementById('calendarDialog').showModal();previewCalendarForTask(task);}));root.append(date);
  const source=detailSection('Source and timing','taskSources');
  if(task.ai_timing_text){const wording=document.createElement('p');wording.textContent=`Original timing: “${task.ai_timing_text}”`;source.append(wording);}
  if(task.deadline_reference_at && task.deadline_timezone){const reference=document.createElement('p');reference.className='provenance';try{const when=new Intl.DateTimeFormat(undefined,{dateStyle:'medium',timeStyle:'short',timeZone:task.deadline_timezone}).format(new Date(task.deadline_reference_at));reference.textContent=`${['edited','cleared'].includes(task.deadline_status)?'Your date choice was saved':'Timing reference'}: ${when} (${task.deadline_timezone.replaceAll('_',' ')}).`;}catch{reference.textContent='Inspect the source for its original timing context.';}source.append(reference);}
  if(task.source_changed){const warning=document.createElement('p');warning.className='notice';warning.textContent='The source changed. Your saved choices have been retained; inspect the source before changing them.';source.append(warning);}
  for(const id of task.source_item_ids){const button=actionButton('Open source capture','detail-link',()=>openSavedItem(id,{trigger:button}));button.dataset.itemId=id;source.append(button);fetchJson(`/api/items/${id}`,{},'Could not load source.').then(item=>{if(button.isConnected)button.textContent=savedItemTitle(item);}).catch(()=>{});}
  if(task.ai_text&&task.ai_text!==task.text){const inferred=document.createElement('p');inferred.className='provenance';inferred.textContent=`Suggested title: ${task.ai_text}`;source.append(inferred);}
  for(const detail of task.details||[]){const text=document.createElement('p');text.className='provenance';text.textContent=detail.text;source.append(text);}root.append(source);
  const actions=detailSection('Actions','taskDetailActions');actions.append(actionButton(task.status==='completed'?'Reopen task':'Complete task','secondary',()=>changeSavedTask(task,task.status==='completed'?'reopen':'complete')));
  if(task.status==='open'){
    const snooze=actionButton('Snooze until tomorrow','quiet',async()=>{const value=new Date(`${taskReference}T12:00:00Z`);value.setUTCDate(value.getUTCDate()+1);try{await fetchJson(`/api/tasks/${task.id}/snooze`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({until:value.toISOString().slice(0,10)})},'Could not snooze task.');await loadSavedTasks();refreshOpenTaskDetail();setStatus('taskDetailStatus','Snoozed until tomorrow.');}catch(error){setStatus('taskDetailStatus',error.message,true);}});actions.append(snooze);
    actions.append(actionButton('Dismiss task','quiet danger-text',()=>changeSavedTask(task,'dismiss')));
  }else if(task.status==='dismissed')actions.append(actionButton('Restore task','quiet',()=>changeSavedTask(task,'reopen')));
  root.append(actions);
}
async function loadFocusedReviews() {
  const ownerEpoch=window.lifeInboxOwnerEpoch;
  try {
    const [reviews,states,tasks,captureFailures]=await Promise.all([fetchJson('/api/analysis/reviews',{},'Could not load possible matches.'),fetchJson('/api/analysis/status',{},'Could not load processing state.'),fetchJson('/api/tasks?status=all',{},'Could not load task review.'),fetchJson('/api/items?processing_status=failed&limit=500',{},'Could not load capture failures.')]);
    if(ownerEpoch!==window.lifeInboxOwnerEpoch)return;
    const pending=reviews.filter(row=>row.status==='pending'),failed=states.filter(row=>['failed','blocked'].includes(row.status)),uncertain=tasks.filter(task=>task.status==='open'&&taskNeedsReview(task));const count=pending.length+failed.length+uncertain.length+captureFailures.length;
    for(const id of ['reviewInboxButton','taskReviewButton']){const button=document.getElementById(id);if(button){button.hidden=!count;button.textContent=`Review (${count})`;}}
    const root=document.getElementById('focusedReviews');root.replaceChildren();
    for(const task of uncertain){const card=detailSection(task.text);card.classList.add('review-card');const text=document.createElement('p');text.textContent=task.source_changed?'The source changed. Check its meaning and saved deadline.':'The date needs your decision.';card.append(text,actionButton('Review date','secondary',()=>openTaskDetail(task.id)));root.append(card);}
    for(const item of captureFailures){const card=detailSection(savedItemTitle(item));card.classList.add('review-card');card.dataset.reviewItem=item.id;const text=document.createElement('p');text.textContent='The original is saved, but its content could not be read.';card.append(text,actionButton('Inspect original','quiet',()=>openSavedItem(item.id)),actionButton('Retry processing','secondary',async()=>{try{const updated=await fetchJson(`/api/items/${item.id}/retry`,{method:'POST'},'Could not retry processing.');await loadFocusedReviews();await loadNotes();if(updated.processing_status==='ready')organizeCapturedItems();}catch(error){text.textContent=error.message;}}));root.append(card);}
    for(const state of failed){const card=detailSection(state.title||'Saved capture');card.classList.add('review-card');card.dataset.reviewItem=state.item_id;const text=document.createElement('p');text.textContent=state.status==='blocked'?'Organization needs more context or a shorter source. Open the capture to inspect it.':'Organization did not finish. The original and previous results are saved.';card.append(text,actionButton('Open capture','quiet',()=>openSavedItem(state.item_id)),actionButton('Retry organization','secondary',async()=>{try{await fetchJson(`/api/items/${state.item_id}/invalidate-analysis`,{method:'POST'},'Could not queue retry.');await organizeCapturedItems();await loadFocusedReviews();}catch(error){text.textContent=error.message;}}));root.append(card);}
    for(const review of pending){const card=detailSection(review.source_title||'Saved capture');card.classList.add('review-card');card.dataset.reviewItem=review.item_id;const text=document.createElement('p');text.textContent=review.kind==='related'?'This capture may be related to an existing task. Both remain separate.':`Possible overlap${review.candidate_task_text?` with “${review.candidate_task_text}”`:''}. Tasks remain separate until you review.`;card.append(text,actionButton('Inspect capture','quiet',()=>openSavedItem(review.item_id)));
      for(const [label,decision] of [['Keep separate','keep_separate'],['Link source; keep tasks','attach_source']]){const button=actionButton(label,'secondary',async()=>{button.disabled=true;try{await fetchJson(`/api/analysis/reviews/${review.id}/resolve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision})},'Could not save review.');await loadFocusedReviews();await loadSavedTasks();}catch(error){text.textContent=error.message;button.disabled=false;}});if(decision==='keep_separate'||review.candidate_task_id)card.append(button);}root.append(card);
    }
    if(!count){const text=document.createElement('p');text.className='empty-state';text.textContent='You are up to date. Nothing needs review.';root.append(text);}
  }catch(error){if(ownerEpoch!==window.lifeInboxOwnerEpoch)return;const root=document.getElementById('focusedReviews');if(root)root.textContent=error.message;for(const id of ['reviewInboxButton','taskReviewButton']){const button=document.getElementById(id);if(button){button.hidden=false;button.textContent='Retry review';}}}
}
document.getElementById('taskDetail').addEventListener('cancel',event=>{event.preventDefault();closeTaskDetail();});
window.addEventListener('DOMContentLoaded',()=>{taskControlsFromUrl();});
window.addEventListener('popstate',()=>{renderTaskCollection();syncTaskDetailFromUrl();});
window.addEventListener('lifeinbox:owner',event=>{const owner=event.detail?.id??null;if(taskOwner===undefined){taskOwner=owner;return;}if(owner===taskOwner)return;taskOwner=owner;taskReadRevision++;savedTasks=[];taskDataReady=false;selectedTaskId=null;document.getElementById('focusedReviews').replaceChildren();document.getElementById('taskDetailBody').replaceChildren();document.getElementById('calendarPreview').replaceChildren();for(const id of ['reviewInboxButton','taskReviewButton'])document.getElementById(id).hidden=true;for(const id of ['taskDetail','calendarDialog','reviewDialog'])document.getElementById(id).close();document.getElementById('taskActionNotice').hidden=true;const url=new URL(location.href);url.searchParams.delete('task');window.history.replaceState({},'',url);});
