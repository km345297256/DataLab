/** Built application interactions against isolated, in-memory library APIs only. */
import assert from 'node:assert/strict';
import { readFile, stat, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchBrowser, browserEngine } from './runtime.mjs';

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../../dist');
const origin = 'http://localhost:7331';
const output = await mkdtemp(join(tmpdir(), 'datalab-library-delete-browser-'));
const report = { engine: browserEngine, passed: false, checks: [], errors: [], unexpected: [] };
const timestamp = '2026-10-22T02:00:00Z';
const assetId = value => value.toString(16).padStart(32, '0');
const file = (id, filename) => ({ file_id: `minio:${id}`, filename, content_type: 'text/csv', size: 24, upload_date: timestamp });
const asset = (id, files, status = 'ready') => ({
  id: assetId(id), name: `测试记录 ${id}`, description: '仅供浏览器测试的数据记录', tags: [],
  source_type: 'upload', files, source_url: null, session_id: null,
  created_at: timestamp, updated_at: timestamp, version: 1, status, source_verified: false,
});
const fixtures = [
  asset(1, [file('measurements', 'measurements.csv'), file('notes', 'notes.csv')]),
  asset(2, [file('missing', 'missing.csv')], 'missing'),
  asset(3, [file('retry', 'retry.csv')]),
  asset(4, [file('icons', 'icons.csv')]),
  asset(5, [file('later', 'later-page.csv'), file('notes', 'notes.csv')]),
];
const records = structuredClone(fixtures);
const objectFiles = structuredClone(fixtures.flatMap(item => item.files));
const mutations = [];
const pageReads = [];
let retryFailures = 1;
const currentRecords = () => records.filter(item => item.files.length);
const loadedAssetIds = new Set(fixtures.slice(0, 4).map(item => item.id));
const lookup = id => records.find(item => item.id === assetId(id));
const key = value => JSON.stringify([value.asset_id, value.file_id]);
const selectedKeys = entries => entries.map(key).sort();
const expected = (id, file_id) => ({ asset_id: assetId(id), file_id: `minio:${file_id}` });

