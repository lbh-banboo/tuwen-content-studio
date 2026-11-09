// 前端接线冒烟（浏览器级）：依赖已装好的 playwright（node）与本机 chromium headless shell。
// 环境变量：BASE（后端同源地址，默认 http://127.0.0.1:8799）、STATIC_ONLY_BASE（纯静态服务，无 API）、
//           PLAYWRIGHT_DIR（含 playwright 包的 node_modules 目录）
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const pwDir = process.env.PLAYWRIGHT_DIR || '/Users/<user>/.hermes/hermes-agent/node_modules';
const { chromium } = require(pwDir + '/playwright');

const BASE = process.env.BASE || 'http://127.0.0.1:8799';
const STATIC_ONLY = process.env.STATIC_ONLY_BASE || 'http://127.0.0.1:8798';
const tag = Date.now().toString(36);
let fail = 0;
const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fail++; };

const browser = await chromium.launch();

async function seed() {
  const ctx = await browser.newContext();
  const r = await ctx.request.post(BASE + '/api/v1/dev/auth/login',
    { data: { email: `smoke-${tag}@example.test`, displayName: '冒烟管理员', asAdmin: true } });
  const login = (await r.json()).data;
  const h = { Origin: BASE, 'X-CSRF-Token': login.csrfToken };
  const post = async (path, data) => {
    const res = await ctx.request.post(BASE + '/api/v1' + path,
      { data, headers: { ...h, 'Idempotency-Key': crypto.randomUUID() } });
    if (!res.ok()) throw new Error(path + ' ' + res.status() + ' ' + await res.text());
    return (await res.json()).data;
  };
  const brand = await post('/brands', { name: `冒烟品牌${tag}` });
  const product = await post('/products', { brandId: brand.id, name: `冒烟产品${tag}` });
  const project = await post('/projects', {
    name: `冒烟项目${tag}`, brandId: brand.id, productId: product.id, ownerId: login.userId,
    targetDeliveryCount: 37, dueDate: '2099-01-15',
  });
  return { ctx, login, brand, product, project };
}

async function open(ctx, url) {
  const page = await ctx.newPage();
  const errs = [];
  // CDN（fonts.googleapis / cdnjs）在内网不可达，且会阻塞其后的脚本；冒烟里一律快速中止，保证结果可重复
  await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, r => r.abort());
  page.on('pageerror', e => errs.push(e.message));
  await page.goto(url, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('#liveBanner', { timeout: 8000 });
  return { page, errs };
}
const banner = p => p.locator('#liveBanner').innerText();

const S = await seed();
const { brand, product, project } = S;

// ① 后端可达 + 已登录：真实数据
{
  const { page, errs } = await open(S.ctx, `${BASE}/project-overview.html?projectId=${project.id}`);
  const title = await page.locator('h1.page-title').innerText();
  ok(title === project.name, `overview 标题来自接口：${title}`);
  ok((await page.locator('.page-desc').innerText()).includes(brand.name) &&
     (await page.locator('.page-desc').innerText()).includes('37 篇'), 'overview 页头含品牌名与计划交付 37 篇');
  ok((await page.locator('[data-bind="code"]').first().innerText()) === project.projectCode, 'overview 项目编号来自接口');
  ok((await page.locator('[data-bind="dueDate"]').innerText()).includes('2099-01-15'), 'overview 截止日期来自接口');
  ok((await banner(page)).includes('已接入后端'), 'overview 显示已接入横幅');
  ok(!(await page.content()).includes('素与 Su& 氨基酸洁面</h1>'), 'overview 不再显示硬编码项目名');
  ok(errs.length === 0, 'overview 无 JS 异常 ' + JSON.stringify(errs));

  const l = await open(S.ctx, `${BASE}/projects.html`);
  const body = await l.page.locator('#plBody').innerText();
  ok(body.includes(project.name) && body.includes(project.projectCode) && body.includes(brand.name) && body.includes(product.name),
     'projects 列表含真实项目/品牌/产品');
  ok(!body.includes('初见 Chuu 精华水'), 'projects 不含硬编码样例行');
  const href = await l.page.locator('#plBody a.btn').first().getAttribute('href');
  ok(href.startsWith('project-overview.html?projectId='), 'projects 进入链接带 projectId');
  ok(l.errs.length === 0, 'projects 无 JS 异常 ' + JSON.stringify(l.errs));

  // 无 projectId：取列表第一个
  const o2 = await open(S.ctx, `${BASE}/project-overview.html`);
  ok((await o2.page.locator('h1.page-title').innerText()).startsWith('冒烟项目'), 'overview 无 projectId 时取列表首项');
}

