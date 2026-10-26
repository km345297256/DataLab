/** Built application + browser interactions; all APIs use isolated test accounts. */
import assert from 'node:assert/strict';
import { readFile, stat, mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchBrowser, browserEngine } from './runtime.mjs';

const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../../dist');
const origin = 'http://localhost:7331';
const output = await mkdtemp(join(tmpdir(), 'datalab-users-browser-'));
const browser = await launchBrowser();
const report = { engine: browserEngine, passed: false, checks: [], errors: [], unexpected: [] };
const timestamp = new Date().toISOString();
const user = (id, role = 'user', status = 'approved') => ({ id, fullname: id === 'admin' ? '管理测试员' : id === 'pending' ? '待审核研究员' : '普通研究员', email: `${id}@example.test`, role, registration_status: status, is_active: status === 'approved', created_at: timestamp, updated_at: timestamp });
const accounts = [user('admin', 'admin'), user('alice'), user('pending', 'user', 'pending')];
const mutations = [];
function token(id) { return `test.${Buffer.from(JSON.stringify({ sub: id, exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url')}.signature`; }
async function contextFor(id) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } });
  await context.addInitScript(({ id, token }) => {
    if (!sessionStorage.getItem('test-auth-seeded')) {
      localStorage.setItem('access_token', token); localStorage.setItem('refresh_token', `test-refresh-${id}`);
      localStorage.setItem('locale', 'zh'); sessionStorage.setItem('test-auth-seeded', '1');
    }
  }, { id, token: token(id) });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) { report.unexpected.push(url.href); return route.abort(); }
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ code: status === 200 ? 0 : status, msg: status === 200 ? 'success' : String(data), data: status === 200 ? data : null }) });
    const actor = accounts.find(item => item.id === id);
    if (url.pathname === '/api/v1/auth/me') return json(actor);
    if (url.pathname === '/api/v1/config/frontend') return json({ auth_provider: 'password', default_agent_name: 'DataLab', default_model_name: 'test' });
    if (url.pathname === '/api/v1/config/client-contract') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ schema_version: 1, supported_client_protocols: [1] }) });
    if (url.pathname === '/api/v1/auth/status') return json({ auth_provider: 'password' });
    if (url.pathname === '/api/v1/auth/users') {
      assert.equal(actor.role, 'admin', 'A regular account must not request user management data');
      const query = url.searchParams.get('query') || '', status = url.searchParams.get('status'), role = url.searchParams.get('role');
      const selected = accounts.filter(item => `${item.fullname}${item.email}`.includes(query) && (!role || item.role === role) && (!status || (status === 'active' ? item.is_active && item.registration_status === 'approved' : status === 'inactive' ? !item.is_active && item.registration_status === 'approved' : item.registration_status === status)));
      return json({ users: selected, total: selected.length });
    }
    if (/^\/api\/v1\/auth\/registrations\/[^/]+\/decision$/.test(url.pathname)) {
      const account = accounts.find(item => item.id === url.pathname.split('/')[5]);
      const body = request.postDataJSON(); mutations.push({ action: 'decision', id: account.id, ...body });
      account.registration_status = body.status; account.is_active = body.status === 'approved'; return json(account);
    }
    if (/^\/api\/v1\/auth\/user\/[^/]+$/.test(url.pathname) && request.method() === 'PATCH') {
      const account = accounts.find(item => item.id === url.pathname.split('/').at(-1));
      const body = request.postDataJSON(); mutations.push({ action: 'update', id: account.id, ...body }); Object.assign(account, body); return json(account);
    }
    if (url.pathname === '/api/v1/auth/change-fullname') { actor.fullname = request.postDataJSON().fullname; return json(actor); }
    if (url.pathname === '/api/v1/auth/change-password') { mutations.push({ action: 'password' }); return json({}); }
    if (url.pathname === '/api/v1/data-library/stats') return json({ file_count: 0, total_bytes: 0, updated_at: null, formats: [] });
    if (url.pathname === '/api/v1/data-library') return json({ items: [], next_cursor: null });
    if (url.pathname === '/api/v1/data-library/folders') return json({ items: [] });
    if (url.pathname.startsWith('/api/')) return json([]);
    const filename = resolve(dist, '.' + decodeURIComponent(url.pathname));
    if (!filename.startsWith(dist + '/') && filename !== dist) return route.abort();
    const mime = { '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
    try { if ((await stat(filename)).isFile()) return route.fulfill({ contentType: mime[extname(filename)] || 'application/octet-stream', body: await readFile(filename) }); } catch {}
    return route.fulfill({ contentType: 'text/html', body: await readFile(join(dist, 'index.html')) });
  });
  return context;
}

