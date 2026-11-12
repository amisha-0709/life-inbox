let loadedItemFilterKey = '';
let isSavingNote = false;
let isAnalyzingInbox = false;
const pendingNoteActions = new Set();
const pendingTaskActions = new Set();
let deadlineEditorId = 0;
let calendarPreviewVersion = 0;

function browserTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch {
    return '';
  }
}
const itemFilterFields = {
  q: 'itemSearch',
  item_type: 'itemTypeFilter',
  topic_id: 'topicFilter',
  priority: 'priorityFilter',
  processing_status: 'processingStatusFilter'
};

function setStatus(elementId, message, isError = false) {
  const element = document.getElementById(elementId);
  if (!element) return;
  element.textContent = message;
  element.classList.toggle('error', isError);
}

function setButtonBusy(buttonId, busy, busyLabel, idleLabel) {
  const button = document.getElementById(buttonId);
  button.disabled = busy;
  button.textContent = busy ? busyLabel : idleLabel;
  button.setAttribute('aria-busy', String(busy));
}

function itemFilterParams() {
  const params = new URLSearchParams();
  for (const [name, elementId] of Object.entries(itemFilterFields)) {
    const value = document.getElementById(elementId).value.trim();
    if (value) params.set(name, value);
  }
  return params;
}

function hasItemFilters() {
  return itemFilterParams().size > 0;
}

function updateUrl(updates) {
  const url = new URL(window.location.href);
  for (const [name, value] of Object.entries(updates)) {
    if (value) {
      url.searchParams.set(name, value);
    } else {
      url.searchParams.delete(name);
    }
  }
  if (url.href !== location.href) window.history.pushState({ownerId: window.lifeInboxOwnerId}, '', url);
}

function syncControlsFromUrl() {
  const params = new URLSearchParams(window.location.search);
  for (const [name, elementId] of Object.entries(itemFilterFields)) {
    const element = document.getElementById(elementId);
    const value = params.get(name) || '';
    if (
      name === 'category'
      && value
      && !Array.from(element.options).some(option => option.value === value)
    ) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value;
      element.appendChild(option);
    }
    element.value = value;
  }


}

function populateCategoryFilter() { loadTopics(); }

function applyItemFilters(event) {
  event.preventDefault();
  const values = {};
  const params = itemFilterParams();
  for (const name of Object.keys(itemFilterFields)) {
    values[name] = params.get(name) || '';
  }
  updateUrl(values);
  updateFiltersLabel(); document.getElementById('extraFilters')?.removeAttribute('open');
  loadNotes();
}

function clearItemFilters() {
  for (const elementId of Object.values(itemFilterFields)) {
    document.getElementById(elementId).value = '';
  }
  updateUrl(
    Object.fromEntries(
      Object.keys(itemFilterFields).map(name => [name, ''])
    )
  );
  updateFiltersLabel(); document.getElementById('extraFilters')?.removeAttribute('open');
  loadNotes();
}

function changeTaskStatusFilter() {
  const status = document.getElementById('taskStatusFilter').value;
  updateUrl({task_status: status});
  loadTasks();
}

async function fetchJson(url, options, friendlyError) {
  const ownerEpoch = window.lifeInboxOwnerEpoch;
  let response;
  try {
    const request = {...(options || {})};
    const headers = new Headers(request.headers || {});
    const timezone = browserTimezone();
    if (timezone) headers.set('X-Life-Inbox-Timezone', timezone);
    response = await fetch(url, {...request, headers});
  } catch {
    throw new Error(friendlyError);
  }

  if (!response.ok) {
    let detail;
    try {
      const body = await response.json();
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      // Keep the useful local message when an upstream response is not JSON.
    }
    throw new Error(detail || friendlyError);
  }

  if (response.status === 204) {
    return null;
  }

  try {
    const result = await response.json();
    if (!url.startsWith('/api/auth/') && ownerEpoch != null && ownerEpoch !== window.lifeInboxOwnerEpoch) throw new Error('Session changed.');
    return result;
  } catch {
    throw new Error(friendlyError);
  }
}

function setJourneyStep(step) {
  const order = ['capture', 'save', 'organize', 'result'];
  const index = order.indexOf(step);
  document.querySelectorAll('#journeySteps li').forEach((node) => {
    const stepName = node.dataset.step;
    const stepIndex = order.indexOf(stepName);
    node.classList.toggle('current', stepName === step);
    node.classList.toggle('done', stepIndex >= 0 && stepIndex < index);
  });
}

function followAppLink(event, view) {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button) return;
  event.preventDefault(); showDemoView(view);
}

