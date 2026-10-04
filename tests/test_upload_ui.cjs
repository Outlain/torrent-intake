// Development-only browser test; disposable container, stubbed submission APIs.
// Backend byte preservation is covered separately by Python/integration tests.
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {randomUUID} = require('node:crypto');
const puppeteer = require('puppeteer');
const docker = (...args) => execFileSync('docker', args, {encoding: 'utf8'}).trim();
const name = `ti-upload-ui-${randomUUID()}`;
let container, browser;

(async () => {
  docker('volume', 'create', name);
  try {
    container = docker('run', '-d', '--name', name, '--read-only', '--cap-drop', 'ALL',
      '--security-opt', 'no-new-privileges:true',
      '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777',
      '--tmpfs', '/events:rw,nosuid,nodev,noexec,size=16m,mode=1777',
      '-e', `TI_NAS_STAGING_LOCATIONS=${JSON.stringify([
        {id: 'main', label: 'Main NAS', path: '/nas/main/intake'},
        {id: 'archive', label: 'Archive NAS', path: '/nas/archive/intake'},
      ])}`, '-e', 'TI_DEFAULT_NAS_STAGING_ID=main',
      '-e', 'TI_POST_PROMOTION_COPY_ENABLED=false', '-e', 'TI_POST_PROMOTION_COPY_DESTINATION=/copy-target/fixed',
      '-e', `TI_POST_PROMOTION_COPY_RULES=${JSON.stringify([{source: '/downloads/<img src=x onerror=alert(1)>', destination: '/copy-target/fixed', enabled: true}])}`,
      '-v', `${name}:/app/data`, '-p', '127.0.0.1::8000',
      process.env.TI_UI_TEST_IMAGE || 'torrent-intake:test');
    const origin = `http://127.0.0.1:${docker('port', container, '8000/tcp').split(':').pop()}`;
    let ready = false;
    for (let n = 0; n < 100; n++) {
      try { ready = (await fetch(`${origin}/controller/status`)).ok; } catch (_) {}
      if (ready) break;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert(ready, 'test server failed to start');
    browser = await puppeteer.launch({headless: true, args: ['--no-sandbox'],
      ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
    const page = await browser.newPage();
    const errors = [], submitted = [], moves = [], hookRetries = [], finalDestinationEdits = [];
    const queuedJob = {id: 'queued-test', state: 'waiting_for_local_space', staging_actual: 'local',
      final_parent: '/downloads/Original', torrent_name: '<img src=x onerror=alert(1)> series', can_edit_final_destination: true,
      nas_staging_id: 'main', nas_staging_label: 'Main NAS', nas_staging_path: '/nas/main/intake'};
    const unavailableFallbackJob = {...queuedJob, id: 'unavailable-fallback-test', state: 'waiting_for_nas'};
    const unavailableNasJob = {...unavailableFallbackJob, id: 'unavailable-nas-test', staging_actual: 'nas'};
    const hookedJob = {id: 'hook-test', state: 'done', staging_actual: 'nas', nas_staging_id: 'archive',
      nas_staging_label: 'Archive NAS', nas_staging_path: '/nas/archive/intake', hook_status: 'failed',
      hook_error: 'Test hook failure', hook_exit_code: 7, hook_output: '<img src=x onerror=alert(1)> safe text'};
    const copiedJob = {...hookedJob, id: 'copy-test', hook_kind: 'copy', hook_status: 'interrupted',
      hook_error: 'Copy interrupted; inspect partial data before retrying', hook_destination: '/copy-target/Movies',
      hook_copy_source_root: '/downloads/Movies', hook_copy_relative_path: 'Action/Film'};
    const scanningJob = {...queuedJob, id: 'scanning-test', state: 'scanning', can_edit_final_destination: false};
    let finalDestinationPaused = false, knownPathRequests = 0;
    let failOnce = true, inFlight = 0, peak = 0;
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    await page.setRequestInterception(true);
    page.on('request', async request => {
      const path = new URL(request.url()).pathname;
      const respond = (body, status = 200) => request.respond({status, contentType: 'application/json', body: JSON.stringify(body)});
      if (request.method() === 'PATCH' && path === '/jobs/queued-test/final-destination') {
        const payload = JSON.parse(request.postData()); finalDestinationEdits.push(payload);
        if (finalDestinationPaused) return respond({detail: 'Intake is paused for maintenance. Verify and resume before editing jobs.'}, 503);
        if (!queuedJob.can_edit_final_destination) return respond({detail: 'Scanning has begun; final location can no longer be edited.'}, 409);
        if (payload.expected_final_parent !== queuedJob.final_parent) return respond({detail: 'Final location changed in another request. Close and reopen the editor.'}, 409);
        queuedJob.final_parent = payload.final_parent;
        return respond(queuedJob);
      }
      if (request.method() === 'POST' && ['/jobs', '/jobs/torrent', '/jobs/bulk'].includes(path)) {
        inFlight++; peak = Math.max(peak, inFlight);
        const body = request.postData() || await request.fetchPostData();
        submitted.push({path, body});
        await new Promise(resolve => setTimeout(resolve, 150));
        inFlight--;
        if (path === '/jobs/torrent' && failOnce) {
          failOnce = false;
          return respond({detail: 'Test upload failure; retained for retry'}, 409);
        }
        return respond(path === '/jobs/bulk' ? {created: JSON.parse(body).jobs.length, failed: 0, errors: {}} : {id: randomUUID()});
      }
      if (path === '/jobs/bulk-move-to-nas') {
        const payload = JSON.parse(request.postData()); moves.push(payload);
        return respond({moved: 1, failed: 0, errors: {}, processed_ids: payload.job_ids});
      }
      if (['/admin/jobs/hook-test/retry-hook', '/admin/jobs/copy-test/retry-hook'].includes(path)) { hookRetries.push({path, headers: request.headers()}); return respond({hook_status: 'pending'}); }
      if (path === '/jobs') return respond([queuedJob, unavailableFallbackJob, unavailableNasJob, hookedJob, copiedJob, scanningJob]);
      if (path === '/qbt/final-path-suggestions') { knownPathRequests++; return respond({paths: ['/downloads/Shows']}); }
      if (path === '/fs/final-path-suggestions') return respond({paths: ['/downloads/Movies', '/downloads/TV']});
      if (path === '/qbt/tags') return respond({tags: ['Review']});
      if (path === '/qbt/categories') return respond({categories: []});
      if (path.startsWith('/qbt/') || path.startsWith('/fs/')) return respond({paths: []});
      return request.continue();
    });
    const fill = (selector, value) => page.$eval(selector, (node, value) => {
      node.value = value; node.dispatchEvent(new Event('input', {bubbles: true}));
    }, value);
    // Job rows are replaced by the normal refresh. Re-resolve detached nodes
    // while waiting for a visible, enabled click target instead of holding one.
    const click = selector => page.locator(selector).click();
    const choose = names => page.$eval('#torrent-file-input', (node, names) => {
      const selection = new DataTransfer();
      for (const name of names) selection.items.add(new File(['test metadata'], name, {type: 'application/x-bittorrent'}));
      node.files = selection.files;
      node.dispatchEvent(new Event('change', {bubbles: true}));
    }, names);
    const magnet = letter => `magnet:?xt=urn:btih:${letter.repeat(40)}`;
    const finished = () => page.waitForFunction(() => !document.querySelector('#bulk-dialog-submit').disabled);

    for (const width of [1440, 390]) {
      queuedJob.final_parent = '/downloads/Original';
      await page.setViewport({width, height: 900});
      await page.goto(`${origin}/ui`, {waitUntil: 'networkidle0'});
      const editSelector = '[data-edit-final-destination="queued-test"]';
      const editsBefore = finalDestinationEdits.length;
      const suggestionsBefore = knownPathRequests;
      assert.equal(await page.$('[data-edit-final-destination="scanning-test"]'), null);
      assert.equal(await page.$('[data-edit-final-destination="hook-test"]'), null);
      assert.equal(await page.$('#jobs-tbody img'), null, 'torrent name must remain plain text');
      await click(editSelector);
      assert.equal(await page.$eval('#edit-final-parent-input', node => node.value), '/downloads/Original');
      assert.equal(await page.$eval('#final-destination-job', node => node.textContent), queuedJob.torrent_name);
      assert.equal(await page.$('#final-destination-dialog img'), null);
      await fill('#edit-final-parent-input', '/downloads/Cancelled');
      await click('#final-destination-cancel');
      assert.equal(finalDestinationEdits.length, editsBefore, 'cancel must not send an update');
      await click(editSelector);
      await fill('#edit-final-parent-input', '/downloads/Shows');
      await page.evaluate(() => loadJobs({silent: true}));
      assert.equal(await page.$eval('#edit-final-parent-input', node => node.value), '/downloads/Shows', 'refresh must retain the edit draft');
      await page.waitForFunction(() => [...document.querySelectorAll('#edit-final-parent-suggestions option')].some(node => node.value === '/downloads/Shows'));
      const editDimensions = await page.$eval('#final-destination-dialog', node => ({client: node.clientWidth, scroll: node.scrollWidth}));
      assert(editDimensions.scroll <= editDimensions.client + 1, `destination editor overflows at ${width}px`);
      await click('#final-destination-save');
      await page.waitForFunction(() => !document.querySelector('#final-destination-dialog').open);
      assert.deepEqual(finalDestinationEdits.at(-1), {final_parent: '/downloads/Shows', expected_final_parent: '/downloads/Original'});
      assert.match(await page.$eval(editSelector, node => node.parentElement.textContent), /\/downloads\/Shows/);
      assert.equal(knownPathRequests, suggestionsBefore, 'editing must reuse known suggestions, not poll qBittorrent again');

      await click(editSelector);
      await fill('#edit-final-parent-input', '/downloads/My draft');
      queuedJob.final_parent = '/downloads/<img src=x onerror=alert(1)>';
      await page.evaluate(() => loadJobs({silent: true}));
      assert.equal(await page.$('#jobs-tbody img'), null, 'final location must also be plain text');
      await click('#final-destination-save');
      await page.waitForFunction(() => document.querySelector('#final-destination-status').textContent.includes('another request'));
      assert.equal(finalDestinationEdits.at(-1).expected_final_parent, '/downloads/Shows', 'refresh must not overwrite the captured concurrency check');
      assert.equal(await page.$eval('#edit-final-parent-input', node => node.value), '/downloads/My draft');
      await click('#final-destination-cancel');
      await click(editSelector);
      assert.equal(await page.$eval('#edit-final-parent-input', node => node.value), queuedJob.final_parent);
      queuedJob.can_edit_final_destination = false;
      await page.evaluate(() => loadJobs({silent: true}));
      await click('#final-destination-save');
      await page.waitForFunction(() => document.querySelector('#final-destination-status').textContent.includes('Scanning has begun'));
      await click('#final-destination-cancel');
      assert.equal(await page.$(editSelector), null, 'scan-start refresh must remove the edit action');
      queuedJob.can_edit_final_destination = true;
      await page.evaluate(() => loadJobs({silent: true}));
      await click(editSelector);
      finalDestinationPaused = true;
      await click('#final-destination-save');
      await page.waitForFunction(() => document.querySelector('#final-destination-status').textContent.includes('paused for maintenance'));
      assert(await page.$eval('#final-destination-dialog', node => node.open));
      await page.keyboard.press('Escape');
      assert(!(await page.$eval('#final-destination-dialog', node => node.open)));
      finalDestinationPaused = false;
      assert(await page.$eval('#nas-staging-field', node => node.hidden));
      assert.equal(await page.$eval('#nas-staging-select', node => node.value), 'main');
      assert.match(await page.$eval('#jobs-tbody .cell-stage', node => node.textContent), /local.*NAS fallback: Main NAS/);
      assert.equal(await page.$eval('.job-hook-result pre', node => node.textContent), hookedJob.hook_output);
      assert.equal(await page.$('.job-hook-result img'), null, 'hook output must be text, never HTML');
      assert.match(await page.$eval('.job-hook-result[data-job-id="hook-test"] summary', node => node.textContent), /Script status: failed/);
      assert.match(await page.$eval('.job-hook-result[data-job-id="copy-test"]', node => node.textContent), /Copy status: interrupted.*Copy destination: \/copy-target\/Movies\/Action\/Film.*Saved copy rule: \/downloads\/Movies → \/copy-target\/Movies.*Copy output:.*Retry copy/s);
      assert.equal(await page.$('.job-hook-result[data-job-id="copy-test"] img'), null, 'copy output must also be text, never HTML');
      assert(await page.$eval('[data-retry-hook]', node => node.hidden), 'hook retry must be administration-only');
      assert.equal(await page.$eval('[data-state="waiting_for_nas"]', node => node.textContent), 'Waiting for NAS mount');
      assert(await page.evaluate(() => {
        const queued = getComputedStyle(document.querySelector('[data-state="waiting_for_local_space"]'));
        const waitingNas = getComputedStyle(document.querySelector('[data-state="waiting_for_nas"]'));
        return queued.backgroundColor === waitingNas.backgroundColor && queued.color === waitingNas.color;
      }), 'NAS waits must have the same amber style as local capacity waits');
      await page.select('#move-nas-staging-select', 'archive');
      await click('.job-checkbox[data-job-id="queued-test"]');
      const moved = page.waitForResponse(response => response.url().endsWith('/jobs/bulk-move-to-nas'));
      await click('#move-selected-to-nas-button'); await moved;
      assert.deepEqual(moves.at(-1), {job_ids: ['queued-test'], nas_staging_id: 'archive'});
      await page.waitForFunction(() => !document.querySelector('.job-checkbox[data-job-id="queued-test"]').checked);
      await click('.job-checkbox[data-job-id="unavailable-nas-test"]');
      assert(await page.$eval('#move-selected-to-nas-button', node => node.disabled), 'already-NAS jobs must not use the local-to-NAS action');
      await click('.job-checkbox[data-job-id="unavailable-nas-test"]');
      await click('.job-checkbox[data-job-id="unavailable-fallback-test"]');
      assert(!(await page.$eval('#move-selected-to-nas-button', node => node.disabled)), 'locally staged jobs waiting for NAS must allow another target');
      const fallbackMoved = page.waitForResponse(response => response.url().endsWith('/jobs/bulk-move-to-nas'));
      await click('#move-selected-to-nas-button'); await fallbackMoved;
      assert.deepEqual(moves.at(-1), {job_ids: ['unavailable-fallback-test'], nas_staging_id: 'archive'});
      await fill('#final-parent-input', '/downloads/Shows');
      await fill('#custom-tag-input', 'Review');
      await click('#custom-tag-add-button');
      await fill('#magnet-input', magnet('a'));
      await choose(['first.torrent']);
      await choose(['second.torrent']);
      assert.match(await page.$eval('#torrent-file-summary', node => node.textContent), /2 selected/);
      await click('#submit-button');
      assert.equal(await page.$$eval('.bulk-row', rows => rows.length), 3);
      await click('#bulk-use-same-settings');
      await fill('.bulk-row[data-index="1"] .bulk-final-parent', '/downloads/Other');
      await page.select('.bulk-row[data-index="1"] .bulk-staging-preference', 'nas');
      await page.select('.bulk-row[data-index="1"] .bulk-nas-staging', 'archive');
      const dimensions = await page.$eval('.bulk-dialog-panel', node => ({client: node.clientWidth, scroll: node.scrollWidth}));
      assert(dimensions.scroll <= dimensions.client + 1, `bulk review overflows at ${width}px`);
      failOnce = true;
      const before = submitted.length;
      await click('#bulk-dialog-submit');
      await page.waitForFunction(() => document.querySelector('#bulk-dialog-submit').disabled);
      await page.evaluate(() => document.querySelector('#bulk-dialog-submit').click()); // Cannot double submit.
      await finished();
      assert.equal(submitted.length - before, 3);
      assert.deepEqual(submitted.slice(before).map(item => item.path), ['/jobs', '/jobs/torrent', '/jobs/torrent']);
      assert(submitted.slice(before).every(item => item.body.includes('Review')));
      assert.equal(await page.$$eval('.bulk-row', rows => rows.length), 1);
      assert.equal(await page.$eval('.bulk-final-parent', node => node.value), '/downloads/Other');
      assert.equal(await page.$eval('.bulk-staging-preference', node => node.value), 'nas');
      assert.equal(await page.$eval('.bulk-nas-staging', node => node.value), 'archive');
      assert.match(await page.$eval('#bulk-dialog-status', node => node.textContent), /2 created, 1 failed/);
      await click('#bulk-dialog-submit');
      await page.waitForFunction(() => document.querySelector('#bulk-dialog').hidden);
      assert.equal(submitted.length - before, 4, 'retry must not re-add successful items');
      assert.match(submitted.at(-1).body, /\/downloads\/Other/);
      assert.match(submitted.at(-1).body, /"nas_staging_id":"archive"/);
      assert.match(await page.$eval('#torrent-file-summary', node => node.textContent), /No .torrent/);

      // Single file and unchanged all-magnet bulk both remain usable.
      await choose(['single.torrent']);
      await click('#submit-button');
      await page.waitForFunction(() => !document.querySelector('#submit-button').disabled);
      assert.equal(submitted.at(-1).path, '/jobs/torrent');
      assert.match(submitted.at(-1).body, /"nas_staging_id":null/);
      await page.select('#staging-preference-select', 'nas');
      await page.select('#nas-staging-select', 'archive');
      await fill('#magnet-input', magnet('e'));
      await click('#submit-button');
      await page.waitForFunction(() => !document.querySelector('#submit-button').disabled);
      assert.equal(JSON.parse(submitted.at(-1).body).nas_staging_id, 'archive');
      await fill('#magnet-input', `${magnet('b')}\n${magnet('c')}`);
      await click('#submit-button');
      await click('#bulk-dialog-submit');
      await page.waitForFunction(() => document.querySelector('#bulk-dialog').hidden);
      assert.equal(submitted.at(-1).path, '/jobs/bulk');
      assert(JSON.parse(submitted.at(-1).body).jobs.every(job => job.nas_staging_id === 'archive'));
      await choose(['not-a-torrent.txt']);
      assert.match(await page.$eval('#form-status', node => node.textContent), /Rejected selection/);
      await choose(Array.from({length: 50}, (_, i) => `${i}.torrent`));
      await fill('#magnet-input', magnet('d'));
      await click('#submit-button');
      assert.match(await page.$eval('#form-status', node => node.textContent), /up to 50/);
      await click('#torrent-files-clear');
      assert.match(await page.$eval('#torrent-file-summary', node => node.textContent), /No .torrent/);
    }
    // Deployment overrides remain read-only even after administration unlock.
    const token = docker('exec', container, 'cat', '/app/data/admin-token');
    await click('#settings-open-button');
    await fill('#admin-token-input', token);
    await click('#admin-unlock');
    await page.waitForFunction(() => !document.querySelector('#edit-qbt_host').disabled);
    assert.equal(await page.$('#edit-nas_staging_locations'), null);
    assert.equal(await page.$('#edit-default_nas_staging_id'), null);
    assert.equal(await page.$('#edit-post_promotion_copy_enabled'), null, 'copy environment overrides must remain read-only');
    assert.equal(await page.$('#edit-post_promotion_copy_destination'), null);
    assert.equal(await page.$('#edit-post_promotion_copy_rules'), null);
    assert(await page.$$eval('.copy-rule-row input', inputs => inputs.every(input => input.disabled)), 'rule environment overrides stay read-only');
    assert(await page.$eval('#copy-rule-add', node => node.disabled));
    assert.equal(await page.$('#copy-rule-editor img'), null, 'rule paths must never render HTML');
    assert.equal(await page.$eval('.copy-rule-source', node => node.value), '/downloads/<img src=x onerror=alert(1)>');
    assert(await page.$$eval('.nas-location-row input', inputs => inputs.every(input => input.disabled)));
    assert(!(await page.$eval('[data-retry-hook]', node => node.hidden)));
    const controllerState = await (await fetch(`${origin}/controller/status`)).json();
    assert.equal(await page.$eval('[data-retry-hook]', node => node.disabled), !controllerState.paused || !controllerState.drained || controllerState.restart_required, 'hook retry requires a drained pause');
    if (!controllerState.paused) await click('#controller-pause');
    await page.waitForFunction(() => !document.querySelector('[data-retry-hook]').disabled);
    await click('#settings-close-button');
    await click('.job-hook-result[data-job-id="hook-test"] summary');
    const retried = page.waitForResponse(response => response.url().endsWith('/admin/jobs/hook-test/retry-hook'));
    await click('[data-retry-hook="hook-test"]'); await retried;
    assert(hookRetries.at(-1).headers['x-ti-admin-token'] === token, 'hook retry must use the unlocked administrator token');
    await click('.job-hook-result[data-job-id="copy-test"] summary');
    const copyRetried = page.waitForResponse(response => response.url().endsWith('/admin/jobs/copy-test/retry-hook'));
    await click('[data-retry-hook="copy-test"]'); await copyRetried;
    assert(hookRetries.at(-1).headers['x-ti-admin-token'] === token, 'copy retry must use the unlocked administrator token');
    assert.equal(peak, 1, 'mixed bulk requests must be sequential');
    assert.deepEqual(errors, []);
    console.log('PASS desktop/mobile final-destination editing/cancel/stale/scan-start/paused errors/draft preservation/escaping, mixed intake, named NAS/default selection, manual NAS moves, per-row retry, environment locks, escaped copy/script output, authenticated copy/script retry, tags and selection limits');
  } finally {
    if (browser) await browser.close();
    if (container) docker('rm', '-f', container);
    docker('volume', 'rm', name);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