// ② 不可达（请求被中止 = NETWORK）与纯静态服务（无 API，返回非 JSON = PROTOCOL）：降级为样例
for (const [name, url, p0] of [['overview', '/project-overview.html', '素与 Su& 氨基酸洁面'], ['projects', '/projects.html', '初见 Chuu 精华水']]) {
  const ctx = await browser.newContext();
  await ctx.route('**/api/v1/**', r => r.abort('connectionrefused'));
  const a = await open(ctx, BASE + url);
  ok((await a.page.content()).includes(p0), `${name} 网络不可达：保留硬编码样例`);
  ok((await banner(a.page)).includes('后端不可达'), `${name} 网络不可达：横幅提示`);
  ok(a.errs.length === 0, `${name} 网络不可达：无 JS 异常 ` + JSON.stringify(a.errs));
  await ctx.close();

  const ctx2 = await browser.newContext();
  const b = await open(ctx2, STATIC_ONLY + url);
  ok((await b.page.content()).includes(p0), `${name} 纯静态服务（无 API）：保留样例`);
  ok(b.errs.length === 0, `${name} 纯静态服务：无 JS 异常 ` + JSON.stringify(b.errs));
  await ctx2.close();

  // 未登录：新上下文无 Cookie -> 401 SESSION_EXPIRED
  const ctx3 = await browser.newContext();
  const c = await open(ctx3, BASE + url);
  ok((await c.page.content()).includes(p0) && (await banner(c.page)).includes('尚未登录'), `${name} 未登录：降级并提示`);
  ok(c.errs.length === 0, `${name} 未登录：无 JS 异常`);
  await ctx3.close();
}

// ③ 错误态：已登录但业务接口报错（后端标准错误体）-> 保留样例 + 透传后端 code
for (const [name, url, p0, route] of [
  ['overview', `/project-overview.html?projectId=${project.id}`, '素与 Su& 氨基酸洁面', '**/api/v1/projects/*'],
  ['projects', '/projects.html', '初见 Chuu 精华水', '**/api/v1/projects?*']]) {
  const ctx = await browser.newContext({ storageState: await S.ctx.storageState() });
  await ctx.route(route, r => r.fulfill({
    status: 503, contentType: 'application/json',
    body: JSON.stringify({ error: { code: 'DEPENDENCY_UNAVAILABLE', message: '依赖不可用', details: {}, retryable: true }, meta: { requestId: 'x' } }),
  }));
  const e = await open(ctx, BASE + url);
  const t = await banner(e.page);
  ok((await e.page.content()).includes(p0) && t.includes('DEPENDENCY_UNAVAILABLE'), `${name} 业务接口 503：保留样例并显示后端 code`);
  ok(e.errs.length === 0, `${name} 业务接口 503：无 JS 异常`);
  await ctx.close();
}

// ④ api.js 行为：错误体解包、写操作头（Idempotency-Key 复用 / CSRF / If-Match）
{
  const page = await S.ctx.newPage();
  await page.route(/^https?:\/\/(?!127\.0\.0\.1)/, r => r.abort());
  await page.goto(`${BASE}/project-overview.html?projectId=${project.id}`);
  await page.waitForSelector('#liveBanner');
  const r = await page.evaluate(async (pid) => {
    const out = {};
    try { await api.get('/projects/00000000-0000-0000-0000-000000000000'); }
    catch (e) { out.nf = { isApiError: e instanceof ApiError, kind: e.kind, code: e.code, known: e.isKnownCode(), retryable: e.retryable }; }
    const seen = [];
    const of = window.fetch;
    window.fetch = (u, i) => { seen.push({ m: i.method, h: { ...i.headers } }); return of(u, i); };
    const opts = { ifMatch: '"nope"' };
    for (let i = 0; i < 2; i++) { try { await api.patch('/projects/' + pid, { name: 'x' }, opts); } catch (e) { out['patch' + i] = e.code; } }
    window.fetch = of;
    out.keysSame = seen[0].h['Idempotency-Key'] === seen[1].h['Idempotency-Key'] && !!seen[0].h['Idempotency-Key'];
    out.csrf = !!seen[0].h['X-CSRF-Token'];
    out.ifMatch = seen[0].h['If-Match'];
    out.newKeyDiffers = api.newIdempotencyKey() !== seen[0].h['Idempotency-Key'];
    return out;
  }, project.id);
  ok(r.nf && r.nf.isApiError && r.nf.kind === 'BACKEND' && r.nf.code === 'RESOURCE_NOT_FOUND' && r.nf.known, '404 -> ApiError(BACKEND, RESOURCE_NOT_FOUND)');
  ok(r.keysSame && r.csrf && r.ifMatch === '"nope"', '写操作：同一 opts 复用 Idempotency-Key、带 CSRF、透传 If-Match ' + JSON.stringify(r));
  ok(typeof r.patch0 === 'string' && r.patch0 !== 'undefined', 'PATCH 被后端以标准错误码拒绝：' + r.patch0);

  // 真实写操作：api.post 自动带 CSRF（来自 /me）与 Idempotency-Key，同源 Origin 由浏览器附带
  const w = await page.evaluate(async (n) => {
    const o = {};
    try {
      const b1 = await api.post('/brands', { name: n }, o);
      return { created: !!b1.id, key: !!o.idempotencyKey }; // 键固化回 opts，供同一操作重试复用
    } catch (e) { return { err: e.kind + ' ' + e.code + ' ' + e.message }; }
  }, `写入冒烟${tag}`);
  ok(w.created && w.key, 'api.post 写成功（CSRF+Origin 通过），幂等键固化在 opts ' + JSON.stringify(w));
}

await browser.close();
console.log(fail ? `RESULT: ${fail} FAIL` : 'RESULT: ALL PASS');
process.exit(fail ? 1 : 0);