try {
  const context = await contextFor('admin');
  const page = await context.newPage(); page.setDefaultTimeout(12_000);
  page.on('pageerror', error => report.errors.push(error.message));
  await page.goto(`${origin}/chat/admin?tab=accounts`);
  await page.getByRole('heading', { name: '用户与权限', exact: true }).waitFor();
  await page.getByRole('row').filter({ hasText: 'pending@example.test' }).getByRole('button', { name: '批准', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'detached' });
  assert.equal(accounts[2].registration_status, 'approved');
  report.checks.push('Registration approval requires explicit confirmation and refreshes the list');
  const alice = () => page.getByRole('row').filter({ hasText: 'alice@example.test' });
  await alice().getByRole('button', { name: '停用', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(accounts[1].is_active, true);
  await alice().getByRole('button', { name: '停用', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
  await alice().getByText('已停用', { exact: true }).waitFor();
  await alice().getByRole('button', { name: '启用', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
  await alice().getByText('已启用', { exact: true }).waitFor();
  await alice().getByRole('button', { name: '修改角色', exact: true }).click();
  await page.getByRole('dialog').getByLabel('新角色').selectOption('software');
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
  await alice().getByText('服务账号', { exact: true }).waitFor();
  report.checks.push('Disable, enable and role changes use the actual UI and API contract');
  await page.getByRole('searchbox').fill('alice@example.test');
  await page.getByRole('button', { name: '查询', exact: true }).click();
  await page.getByText('共 1 个账号', { exact: true }).waitFor();
  await page.screenshot({ path: join(output, 'user-management-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  await page.screenshot({ path: join(output, 'user-management-mobile.png'), fullPage: true });
  report.checks.push('User filtering and mobile page layout');
  await page.setViewportSize({ width: 1440, height: 1050 });
  await page.goto(`${origin}/account`);
  await page.getByRole('button', { name: /账号与设置/ }).click();
  await page.getByRole('heading', { name: '账号与安全', exact: true }).waitFor();
  await page.getByLabel('姓名', { exact: true }).fill('更新姓名测试员');
  await page.getByRole('button', { name: '保存姓名', exact: true }).click();
  await page.getByText('姓名已保存。', { exact: true }).waitFor();
  await page.getByLabel('当前密码', { exact: true }).fill('old-password');
  await page.getByLabel('新密码（至少 6 个字符）', { exact: true }).fill('new-password');
  await page.getByLabel('确认新密码', { exact: true }).fill('different-password');
  await page.getByRole('button', { name: '修改密码并重新登录', exact: true }).click();
  await page.getByText('两次输入的新密码不一致。', { exact: true }).waitFor();
  assert.equal(mutations.some(item => item.action === 'password'), false);
  await page.screenshot({ path: join(output, 'account-security.png'), fullPage: true });
  await page.getByLabel('确认新密码', { exact: true }).fill('new-password');
  await page.getByRole('button', { name: '修改密码并重新登录', exact: true }).click();
  await page.waitForURL('**/login?password_changed=1');
  assert.equal(await page.evaluate(() => localStorage.getItem('access_token')), null);
  report.checks.push('Profile update, password confirmation and forced re-login after password change');
  await context.close();
  const regular = await contextFor('alice');
  const regularPage = await regular.newPage();
  regularPage.on('pageerror', error => report.errors.push(error.message));
  await regularPage.goto(`${origin}/chat/admin?tab=accounts`);
  await regularPage.waitForURL(url => !url.pathname.endsWith('/admin'));
  assert.equal(await regularPage.getByRole('heading', { name: '用户与权限', exact: true }).count(), 0);
  report.checks.push('Non-administrator accounts cannot open the user management page');
  await regular.close();
  assert.deepEqual(report.errors, []); assert.deepEqual(report.unexpected, []); report.passed = true;
} finally {
  await writeFile(join(output, 'report.json'), JSON.stringify(report, null, 2));
  await browser.close(); console.log(JSON.stringify({ ...report, output }, null, 2));
}