function showDemoView(view, {history = true, focus = true} = {}) {
  const allowed = ['inbox', 'tasks', 'ask', 'settings'];
  const next = allowed.includes(view) ? view : 'inbox';
  const current = new URLSearchParams(location.search).get('view') || 'inbox';
  if (!allowed.includes(current)) { const url = new URL(location.href); url.searchParams.set('view', next); window.history.replaceState(window.history.state, '', url); }
  if (history && current !== next) updateUrl({view: next});
  document.getElementById('accountMenu')?.removeAttribute('open');
  document.title = `${next === 'settings' ? 'Settings' : next[0].toUpperCase() + next.slice(1)} · Life Inbox`;
  document.querySelectorAll('.view').forEach((section) => {
    const active = section.id === `view-${next}`;
    section.classList.toggle('active', active);
    section.hidden = !active;
  });
  document.querySelectorAll('.nav-link').forEach((button) => {
    button.classList.toggle('active', button.dataset.view === next);
    if (button.dataset.view === next) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  if (next === 'ask') updateAskKeyBanner();
  if (focus) document.getElementById('mainContent')?.focus({preventScroll: true});
}

function updateAskKeyBanner() { /* Ask presents evidence, not provider diagnostics. */ }

async function addNote() {
  if (isSavingNote) return;

  const textarea = document.getElementById('noteText');
  const text = textarea.value.trim();
  if (!text) {
    setStatus('noteStatus', 'Write a note before saving.', true);
    setJourneyStep('capture');
    textarea.focus();
    return;
  }

  isSavingNote = true;
  setJourneyStep('save');
  setButtonBusy('addNoteButton', true, 'Saving…', 'Save note');
  setStatus('noteStatus', 'Saving your note…');
  setStatus('journeyStatus', 'Saving note…');

  try {
    await fetchJson(
      '/api/items',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({raw_text: text})
      },
      'Unable to save note.'
    );

    if (textarea.value.trim() === text) {
      textarea.value = '';
    }
    setStatus('noteStatus', 'Note saved. Organize when you are ready.');
    setStatus('journeyStatus', 'Saved. Next: organize.');
    setJourneyStep('organize');
    await loadNotes(
      'Your note was saved, but the list could not refresh. Reload to try again.'
    );
    await loadLatestAnalysis();
  } catch {
    setJourneyStep('capture');
    setStatus(
      'noteStatus',
      'We could not save your note. Your text is still here—please try again.',
      true
    );
    setStatus('journeyStatus', 'Save failed. Try again.', true);
  } finally {
    isSavingNote = false;
    setButtonBusy('addNoteButton', false, 'Saving…', 'Save note');
  }
}

async function addPdf() {
  const input = document.getElementById('pdfFile');
  const file = input.files && input.files[0];
  if (!file) {
    setStatus('noteStatus', 'Choose a PDF file before uploading.', true);
    input.focus();
    return;
  }
  setButtonBusy('addPdfButton', true, 'Uploading…', 'Upload PDF');
  setStatus('noteStatus', 'Saving PDF and extracting text…');
  const formData = new FormData();
  formData.append('file', file);
  try {
    const item = await fetchJson(
      '/api/items/pdf',
      {method: 'POST', body: formData},
      'Unable to upload PDF.'
    );
    input.value = '';
    setStatus(
      'noteStatus',
      item.processing_status === 'ready'
        ? 'PDF saved and processed.'
        : `PDF saved but processing failed: ${item.error_message || 'unknown error'}`,
      item.processing_status !== 'ready'
    );
    await loadNotes();
    await loadLatestAnalysis();
    await loadDashboard();
  } catch {
    setStatus('noteStatus', 'We could not upload that PDF.', true);
  } finally {
    setButtonBusy('addPdfButton', false, 'Uploading…', 'Upload PDF');
  }
}

async function addImage() {
  const input = document.getElementById('imageFile');
  const file = input.files && input.files[0];
  if (!file) {
    setStatus('noteStatus', 'Choose an image before uploading.', true);
    input.focus();
    return;
  }
  setButtonBusy('addImageButton', true, 'Uploading…', 'Upload image');
  setStatus('noteStatus', 'Saving image and running OCR…');
  const formData = new FormData();
  formData.append('file', file);
  try {
    const item = await fetchJson(
      '/api/items/image',
      {method: 'POST', body: formData},
      'Unable to upload image.'
    );
    input.value = '';
    setStatus(
      'noteStatus',
      item.processing_status === 'ready'
        ? 'Image saved and processed.'
        : `Image saved but processing failed: ${item.error_message || 'unknown error'}`,
      item.processing_status !== 'ready'
    );
    await loadNotes();
    await loadLatestAnalysis();
    await loadDashboard();
  } catch {
    setStatus('noteStatus', 'We could not upload that image.', true);
  } finally {
    setButtonBusy('addImageButton', false, 'Uploading…', 'Upload image');
  }
}

let voiceRecorder = null;
let voiceChunks = [];
let voiceStream = null;

async function addVoice() {
  const input = document.getElementById('voiceFile');
  const file = input.files && input.files[0];
  if (!file) {
    setStatus('noteStatus', 'Choose an audio file or record one first.', true);
    input.focus();
    return;
  }
  await uploadVoiceFile(file, 'Uploading voice note…');
  input.value = '';
}

async function uploadVoiceFile(file, statusMessage) {
  setButtonBusy('addVoiceButton', true, 'Uploading…', 'Upload audio');
  setStatus('noteStatus', statusMessage);
  const formData = new FormData();
  formData.append('file', file, file.name || 'voice-note.webm');
  try {
    const item = await fetchJson(
      '/api/items/voice',
      {method: 'POST', body: formData},
      'Unable to upload voice note.'
    );
    setStatus(
      'noteStatus',
      item.processing_status === 'ready'
        ? (item.extracted_text
          ? 'Voice note saved and transcribed.'
          : 'Voice note saved. Add or edit the transcript if needed.')
        : `Voice note saved but processing failed: ${item.error_message || 'unknown error'}`,
      item.processing_status !== 'ready'
    );
    setStatus('voiceStatus', '');
    await loadNotes();
    await loadLatestAnalysis();
    await loadDashboard();
  } catch {
    setStatus('noteStatus', 'We could not upload that voice note.', true);
  } finally {
    setButtonBusy('addVoiceButton', false, 'Uploading…', 'Upload audio');
  }
}

async function toggleVoiceRecording() {
  if (voiceRecorder && voiceRecorder.state === 'recording') {
    voiceRecorder.stop();
    setButtonBusy('recordVoiceButton', false, 'Recording…', 'Record');
    setStatus('voiceStatus', 'Processing recording…');
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    setStatus('voiceStatus', 'Browser recording is not available here. Upload an audio file instead.', true);
    return;
  }

  try {
    voiceStream = await navigator.mediaDevices.getUserMedia({audio: true});
    voiceChunks = [];
    voiceRecorder = new MediaRecorder(voiceStream);
    voiceRecorder.addEventListener('dataavailable', event => {
      if (event.data && event.data.size > 0) voiceChunks.push(event.data);
    });
    voiceRecorder.addEventListener('stop', async () => {
      if (voiceStream) {
        voiceStream.getTracks().forEach(track => track.stop());
        voiceStream = null;
      }
      const blob = new Blob(voiceChunks, {type: voiceRecorder.mimeType || 'audio/webm'});
      voiceRecorder = null;
      if (!blob.size) {
        setStatus('voiceStatus', 'Recording was empty.', true);
        return;
      }
      const file = new File([blob], `recording-${Date.now()}.webm`, {
        type: blob.type || 'audio/webm'
      });
      await uploadVoiceFile(file, 'Saving recorded voice note…');
    });
    voiceRecorder.start();
    setButtonBusy('recordVoiceButton', true, 'Stop', 'Record');
    document.getElementById('recordVoiceButton').textContent = 'Stop';
    document.getElementById('recordVoiceButton').disabled = false;
    setStatus('voiceStatus', 'Recording… click Stop when finished.');
  } catch {
    setStatus(
      'voiceStatus',
      'Could not access the microphone. You can still upload an audio file.',
      true
    );
  }
}

async function addUrl() {
  const input = document.getElementById('urlText');
  const url = input.value.trim();
  if (!url) {
    setStatus('noteStatus', 'Paste a URL before saving.', true);
    input.focus();
    return;
  }
  setButtonBusy('addUrlButton', true, 'Saving…', 'Save URL');
  setStatus('noteStatus', 'Saving URL and fetching article text…');
  try {
    const item = await fetchJson(
      '/api/items/url',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url})
      },
      'Unable to save URL.'
    );
    if (input.value.trim() === url) input.value = '';
    setStatus(
      'noteStatus',
      item.processing_status === 'ready'
        ? 'URL saved and processed.'
        : `URL saved but processing failed: ${item.error_message || 'unknown error'}`,
      item.processing_status !== 'ready'
    );
    await loadNotes();
    await loadLatestAnalysis();
    await loadDashboard();
  } catch {
    setStatus('noteStatus', 'We could not save that URL.', true);
  } finally {
    setButtonBusy('addUrlButton', false, 'Saving…', 'Save URL');
  }
}

