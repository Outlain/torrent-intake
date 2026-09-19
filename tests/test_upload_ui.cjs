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
    const errors = [], submitted = [];
    let failOnce = true, inFlight = 0, peak = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.setRequestInterception(true);
    page.on('request', async request => {
      const path = new URL(request.url()).pathname;
      const respond = (body, status = 200) => request.respond({status, contentType: 'application/json', body: JSON.stringify(body)});
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
      if (path === '/jobs') return respond([]);
      if (path === '/qbt/tags') return respond({tags: ['Review']});
      if (path === '/qbt/categories') return respond({categories: []});
      if (path.startsWith('/qbt/') || path.startsWith('/fs/')) return respond({paths: []});
      return request.continue();
    });
    const fill = (selector, value) => page.$eval(selector, (node, value) => {
      node.value = value; node.dispatchEvent(new Event('input', {bubbles: true}));
    }, value);
    const click = async selector => {
      await page.$eval(selector, node => node.scrollIntoView({block: 'center'}));
      await page.click(selector);
    };
    const choose = names => page.$eval('#torrent-file-input', (node, names) => {
      const selection = new DataTransfer();
      for (const name of names) selection.items.add(new File(['test metadata'], name, {type: 'application/x-bittorrent'}));
      node.files = selection.files;
      node.dispatchEvent(new Event('change', {bubbles: true}));
    }, names);
    const magnet = letter => `magnet:?xt=urn:btih:${letter.repeat(40)}`;
    const finished = () => page.waitForFunction(() => !document.querySelector('#bulk-dialog-submit').disabled);

    for (const width of [1440, 390]) {
      await page.setViewport({width, height: 900});
      await page.goto(`${origin}/ui`, {waitUntil: 'networkidle0'});
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
      assert.match(await page.$eval('#bulk-dialog-status', node => node.textContent), /2 created, 1 failed/);
      await click('#bulk-dialog-submit');
      await page.waitForFunction(() => document.querySelector('#bulk-dialog').hidden);
      assert.equal(submitted.length - before, 4, 'retry must not re-add successful items');
      assert.match(submitted.at(-1).body, /\/downloads\/Other/);
      assert.match(await page.$eval('#torrent-file-summary', node => node.textContent), /No .torrent/);

      // Single file and unchanged all-magnet bulk both remain usable.
      await choose(['single.torrent']);
      await click('#submit-button');
      await page.waitForFunction(() => !document.querySelector('#submit-button').disabled);
      assert.equal(submitted.at(-1).path, '/jobs/torrent');
      await fill('#magnet-input', `${magnet('b')}\n${magnet('c')}`);
      await click('#submit-button');
      await click('#bulk-dialog-submit');
      await page.waitForFunction(() => document.querySelector('#bulk-dialog').hidden);
      assert.equal(submitted.at(-1).path, '/jobs/bulk');
      await choose(['not-a-torrent.txt']);
      assert.match(await page.$eval('#form-status', node => node.textContent), /Rejected selection/);
      await choose(Array.from({length: 50}, (_, i) => `${i}.torrent`));
      await fill('#magnet-input', magnet('d'));
      await click('#submit-button');
      assert.match(await page.$eval('#form-status', node => node.textContent), /up to 50/);
      await click('#torrent-files-clear');
      assert.match(await page.$eval('#torrent-file-summary', node => node.textContent), /No .torrent/);
    }
    assert.equal(peak, 1, 'mixed bulk requests must be sequential');
    assert.deepEqual(errors, []);
    console.log('PASS desktop/mobile mixed bulk uploads, per-row settings, tags, bounded sequential submits, partial retry, single upload, magnets, and selection limits');
  } finally {
    if (browser) await browser.close();
    if (container) docker('rm', '-f', container);
    docker('volume', 'rm', name);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
