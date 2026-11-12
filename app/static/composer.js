/* Unified capture. Only explicit save/edit/retry work queues organization. */
const captureDraft = {files: [], saving: false, recorder: null, requesting: false, stream: null, cancelRecording: false, owner: null, epoch: 0};
let organizationQueued = false;
let organizationRunning = false;
let organizationEpoch = null;

function captureFileKind(file) {
  const type = file.type || '';
  const name = file.name.toLowerCase();
  if (type === 'application/pdf' || name.endsWith('.pdf')) return 'pdf';
  if (/^image\/(png|jpeg|webp|gif)$/.test(type) || /\.(png|jpe?g|webp|gif)$/.test(name)) return 'image';
  if (type.startsWith('audio/') || type === 'video/webm' || /\.(wav|mp3|m4a|ogg|webm)$/.test(name)) return 'voice';
  return null;
}

function addComposerFiles(files) {
  for (const file of files) {
    const kind = captureFileKind(file);
    if (!kind) { setStatus('noteStatus', `${file.name}: choose a PDF, image or audio file.`, true); continue; }
    if (captureDraft.files.length >= 8) { setStatus('noteStatus', 'Save these attachments before adding more (up to 8 at once).', true); break; }
    captureDraft.files.push({file, kind, url: URL.createObjectURL(file)});
  }
  renderComposerFiles();
}

function renderComposerFiles() {
  const root = document.getElementById('attachmentPreviews');
  root.replaceChildren();
  for (const attachment of captureDraft.files) {
    const row = document.createElement('div');
    row.className = 'attachment-preview';
    if (attachment.kind === 'image') {
      const img = document.createElement('img'); img.src = attachment.url; img.alt = attachment.file.name;
      row.append(img);
    }
    const name = document.createElement('span'); name.textContent = attachment.file.name;
    row.append(name);
    if (attachment.kind === 'voice') {
      const audio = document.createElement('audio'); audio.controls = true; audio.src = attachment.url;
      audio.setAttribute('aria-label', `Review ${attachment.file.name}`); row.append(audio);
    }
    const remove = actionButton('Remove', 'quiet', () => {
      captureDraft.files.splice(captureDraft.files.indexOf(attachment), 1);
      URL.revokeObjectURL(attachment.url); renderComposerFiles();
    });
    remove.setAttribute('aria-label', `Remove ${attachment.file.name}`); remove.disabled = captureDraft.saving;
    row.append(remove); root.append(row);
  }
}

async function startComposerRecording() {
  if (captureDraft.recorder || captureDraft.requesting || captureDraft.saving) return;
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    setStatus('voiceStatus', 'Recording is unavailable here. You can attach an audio file instead.', true); return;
  }
  const epoch = captureDraft.epoch;
  captureDraft.requesting = true;
  document.getElementById('recordVoiceButton').disabled = true;
  document.getElementById('addNoteButton').disabled = true;
  setStatus('voiceStatus', 'Allow microphone access to start recording.');
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    if (epoch !== captureDraft.epoch) { stream.getTracks().forEach(t => t.stop()); return; }
    const mimeType = ['audio/webm', 'audio/mp4'].find(type => MediaRecorder.isTypeSupported(type));
    const recorder = new MediaRecorder(stream, mimeType ? {mimeType} : {});
    const chunks = [];
    captureDraft.stream = stream; captureDraft.recorder = recorder; captureDraft.cancelRecording = false;
    recorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
    recorder.onstop = () => {
      const cancelled = captureDraft.cancelRecording || epoch !== captureDraft.epoch;
      stream.getTracks().forEach(track => track.stop());
      captureDraft.stream = null; captureDraft.recorder = null;
      if (!cancelled && chunks.length) {
        const type = recorder.mimeType || 'audio/webm';
        const file = new File(chunks, `Voice note.${type.includes('mp4') ? 'm4a' : 'webm'}`, {type});
        addComposerFiles([file]); setStatus('voiceStatus', 'Recording ready. Listen before saving, or remove it.');
      } else setStatus('voiceStatus', 'Recording cancelled.');
      updateRecordingControls(false);
    };
    recorder.onerror = () => { captureDraft.cancelRecording = true; stream.getTracks().forEach(track => track.stop()); captureDraft.recorder = null; captureDraft.stream = null; updateRecordingControls(false); setStatus('voiceStatus', 'Recording stopped unexpectedly. Try again or attach audio.', true); };
    recorder.start(); updateRecordingControls(true);
    setStatus('voiceStatus', 'Recording… Stop when you are ready to review.');
  } catch {
    captureDraft.stream?.getTracks().forEach(track => track.stop());
    captureDraft.stream = null; captureDraft.recorder = null;
    updateRecordingControls(false);
    setStatus('voiceStatus', 'Microphone access was not available. Try again or attach audio.', true);
  } finally { captureDraft.requesting = false; document.getElementById('recordVoiceButton').disabled = captureDraft.saving; document.getElementById('addNoteButton').disabled = !!captureDraft.recorder || captureDraft.saving; }
}
function updateRecordingControls(recording) {
  document.getElementById('recordVoiceButton').hidden = recording;
  document.getElementById('stopRecordingButton').hidden = !recording;
  document.getElementById('cancelRecordingButton').hidden = !recording;
  document.getElementById('addNoteButton').disabled = recording || captureDraft.saving;
}
function stopComposerRecording() { if (captureDraft.recorder?.state === 'recording') captureDraft.recorder.stop(); }
function cancelComposerRecording() { captureDraft.cancelRecording = true; stopComposerRecording(); }