function esc(str) {
  return String(str ?? '').replace(/[&<>'"]/g, tag => ({
    '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'
  }[tag]));
}

function actionButton(label, className, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = label;
  button.className = className;
  button.addEventListener('click', onClick);
  return button;
}

function captureTypeLabel(type) { return ({text: 'Note', url: 'Link', pdf: 'PDF', image: 'Image', voice: 'Voice note'})[type] || 'Saved item'; }

function renderNote(note) { return renderCollectionItem(note); }

function showEditExtractedText(note, root) {
  if (pendingNoteActions.has(note.id)) return;

  const textarea = document.createElement('textarea');
  textarea.className = 'note-edit-input';
  textarea.maxLength = 20000;
  textarea.value = note.extracted_text || '';

  const hint = document.createElement('div');
  hint.className = 'provenance';
  const isVoice = note.item_type === 'voice';
  hint.innerHTML = isVoice
    ? '<span class="label">Editing transcript</span> (extracted text from the original audio).'
    : '<span class="label">Editing extracted / OCR text</span> (facts). AI description stays separate.';

  const actions = document.createElement('div');
  actions.className = 'note-actions';

  const saveButton = actionButton(isVoice ? 'Save transcript' : 'Save OCR text', '', async () => {
    if (pendingNoteActions.has(note.id)) return;
    pendingNoteActions.add(note.id);
    saveButton.disabled = true;
    cancelButton.disabled = true;
    saveButton.textContent = 'Saving…';
    setStatus('noteStatus', 'Saving extracted text…');
    try {
      const updated = await fetchJson(
        `/api/items/${note.id}/extracted-text`,
        {
          method: 'PATCH',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({extracted_text: textarea.value})
        },
        'Unable to update extracted text.'
      );
      root.replaceWith(renderNote(updated));
      setStatus('noteStatus', isVoice ? 'Transcript updated.' : 'Extracted text updated.');
      await loadLatestAnalysis();
      await refreshDeadlineViews();
    } catch {
      setStatus('noteStatus', 'Could not update extracted text.', true);
      saveButton.disabled = false;
      cancelButton.disabled = false;
      saveButton.textContent = isVoice ? 'Save transcript' : 'Save OCR text';
    } finally {
      pendingNoteActions.delete(note.id);
    }
  });

  const cancelButton = actionButton('Cancel', 'secondary', () => {
    if (pendingNoteActions.has(note.id)) return;
    root.replaceWith(renderNote(note));
  });

  actions.append(saveButton, cancelButton);
  root.replaceChildren(hint, textarea, actions);
}

