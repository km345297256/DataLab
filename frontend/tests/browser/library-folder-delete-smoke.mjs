/** Recursive folder deletion through the built UI; all APIs are in-memory mocks. */
import assert from 'node:assert/strict';
import { readFile, stat, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchBrowser, browserEngine } from './runtime.mjs';

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../../dist');
const origin = 'http://localhost:7331';
const output = await mkdtemp(join(tmpdir(), 'datalab-folder-delete-browser-'));
const report = { engine: browserEngine, passed: false, checks: [], errors: [], unexpected: [] };
const timestamp = '2026-10-22T04:00:00Z';
const id = value => value.toString(16).padStart(32, '0');
const folder = (value, path, parent = null) => ({ id: id(value), name: path.split('/').at(-1), path, parent_id: parent === null ? null : id(parent), created_at: timestamp, updated_at: timestamp });
const folders = [folder(1, '实验[1]'), folder(2, '实验[1]/原始', 1), folder(3, '实验[1]/原始/2026', 2), folder(4, '实验[1]-备份'), folder(5, '其他'), folder(6, '空目录'), folder(7, '实验1')];
const file = (key, path) => ({ file_id: `minio:${key}`, filename: path.split('/').at(-1), relative_path: path.includes('/') ? path : undefined, content_type: 'text/csv', size: 24, upload_date: timestamp });
const asset = (value, files) => ({ id: id(value), name: `目录测试记录 ${value}`, description: '', tags: [], source_type: 'upload', files, source_url: null, session_id: null, created_at: timestamp, updated_at: timestamp, version: 1, status: 'ready', source_verified: false });
const records = [
  asset(100, [file('target-direct', '实验[1]/direct.csv'), file('mixed-keep', '其他/keep.csv')]),
  asset(101, [file('target-child', '实验[1]/原始/raw.csv')]),
  ...Array.from({ length: 24 }, (_, index) => asset(200 + index, [file(`target-deep-${index}`, `实验[1]/原始/2026/run-${String(index).padStart(2, '0')}.csv`)])),
  asset(300, [file('prefix-neighbor', '实验[1]-备份/backup.csv')]),
  asset(301, [file('regex-neighbor', '实验1/neighbor.csv')]),
  asset(302, [file('root-file', 'root.csv')]),
];
const originalObjects = structuredClone(records.flatMap(item => item.files));
const objects = structuredClone(originalObjects);
const mutations = [], previews = [], pageReads = [];
let partialFailure = true;
const within = (path, target) => path === target || path.startsWith(`${target}/`);
const folderById = value => folders.find(item => item.id === value);
const subtreeFiles = path => records.flatMap(item => item.files).filter(item => item.relative_path?.startsWith(`${path}/`));
const subtreeFolders = path => folders.filter(item => within(item.path, path));
const jsonEnvelope = (data, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify({ code: status === 200 ? 0 : status, msg: status === 200 ? 'success' : String(data), data: status === 200 ? data : null }) });

