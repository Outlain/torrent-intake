window.TISettings = (() => {
  const el = (id) => document.getElementById(id);
  const fields = [...document.querySelectorAll('[data-local-setting]')];
  const clears = [...document.querySelectorAll('[data-clear-setting]')];
  const baselines = new Map(fields.map(input => [input.dataset.localSetting, input.value]));
  let token = '', revision = '', busy = false, reviewed = null;
  let controller = {paused: false, drained: false, restart_required: false};

  function draft() {
    const values = {};
    for (const input of fields) {
      const name = input.dataset.localSetting;
      const clear = clears.find(box => box.dataset.clearSetting === name);
      if (clear?.checked) { values[name] = null; continue; }
      if (input.value === baselines.get(name) || (input.dataset.type === 'password' && !input.value)) continue;
      values[name] = input.dataset.type === 'boolean' ? input.value === 'true' : input.value;
    }
    return values;
  }

  function filter() {
    const query = el('settings-search').value.trim().toLowerCase();
    const selected = el('settings-section').value;
    let count = 0;
    document.querySelectorAll('[data-settings-group]').forEach(group => {
      let matches = 0;
      group.querySelectorAll('[data-setting-item]').forEach(card => {
        card.hidden = Boolean(query) && !card.dataset.settingSearch.toLowerCase().includes(query);
        if (!card.hidden) matches++;
      });
      group.hidden = query ? !matches : group.dataset.section !== selected;
      if (!group.hidden) count += matches;
    });
    el('settings-backup').hidden = Boolean(query) || selected !== 'backup';
    el('settings-no-results').hidden = !query || count > 0;
  }

  function updateControls() {
    const count = Object.keys(draft()).length;
    const locked = !token || busy || controller.restart_required;
    fields.forEach(input => {
      const advanced = input.dataset.advanced === 'true';
      input.disabled = locked || (advanced && !el('advanced-unlock').checked);
      const card = input.closest('[data-setting-item]');
      card.querySelector('.setting-lock-reason').textContent = controller.restart_required
        ? 'Saved edits are pending. Restart in Portainer before editing again.'
        : !token ? 'Unlock administration above to edit.'
        : advanced && !el('advanced-unlock').checked ? 'Enable the advanced warning unlock above to edit.'
        : 'Edits are a draft until reviewed and saved. A restart is required to apply them.';
      if (advanced) card.querySelector('.setting-badge').textContent = input.disabled ? 'Advanced lock' : 'Advanced';
    });
    clears.forEach(box => { box.disabled = locked || el(`edit-${box.dataset.clearSetting}`).disabled; });
    el('advanced-unlock').disabled = locked;
    el('admin-controls').hidden = !token;
    el('settings-unlock-fields').hidden = Boolean(token);
    el('settings-access-state').textContent = token ? 'Administration unlocked for this page' : 'View mode · unlock to edit';
    el('settings-draft-count').textContent = controller.restart_required ? 'Saved changes require a Portainer restart' : count ? `${count} unsaved change${count === 1 ? '' : 's'}` : 'No unsaved changes';
    el('settings-review-button').disabled = locked || !count;
    el('settings-discard').disabled = locked || !count;
    el('settings-save').disabled = locked || !reviewed || (reviewed?.advanced && !el('advanced-confirm').checked);
    el('settings-review-cancel').disabled = busy;
    el('controller-pause').disabled = !token || busy || controller.paused;
    el('controller-resume').disabled = locked || !controller.drained || count > 0;
    el('setup-check').disabled = !token || busy;
    el('qbt-test').disabled = locked;
    el('admin-unlock').disabled = busy;
    el('admin-lock').disabled = busy;
    ['backup-download', 'backup-restore'].forEach(id => { el(id).disabled = locked || !controller.drained || count > 0; });
    el('settings-restart-guide').hidden = !controller.restart_required;
    el('controller-state').textContent = controller.paused
      ? `Intake paused · ${controller.drained ? 'drained, ready for maintenance' : 'waiting for workers to stop'}. ${controller.reason || ''}`
      : 'Intake is running. Editing fields does not change active settings until you save and restart.';
  }

  function clearErrors() {
    document.querySelectorAll('[data-field-error]').forEach(node => { node.textContent = ''; node.hidden = true; });
    fields.forEach(input => input.removeAttribute('aria-invalid'));
    el('admin-result').classList.remove('error');
  }

  function checksList(checks) {
    el('setup-results').replaceChildren();
    for (const check of checks) {
      const item = document.createElement('li');
      item.textContent = `${check.ok ? 'Passed' : 'Needs attention'} — ${check.name}: ${check.message}`;
      el('setup-results').append(item);
    }
  }

  function showError(error) {
    el('admin-result').textContent = error.message;
    el('admin-result').classList.add('error');
    if (error.checks) checksList(error.checks);
    let first = null;
    for (const [name, message] of Object.entries(error.fields || {})) {
      const card = el(`setting-${name}`), line = el(`error-${name}`), input = el(`edit-${name}`);
      if (!card || !line) { el('admin-result').textContent += ` ${message}`; continue; }
      line.textContent = message; line.hidden = false;
      input?.setAttribute('aria-invalid', 'true');
      first ||= card;
    }
    if (first) {
      el('settings-search').value = '';
      el('settings-section').value = first.closest('[data-section]').dataset.section;
      filter(); first.scrollIntoView({block: 'center'});
    } else {
      el('admin-result').scrollIntoView({block: 'center'});
    }
  }

  async function request(path, options = {}) {
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 60000);
    try {
      const response = await fetch(path, {...options, signal: abort.signal, cache: 'no-store',
        headers: {'X-TI-Admin-Token': token, ...(options.headers || {})}});
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const detail = body.detail;
        const error = new Error(typeof detail === 'string' ? detail : detail?.message || `Request failed (${response.status}).`);
        error.fields = detail?.fields; error.checks = detail?.checks;
        throw error;
      }
      return response;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Request timed out. Check the controller status before retrying; the server may still be finishing the operation.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function post(path, payload = {}) {
    return (await request(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)})).json();
  }
  async function refreshStatus() {
    controller = await (await request('/controller/status')).json();
    updateControls();
  }
  async function loadAdminState() {
    const state = await (await request('/admin/status')).json();
    controller = state; revision = state.revision;
    for (const group of state.settings) for (const setting of group.settings) {
      const card = el(`setting-${setting.name}`), input = el(`edit-${setting.name}`);
      if (!card) continue;
      card.querySelector('[data-active-value]').textContent = setting.current;
      const pending = card.querySelector('[data-pending-value]');
      pending.hidden = setting.pending === null;
      pending.textContent = setting.pending === null ? '' : `Saved for next restart: ${setting.pending}`;
      if (input) {
        input.value = setting.input_type === 'boolean' ? String(setting.input_value) : setting.input_value ?? '';
        baselines.set(setting.name, input.value);
      }
    }
    clears.forEach(box => { box.checked = false; });
    const size = state.backup.database_bytes;
    el('backup-database-size').textContent = `${size === null ? 'Database size unavailable' : `Database: ${(size / 1048576).toFixed(2)} MiB`}. Portable backup limit: ${state.backup.database_limit_bytes / 1048576} MiB; this is not a torrent-size limit.`;
    updateControls();
  }
  async function action(operation) {
    if (busy) return;
    busy = true; clearErrors(); updateControls();
    el('admin-result').textContent = 'Working…';
    try { await operation(); } catch (error) { showError(error); }
    finally {
      try { await refreshStatus(); } catch (_) { /* Keep the operation's result visible. */ }
      busy = false; updateControls();
    }
  }
  function invalidateReview() {
    reviewed = null; el('settings-review').hidden = true; el('advanced-confirm').checked = false;
    updateControls();
  }
  fields.forEach(input => input.addEventListener('input', invalidateReview));
  clears.forEach(input => input.addEventListener('change', invalidateReview));
  el('advanced-unlock').addEventListener('change', updateControls);
  el('advanced-confirm').addEventListener('change', updateControls);
  el('settings-section').addEventListener('change', () => { el('settings-search').value = ''; filter(); });
  el('settings-discard').addEventListener('click', () => {
    fields.forEach(input => { input.value = baselines.get(input.dataset.localSetting); });
    clears.forEach(box => { box.checked = false; });
    clearErrors(); invalidateReview(); el('admin-result').textContent = 'Draft discarded. No saved settings were changed.';
  });
  el('settings-review-cancel').addEventListener('click', invalidateReview);
  el('admin-unlock').addEventListener('click', () => action(async () => {
    token = el('admin-token-input').value.trim(); el('admin-token-input').value = '';
    try { await loadAdminState(); } catch (error) { token = ''; throw error; }
    el('admin-result').textContent = 'Unlocked. Edit fields, then Review changes. Portainer overrides and deployment-only settings remain locked.';
  }));
  el('admin-lock').addEventListener('click', () => {
    if (Object.keys(draft()).length && !confirm('Discard unsaved edits and lock administration?')) return;
    el('settings-discard').click(); token = ''; el('advanced-unlock').checked = false;
    fields.filter(input => input.dataset.type === 'password').forEach(input => { input.value = ''; });
    el('backup-passphrase').value = ''; el('admin-result').textContent = 'Editing locked.'; updateControls();
  });
  el('settings-review-button').addEventListener('click', () => action(async () => {
    const updates = draft();
    const result = await post('/admin/settings/review', {settings: updates, revision});
    if (!result.changes.length) throw new Error('These values already match the active settings. No changes to save.');
    reviewed = {updates, revision: result.revision, advanced: result.changes.some(change => change.advanced)};
    el('settings-review-list').replaceChildren();
    for (const change of result.changes) {
      const item = document.createElement('li'); item.textContent = `${change.label}: ${change.before} → ${change.after}`;
      el('settings-review-list').append(item);
    }
    el('advanced-confirm-label').hidden = !reviewed.advanced; el('advanced-confirm').checked = false;
    el('settings-review').hidden = false; el('settings-review').scrollIntoView({block: 'center'}); el('settings-review').focus();
    el('admin-result').textContent = 'Review ready. Nothing has been saved or paused yet.';
  }));
  el('settings-save').addEventListener('click', () => action(async () => {
    if (!reviewed) throw new Error('Review your changes first.');
    const payload = {settings: reviewed.updates, revision: reviewed.revision, confirm_advanced: el('advanced-confirm').checked};
    await post('/admin/settings/review', payload); // Reject stale edits before pausing.
    await post('/admin/pause');
    const deadline = Date.now() + 120000;
    do {
      await refreshStatus();
      if (controller.drained) break;
      el('admin-result').textContent = 'Waiting for Intake workers to stop safely. Settings have not been saved yet.';
      await new Promise(resolve => setTimeout(resolve, 1000));
    } while (Date.now() < deadline);
    if (!controller.drained) throw new Error('Workers are still draining. This request has not saved changes. Keep Intake paused and retry when it reports drained.');
    await post('/admin/settings', payload);
    invalidateReview(); await loadAdminState();
    el('admin-result').textContent = 'Settings saved. Restart torrent-intake in Portainer, reload this page, then Verify & resume. Intake remains paused.';
    el('settings-restart-guide').scrollIntoView({block: 'center'});
  }));
  el('controller-pause').addEventListener('click', () => action(async () => {
    await post('/admin/pause'); el('admin-result').textContent = 'Pause requested. Wait for drained; qBittorrent downloads continue.';
  }));
  el('controller-resume').addEventListener('click', () => {
    if (!confirm('Confirm the media/staging mounts and qBittorrent state are correct, and the old controller is stopped. Resume scanning and the configured infection action?')) return;
    action(async () => { await post('/admin/resume', {confirm_external_state: true}); el('setup-results').replaceChildren(); el('admin-result').textContent = 'Checks passed. Intake resumed.'; });
  });
  el('setup-check').addEventListener('click', () => action(async () => {
    checksList((await post('/admin/checks')).checks);
    el('admin-result').textContent = 'Setup checks finished using active settings. Directory access is not proof that the intended NAS is mounted.';
  }));
  el('qbt-test').addEventListener('click', () => action(async () => {
    const updates = Object.fromEntries(Object.entries(draft()).filter(([name]) => ['qbt_host', 'qbt_username', 'qbt_password', 'qbt_web_url', 'qbt_request_timeout_seconds'].includes(name)));
    const result = await post('/admin/test-connection', {settings: updates, revision});
    el('qbt-test-result').textContent = result.message; el('qbt-test-result').classList.toggle('error', !result.ok);
    el('admin-result').textContent = result.message;
  }));
  el('backup-download').addEventListener('click', () => action(async () => {
    const passphrase = el('backup-passphrase').value;
    if (passphrase.length < 12) throw new Error('Choose a backup passphrase of at least 12 characters. It cannot be recovered.');
    const response = await request('/admin/backup', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({passphrase})});
    const url = URL.createObjectURL(await response.blob()), link = document.createElement('a');
    link.href = url; link.download = `torrent-intake-${new Date().toISOString().slice(0, 10)}.tibak`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000); el('backup-passphrase').value = '';
    el('admin-result').textContent = 'Encrypted backup downloaded. Keep the passphrase separately. Resume only if this remains the active controller.';
  }));
  el('backup-restore').addEventListener('click', () => {
    if (!confirm('Replace Intake database/settings at the next restart? A rollback copy will be kept. Downloaded files and qBittorrent state are not restored.')) return;
    action(async () => {
      const file = el('backup-upload').files[0]; if (!file) throw new Error('Select a .tibak backup first.');
      const phrase = el('backup-passphrase').value;
      const encoded = btoa(Array.from(new TextEncoder().encode(phrase), byte => String.fromCharCode(byte)).join(''));
      const response = await request('/admin/restore', {method: 'POST', headers: {'Content-Type': 'application/octet-stream', 'X-TI-Backup-Passphrase': encoded, 'X-TI-Confirm-Restore': 'replace-after-restart'}, body: file});
      el('backup-passphrase').value = ''; el('admin-result').textContent = (await response.json()).message;
    });
  });
  window.addEventListener('beforeunload', event => {
    if (Object.keys(draft()).length || busy) { event.preventDefault(); event.returnValue = ''; }
  });
  setInterval(() => { if (!el('settings-dialog').hidden && !busy) refreshStatus().catch(showError); }, 5000);
  updateControls();
  return {filter, onOpen: () => refreshStatus().catch(showError)};
})();
