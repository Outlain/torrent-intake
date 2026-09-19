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
    await unlock();
    assert.equal(await page.$('#edit-infected_action'), null);
    assert.equal(await page.$('#edit-database_url'), null);
    assert.equal(await page.$('#edit-debug'), null); // Environment override.
    assert.equal(await page.$('#deployment-notes'), null);
    assert(await page.$eval('#edit-large_media_chunk_mib', node => node.disabled));

    // All sections remain accessible on a narrow mobile screen without overflow.
    await page.setViewport({width: 390, height: 844});
    for (const section of ['connection', 'storage', 'scanner', 'general', 'backup', 'deployment']) {
      await page.select('#settings-section', section);
      const dimensions = await page.$eval('#settings-drawer', node => ({client: node.clientWidth, scroll: node.scrollWidth}));
      assert(dimensions.scroll <= dimensions.client + 1, `${section} overflows on mobile`);
    }
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
    await click('#advanced-unlock');
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
    await click('#controller-resume');
    await finished();
    assert.match(await page.$eval('#admin-result', node => node.textContent), /Resume checks failed/);
    assert.match(await page.$eval('#setup-results', node => node.textContent), /Needs attention/);
    const state = await (await fetch(`${origin}/controller/status`)).json();
    assert(state.paused && state.drained && !state.restart_required);
    assert.deepEqual(errors, []);
    console.log('PASS desktop/mobile editor, field errors, locks, private connection test, advanced confirmation, save/restart, pending values and fail-closed resume');
  } catch (error) {
    if (container) console.error(docker('logs', '--tail', '20', container));
    throw error;
  } finally {
    if (browser) await browser.close();
    if (container) docker('rm', '-f', container);
    docker('volume', 'rm', name);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