function showEditNote(note, root) {
  if (pendingNoteActions.has(note.id)) return;

  const textarea = document.createElement('textarea');
  textarea.className = 'note-edit-input';
  textarea.maxLength = 2000;
  textarea.value = note.raw_text;

  const actions = document.createElement('div');
  actions.className = 'note-actions';

  const saveButton = actionButton('Save', '', async () => {
    const text = textarea.value.trim();
    if (!text) {
      setStatus('noteStatus', 'Edited note text cannot be empty.', true);
      textarea.focus();
      return;
    }
    if (pendingNoteActions.has(note.id)) return;

    pendingNoteActions.add(note.id);
    saveButton.disabled = true;
    cancelButton.disabled = true;
    saveButton.textContent = 'Saving…';
    setStatus('noteStatus', 'Saving your changes…');

    try {
      const updatedNote = await fetchJson(
        `/api/items/${note.id}`,
        {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({raw_text: text})
        },
        'Unable to update note.'
      );
      LifeInboxPagination.afterLoadedNoteUpdated(itemPagination, updatedNote);
      root.replaceWith(renderNote(updatedNote));
      setStatus('noteStatus', 'Note updated.');
      await loadLatestAnalysis();
      await refreshDeadlineViews();
    } catch {
      setStatus(
        'noteStatus',
        'We could not update this note. Your changes are still here—please try again.',
        true
      );
      saveButton.disabled = false;
      cancelButton.disabled = false;
      saveButton.textContent = 'Save';
    } finally {
      pendingNoteActions.delete(note.id);
    }
  });

  const cancelButton = actionButton('Cancel', 'secondary', () => {
    if (pendingNoteActions.has(note.id)) return;
    root.replaceWith(renderNote(note));
    setStatus('noteStatus', 'Edit cancelled.');
  });

  actions.append(saveButton, cancelButton);
  root.replaceChildren(textarea, actions);
  textarea.focus();
}

async function deleteExistingNote(note, root, editButton, deleteButton) {
  if (pendingNoteActions.has(note.id)) return;

  const confirmed = window.confirm(
    'Delete this item? This action cannot be undone.'
  );
  if (!confirmed) return;

  pendingNoteActions.add(note.id);
  if (editButton) editButton.disabled = true;
  if (deleteButton) {
    deleteButton.disabled = true;
    deleteButton.textContent = 'Deleting…';
  }
  setStatus('noteStatus', 'Deleting note…');

  try {
    await fetchJson(
      `/api/items/${note.id}`,
      {method: 'DELETE'},
      'Unable to delete note.'
    );
    LifeInboxPagination.afterLoadedNoteDeleted(itemPagination, note.id);
    root.remove();
    setStatus('noteStatus', 'Note deleted.');
    invalidateCalendarPreview();
    await loadLatestAnalysis();
    await loadTasks();
  } catch {
    setStatus(
      'noteStatus',
      'We could not delete this note. It is still here—please try again.',
      true
    );
    if (editButton) editButton.disabled = false;
    if (deleteButton) {
      deleteButton.disabled = false;
      deleteButton.textContent = 'Delete';
    }
  } finally {
    pendingNoteActions.delete(note.id);
  }
}

function deadlineStatusLabel(task) {
  return {
    inferred: 'Inferred',
    confirmed: 'Confirmed by you',
    edited: 'Edited by you',
    cleared: 'Cleared by you',
    legacy: 'Previously saved'
  }[task.deadline_status] || (task.timing_confirmed ? 'Confirmed by you' : 'Inferred');
}

function hasUnreviewedSourceChange(task) {
  return task.source_changed && !['confirmed', 'edited', 'cleared'].includes(task.deadline_status);
}

function renderDeadline(task) {
  const block = document.createElement('div');
  block.className = 'deadline-summary';
  const resolved = document.createElement('div');
  resolved.className = 'deadline-value';
  resolved.textContent = task.due_on
    ? `Deadline: ${task.due_on} · Date only`
    : 'No deadline';
  const status = document.createElement('span');
  status.className = 'deadline-state';
  status.textContent = deadlineStatusLabel(task);
  resolved.append(' ', status);
  block.appendChild(resolved);

  if (task.ai_timing_text) {
    const wording = document.createElement('div');
    wording.className = 'provenance';
    wording.textContent = `Source wording: “${task.ai_timing_text}”`;
    block.appendChild(wording);
  }
  if (task.deadline_reference_at && task.deadline_timezone) {
    const reference = document.createElement('div');
    reference.className = 'provenance';
    let timestamp = task.deadline_reference_at;
    try {
      timestamp = new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium', timeStyle: 'short', timeZone: task.deadline_timezone
      }).format(new Date(timestamp));
    } catch {
      // Preserve the saved timestamp if this browser cannot format its timezone.
    }
    const label = ['edited', 'cleared'].includes(task.deadline_status)
      ? 'Deadline changed on'
      : 'Source date reference';
    reference.textContent = `${label}: ${timestamp} (${task.deadline_timezone})`;
    block.appendChild(reference);
  }
  if (task.deadline_note || task.deadline_needs_review) {
    const note = document.createElement('div');
    note.className = task.deadline_needs_review ? 'deadline-warning' : 'provenance';
    note.textContent = task.deadline_note || 'Review the wording and choose the intended date.';
    block.appendChild(note);
  }
  if (task.source_changed) {
    const changed = document.createElement('div');
    changed.className = 'deadline-warning';
    changed.textContent = hasUnreviewedSourceChange(task)
      ? (task.deadline_needs_review
        ? 'Source timing needs review. Choose or clear the intended deadline.'
        : 'Source changed since this deadline was inferred. Analyze again to review it.')
      : 'Source changed. Your chosen deadline stays saved when you analyze again.';
    block.appendChild(changed);
  }
  return block;
}

function invalidateCalendarPreview() {
  calendarPreviewVersion += 1;
  document.getElementById('calendarPreview').textContent =
    'Dates changed. Preview the calendar again to use the saved deadline.';
  setStatus('calendarStatus', '');
}