async function saveCapture(event) {
  event?.preventDefault();
  if (captureDraft.saving || captureDraft.recorder || captureDraft.requesting) return;
  if (captureDraft.owner == null) { setStatus('noteStatus', 'Check your account before saving.', true); return; }
  const input = document.getElementById('noteText');
  const text = input.value.trim();
  if (!text && !captureDraft.files.length) { input.focus(); setStatus('noteStatus', 'Write something or attach a file first.', true); return; }
  captureDraft.saving = true; input.disabled = true;
  setButtonBusy('addNoteButton', true, 'Saving…', 'Save');
  document.getElementById('attachButton').disabled = true;
  document.getElementById('recordVoiceButton').disabled = true;
  renderComposerFiles();
  const saved = [];
  const epoch = captureDraft.epoch;
  try {
    if (text) {
      const link = /^https?:\/\/\S+$/i.test(text);
      const item = await fetchJson(link ? '/api/items/url?defer_processing=true' : '/api/items', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(link ? {url: text} : {raw_text: text})
      }, 'Could not save. Your draft is still here.');
      if (epoch !== captureDraft.epoch) return;
      saved.push(item); input.value = ''; persistComposerText();
    }
    for (const attachment of [...captureDraft.files]) {
      const body = new FormData(); body.append('file', attachment.file);
      const item = await fetchJson(`/api/items/${attachment.kind}?defer_processing=true`, {method: 'POST', body}, 'Could not upload. Your attachment is still here.');
      if (epoch !== captureDraft.epoch) return;
      saved.push(item); captureDraft.files.splice(captureDraft.files.indexOf(attachment), 1);
      URL.revokeObjectURL(attachment.url); renderComposerFiles();
    }
    setStatus('noteStatus', `Saved${saved.length > 1 ? ` ${saved.length} items` : ''}. You can capture something else.`);
  } catch (error) {
    if (epoch === captureDraft.epoch) setStatus('noteStatus', `${saved.length ? 'Some items were saved. ' : ''}${error.message}`, true);
  } finally {
    captureDraft.saving = false; input.disabled = false;
    setButtonBusy('addNoteButton', false, 'Saving…', 'Save');
    document.getElementById('addNoteButton').disabled = captureDraft.owner == null;
    document.getElementById('attachButton').disabled = false;
    document.getElementById('recordVoiceButton').disabled = false;
    renderComposerFiles();
    if (saved.length && epoch === captureDraft.epoch) {
      await loadNotes();
      watchCapturedItems(saved, epoch);
    }
  }
}

async function watchCapturedItems(items, epoch) {
  let pending = items.filter(item => ['captured', 'processing'].includes(item.processing_status));
  try {
    for (let attempt = 0; pending.length && attempt < 120; attempt++) {
      if (epoch !== captureDraft.epoch) return;
      setStatus('backgroundStatus', `Saved. Processing ${pending.length === 1 ? 'your attachment' : `${pending.length} attachments`}…`);
      await new Promise(resolve => setTimeout(resolve, 1000));
      const updated = await Promise.all(pending.map(item => fetchJson(`/api/items/${item.id}`, {}, 'Could not check processing. Your original is saved.')));
      pending = updated.filter(item => ['captured', 'processing'].includes(item.processing_status));
      items = items.map(item => updated.find(row => row.id === item.id) || item);
    }
    if (epoch !== captureDraft.epoch) return;
    await loadNotes();
    if (pending.length) { setStatus('backgroundStatus', 'Saved. Processing is taking longer; check the item or retry from its details.', true); return; }
    if (items.some(item => item.processing_status === 'failed')) setStatus('noteStatus', 'Original saved. Some content needs attention; open the item to retry.', true);
    await organizeCapturedItems();
  } catch (error) {
    if (epoch === captureDraft.epoch) setStatus('backgroundStatus', error.message, true);
  }
}

