/* Settings panel: connect a Google Calendar by its secret iCal address. */
function calendarSyncedLabel(iso) {
  if (!iso) return 'not yet';
  try { return new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(iso)); } catch { return iso; }
}
function renderCalendarFeed(feed) {
  const connected = !!(feed && feed.connected);
  document.getElementById('calendarFeedConnected').hidden = !connected;
  document.getElementById('calendarFeedForm').hidden = connected;
  if (!connected) return;
  const lines = [`Connected to ${feed.calendar_hint}. Meetings for the next ${feed.days} days are imported`];
  lines[0] += feed.sync_minutes ? `, and it checks again every ${feed.sync_minutes} minutes while Life Inbox is running.` : '.';
  lines.push(`Last sync: ${calendarSyncedLabel(feed.last_synced_at)}${feed.last_status === 'ok' ? ` · ${feed.last_imported} meeting${feed.last_imported === 1 ? '' : 's'} found` : ''}.`);
  document.getElementById('calendarFeedSummary').textContent = lines.join(' ');
  if (feed.last_status === 'error' && feed.last_error) setStatus('calendarFeedStatus', `Last sync failed: ${feed.last_error}`, true);
}
function calendarSyncMessage(sync) {
  if (!sync) return '';
  const parts = [`${sync.created} new meeting task${sync.created === 1 ? '' : 's'}`];
  if (sync.updated) parts.push(`${sync.updated} updated`);
  if (sync.removed) parts.push(`${sync.removed} cancelled meeting${sync.removed === 1 ? '' : 's'} dismissed`);
  return `Synced: ${parts.join(', ')}.`;
}
function refreshTasksAfterCalendarSync() {
  if (typeof loadSavedTasks === 'function') loadSavedTasks();
}
async function loadCalendarFeed() {
  try {
    renderCalendarFeed(await fetchJson('/api/calendar/feed', {}, 'Could not load calendar settings.'));
  } catch (error) {
    setStatus('calendarFeedStatus', error.message, true);
  }
}
async function connectCalendarFeed(event) {
  event.preventDefault();
  const input = document.getElementById('calendarFeedUrl');
  const button = document.getElementById('calendarConnectButton');
  button.disabled = true;
  setStatus('calendarFeedStatus', 'Connecting and importing meetings…');
  try {
    const result = await fetchJson('/api/calendar/feed', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ics_url: input.value})}, 'Could not connect the calendar.');
    input.value = '';
    renderCalendarFeed(result);
    setStatus('calendarFeedStatus', calendarSyncMessage(result.sync));
    refreshTasksAfterCalendarSync();
  } catch (error) {
    setStatus('calendarFeedStatus', error.message, true);
  } finally {
    button.disabled = false;
  }
}
async function syncCalendarFeed() {
  const button = document.getElementById('calendarSyncButton');
  button.disabled = true;
  setStatus('calendarFeedStatus', 'Syncing…');
  try {
    const result = await fetchJson('/api/calendar/feed/sync', {method: 'POST'}, 'Could not sync the calendar.');
    renderCalendarFeed(result);
    setStatus('calendarFeedStatus', calendarSyncMessage(result.sync));
    refreshTasksAfterCalendarSync();
  } catch (error) {
    setStatus('calendarFeedStatus', error.message, true);
    loadCalendarFeed();
  } finally {
    button.disabled = false;
  }
}
async function disconnectCalendarFeed() {
  if (!window.confirm('Disconnect Google Calendar? Meetings already imported stay in your tasks.')) return;
  try {
    renderCalendarFeed(await fetchJson('/api/calendar/feed', {method: 'DELETE'}, 'Could not disconnect the calendar.'));
    setStatus('calendarFeedStatus', 'Disconnected. Imported meetings stay in your tasks.');
  } catch (error) {
    setStatus('calendarFeedStatus', error.message, true);
  }
}
window.addEventListener('lifeinbox:owner', () => { setStatus('calendarFeedStatus', ''); loadCalendarFeed(); });
loadCalendarFeed();