async function refreshDeadlineViews() {
  invalidateCalendarPreview();
  await loadTasks(); await loadFocusedReviews();
  if (typeof refreshOpenTaskDetail === 'function') refreshOpenTaskDetail();
}

function showDeadlineEditor(task, root, trigger) {
  if (pendingTaskActions.has(task.id)) return;
  const existing = root.querySelector('.deadline-editor');
  if (existing) {
    existing.querySelector('input').focus();
    return;
  }

  const form = document.createElement('form');
  form.className = 'deadline-editor';
  const inputId = `deadline-${task.id}-${++deadlineEditorId}`;
  const label = document.createElement('label');
  label.htmlFor = inputId;
  label.textContent = 'Deadline (date only)';
  const input = document.createElement('input');
  input.id = inputId;
  input.type = 'date';
  input.required = true;
  input.value = task.due_on || '';
  const hint = document.createElement('p');
  hint.id = `${inputId}-hint`;
  hint.className = 'provenance';
  hint.textContent = 'Choose the intended calendar date. Changing or clearing it is kept when you analyze again. Exact times are not scheduled.';
  input.setAttribute('aria-describedby', hint.id);
  const error = document.createElement('p');
  error.className = 'status error';
  error.setAttribute('role', 'status');
  error.setAttribute('aria-live', 'polite');
  const actions = document.createElement('div');
  actions.className = 'task-actions';
  const save = document.createElement('button');
  save.type = 'submit';
  save.textContent = 'Save deadline';
  const clear = actionButton('Clear deadline', 'secondary', () => saveDeadline(null));
  const cancel = actionButton('Cancel', 'secondary', () => {
    form.remove();
    if (trigger && trigger.isConnected) trigger.focus();
  });
  actions.append(save, clear, cancel);
  form.append(label, input, hint, actions, error);
  root.appendChild(form);

  async function saveDeadline(dueOn) {
    if (pendingTaskActions.has(task.id)) return;
    pendingTaskActions.add(task.id);
    actions.querySelectorAll('button').forEach(button => { button.disabled = true; });
    input.disabled = true;
    error.textContent = '';
    try {
      await fetchJson(`/api/tasks/${task.id}/deadline`, {
        method: 'PATCH',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({due_on: dueOn})
      }, 'Unable to save this deadline. Your selection is still here.');
      setStatus('taskStatus', dueOn ? `Deadline saved as ${dueOn}.` : 'Deadline cleared. Analysis will keep it cleared.');
      await refreshDeadlineViews();
    } catch (failure) {
      error.textContent = failure.message;
      actions.querySelectorAll('button').forEach(button => { button.disabled = false; });
      input.disabled = false;
    } finally {
      pendingTaskActions.delete(task.id);
    }
  }

  form.addEventListener('submit', event => {
    event.preventDefault();
    if (input.reportValidity()) saveDeadline(input.value);
  });
  input.focus();
}

async function confirmSavedDeadline(task) {
  if (pendingTaskActions.has(task.id)) return;
  pendingTaskActions.add(task.id);
  setStatus('taskStatus', 'Confirming the saved deadline…');
  try {
    await fetchJson(`/api/tasks/${task.id}/confirm-timing`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    }, 'Unable to confirm this deadline.');
    setStatus('taskStatus', `Deadline confirmed as ${task.due_on}.`);
    await refreshDeadlineViews();
  } catch (failure) {
    setStatus('taskStatus', failure.message, true);
  } finally {
    pendingTaskActions.delete(task.id);
  }
}

function renderTask(task) {
  const root = document.createElement('div');
  root.className = 'task';
  root.dataset.taskId = String(task.id);

  const text = document.createElement('strong');
  text.textContent = task.text;

  const priority = document.createElement('span');
  priority.className = 'pill';
  priority.textContent = task.priority;

  const content = document.createElement('div');
  content.append(text, priority);

  const meta = document.createElement('div');
  meta.className = 'task-meta';
  const sources = task.source_item_ids
    .map(itemId => `#${itemId}`)
    .join(', ');
  meta.textContent = `${task.status} · Sources: ${sources}`;

  const actions = document.createElement('div');
  actions.className = 'task-actions';

  if (task.status === 'open') {
    actions.append(
      actionButton('Complete', '', () => {
        transitionTask(task.id, 'complete', actions);
      }),
      actionButton('Dismiss', 'secondary', () => {
        transitionTask(task.id, 'dismiss', actions);
      }),
      actionButton('Snooze 1 day', 'secondary', async () => {
        const until = new Date();
        until.setUTCDate(until.getUTCDate() + 1);
        const iso = until.toISOString().slice(0, 10);
        try {
          await fetchJson(
            `/api/tasks/${task.id}/snooze`,
            {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({until: iso})
            },
            'Unable to snooze task.'
          );
          setStatus('taskStatus', `Snoozed until ${iso}.`);
          await loadTasks();
          await loadAttention();
        } catch {
          setStatus('taskStatus', 'Could not snooze task.', true);
        }
      })
    );
    if (task.due_on && !task.timing_confirmed && !task.deadline_needs_review && !hasUnreviewedSourceChange(task)) {
      actions.append(
        actionButton('Confirm deadline', 'secondary', () => confirmSavedDeadline(task))
      );
    }
    if (task.due_on && !task.deadline_needs_review && !hasUnreviewedSourceChange(task)) {
      actions.append(
        actionButton('Preview calendar', 'secondary', () => {
          previewCalendarForTask(task);
        })
      );
    }
  } else {
    actions.append(
      actionButton('Reopen', '', () => {
        transitionTask(task.id, 'reopen', actions);
      })
    );
  }

  const edit = actionButton('Edit deadline', 'secondary', () => showDeadlineEditor(task, root, edit));
  actions.appendChild(edit);
  root.append(content, renderDeadline(task), meta, actions);
  for (const detail of task.details || []) {
    const row = document.createElement('div');
    row.className = 'provenance';
    row.textContent = `Additional source detail (inferred) · #${detail.source_item_id}: ${detail.text}`;
    root.appendChild(row);
  }
  return root;
}