const browser = await launchBrowser();
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
  const token = `test.${Buffer.from(JSON.stringify({ sub: 'library-delete-test', exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url')}.signature`;
  await context.addInitScript(token => {
    localStorage.setItem('access_token', token);
    localStorage.setItem('refresh_token', 'test-refresh-library-delete');
    localStorage.setItem('locale', 'zh');
  }, token);
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) { report.unexpected.push(url.href); return route.abort(); }
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status === 200 ? 0 : status, msg: status === 200 ? 'success' : String(data), data: status === 200 ? data : null }) });
    if (url.pathname === '/api/v1/auth/me') return json({ id: 'library-delete-test', fullname: '数据管理测试员', email: 'library-delete@example.test', role: 'user', registration_status: 'approved', is_active: true, created_at: timestamp, updated_at: timestamp });
    if (url.pathname === '/api/v1/config/frontend') return json({ auth_provider: 'password', default_agent_name: 'DataLab', default_model_name: 'test' });
    if (url.pathname === '/api/v1/config/client-contract') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ schema_version: 1, supported_client_protocols: [1] }) });
    if (url.pathname === '/api/v1/auth/status') return json({ auth_provider: 'password' });
    if (url.pathname === '/api/v1/data-library/files/delete') {
      assert.equal(request.method(), 'POST');
      const body = request.postDataJSON();
      assert.deepEqual(Object.keys(body), ['files']);
      assert.ok(Array.isArray(body.files) && body.files.length > 0);
      assert.equal(new Set(selectedKeys(body.files)).size, body.files.length, 'Delete targets must be unique references');
      mutations.push(structuredClone(body));
      const results = body.files.map(target => {
        assert.deepEqual(Object.keys(target).sort(), ['asset_id', 'file_id']);
        const row = records.find(item => item.id === target.asset_id);
        const index = row?.files.findIndex(item => item.file_id === target.file_id) ?? -1;
        if (index < 0) return { ...target, status: 'not_found' };
        if (target.file_id === 'minio:retry' && retryFailures > 0) {
          retryFailures--;
          return { ...target, status: 'failed', message: '暂时无法删除，请重试' };
        }
        row.files.splice(index, 1);
        return { ...target, status: 'deleted' };
      });
      return json({ results, deleted_count: results.filter(item => item.status === 'deleted').length, failed_count: results.filter(item => item.status === 'failed').length });
    }
    if (url.pathname === '/api/v1/data-library/stats') {
      const files = currentRecords().flatMap(item => item.files);
      return json({ file_count: files.length, total_bytes: files.reduce((total, item) => total + item.size, 0), updated_at: timestamp, formats: ['csv'] });
    }
    if (url.pathname === '/api/v1/data-library/folders') return json({ items: [] });
    if (url.pathname === '/api/v1/data-library') {
      assert.equal(request.method(), 'GET');
      const cursor = url.searchParams.get('cursor');
      pageReads.push(cursor);
      assert.ok(cursor === null || cursor === assetId(4));
      const rows = currentRecords().filter(item => cursor ? !loadedAssetIds.has(item.id) : loadedAssetIds.has(item.id));
      return json({ items: rows, next_cursor: !cursor && lookup(5).files.length ? assetId(4) : null });
    }
    if (url.pathname.startsWith('/api/')) {
      // A physical file deletion, legacy whole-asset deletion, or any other
      // mutation must fail the scenario instead of touching a live service.
      if (!['GET', 'HEAD'].includes(request.method())) {
        report.unexpected.push(`${request.method()} ${url.pathname}`);
        return json('Unexpected mutation in isolated library smoke', 400);
      }
      return json([]);
    }
    const filename = resolve(dist, '.' + decodeURIComponent(url.pathname));
    if (!filename.startsWith(dist + '/') && filename !== dist) return route.abort();
    const mime = { '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.ico': 'image/x-icon' };
    try { if ((await stat(filename)).isFile()) return route.fulfill({ contentType: mime[extname(filename)] || 'application/octet-stream', body: await readFile(filename) }); } catch {}
    return route.fulfill({ contentType: 'text/html', body: await readFile(join(dist, 'index.html')) });
  });

  const page = await context.newPage();
  page.setDefaultTimeout(12_000);
  page.on('pageerror', error => report.errors.push(error.message));
  const dialog = () => page.getByRole('dialog');
  // Reka hides background roles from assistive technology while a modal is
  // open; inspect retained selection without interacting behind that modal.
  const checkbox = name => page.getByRole('checkbox', { name: `选择文件 ${name}`, exact: true, includeHidden: true });
  const deleteButton = name => page.getByRole('button', { name: `删除文件 ${name}`, exact: true });
  const checkNoOverflow = async () => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Mobile page must not overflow horizontally');

  await page.goto(`${origin}/my-data`);
  await page.getByRole('heading', { name: '我的数据', exact: true }).waitFor();
  await checkbox('measurements.csv').waitFor();
  assert.equal(await checkbox('later-page.csv').count(), 0);

  await deleteButton('measurements.csv').click();
  await dialog().getByRole('heading', { name: '删除 1 个文件？', exact: true }).waitFor();
  await dialog().getByRole('button', { name: '取消', exact: true }).click();
  await dialog().waitFor({ state: 'detached' });
  assert.deepEqual(mutations, []);
  assert.equal(lookup(1).files.length, 2);
  report.checks.push('Cancelling single-file deletion sends no request');

  await deleteButton('measurements.csv').click();
  await dialog().getByRole('button', { name: '确认删除', exact: true }).click();
  await dialog().waitFor({ state: 'detached' });
  await checkbox('measurements.csv').waitFor({ state: 'detached' });
  await checkbox('notes.csv').waitFor();
  assert.deepEqual(mutations[0], { files: [expected(1, 'measurements')] });
  assert.deepEqual(lookup(1).files.map(item => item.file_id), ['minio:notes']);
  report.checks.push('Confirmed single deletion uses asset plus file identity and preserves its sibling');

  await page.getByRole('button', { name: '图标视图', exact: true }).click();
  // Desktop icon actions appear when the user hovers or focuses the card.
  await page.getByRole('button', { name: '查看文件 icons.csv', exact: true }).hover();
  assert.equal(await deleteButton('icons.csv').isVisible(), true);
  await deleteButton('icons.csv').click();
  await dialog().getByRole('button', { name: '取消', exact: true }).click();
  await dialog().waitFor({ state: 'detached' });
  await page.setViewportSize({ width: 390, height: 844 });
  await checkNoOverflow();
  await page.screenshot({ path: join(output, 'library-delete-icons-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '列表视图', exact: true }).click();
  assert.equal(await deleteButton('icons.csv').isVisible(), true);
  await checkNoOverflow();
  await page.setViewportSize({ width: 1440, height: 1050 });
  report.checks.push('List and icon views both expose deletion, including a mobile layout without overflow');

  assert.equal(await checkbox('missing.csv').isEnabled(), true);
  await page.getByRole('button', { name: '全选当前列表', exact: true }).click();
  for (const name of ['notes.csv', 'missing.csv', 'retry.csv', 'icons.csv']) assert.equal(await checkbox(name).isChecked(), true);
  await page.getByText('已选 4 个文件', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '分析所选文件', exact: true }).isDisabled(), true);
  await page.getByRole('button', { name: '删除所选', exact: true }).click();
  await dialog().getByRole('heading', { name: '删除 4 个文件？', exact: true }).waitFor();
  await dialog().getByRole('button', { name: '确认删除', exact: true }).click();
  await dialog().getByRole('button', { name: '重试删除', exact: true }).waitFor();
  assert.deepEqual(selectedKeys(mutations[1].files), selectedKeys([expected(1, 'notes'), expected(2, 'missing'), expected(3, 'retry'), expected(4, 'icons')]));
  assert.equal(lookup(1).files.length, 0);
  assert.equal(lookup(2).files.length, 0);
  assert.equal(lookup(4).files.length, 0);
  assert.equal(lookup(5).files.length, 2, 'Select all must not delete an unloaded page or another reference to the same object');
  assert.equal(pageReads.includes(assetId(4)), false);
  report.checks.push('Select all includes unavailable files but only current loaded entries; batch preserves unloaded data');

  const pending = dialog().getByRole('list', { name: '待删除文件', exact: true });
  assert.deepEqual(await pending.getByRole('listitem').allTextContents(), ['retry.csv']);
  assert.match(await dialog().getByRole('alert').innerText(), /失败|重试/);
  await checkbox('retry.csv').waitFor();
  assert.equal(await checkbox('retry.csv').isChecked(), true);
  for (const name of ['notes.csv', 'missing.csv', 'icons.csv']) await checkbox(name).waitFor({ state: 'detached' });
  await page.screenshot({ path: join(output, 'library-delete-partial-failure.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await checkNoOverflow();
  await page.screenshot({ path: join(output, 'library-delete-retry-mobile.png'), fullPage: true });
  await dialog().getByRole('button', { name: '重试删除', exact: true }).click();
  await dialog().waitFor({ state: 'detached' });
  await checkbox('retry.csv').waitFor({ state: 'detached' });
  assert.deepEqual(mutations[2], { files: [expected(3, 'retry')] });
  assert.equal(lookup(3).files.length, 0);
  report.checks.push('Partial failure keeps just failed references selected and retries only those files');

  await page.getByRole('button', { name: '加载更多数据', exact: true }).click();
  await checkbox('later-page.csv').waitFor();
  await checkbox('notes.csv').waitFor();
  assert.equal(pageReads.includes(assetId(4)), true);
  await checkNoOverflow();
  await page.screenshot({ path: join(output, 'library-delete-preserved-unloaded.png'), fullPage: true });
  assert.deepEqual(objectFiles, fixtures.flatMap(item => item.files), 'Removing library references must preserve source objects');
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.unexpected, []);
  report.checks.push('Unloaded data and another saved reference to the same object remain available, with no object-deletion API calls');
  report.passed = true;
  await context.close();
} finally {
  await writeFile(join(output, 'report.json'), JSON.stringify({ ...report, mutations }, null, 2));
  await browser.close();
  console.log(JSON.stringify({ ...report, output }, null, 2));
}