const browser = await launchBrowser();
try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
  const token = `test.${Buffer.from(JSON.stringify({ sub: 'folder-delete-test', exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url')}.signature`;
  await context.addInitScript(token => {
    localStorage.setItem('access_token', token);
    localStorage.setItem('refresh_token', 'test-refresh-folder-delete');
    localStorage.setItem('locale', 'zh');
  }, token);
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) { report.unexpected.push(url.href); return route.abort(); }
    const json = (data, status = 200) => route.fulfill(jsonEnvelope(data, status));
    if (url.pathname === '/api/v1/auth/me') return json({ id: 'folder-delete-test', fullname: '目录删除测试员', email: 'folder-delete@example.test', role: 'user', registration_status: 'approved', is_active: true, created_at: timestamp, updated_at: timestamp });
    if (url.pathname === '/api/v1/config/frontend') return json({ auth_provider: 'password', default_agent_name: 'DataLab', default_model_name: 'test' });
    if (url.pathname === '/api/v1/config/client-contract') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ schema_version: 1, supported_client_protocols: [1] }) });
    if (url.pathname === '/api/v1/auth/status') return json({ auth_provider: 'password' });
    const previewMatch = url.pathname.match(/^\/api\/v1\/data-library\/folders\/([a-f0-9]{32})\/deletion-preview$/);
    if (previewMatch) {
      assert.equal(request.method(), 'GET');
      const target = folderById(previewMatch[1]);
      assert.ok(target, 'Preview must identify an existing owned test folder');
      previews.push(target.id);
      return json({ folder: target, deletion_token: target.id.repeat(2), subfolder_count: subtreeFolders(target.path).length - 1, file_count: subtreeFiles(target.path).length });
    }
    const deleteMatch = url.pathname.match(/^\/api\/v1\/data-library\/folders\/([a-f0-9]{32})$/);
    if (deleteMatch && request.method() === 'DELETE') {
      const target = folderById(deleteMatch[1]);
      assert.ok(target, 'Delete must identify an existing owned test folder');
      assert.equal(url.searchParams.get('deletion_token'), target.id.repeat(2), 'Deletion must use the preview token for this folder incarnation');
      assert.equal(request.postData(), null, 'Folder deletion scope must come from the server, not a visible-file body');
      mutations.push({ folder_id: target.id });
      const beforeFiles = subtreeFiles(target.path).length;
      const beforeFolders = subtreeFolders(target.path).length;
      if (target.id === id(1) && partialFailure) {
        partialFailure = false;
        const mixed = records.find(item => item.id === id(100));
        mixed.files = mixed.files.filter(item => item.file_id !== 'minio:target-direct');
        return json({ folder_id: target.id, parent_id: target.parent_id, completed: false,
          deleted_file_count: 1, deleted_folder_count: 0, remaining_file_count: subtreeFiles(target.path).length,
          remaining_folder_count: beforeFolders, message: '部分数据暂时无法删除，请重试' });
      }
      for (const record of records) record.files = record.files.filter(item => !item.relative_path?.startsWith(`${target.path}/`));
      for (let index = folders.length - 1; index >= 0; index--) if (within(folders[index].path, target.path)) folders.splice(index, 1);
      return json({ folder_id: target.id, parent_id: target.parent_id, completed: true,
        deleted_file_count: beforeFiles, deleted_folder_count: beforeFolders, remaining_file_count: 0, remaining_folder_count: 0, message: null });
    }
    if (url.pathname === '/api/v1/data-library/folders') return json({ items: folders });
    if (url.pathname === '/api/v1/data-library/stats') {
      const files = records.flatMap(item => item.files);
      return json({ file_count: files.length, total_bytes: files.reduce((total, item) => total + item.size, 0), updated_at: timestamp, formats: ['csv'] });
    }
    if (url.pathname === '/api/v1/data-library') {
      assert.equal(request.method(), 'GET');
      const folderId = url.searchParams.get('folder_id') || 'root';
      const target = folderId === 'root' ? null : folderById(folderId);
      const cursor = url.searchParams.get('cursor');
      pageReads.push({ folder_id: folderId, cursor });
      if (folderId !== 'root' && !target) return json('文件夹不存在或不可访问', 404);
      const path = target?.path || '';
      const matches = records.filter(record => record.files.some(item => (item.relative_path || item.filename).split('/').slice(0, -1).join('/') === path));
      const offset = cursor ? matches.findIndex(item => item.id === cursor) + 1 : 0;
      const items = matches.slice(offset, offset + 20);
      return json({ items, next_cursor: offset + 20 < matches.length ? items.at(-1).id : null });
    }
    if (url.pathname.startsWith('/api/')) {
      if (!['GET', 'HEAD'].includes(request.method())) {
        report.unexpected.push(`${request.method()} ${url.pathname}`);
        return json('Unexpected mutation in isolated recursive deletion smoke', 400);
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
  const menu = () => page.getByRole('menu', { name: '文件夹操作', exact: true });
  const navigation = () => page.getByRole('complementary', { name: '文件夹导航', exact: true });
  const checkbox = name => page.getByRole('checkbox', { name: `选择文件 ${name}`, exact: true, includeHidden: true });
  const checkNoOverflow = async () => assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Mobile folder management must not overflow horizontally');
  const openFolderMenu = async name => {
    await navigation().getByRole('button', { name: `前往目录 ${name}`, exact: true }).hover();
    await navigation().getByRole('button', { name: `文件夹 ${name} 的操作`, exact: true }).click();
    await menu().getByRole('menuitem', { name: '删除文件夹及全部内容', exact: true }).click();
    await dialog().getByRole('heading', { name: '删除文件夹及全部内容？', exact: true }).waitFor();
  };

  await page.goto(`${origin}/my-data`);
  await page.getByRole('heading', { name: '我的数据', exact: true }).waitFor();
  await checkbox('root.csv').waitFor();
  assert.equal(await page.getByRole('button', { name: '删除当前文件夹', exact: true }).count(), 0);
  await navigation().getByRole('button', { name: '我的数据', exact: true }).click({ button: 'right' });
  await menu().waitFor();
  assert.equal(await menu().getByRole('menuitem', { name: '删除文件夹及全部内容', exact: true }).count(), 0);
  await page.getByRole('heading', { name: '我的数据', exact: true }).click();
  await menu().waitFor({ state: 'detached' });
  report.checks.push('The virtual library root has no deletion action');

  await navigation().getByRole('button', { name: '前往目录 实验[1]', exact: true }).click();
  await checkbox('direct.csv').waitFor();
  assert.equal(await page.getByRole('button', { name: '删除当前文件夹', exact: true }).isVisible(), true);
  await navigation().getByRole('button', { name: '前往目录 原始', exact: true }).click();
  await checkbox('raw.csv').waitFor();
  await navigation().getByRole('button', { name: '前往目录 2026', exact: true }).click();
  await checkbox('run-00.csv').waitFor();
  assert.equal(await checkbox('run-23.csv').count(), 0);
  assert.equal(await page.getByRole('button', { name: '加载更多数据', exact: true }).count(), 1);

  await openFolderMenu('实验[1]');
  await dialog().getByText('范围：此文件夹及 2 个子文件夹，共 26 个文件。', { exact: true }).waitFor();
  await dialog().getByRole('button', { name: '取消', exact: true }).click();
  await dialog().waitFor({ state: 'detached' });
  assert.deepEqual(mutations, []);
  assert.equal(subtreeFiles('实验[1]').length, 26);
  report.checks.push('Recursive preview counts all descendants and unloaded files; cancellation keeps the entire subtree');

  await openFolderMenu('实验[1]');
  await dialog().getByText('范围：此文件夹及 2 个子文件夹，共 26 个文件。', { exact: true }).waitFor();
  await dialog().getByRole('button', { name: '确认删除文件夹', exact: true }).click();
  await dialog().getByRole('button', { name: '重试删除文件夹', exact: true }).waitFor();
  await dialog().getByText('范围：此文件夹及 2 个子文件夹，共 25 个文件。', { exact: true }).waitFor();
  assert.match(await dialog().getByRole('alert').innerText(), /未完成.*重试/);
  assert.equal(subtreeFolders('实验[1]').length, 3);
  assert.equal(subtreeFiles('实验[1]').length, 25);
  assert.deepEqual(mutations, [{ folder_id: id(1) }]);
  await page.screenshot({ path: join(output, 'folder-delete-partial-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await checkNoOverflow();
  await page.screenshot({ path: join(output, 'folder-delete-retry-mobile.png'), fullPage: true });
  await dialog().getByRole('button', { name: '重试删除文件夹', exact: true }).click();
  await dialog().waitFor({ state: 'detached' });
  await checkbox('root.csv').waitFor();
  assert.equal(await page.getByRole('button', { name: '删除当前文件夹', exact: true }).count(), 0);
  assert.equal((await page.getByRole('navigation', { name: '数据目录位置', exact: true }).innerText()).trim(), '我的数据');
  assert.equal(subtreeFiles('实验[1]').length, 0);
  assert.equal(subtreeFolders('实验[1]').length, 0);
  assert.deepEqual(mutations, [{ folder_id: id(1) }, { folder_id: id(1) }]);
  assert.equal(pageReads.some(read => read.cursor !== null), false, 'Recursive deletion must not enumerate pages in the browser');
  assert.deepEqual(records.find(item => item.id === id(100)).files.map(item => item.file_id), ['minio:mixed-keep']);
  await checkNoOverflow();
  report.checks.push('Partial deletion remains retryable; success removes every descendant and returns from a deleted child to the surviving root');

  await page.setViewportSize({ width: 1440, height: 1050 });
  await navigation().getByRole('button', { name: '前往目录 实验[1]-备份', exact: true }).waitFor();
  await navigation().getByRole('button', { name: '前往目录 实验1', exact: true }).waitFor();
  await navigation().getByRole('button', { name: '前往目录 其他', exact: true }).click();
  await checkbox('keep.csv').waitFor();
  await navigation().getByRole('button', { name: '前往目录 实验[1]-备份', exact: true }).click();
  await checkbox('backup.csv').waitFor();
  await navigation().getByRole('button', { name: '前往目录 实验1', exact: true }).click();
  await checkbox('neighbor.csv').waitFor();
  report.checks.push('Mixed-record files outside the subtree, prefix-neighbor folders and regex-like names remain intact');

  await navigation().getByRole('button', { name: '前往目录 空目录', exact: true }).click();
  await page.getByRole('heading', { name: '此文件夹为空', exact: true }).waitFor();
  await page.getByRole('button', { name: '删除当前文件夹', exact: true }).click();
  await dialog().getByText('范围：此文件夹及 0 个子文件夹，共 0 个文件。', { exact: true }).waitFor();
  await dialog().getByRole('button', { name: '确认删除文件夹', exact: true }).click();
  await dialog().waitFor({ state: 'detached' });
  await checkbox('root.csv').waitFor();
  assert.equal(folderById(id(6)), undefined);
  assert.equal(await navigation().getByRole('button', { name: '前往目录 空目录', exact: true }).count(), 0);
  assert.deepEqual(mutations.at(-1), { folder_id: id(6) });
  await page.setViewportSize({ width: 390, height: 844 });
  await checkNoOverflow();
  await page.screenshot({ path: join(output, 'folder-delete-surviving-root-mobile.png'), fullPage: true });
  report.checks.push('An empty directory can be removed and navigation returns to the parent');

  assert.deepEqual(objects, originalObjects);
  assert.deepEqual(report.errors, []);
  assert.deepEqual(report.unexpected, []);
  assert.ok(previews.filter(value => value === id(1)).length >= 2);
  report.checks.push('Only folder-reference deletion endpoints mutate test data; source objects are never deleted');
  report.passed = true;
  await context.close();
} finally {
  await writeFile(join(output, 'report.json'), JSON.stringify({ ...report, mutations, previews }, null, 2));
  await browser.close();
  console.log(JSON.stringify({ ...report, output }, null, 2));
}