async function transitionTask(taskId, action, actions) {
  if (pendingTaskActions.has(taskId)) return;

  pendingTaskActions.add(taskId);
  const buttons = actions.querySelectorAll('button');
  buttons.forEach(button => {
    button.disabled = true;
  });
  setStatus('taskStatus', 'Updating task…');

  try {
    await fetchJson(
      `/api/tasks/${taskId}/${action}`,
      {method: 'POST'},
      'Unable to update task.'
    );
    const messages = {
      complete: 'Task completed.',
      dismiss: 'Task dismissed.',
      reopen: 'Task reopened.'
    };
    setStatus('taskStatus', messages[action]);
    await loadTasks();
    await loadAttention();
  } catch {
    setStatus(
      'taskStatus',
      'We could not update this task. Please try again.',
      true
    );
    buttons.forEach(button => {
      button.disabled = false;
    });
  } finally {
    pendingTaskActions.delete(taskId);
  }
}

async function loadTasks() { return loadSavedTasks(); }

function renderAnalysis() { /* Organization is represented by the saved collection and tasks. */ }

async function loadLatestAnalysis() { return true; }

async function analyzeInbox() {
  if (isAnalyzingInbox) return;

  isAnalyzingInbox = true;
  setJourneyStep('organize');
  setButtonBusy(
    'analyzeButton',
    true,
    'Analyzing…',
    'Organize new / changed items'
  );
  setStatus('analysisStatus', 'Analyzing your saved notes…');
  setStatus('journeyStatus', 'Organizing into topics, priorities, and tasks…');

  try {
    const data = await fetchJson(
      '/api/analyze',
      {method: 'POST'},
      'Unable to analyze inbox.'
    );
    populateCategoryFilter(data);
    renderAnalysis(data);
    invalidateCalendarPreview();
    await loadNotes();
    await loadTasks();
    await loadTopics();
    await loadDashboard();
    await loadAttention();
    setJourneyStep('result');
    const m = data.metrics;
    setStatus('analysisStatus', m
      ? (m.noop ? 'Nothing new or changed. No organizer calls.'
        : `Organized ${m.target_count} item(s); supporting context: ${m.context_task_count} tasks, ${m.context_snippet_count} snippets, ${m.context_characters} characters. ${m.model_calls} model call(s). ${m.input_tokens == null ? 'Provider token usage unavailable.' : `${m.input_tokens} provider input tokens.`}${m.pending_count ? ` ${m.pending_count} item(s) remain for the next batch.` : ''}`)
      : 'Analysis complete.');
    await loadAnalysisReviews();
    setStatus(
      'journeyStatus',
      'Organized. Check Organization for topics/priorities and Today/Tasks for produced tasks.'
    );
  } catch (error) {
    setStatus(
      'analysisStatus',
      error.message || 'We could not organize these items. Your captures are still saved.',
      true
    );
    setStatus('journeyStatus', 'Organize failed. Try again.', true);
    await loadAnalysisReviews();
  } finally {
    isAnalyzingInbox = false;
    setButtonBusy(
      'analyzeButton',
      false,
      'Analyzing…',
      'Organize new / changed items'
    );
  }
}

async function loadAnalysisReviews() { return loadFocusedReviews(); }

const itemPagination = LifeInboxPagination.createState(
  40
);

function updateLoadMoreButton(failureMessage) {
  const existingMore = document.getElementById('loadMoreItems');
  if (existingMore) existingMore.remove();
  if (!itemPagination.hasMore) return;

  const root = document.getElementById('notes');
  const more = document.createElement('button');
  more.id = 'loadMoreItems';
  more.type = 'button';
  more.className = 'secondary';
  more.textContent = itemPagination.listRequestInFlight
    ? 'Loading…'
    : 'Load more';
  more.disabled = itemPagination.listRequestInFlight;
  more.setAttribute(
    'aria-busy',
    String(itemPagination.listRequestInFlight)
  );
  more.addEventListener('click', () => {
    if (itemPagination.listRequestInFlight) return;
    loadNotes(failureMessage, {append: true});
  });
  root.appendChild(more);
}

let itemListRevision = 0;
async function loadNotes(failureMessage = 'Could not load your saved items. Try again.', {append = false} = {}) {
  if (append && itemPagination.listRequestInFlight) return false;
  const revision = ++itemListRevision;
  const params = itemFilterParams();
  const filterKey = params.toString();
  const sameFilters = filterKey === loadedItemFilterKey;
  const limit = append ? itemPagination.pageSize : sameFilters ? Math.min(500, Math.max(itemPagination.pageSize, itemPagination.loadedOffset)) : itemPagination.pageSize;
  const previousScroll = scrollY;
  params.set('sort','newest'); params.set('limit',String(limit)); params.set('offset',String(append ? itemPagination.loadedOffset : 0));
  itemPagination.listRequestInFlight = true;
  try {
    const notes = await fetchJson(`/api/items?${params}`, {}, failureMessage);
    if (revision !== itemListRevision) return false;
    const root = document.getElementById('notes');
    if (!append) root.replaceChildren(); else document.getElementById('loadMoreItems')?.remove();
    const {textNotes} = LifeInboxPagination.applyFetchedPage(itemPagination, notes, {append});
    itemPagination.hasMore = notes.length === limit;
    loadedItemFilterKey = filterKey;
    for (const note of textNotes) root.append(renderNote(note));
    if (!itemPagination.loadedItems.length) { const empty=document.createElement('div');empty.className='empty-state';empty.textContent=filterKey?'No captures match. Try another search or clear the filters.':'Your Inbox starts here. Save a thought, link or file above.';root.append(empty); }
    setStatus('searchStatus', `${itemPagination.loadedItems.length}${itemPagination.hasMore ? '+' : ''} saved item${itemPagination.loadedItems.length === 1 ? '' : 's'}${filterKey ? ' matching your filters' : ''}`);
    if (sameFilters) window.scrollTo(0,previousScroll);
    return true;
  } catch(error) { if(revision===itemListRevision)setStatus('searchStatus',error.message,true);return false; }
  finally {if(revision===itemListRevision){itemPagination.listRequestInFlight=false;if(itemPagination.hasMore)updateLoadMoreButton(failureMessage);}}
}