async function organizeCapturedItems() {
  organizationQueued = true;
  const epoch = captureDraft.epoch;
  if (organizationRunning && organizationEpoch === epoch) return;
  organizationRunning = true; organizationEpoch = epoch;
  try {
    for (let pass = 0; organizationQueued && pass < 8; pass++) {
      organizationQueued = false;
      if (epoch !== captureDraft.epoch) return;
      setStatus('backgroundStatus', 'Saved. Organizing new and changed items…');
      const result = await fetchJson('/api/analyze', {method: 'POST'}, 'Saved, but organization paused. Retry using Organization options.');
      if (epoch !== captureDraft.epoch) return;
      if (result.metrics?.pending_count) organizationQueued = true;
      setStatus('backgroundStatus', result.metrics?.noop ? 'Everything is up to date.' : 'Organized. Your saved items and tasks are ready.');
      populateCategoryFilter(result);
      await Promise.all([loadNotes(), loadTasks(), loadAnalysisReviews()]);
      window.dispatchEvent(new CustomEvent('lifeinbox:organized', {detail:{changed:!!result.metrics?.target_count}}));
    }
    if (organizationQueued) setStatus('backgroundStatus', 'More items are waiting. Continue from Organization options.');
  } catch (error) {
    if (epoch === captureDraft.epoch) { setStatus('backgroundStatus', error.message, true); await loadAnalysisReviews(); }
  } finally { if (organizationEpoch === epoch) organizationRunning = false; }
}

function persistComposerText() {
  if (captureDraft.owner == null) return;
  try { sessionStorage.setItem(`life-inbox-draft-${captureDraft.owner}`, document.getElementById('noteText').value); } catch { /* Storage can be disabled. */ }
}
function resetOwnerNavigation() {
  const url = new URL(location.href);
  for (const key of ['q','item_type','category','topic_id','priority','processing_status','item','task','task_q','task_filter','task_status']) url.searchParams.delete(key);
  window.history.replaceState({ownerId: window.lifeInboxOwnerId}, '', url);
  for (const id of Object.values(itemFilterFields)) document.getElementById(id).value = '';
  document.getElementById('taskSearch').value=''; document.getElementById('taskFilter').value='all';
  for(const id of ['searchStatus','taskStatus','taskViewDate','calendarStatus']) document.getElementById(id).textContent='';
  const topic = document.getElementById('topicFilter'); if (topic) topic.replaceChildren(new Option('All topics', ''));
  document.getElementById('topics')?.replaceChildren();
  LifeInboxPagination.resetForReload(itemPagination); itemPagination.listRequestInFlight = false;
  itemListRevision++; loadedItemFilterKey = '';
}
window.addEventListener('lifeinbox:owner', event => {
  const owner = event.detail?.id ?? null;
  if (owner === captureDraft.owner) return;
  const wasBound = captureDraft.owner !== null;
  window.lifeInboxOwnerId = owner;
  if (wasBound || (window.history.state?.ownerId != null && window.history.state.ownerId !== owner)) resetOwnerNavigation();
  captureDraft.epoch++; window.lifeInboxOwnerEpoch = captureDraft.epoch; cancelComposerRecording();
  if (wasBound) {
    for (const id of ['notes', 'tasks', 'dashboard', 'aiOutput', 'analysisReviews', 'focusedReviews', 'attention', 'askOutput', 'calendarPreview']) document.getElementById(id)?.replaceChildren();
    for (const id of ['noteStatus', 'voiceStatus', 'backgroundStatus', 'askStatus']) setStatus(id, '');
  }
  captureDraft.files.forEach(attachment => URL.revokeObjectURL(attachment.url)); captureDraft.files = [];
  captureDraft.owner = owner; organizationQueued = false;
  document.getElementById('addNoteButton').disabled = owner == null || captureDraft.saving;
  const input = document.getElementById('noteText');
  if (wasBound || !input.value) {
    try { input.value = owner == null ? '' : sessionStorage.getItem(`life-inbox-draft-${owner}`) || ''; } catch { input.value = ''; }
  }
  renderComposerFiles();
});
const composer = document.getElementById('captureComposer');
document.getElementById('captureFiles').addEventListener('change', event => { addComposerFiles(event.target.files); event.target.value = ''; });
const captureText = document.getElementById('noteText');
captureText.addEventListener('input', persistComposerText);
captureText.addEventListener('keydown', event => { if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') saveCapture(event); });
captureText.addEventListener('paste', event => {
  const files = [...(event.clipboardData?.files || [])];
  if (files.length) { if (!event.clipboardData.getData('text/plain')) event.preventDefault(); addComposerFiles(files); }
});
composer.addEventListener('dragover', event => { event.preventDefault(); composer.classList.add('dragging'); });
composer.addEventListener('dragleave', () => composer.classList.remove('dragging'));
composer.addEventListener('drop', event => {
  event.preventDefault(); composer.classList.remove('dragging');
  if (event.dataTransfer.files.length) addComposerFiles(event.dataTransfer.files);
  else { captureText.value += event.dataTransfer.getData('text/plain'); persistComposerText(); }
});
