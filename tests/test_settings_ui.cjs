// Optional browser regression test. Uses only a disposable container/data volume.
// Requires Docker, Node and development-only Puppeteer; no application dependency.
const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const {randomUUID} = require('node:crypto');
const puppeteer = require('puppeteer');

const docker = (...args) => execFileSync('docker', args, {encoding: 'utf8'}).trim();
const name = `ti-settings-ui-${randomUUID()}`;
const image = process.env.TI_UI_TEST_IMAGE || 'torrent-intake:test';
let browser, container;

(async () => {
  docker('volume', 'create', name);
  try {
    container = docker('run', '-d', '--name', name, '--read-only', '--cap-drop', 'ALL',
      '--security-opt', 'no-new-privileges:true',
      '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=256m,mode=1777',
      '--tmpfs', '/events:rw,nosuid,nodev,noexec,size=16m,mode=1777',
      '-v', `${name}:/app/data`, '-e', 'TI_DEBUG=false', '-p', '127.0.0.1::8000', image);
    const port = docker('port', container, '8000/tcp').split(':').pop();
    let origin = `http://127.0.0.1:${port}`;
    let ready = false;
    for (let attempt = 0; attempt < 100; attempt++) {
      try { ready = (await fetch(`${origin}/controller/status`)).ok; } catch (_) {}
      if (ready) break;
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    assert(ready, 'test server did not start');
    const token = docker('exec', container, 'cat', '/app/data/admin-token');
    browser = await puppeteer.launch({headless: true,
      ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {}),
      args: ['--no-sandbox']});
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('dialog', dialog => dialog.accept());
    const click = async selector => {
      await page.$eval(selector, node => node.scrollIntoView({block: 'center'}));
      await page.click(selector);
    };
    const fill = async (selector, value) => page.$eval(selector, (node, text) => {
      node.value = text; node.dispatchEvent(new Event('input', {bubbles: true}));
    }, value);
    const open = async () => {
      await page.goto(`${origin}/ui`, {waitUntil: 'networkidle0'});
      await click('#settings-open-button');
    };
    const unlock = async () => {
      await fill('#admin-token-input', token);
      await click('#admin-unlock');
      await page.waitForFunction(() => !document.querySelector('#edit-qbt_host').disabled);
    };
    const finished = () => page.waitForFunction(() => !document.querySelector('#admin-lock').disabled);

    await page.setViewport({width: 1440, height: 1000});
    await open();
    assert(await page.$eval('#edit-qbt_host', node => node.disabled));
    assert(await page.$eval('#nas-location-add', node => node.disabled));
    assert.equal(await page.$$eval('.nas-location-row', rows => rows.length), 1, 'legacy staging must render as one effective NAS');
    await unlock();
    assert.equal(await page.$('#edit-infected_action'), null);
    assert.equal(await page.$('#edit-database_url'), null);
    assert.equal(await page.$('#edit-debug'), null); // Environment override.
    assert.equal(await page.$('#deployment-notes'), null);
    assert(await page.$eval('#edit-large_media_chunk_mib', node => node.disabled));
    assert.equal(await page.$('#edit-post_promotion_script'), null, 'trusted executable must remain deployment-only');
    assert(await page.$eval('#edit-post_promotion_enabled', node => node.disabled));
    assert(await page.$eval('#edit-post_promotion_copy_enabled', node => node.disabled));
    assert(await page.$eval('#edit-post_promotion_copy_rules', node => node.disabled));
    assert(await page.$eval('#copy-rule-add', node => node.disabled));
    assert.equal(await page.$('#edit-post_promotion_copy_destination'), null, 'legacy destination must not be editable');
    assert(!(await page.$eval('#edit-post_promotion_timeout_seconds', node => node.disabled)));

    await page.select('#settings-section', 'storage');
    await click('#advanced-unlock');
    assert.match(await page.$eval('#copy-after-promotion-help', node => node.textContent), /\.intake-copy-mount/);
    assert.match(await page.$eval('#copy-after-promotion-help', node => node.textContent), /No custom script/);
    await page.select('#edit-post_promotion_enabled', 'true');
    await page.select('#edit-post_promotion_copy_enabled', 'true');
    assert.equal(await page.$eval('#edit-post_promotion_enabled', node => node.value), 'false', 'built-in copy and custom scripts are mutually exclusive');
    await page.select('#edit-post_promotion_enabled', 'true');
    assert.equal(await page.$eval('#edit-post_promotion_copy_enabled', node => node.value), 'false', 'script selection switches off the built-in copy draft');
    await page.select('#edit-post_promotion_copy_enabled', 'true');
    assert(!(await page.$eval('#copy-rules-warning', node => node.hidden)), 'enabled master without rules must warn that no new copies will run');
    await click('#copy-rule-add');
    await fill('.copy-rule-row:first-child .copy-rule-source', '/downloads/Movies');
    await fill('.copy-rule-row:first-child .copy-rule-destination', '/app/data');
    await click('#settings-review-button');
    await finished();
    assert.equal(await page.$eval('#edit-post_promotion_copy_rules', node => node.getAttribute('aria-invalid')), 'true');
    await fill('.copy-rule-row:first-child .copy-rule-destination', '/copy-target/Movies');
    assert(await page.$eval('#copy-rules-warning', node => node.hidden));
    await click('#copy-rule-add');
    await fill('.copy-rule-row:last-child .copy-rule-source', '/downloads/TV');
    await fill('.copy-rule-row:last-child .copy-rule-destination', '/copy-target/TV');
    await click('.copy-rule-row:last-child .copy-rule-enabled');
    await click('.copy-rule-row:first-child .copy-rule-enabled');
    assert(!(await page.$eval('#copy-rules-warning', node => node.hidden)), 'disabled rules are not matches');
    await click('.copy-rule-row:first-child .copy-rule-enabled');
    await click('#copy-rule-add');
    await click('.copy-rule-row:last-child .copy-rule-remove');
    assert.equal(await page.$$eval('.copy-rule-row', rows => rows.length), 2);
    await click('#nas-location-add');
    assert.equal(await page.$$eval('.nas-location-row', rows => rows.length), 2);
    await fill('.nas-location-row:last-child .nas-label', 'Archive NAS');
    await fill('.nas-location-row:last-child .nas-path', '/nas-extra/intake');
    await fill('.nas-location-row:last-child .nas-marker', '/nas-extra/.mounted');
    await click('.nas-location-row:last-child input[type=radio]');
    const archiveId = await page.$eval('.nas-location-row:last-child', row => row.dataset.nasId);
    assert.equal(await page.$$eval('.nas-location-row input[type=radio]:checked', rows => rows.length), 1);
    await click('#nas-location-add');
    await click('.nas-location-row:last-child .nas-remove');
    assert.equal(await page.$$eval('.nas-location-row', rows => rows.length), 2);
    assert.equal(await page.$eval('#edit-default_nas_staging_id', node => node.value), archiveId);

    // All sections remain accessible on a narrow mobile screen without overflow.
    await page.setViewport({width: 390, height: 844});
    for (const section of ['connection', 'storage', 'scanner', 'general', 'backup', 'deployment']) {
      await page.select('#settings-section', section);
      const dimensions = await page.$eval('#settings-drawer', node => ({client: node.clientWidth, scroll: node.scrollWidth}));
      assert(dimensions.scroll <= dimensions.client + 1, `${section} overflows on mobile`);
    }
    await page.select('#settings-section', 'storage');
    assert(!(await page.$eval('.copy-rule-source', node => node.disabled)), 'mobile has the same editable copy rules');
    await fill('#settings-search', 'TI_DATABASE_URL');
    assert(await page.$eval('#setting-database_url', node => node.checkVisibility()));
    await page.select('#settings-section', 'connection');
    await fill('#edit-qbt_host', 'bad address');
    await click('#settings-review-button');
    await finished();
    assert.equal(await page.$eval('#edit-qbt_host', node => node.getAttribute('aria-invalid')), 'true');
    await fill('#edit-qbt_host', 'http://127.0.0.1:9');
    await fill('#edit-qbt_password', 'browser-test-secret');
    await click('#qbt-test');
    await finished();
    assert.match(await page.$eval('#qbt-test-result', node => node.textContent), /Cannot authenticate or reach/);
    assert(!docker('exec', container, 'cat', '/app/data/settings.json').includes('browser-test-secret'));

    await page.select('#settings-section', 'scanner');
    if (!(await page.$eval('#advanced-unlock', node => node.checked))) await click('#advanced-unlock');
    assert(!(await page.$eval('#edit-large_media_chunk_mib', node => node.disabled)));
    await fill('#edit-large_media_chunk_mib', '256');
    if (process.env.TI_UI_SCREENSHOT_DIR) {
      await page.screenshot({path: `${process.env.TI_UI_SCREENSHOT_DIR}/settings-mobile.png`});
      await page.setViewport({width: 1440, height: 1000});
      await page.screenshot({path: `${process.env.TI_UI_SCREENSHOT_DIR}/settings-desktop.png`});
      await page.setViewport({width: 390, height: 844});
    }
    await click('#settings-review-button');
    await finished();
    assert(await page.$eval('#settings-save', node => node.disabled));
    assert(!((await page.$eval('#settings-review-list', node => node.textContent)).includes('browser-test-secret')));
    await click('#advanced-confirm');
    await click('#settings-save');
    await page.waitForFunction(() => !document.querySelector('#settings-restart-guide').hidden);
    await finished();
    assert(await page.$eval('#controller-resume', node => node.disabled));
    assert.match(await page.$eval('#setting-large_media_chunk_mib [data-active-value]', node => node.textContent), /512/);
    assert.match(await page.$eval('#setting-large_media_chunk_mib [data-pending-value]', node => node.textContent), /256/);
    assert.equal(JSON.parse(docker('exec', container, 'cat', '/app/data/settings.json')).settings.large_media_chunk_mib, 256);
    const saved = JSON.parse(docker('exec', container, 'cat', '/app/data/settings.json')).settings;
    assert.equal(saved.default_nas_staging_id, archiveId);
    assert.equal(saved.nas_staging_locations.length, 2);
    assert.deepEqual(saved.nas_staging_locations[1], {id: archiveId, label: 'Archive NAS', path: '/nas-extra/intake', mount_marker: '/nas-extra/.mounted'});
    assert.equal(saved.post_promotion_copy_enabled, true);
    assert.deepEqual(saved.post_promotion_copy_rules, [
      {source: '/downloads/Movies', destination: '/copy-target/Movies', enabled: true},
      {source: '/downloads/TV', destination: '/copy-target/TV', enabled: false},
    ]);
    assert.equal(saved.post_promotion_enabled, false);
    assert.equal(saved.post_promotion_script, null, 'built-in copy needs no executable path');

    docker('restart', container);
    origin = `http://127.0.0.1:${docker('port', container, '8000/tcp').split(':').pop()}`;
    for (let attempt = 0; attempt < 100; attempt++) {
      try { if ((await fetch(`${origin}/controller/status`)).ok) break; } catch (_) {}
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    await open();
    await unlock();
    assert(await page.$eval('#settings-restart-guide', node => node.hidden));
    assert.match(await page.$eval('#setting-large_media_chunk_mib [data-active-value]', node => node.textContent), /256/);
    assert.equal(await page.$eval('#edit-qbt_password', node => node.value), '');
    assert.equal(await page.$eval('#nas-staging-select', node => node.value), archiveId);
    assert.equal(await page.$$eval('.nas-location-row input[type=radio]:checked', rows => rows.length), 1);
    assert.equal(await page.$eval('#edit-post_promotion_copy_enabled', node => node.value), 'true');
    assert.equal(await page.$eval('.copy-rule-row:first-child .copy-rule-source', node => node.value), '/downloads/Movies');
    assert.equal(await page.$eval('.copy-rule-row:first-child .copy-rule-destination', node => node.value), '/copy-target/Movies');
    assert(!(await page.$eval('.copy-rule-row:last-child .copy-rule-enabled', node => node.checked)));
    await click('#controller-resume');
    await finished();
    assert.match(await page.$eval('#admin-result', node => node.textContent), /Resume checks failed/);
    assert.match(await page.$eval('#setup-results', node => node.textContent), /Needs attention/);
    const state = await (await fetch(`${origin}/controller/status`)).json();
    assert(state.paused && state.drained && !state.restart_required);
    assert.deepEqual(errors, []);
    console.log('PASS desktop/mobile editor, named NAS/default/marker persistence, routed copy editing/add/remove/disabled rules/empty-rule warnings/mode exclusion/persistence, field errors, locks, private connection test, advanced confirmation, save/restart, pending values and fail-closed resume');
  } catch (error) {
    if (container) console.error(docker('logs', '--tail', '20', container));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (container) docker('rm', '-f', container);
    docker('volume', 'rm', name);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