async function loadTopics() {
  try {
    const topics = await fetchJson(
      '/api/topics',
      undefined,
      'Unable to load topics.'
    );
    fillTopicChoices(document.getElementById('topicFilter'), topics, true);
    updateFiltersLabel();
    if (itemFilterParams().toString() !== loadedItemFilterKey) loadNotes();
    const root = document.getElementById('topics');
    root.innerHTML = '';
    if (!topics.length) {
      root.innerHTML = '<div class="panel muted">No topics yet.</div>';
      return;
    }
    for (const topic of topics) {
      const row = document.createElement('div');
      row.className = 'topic-chip';
      row.textContent = topic.name;
      root.appendChild(row);
    }
  } catch {
    setStatus('topicStatus', 'We could not load topics.', true);
  }
}

async function createTopic(event) {
  event.preventDefault();
  const input = document.getElementById('topicName');
  const name = input.value.trim();
  if (!name) return;
  try {
    await fetchJson(
      '/api/topics',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name})
      },
      'Unable to create topic.'
    );
    input.value = '';
    setStatus('topicStatus', 'Topic added.');
    await loadTopics();
    await loadDashboard();
  } catch {
    setStatus('topicStatus', 'Could not create that topic. It may already exist.', true);
  }
}

async function loadDashboard() { return true; }

function fillAskExample(text) {
  const input = document.getElementById('askQuestion');
  input.value = text;
  input.dispatchEvent(new Event('input', {bubbles:true}));
  input.focus();
}

function renderAskResponse(data) { renderGroundedAnswer(data); }
async function askInbox(event) { return submitInboxQuestion(event); }

async function loadAttention() { return true; }

async function previewCalendarForTask(task) {
  const version = ++calendarPreviewVersion;
  setStatus('calendarStatus', 'Building calendar preview…');
  try {
    const preview = await fetchJson(
      '/api/calendar/preview',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task_id: task.id})
      },
      'Unable to preview calendar event.'
    );
    if (version !== calendarPreviewVersion) return;
    const root = document.getElementById('calendarPreview');
    root.innerHTML = [
      '<strong>All-day preview</strong>',
      `<div>${esc(preview.title)} · ${esc(preview.start_date)} · All-day / date only (${esc(preview.timezone)})</div>`,
      '<p class="muted">Preview only. Confirmation records a local receipt; it does not add an event to an external calendar.</p>'
    ].join('');
    const confirmBtn = actionButton('Record local confirmation', '', async () => {
      if (version !== calendarPreviewVersion) return;
      confirmBtn.disabled = true;
      try {
        const confirmed = await fetchJson(
          '/api/calendar/confirm',
          {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({draft_id: preview.draft_id})
          },
          'Unable to confirm calendar action.'
        );
        if (version !== calendarPreviewVersion) return;
        setStatus(
          'calendarStatus',
          confirmed.status === 'confirmed_mock'
            ? 'Mock calendar confirmation recorded. No external event created.'
            : `Calendar action status: ${confirmed.status}`
        );
        const receipt = document.createElement('div');
        receipt.className = 'provenance';
        receipt.textContent = 'Local confirmation saved.';
        root.appendChild(receipt);
      } catch (error) {
        if (version === calendarPreviewVersion) {
          setStatus('calendarStatus', error.message || 'Calendar confirm blocked or failed.', true);
          confirmBtn.disabled = false;
        }
      }
    });
    root.appendChild(confirmBtn);
    setStatus('calendarStatus', 'Preview ready — confirm explicitly to record a mock receipt.');
  } catch (error) {
    if (version === calendarPreviewVersion) {
      setStatus('calendarStatus', error.message || 'Calendar preview failed.', true);
    }
  }
}

let authHealth = null;

async function loadAuthUser() {
  try {
    authHealth = await fetchJson('/api/health', {}, 'Unable to load health.');
  } catch {
    authHealth = null;
  }

  const devPanel = document.getElementById('devLoginPanel');
  if (devPanel) {
    const showDev = !!(authHealth && authHealth.allow_dev_login);
    devPanel.hidden = !showDev;
  }

  const googleHint = document.getElementById('googleAuthHint');
  const googleButton = document.getElementById('googleSignInButton');
  if (authHealth && !authHealth.google_oauth_configured) {
    if (googleHint) {
      googleHint.textContent =
        'Google sign-in is unavailable on this installation. Contact the person who runs it.';
    }
    if (googleButton) {
      googleButton.hidden = true;
      googleButton.setAttribute('aria-disabled', 'true');
      googleButton.classList.add('secondary');
    }
  } else if (googleHint) {
    if(googleButton){googleButton.hidden=false;googleButton.removeAttribute('aria-disabled');}
    googleHint.textContent = '';
  }

  try {
    const user = await fetchJson('/api/auth/me', {}, 'Unable to load session.');
    window.dispatchEvent(new CustomEvent('lifeinbox:owner', {detail: user}));
    document.getElementById('authUser').textContent =
      `Signed in as ${user.display_name || user.email}`;
    const detail = document.getElementById('settingsSessionDetail');
    if (detail) {
      detail.textContent =
        `Owner #${user.id} · ${user.display_name} <${user.email}> · provider ${user.auth_provider}`
        + (user.google_sub ? ` · Google sub ${user.google_sub}` : '');
    }
  } catch {
    window.dispatchEvent(new CustomEvent('lifeinbox:owner', {detail: null}));
    document.getElementById('authUser').textContent = 'Not signed in';
    const detail = document.getElementById('settingsSessionDetail');
    if (detail) {
      detail.textContent = 'Not signed in. Use Google (or Dev Login locally) to create a private owner.';
    }
  }
}

async function devLogin(event) {
  event.preventDefault();
  const email = document.getElementById('devLoginEmail').value.trim();
  if (!email) return;
  window.dispatchEvent(new CustomEvent('lifeinbox:owner', {detail: null}));
  try {
    const user = await fetchJson(
      '/api/auth/dev-login',
      {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email})
      },
      'Unable to sign in.'
    );
    setStatus('authStatus', `Signed in as ${user.email}`);
    document.getElementById('focusedReviews')?.replaceChildren();
    invalidateCalendarPreview();
    await loadAuthUser();
    await loadTopics();
    await loadLatestAnalysis();
    await loadNotes();
    await loadTasks();
    await loadAttention();
    await loadDashboard();
    await loadAnalysisReviews();
  } catch {
    await loadAuthUser();
    setStatus('authStatus', 'Sign-in failed. Your previous account is still active.', true);
  }
}

async function logoutSession() {
  window.dispatchEvent(new CustomEvent('lifeinbox:owner', {detail: null}));
  try {
    await fetchJson('/api/auth/logout', {method: 'POST'}, 'Unable to sign out.');
    setStatus('authStatus', 'Signed out.');
    document.getElementById('focusedReviews')?.replaceChildren();
    invalidateCalendarPreview();
    await loadAuthUser();
    await loadTopics();
    await loadLatestAnalysis();
    await loadNotes();
    await loadTasks();
    await loadAttention();
    await loadDashboard();
    await loadAnalysisReviews();
  } catch {
    await loadAuthUser();
    setStatus('authStatus', 'Sign-out failed.', true);
  }
}

const captureTimezoneHint = document.getElementById('captureTimezoneHint');
if (captureTimezoneHint) {
  const timezone = browserTimezone();
  captureTimezoneHint.textContent = timezone
    ? `Dates use ${timezone.replaceAll('_', ' ')}.`
    : 'Relative dates use your capture time.';
}

window.addEventListener('DOMContentLoaded', () => {

syncControlsFromUrl();
setJourneyStep('capture');
showDemoView(
  new URLSearchParams(window.location.search).get('view') || 'inbox', {history: false, focus: false}
);
loadAuthUser();
loadAnalysisReviews();
loadNotes();
loadLatestAnalysis();
loadTasks();
loadTopics();
loadDashboard();
loadAttention();
updateAskKeyBanner();

});

window.addEventListener('popstate', () => {
  if(window.history.state?.ownerId != null && window.history.state.ownerId !== window.lifeInboxOwnerId) resetOwnerNavigation();
  syncControlsFromUrl();
  showDemoView(
    new URLSearchParams(window.location.search).get('view') || 'inbox', {history: false}
  );
  updateFiltersLabel();
  if (itemFilterParams().toString() !== loadedItemFilterKey) loadNotes();
  if (new URLSearchParams(location.search).get('view') === 'tasks') loadTasks();
});

document.addEventListener('keydown', event => { if (event.key === 'Escape' && document.getElementById('accountMenu')?.open) { document.getElementById('accountMenu').open = false; document.querySelector('#accountMenu summary').focus(); } });

// Keep keyboard focus in an open modal, including Chrome's mobile Tab boundary.
document.addEventListener('keydown', event => {
  if (event.key !== 'Tab') return;
  const dialogs = [...document.querySelectorAll('dialog[open]')];
  const dialog = dialogs.find(node => node.contains(document.activeElement)) || dialogs.at(-1);
  if (!dialog) return;
  const controls = [...dialog.querySelectorAll('a[href], button, input, select, textarea, summary, audio[controls], [tabindex]:not([tabindex="-1"])')]
    .filter(node => {
      if (node.disabled || node.tabIndex === -1 || !node.getClientRects().length) return false;
      for (let parent = node.parentElement; parent && parent !== dialog; parent = parent.parentElement) {
        if (parent.tagName === 'DETAILS' && !parent.open) {
          const summary = parent.querySelector(':scope > summary');
          if (node !== summary && !summary?.contains(node)) return false;
        }
      }
      return true;
    });
  const first = controls[0], last = controls.at(-1);
  if (!first) { event.preventDefault(); return; }
  if (event.shiftKey && (document.activeElement === first || !controls.includes(document.activeElement))) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && (document.activeElement === last || !controls.includes(document.activeElement))) { event.preventDefault(); first.focus(); }
});
document.addEventListener('click', event => {
  for (const id of ['accountMenu', 'extraFilters']) {
    const menu = document.getElementById(id);
    if (menu?.open && !menu.contains(event.target)) menu.open = false;
  }
});
