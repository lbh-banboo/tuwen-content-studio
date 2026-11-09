/* ============================================================================
 * render/_verify_mount.js —— 全站共享的「挂载层等价性 + 静态降级」验证器
 * ============================================================================
 * 纯 Node、零依赖、零 node_modules。不是三件套的一部分，是 28 页共用的一个工具。
 *
 * 为什么要有它
 * -----------
 * 纯函数层有单测（`node render/<page>.test.js`），但 `<page>.v2.html` 里那约 40 行
 * 挂载 IIFE 一直**没有可重复的验证手段**：之前的 `verify_p0` / `verify_p1` /
 * `verify_delivery_v2` 都是一次性脚本，跑完没留在磁盘上，所以已交付的 20 页
 * 现在无法重跑等价性验证。本文件把那套做法持久化，于是：
 *   · 任何 `app.js` / `api.js` 改动都能对全部 v2 页回归；
 *   · 新页加一个 spec 就能验，不用每次重写 DOM stub。
 *
 * 验的是什么（方案 §4.1 的第 ③ ④ 两步）
 * -----------------------------------
 *   ③ 等价性：在 Node 的 vm 里跑**页面真实的挂载 IIFE + 真实的 api.js + 真实的
 *      渲染模块**，用 fixture 当 HTTP 响应，断言每个容器被写入的 innerHTML
 *      与「同一份 JSON 直接喂纯函数」的结果**逐字节相等**。
 *      顺带把挂载层实际发出的 fetch（method / path / body / If-Match）记下来，
 *      可与 `*Request()` 请求描述符纯函数的输出逐字段比对 —— 这是 content-feedback
 *      的乐观锁唯一能被验证的地方（If-Match 拼错在浏览器里只表现为一个 412）。
 *   ④ 静态降级：让所有 fetch 抛网络错误，断言页面不白屏、落到错误态或静态样例、
 *      `API.isOffline() === true`。
 *
 * 用法
 * ----
 *   const { verify, run } = require('./_verify_mount.js');
 *   run([
 *     {
 *       name: 'cost',
 *       page: 'cost.v2.html',                  // 相对 demo 根目录
 *       modules: ['render/cost.js'],           // 浏览器脚本，按序注入
 *       renderKey: 'cost',                     // RednoteRender.<renderKey>
 *       search: '?id=00000000-0000-4000-8000-000000000001',
 *       routes: {                              // 路径（去掉 base 与 query）→ 响应 data
 *         '/projects/{projectId}/budget': BUDGET,
 *         '/projects/{projectId}/ledger-entries': { __envelope: { data: [...], page: null } }
 *       },
 *       expect: function (R, ctx) {            // 容器 id → 期望 HTML
 *         return { 'cost-budget': R.renderBudgetCard(BUDGET) };
 *       }
 *     }
 *   ]);
 *
 * routes 的值有三种写法：
 *   · 普通值            → 包成 `{"data": 值}`（与后端 DataEnvelope 一致）
 *   · `{__envelope:{}}` → 原样作为响应体（用于带 `page` / `meta` 的 PaginatedEnvelope）
 *   · `{__status:412, __body:{...}, __headers:{ETag:'"x:1"'}}` → 构造错误响应
 * 路径模板里的 `{xxx}` 匹配任意一个路径段。精确匹配优先于模板匹配。
 * ========================================================================== */

'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.resolve(__dirname, '..');

/* ==========================================================================
 * 1. 极简 DOM stub
 *    只实现挂载层真正用到的那几个能力：getElementById / querySelector(All) /
 *    innerHTML setter / textContent / className / addEventListener /
 *    getAttribute / setAttribute / closest / dataset(data-action)。
 *    故意**不**实现 HTML 解析 —— innerHTML 只被捕获，不被解析成节点树；
 *    等价性验证只需要「写进去的字符串」。
 * ======================================================================== */

function makeElement(id, attrs) {
  const el = {
    id: id || '',
    _attrs: Object.assign({}, attrs),
    _html: '',
    _text: '',
    className: '',
    href: '',
    listeners: {},
    htmlWrites: [],
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); this.htmlWrites.push(String(v)); },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    getAttribute(name) {
      if (name === 'href') return this.href || (this._attrs.href || null);
      return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
    },
    setAttribute(name, value) {
      if (name === 'href') this.href = String(value); else this._attrs[name] = String(value);
    },
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    /* 事件委托里的 e.target.closest(sel) —— 支持 [data-action="x"] 与 [data-action] 两种 */
    closest(sel) {
      var m = /^\[data-([a-z-]+)="?([^"\]]*)"?\]$/.exec(sel);
      if (m) return this._attrs['data-' + m[1]] === m[2] ? this : null;
      var p = /^\[data-([a-z-]+)\]$/.exec(sel);
      if (p) {
        return Object.prototype.hasOwnProperty.call(this._attrs, 'data-' + p[1]) ? this : null;
      }
      return null;
    },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    focus() {},
    get value() { return this._attrs.value || ''; },
    set value(v) { this._attrs.value = String(v); }
  };
  if (attrs && attrs.href) el.href = attrs.href;
  return el;
}

/**
 * 从 HTML 文本里抽出所有 id 与所有带 data-keep-id 的锚点，预建元素。
 * 挂载层 getElementById 一个不存在的 id 时**不静默返回 null**：那正是
 * 「v2.html 改了容器 id 但忘了改挂载层」这类 bug 的发生处，必须报出来。
 */
function makeDocument(html, missingIds) {
  const ids = new Set();
  const re = /\sid="([^"]+)"/g;
  let m;
  while ((m = re.exec(html)) !== null) ids.add(m[1]);

  const store = new Map();
  ids.forEach(function (id) { store.set(id, makeElement(id, {})); });

  const keepIdAnchors = [];
  const anchorRe = /<a\b[^>]*\bdata-keep-id\b[^>]*>/g;
  let a;
  while ((a = anchorRe.exec(html)) !== null) {
    const href = /\bhref="([^"]*)"/.exec(a[0]);
    keepIdAnchors.push(makeElement('', { 'data-keep-id': '', href: href ? href[1] : '' }));
  }

  const doc = {
    cookie: '',
    getElementById(id) {
      if (!store.has(id)) {
        missingIds.push(id);
        store.set(id, makeElement(id, {}));
      }
      return store.get(id);
    },
    querySelector(sel) {
      const byId = /^#(.+)$/.exec(sel);
      if (byId) return doc.getElementById(byId[1]);
      const byClass = /^\.([a-zA-Z0-9_-]+)$/.exec(sel);
      if (byClass) {
        const key = '.' + byClass[1];
        if (!store.has(key)) store.set(key, makeElement(key, {}));
        return store.get(key);
      }
      return null;
    },
    querySelectorAll(sel) {
      if (sel === '[data-keep-id]') return keepIdAnchors;
      return [];
    },
    createElement() { return makeElement('', {}); },
    addEventListener() {},
    body: makeElement('body', {}),
    _store: store,
    _keepIdAnchors: keepIdAnchors
  };
  return doc;
}

/* ==========================================================================
 * 2. fetch stub（路由表 → 响应）
 * ======================================================================== */

function matchRoute(routes, reqPath) {
  if (Object.prototype.hasOwnProperty.call(routes, reqPath)) return routes[reqPath];
  const reqSegs = reqPath.split('/');
  const keys = Object.keys(routes);
  for (let i = 0; i < keys.length; i += 1) {
    const segs = keys[i].split('/');
    if (segs.length !== reqSegs.length) continue;
    let ok = true;
    for (let j = 0; j < segs.length; j += 1) {
      if (/^\{.+\}$/.test(segs[j])) continue;
      if (segs[j] !== reqSegs[j]) { ok = false; break; }
    }
    if (ok) return routes[keys[i]];
  }
  return undefined;
}

function responseBodyFor(entry) {
  if (entry && typeof entry === 'object' && entry.__envelope) return entry.__envelope;
  if (entry && typeof entry === 'object' && Object.prototype.hasOwnProperty.call(entry, '__body')) {
    return entry.__body;
  }
  return { data: entry === undefined ? null : entry };
}

function makeFetch(spec, calls, mode) {
  const base = spec.base || '/api/v1';
  const routes = spec.routes || {};

  return function fetchStub(url, init) {
    const opts = init || {};
    const method = String(opts.method || 'GET').toUpperCase();
    const full = String(url);
    const noQuery = full.split('?')[0];
    const reqPath = noQuery.indexOf(base) === 0 ? noQuery.slice(base.length) : noQuery;
    const headers = opts.headers || {};

    calls.push({
      method: method,
      path: reqPath,
      url: full,
      query: full.indexOf('?') === -1 ? '' : full.slice(full.indexOf('?') + 1),
      body: opts.body ? JSON.parse(opts.body) : null,
      ifMatch: headers['If-Match'] || null,
      idempotencyKey: headers['Idempotency-Key'] || null
    });

    if (mode === 'offline') {
      const err = new Error('network_error (verify_mount offline mode)');
      err.reason = 'network_error';
      return Promise.reject(err);
    }

    const entry = matchRoute(routes, reqPath);
    if (entry === undefined) {
      /* 未登记的路径返回 404 —— 不要静默返回 200 空体，否则挂载层会把
         「忘了登记 fixture」渲染成「后端真的没有这条数据」 */
      return Promise.resolve(makeResponse(404, { error: {
        code: 'RESOURCE_NOT_FOUND',
        message: 'verify_mount: 路由表里没有登记 ' + method + ' ' + reqPath
      } }, {}));
    }

    const status = (entry && typeof entry === 'object' && entry.__status) || 200;
    const extraHeaders = (entry && typeof entry === 'object' && entry.__headers) || {};
    return Promise.resolve(makeResponse(status, responseBodyFor(entry), extraHeaders));
  };
}

function makeResponse(status, body, headers) {
  const text = JSON.stringify(body);
  const h = {};
  Object.keys(headers || {}).forEach(function (k) { h[k.toLowerCase()] = headers[k]; });
  return {
    status: status,
    ok: status >= 200 && status < 300,
    headers: { get: function (name) { return h[String(name).toLowerCase()] || null; } },
    text: function () { return Promise.resolve(text); },
    json: function () { return Promise.resolve(body); }
  };
}

/* ==========================================================================
 * 3. 抽出页面里的挂载 IIFE
 *    v2 页的约定：最后一个内联 <script> 块 = initPage(...) + 挂载 IIFE。
 * ======================================================================== */

function extractMountScript(html) {
  const blocks = [];
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = re.exec(html)) !== null) blocks.push(m[1]);
  if (!blocks.length) throw new Error('页面里没有内联 <script> 块');
  return blocks[blocks.length - 1];
}

function externalScripts(html) {
  const out = [];
  const re = /<script[^>]*\bsrc="([^"]+)"[^>]*>/g;
  let m;
  while ((m = re.exec(html)) !== null) out.push(m[1]);
  return out;
}

/* ==========================================================================
 * 4. 跑一次挂载
 * ======================================================================== */

function runMount(spec, mode) {
  const pagePath = path.join(ROOT, spec.page);
  const html = fs.readFileSync(pagePath, 'utf8');
  const missingIds = [];
  const calls = [];
  const doc = makeDocument(html, missingIds);

  const sandbox = {
    console: { log: function () {}, warn: function () {}, error: function () {}, info: function () {} },
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    Promise: Promise,
    JSON: JSON,
    Math: Math,
    Date: Date,
    Map: Map,
    Set: Set,
    Array: Array,
    Object: Object,
    String: String,
    Number: Number,
    Boolean: Boolean,
    Error: Error,
    RegExp: RegExp,
    isFinite: isFinite,
    isNaN: isNaN,
    parseInt: parseInt,
    parseFloat: parseFloat,
    encodeURIComponent: encodeURIComponent,
    decodeURIComponent: decodeURIComponent,
    URLSearchParams: URLSearchParams,
    AbortController: AbortController,
    crypto: { getRandomValues: function (a) { for (let i = 0; i < a.length; i += 1) a[i] = i; return a; },
              randomUUID: function () { return '00000000-0000-4000-8000-00000000beef'; } },
    document: doc,
    navigator: { onLine: true },
    location: { search: spec.search || '', href: 'http://localhost/' + spec.page, origin: 'http://localhost' },
    /* app.js 不加载（它只画侧栏/面包屑，与数据无关），initPage 存一下调用参数 */
    initPage: function (cfg) { sandbox.__initPage = cfg; },
    REDNOTE_API_BASE: spec.base || '/api/v1'
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.self = sandbox;
  sandbox.fetch = makeFetch(spec, calls, mode);

  const ctx = vm.createContext(sandbox);

  /* 先注入 api.js，再按页面声明顺序注入渲染模块 */
  const srcs = externalScripts(html).filter(function (s) { return s !== 'app.js'; });
  const toLoad = (spec.modules && spec.modules.length) ? ['api.js'].concat(spec.modules) : srcs;
  toLoad.forEach(function (rel) {
    const file = path.join(ROOT, rel);
    vm.runInContext(fs.readFileSync(file, 'utf8'), ctx, { filename: rel });
  });

  /* 页面自己的 <script> window.REDNOTE_API_BASE = ... 已由 sandbox 预设；
     这里只跑最后那个内联块（含 initPage + 挂载 IIFE） */
  vm.runInContext(extractMountScript(html), ctx, { filename: spec.page + '#mount' });

  return { sandbox: sandbox, doc: doc, calls: calls, missingIds: missingIds, html: html };
}

/** 等所有挂起的微任务 + fetch 链跑完（挂载层是 async load()，没有回调可等） */
function drain(times) {
  let p = Promise.resolve();
  for (let i = 0; i < (times || 60); i += 1) {
    p = p.then(function () { return new Promise(function (r) { setTimeout(r, 0); }); });
  }
  return p;
}

/* ==========================================================================
 * 5. 一个 spec 的完整验证
 * ======================================================================== */

async function verify(spec) {
  const results = [];
  function ok(name) { results.push({ ok: true, name: name }); }
  function fail(name, detail) { results.push({ ok: false, name: name, detail: detail }); }

  /* ---- ③ 等价性 ---- */
  let online;
  try {
    online = runMount(spec, 'online');
    await drain(spec.drain);
  } catch (err) {
    fail('挂载 IIFE 可执行', err.message + '\n' + (err.stack || '').split('\n').slice(1, 4).join('\n'));
    return { name: spec.name, results: results };
  }
  ok('挂载 IIFE 可执行（真实 api.js + 真实渲染模块 + vm 沙箱）');

  if (online.missingIds.length) {
    fail('挂载层引用的容器 id 都存在于 v2.html',
      '缺失：' + Array.from(new Set(online.missingIds)).join(', '));
  } else {
    ok('挂载层引用的容器 id 都存在于 v2.html');
  }

  const R = online.sandbox.RednoteRender && online.sandbox.RednoteRender[spec.renderKey];
  if (!R) {
    fail('渲染模块已挂到 RednoteRender.' + spec.renderKey, '未找到');
    return { name: spec.name, results: results };
  }

  const expected = spec.expect ? spec.expect(R, { calls: online.calls, sandbox: online.sandbox }) : {};
  Object.keys(expected).forEach(function (id) {
    const el = online.doc._store.get(id);
    const actual = el ? el.innerHTML : null;
    const want = expected[id];
    if (actual === want) {
      ok('等价性 · #' + id + ' innerHTML 与纯函数输出逐字节相等（' + String(want).length + ' 字节）');
    } else {
      fail('等价性 · #' + id, diffHint(want, actual));
    }
  });

  /* 没被 expect 覆盖的容器：至少断言「被写过且非空」，防白块 */
  (spec.nonEmpty || []).forEach(function (id) {
    const el = online.doc._store.get(id);
    if (el && el.innerHTML && el.innerHTML.length > 0) ok('非空 · #' + id + '（' + el.innerHTML.length + ' 字节）');
    else fail('非空 · #' + id, '容器为空或从未写入');
  });

  /* 请求描述符核对：挂载层实际发出的 fetch 与 *Request() 纯函数输出比对 */
  if ((spec.clicks || []).length) {
    /* 模拟事件委托的点击：造一个带 data-* 的假 target，喂给页面注册的监听器。
       这是验「写动作的 URL / body / If-Match 有没有被挂载层原样发出」的唯一办法 ——
       If-Match 拼错在浏览器里只表现为一个 412，很难归因。 */
    const host = online.doc.querySelector('.canvas');
    const listeners = (host && host.listeners.click) || [];
    if (!listeners.length) {
      fail('页面在 .canvas 上注册了 click 事件委托', '没有找到 click 监听器');
    }
    for (const click of spec.clicks) {
      const target = makeElement('', click.attrs || {});
      listeners.forEach(function (fn) {
        try { fn({ target: target, preventDefault: function () {} }); }
        catch (err) { fail('点击 ' + JSON.stringify(click.attrs), err.message); }
      });
      await drain(spec.drain);
    }
  }

  (spec.expectCalls || []).forEach(function (want, i) {
    const got = online.calls.filter(function (c) { return c.method !== 'GET'; })[i];
    if (!got) { fail('写请求 #' + i + ' 已发出', '没有捕获到第 ' + (i + 1) + ' 个非 GET 请求'); return; }
    const mismatch = [];
    ['method', 'path', 'ifMatch'].forEach(function (k) {
      if (Object.prototype.hasOwnProperty.call(want, k) && String(got[k]) !== String(want[k])) {
        mismatch.push(k + ': 期望 ' + want[k] + '，实际 ' + got[k]);
      }
    });
    if (Object.prototype.hasOwnProperty.call(want, 'body') &&
        JSON.stringify(got.body) !== JSON.stringify(want.body)) {
      mismatch.push('body: 期望 ' + JSON.stringify(want.body) + '，实际 ' + JSON.stringify(got.body));
    }
    if (mismatch.length) fail('写请求 #' + i + ' 的 URL/body/If-Match', mismatch.join('; '));
    else ok('写请求 #' + i + ' 的 URL/body/If-Match 与请求描述符一致（' + want.method + ' ' + want.path + '）');
  });

  if (online.calls.length) {
    ok('共发出 ' + online.calls.length + ' 次请求：' +
      online.calls.map(function (c) { return c.method + ' ' + c.path; }).join(' | '));
  } else {
    fail('挂载层发出了至少一次请求', '零次 —— 取数逻辑可能没跑');
  }

  /* ---- ④ 静态降级 ---- */
  let offline;
  try {
    offline = runMount(spec, 'offline');
    await drain(spec.drain);
  } catch (err) {
    fail('静态降级 · 挂载 IIFE 在离线下不抛未捕获异常', err.message);
    return { name: spec.name, results: results };
  }

  const api = offline.sandbox.RednoteAPI;
  if (api && api.isOffline()) ok('静态降级 · API.isOffline() === true');
  else fail('静态降级 · API.isOffline() === true', '实际 mode=' + (api ? api.mode : 'n/a'));

  const painted = Array.from(offline.doc._store.values()).filter(function (el) {
    return el.innerHTML && el.innerHTML.length > 0;
  });
  if (painted.length) {
    ok('静态降级 · 不白屏（' + painted.length + ' 个容器有内容）');
  } else {
    fail('静态降级 · 不白屏', '所有容器都是空的');
  }

  const anyErrorState = painted.some(function (el) {
    return /读取失败|data-action="retry"|连不上后端/.test(el.innerHTML);
  });
  if (anyErrorState) ok('静态降级 · 落到错误态或静态样例（含重试入口）');
  else fail('静态降级 · 落到错误态或静态样例', '没有任何容器渲染出错误态/重试按钮');

  return { name: spec.name, results: results };
}

function diffHint(want, actual) {
  if (actual === null || actual === undefined) return '容器从未被写入（期望 ' + String(want).length + ' 字节）';
  const w = String(want), a = String(actual);
  let i = 0;
  while (i < w.length && i < a.length && w[i] === a[i]) i += 1;
  return '首个差异在第 ' + i + ' 字节（期望 ' + w.length + ' 字节 / 实际 ' + a.length + ' 字节）\n' +
    '        期望 …' + w.slice(Math.max(0, i - 40), i + 60).replace(/\n/g, '⏎') + '…\n' +
    '        实际 …' + a.slice(Math.max(0, i - 40), i + 60).replace(/\n/g, '⏎') + '…';
}

/* ==========================================================================
 * 6. CLI 入口
 * ======================================================================== */

async function run(specs) {
  let pass = 0, failn = 0;
  for (const spec of specs) {
    const out = await verify(spec);
    console.log('');
    console.log('=== ' + out.name + ' (' + spec.page + ') ===');
    out.results.forEach(function (r) {
      if (r.ok) { pass += 1; console.log('  [PASS] ' + r.name); }
      else { failn += 1; console.log('  [FAIL] ' + r.name + '\n         ' + r.detail); }
    });
  }
  console.log('');
  console.log('VERIFY_MOUNT: ' + pass + '/' + (pass + failn) + ' PASS');
  if (failn) process.exitCode = 1;
  return { pass: pass, fail: failn };
}

module.exports = { verify, run, runMount, drain, makeDocument, makeFetch, matchRoute, ROOT };

/* ==========================================================================
 * 7. 内置 spec 清单
 *    刻意放在本文件内而不是另开一个 spec 文件：本批的交付约定是「每页三件套 +
 *    1 个共享脚本」，再多一个文件就破了约定。以后新增页在这里加一条即可。
 *    运行：node render/_verify_mount.js            跑全部
 *          node render/_verify_mount.js cost       只跑一页
 * ======================================================================== */

const PID = '00000000-0000-4000-8000-000000000001';
const CID = 'cccccccc-1111-4111-8111-111111111111';
const VER1 = 'v1111111-1111-4111-8111-111111111111';
const VER2 = 'v2222222-2222-4222-8222-222222222222';

/* fixture 形状同各页 *.test.js，源头是后端 pydantic 模型（见各 render/*.js 头部） */
const FX = {
  projects: [{ id: PID, name: '素与 Su& 氨基酸洁面', ownerId: 'u1', state: 'ACTIVE' }],
  contents: [{ contentId: CID, projectId: PID, title: '卸妆后要不要二次清洁',
    state: 'GENERATED', currentVersionNo: 2,
    createdAt: '2026-10-15T10:00:00+08:00', updatedAt: '2026-10-16T14:30:00+08:00' }],
  budget: { projectId: PID, currency: 'CNY', limit: '4000.00000000',
    committed: '3000.00000000', reserved: '925.02000000', available: '74.98000000',
    configured: true },
  ledgerSummary: { projectId: PID, byKind: { USAGE: '3200.50000000', BILL: '100.00000000',
    REVERSAL: '-300.50000000' }, net: '3000.00000000', entryCount: 42, truncated: false },
  ledgerEntries: [
    { id: 'e1111111-1111-4111-8111-111111111111', projectId: PID, entryKind: 'USAGE',
      currency: 'CNY', amount: '12.58000000', quantity: '12000', unit: 'TOKEN',
      description: '标题候选生成 · 第 1 次尝试' },
    { id: 'e2222222-2222-4222-8222-222222222222', projectId: PID, entryKind: 'REVERSAL',
      currency: 'CNY', amount: '-8.40000000', quantity: null, unit: null,
      description: '生成失败全额冲正' }
  ],
  estimates: [{ id: 'c1111111-1111-4111-8111-111111111111', projectId: PID,
    batchKind: 'CONTENT_GENERATION', quantity: 10, currency: 'CNY', state: 'SETTLED',
    totalAmount: '120.00000000', actualAmount: '131.40000000', varianceAmount: '11.40000000',
    pricingVersionId: null, lines: [] }],
  reservations: [{ id: 'r1111111-1111-4111-8111-111111111111', projectId: PID, state: 'HELD',
    currency: 'CNY', amount: '48.00000000', settledAmount: null, ref: 'batch:media-01',
    note: null, estimateId: null }],
  pricing: [{ id: 'pv111111-1111-4111-8111-111111111111',
    providerId: 'pr111111-1111-4111-8111-111111111111', modelProfileId: null,
    state: 'PUBLISHED', currency: 'CNY', label: 'live-pack 文本 v1.4',
    effectiveFrom: '2026-09-01T00:00:00+08:00', lines: [{ unit: 'TOKEN', unitPrice: '0.001' }] }],
  versions: [
    { versionId: VER1, versionNo: 1, title: '卸妆后还要洗脸吗', blueprintId: null,
      adoptedTitleCandidateId: null, generationContextId: null,
      payloadHash: 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6', schemaVersion: 1,
      createdAt: '2026-10-15T10:00:00+08:00', createdBy: null },
    { versionId: VER2, versionNo: 2, title: '卸妆后要不要二次清洁', blueprintId: null,
      adoptedTitleCandidateId: 'tc111111-1111-4111-8111-111111111111',
      generationContextId: null, payloadHash: 'ffffffffeeeeeeeedddddddd00000000',
      schemaVersion: 1, createdAt: '2026-10-16T14:30:00+08:00', createdBy: 'u1' }
  ],
  feedback: [
    { id: 'f1111111-1111-4111-8111-111111111111', contentItemId: CID, projectId: PID,
      observedVersionId: VER1, anchorKind: 'PARAGRAPH', anchorState: 'FRESH',
      paragraphRef: 'p3-9a1f2b7c', selectedText: '氨基酸表活占比 62%', mediaItemId: null,
      category: 'FACT', suggestedCategory: null, severity: 'BLOCKING', scope: 'CURRENT_ONLY',
      state: 'OPEN', authorKind: 'USER', body: '参数表述需替换为可核实口径',
      rejectReason: null, resolvedVersionId: null, createdBy: 'u1',
      createdAt: '2026-10-16T09:12:00+08:00', updatedAt: '2026-10-16T09:12:00+08:00',
      lockVersion: 1 },
    { id: 'f2222222-2222-4222-8222-222222222222', contentItemId: CID, projectId: PID,
      observedVersionId: VER1, anchorKind: 'MEDIA', anchorState: 'STALE',
      paragraphRef: 'p2-33cc44dd', selectedText: null,
      mediaItemId: 'm1111111-1111-4111-8111-111111111111',
      category: 'MEDIA_CONSISTENCY', suggestedCategory: 'TECH_FORMAT', severity: 'WARNING',
      scope: 'FUTURE_GENERATIONS', state: 'RESOLVED', authorKind: 'USER',
      body: '起泡图与文案描述不一致，需替换', rejectReason: null, resolvedVersionId: VER2,
      createdBy: 'u1', createdAt: '2026-10-16T10:00:00+08:00',
      updatedAt: '2026-10-17T08:00:00+08:00', lockVersion: 3 }
  ],
  assets: [
    { id: 'aa111111-1111-4111-8111-111111111111', category: 'FACT',
      title: '氨基酸表活占比口径', scope: 'PRODUCT',
      brandId: 'br111111-1111-4111-8111-111111111111',
      productId: 'pd111111-1111-4111-8111-111111111111', projectId: null,
      activeVersionId: 'kv222222-2222-4222-8222-222222222222', state: 'ACTIVE',
      archivedAt: null, createdAt: '2026-09-20T10:00:00+08:00' },
    { id: 'aa222222-2222-4222-8222-222222222222', category: 'FEEDBACK_EXPERIMENT',
      title: '疑问句标题的收藏率证据', scope: 'BRAND',
      brandId: 'br111111-1111-4111-8111-111111111111', productId: null, projectId: null,
      activeVersionId: null, state: 'DRAFT', archivedAt: null,
      createdAt: '2026-10-01T09:00:00+08:00' }
  ],
  assetDetail: {
    assetId: 'aa111111-1111-4111-8111-111111111111', category: 'FACT',
    title: '氨基酸表活占比口径', scope: 'PRODUCT',
    brandId: 'br111111-1111-4111-8111-111111111111',
    productId: 'pd111111-1111-4111-8111-111111111111', projectId: null,
    state: 'ACTIVE', archivedAt: null,
    activeVersionId: 'kv222222-2222-4222-8222-222222222222',
    version: { versionId: 'kv222222-2222-4222-8222-222222222222',
      body: '氨基酸表活占比以配方总表活量为分母计算。',
      structuredPayload: { unit: 'percent' },
      sourceSegmentIds: ['ss111111-1111-4111-8111-111111111111'],
      changeReason: '按第三方检测报告更正分母口径',
      effectiveFrom: '2026-10-01T00:00:00+08:00', effectiveUntil: null,
      createdAt: '2026-10-01T00:00:00+08:00' },
    versions: [
      { versionId: 'kv222222-2222-4222-8222-222222222222', versionState: 'ACTIVE',
        changeReason: '按第三方检测报告更正分母口径', createdAt: '2026-10-01T00:00:00+08:00' }
    ],
    publicationHistory: [
      { id: 'ph111111-1111-4111-8111-111111111111',
        versionId: 'kv111111-1111-4111-8111-111111111111', action: 'ACTIVATE', sequence: 1,
        actorId: 'u1', reason: '首次发布', createdAt: '2026-09-20T10:05:00+08:00' }
    ],
    activeChunkCount: 7
  },
  knowledgeVersions: [
    { versionId: 'kv222222-2222-4222-8222-222222222222', versionState: 'ACTIVE',
      changeReason: '按第三方检测报告更正分母口径', createdAt: '2026-10-01T00:00:00+08:00' },
    { versionId: 'kv111111-1111-4111-8111-111111111111', versionState: 'SUPERSEDED',
      changeReason: '首次入库', createdAt: '2026-09-20T10:00:00+08:00' }
  ],
  references: [
    { parentVersionId: 'cv111111-1111-4111-8111-111111111111', purpose: 'BASELINE_CLAIM',
      projectId: PID, selectedFragmentHash: 'f1e2d3c4b5a69788' }
  ],
  proposals: [
    { id: 'pp111111-1111-4111-8111-111111111111', state: 'PENDING', scope: 'PRODUCT',
      projectId: PID, targetAssetId: 'aa111111-1111-4111-8111-111111111111',
      proposedPayload: { field: 'body', to: '分母改为总表活量' },
      confirmedBy: null, createdAt: '2026-10-17T11:00:00+08:00' }
  ],
  preview: {
    contentId: CID, projectId: PID, currentVersionId: VER2, versionNo: 2, lockVersion: 5,
    etag: '"' + CID + ':5"', title: '卸妆后要不要二次清洁',
    body: '第一段正文。\n\n第二段正文。', tags: ['卸妆', '氨基酸洁面'],
    bindings: [], paragraphs: [
      { index: 0, text: '卸妆后是否需要二次清洁，取决于卸妆产品的残留情况。', ref: 'p1-aaaa1111' },
      { index: 1, text: '氨基酸表活占比 62%，温和度较高。', ref: 'p2-bbbb2222' }
    ],
    mediaItems: [
      { mediaItemId: 'm1111111-1111-4111-8111-111111111111', paragraphRef: 'p1-aaaa1111',
        sortOrder: 0, state: 'READY' }
    ],
    titleCandidates: [
      { titleCandidateId: 'tc111111-1111-4111-8111-111111111111',
        title: '卸妆后要不要二次清洁', isAdopted: true }
    ],
    feedbackCount: 2, selfCheckSummary: null
  },
  media: [
    { mediaId: 'm1111111-1111-4111-8111-111111111111', contentId: CID, projectId: PID,
      state: 'READY', paragraphRef: 'p1-aaaa1111', sortOrder: 0,
      currentVersionId: 'mv111111-1111-4111-8111-111111111111' }
  ],
  /* effect-view 的 fixture 来自 M4 草案 + tools/dev/mock_m4/samples.json */
  experiments: [
    { id: 'exexexex-1111-4111-8111-111111111111', projectId: PID,
      name: '标题风格 A/B（样例）', hypothesis: '疑问句标题提升收藏率（虚构假设）',
      state: 'RUNNING', startedAt: '2026-10-07T09:00:00+08:00', closedAt: null }
  ],
  evidence: [
    { id: 'svsvsvsv-1111-4111-8111-111111111111', projectId: PID,
      experimentId: 'exexexex-1111-4111-8111-111111111111',
      strategyRef: 'title-style:question', state: 'INCONCLUSIVE',
      evidenceGrade: 'INSUFFICIENT', note: '样本量不足，标证据不足，不声称因果。' }
  ],
  definitions: [
    { id: 'dddddddd-1111-4111-8111-111111111111', metricCode: 'LIKE_COUNT',
      scope: 'CUMULATIVE', valueKind: 'NUMERIC', unit: '次', label: '点赞数' },
    { id: 'dddddddd-2222-4222-8222-222222222222', metricCode: 'VIEW_COUNT',
      scope: 'LAST_24H', valueKind: 'NUMERIC', unit: '次', label: '近 24h 浏览量' }
  ],
  metricLatest: [
    { publicationId: 'pppppppp-1111-4111-8111-111111111111',
      metricDefinitionId: 'dddddddd-1111-4111-8111-111111111111',
      observationId: 'obobobob-1111-4111-8111-111111111111', valueNum: 12000,
      valueText: null, observedAt: '2026-10-07T18:00:00+08:00', label: 'QUALITY',
      rebuiltAt: '2026-10-07T20:35:00+08:00' },
    { publicationId: 'pppppppp-1111-4111-8111-111111111111',
      metricDefinitionId: 'dddddddd-2222-4222-8222-222222222222',
      observationId: 'obobobob-2222-4222-8222-222222222222', valueNum: null,
      valueText: null, observedAt: '2026-10-07T18:00:00+08:00', label: 'PENDING',
      rebuiltAt: '2026-10-07T20:35:00+08:00' }
  ],
  publications: [
    { id: 'pppppppp-1111-4111-8111-111111111111', projectId: PID,
      deliveryId: '22222222-2222-4222-8222-222222222222',
      publicationKey: 'PUB-2026-0001', noteId: 'note-demo-0001',
      noteUrl: 'https://www.xiaohongshu.com/explore/abc123?xsec_token=XYZ',
      urlCanonical: 'https://www.xiaohongshu.com/explore/abc123', state: 'PUBLISHED',
      contentState: 'VERIFIED', publishedAt: '2026-10-07T12:00:00+08:00' }
  ],
  deliveries: [
    { id: '22222222-2222-4222-8222-222222222222', projectId: PID, kocRecordId: null,
      contentItemId: 'cccccccc-2222-4222-8222-222222222222',
      versionNodeId: 'vvvvvvvv-2222-4222-8222-222222222222', state: 'DELIVERED',
      eligibilityHash: 'sha256:ef567890', replacesDeliveryId: null,
      deliveredAt: '2026-10-07T10:00:00+08:00', cancelledAt: null }
  ]
};

const SPECS = [
  {
    name: 'cost',
    page: 'cost.v2.html',
    modules: ['render/cost.js'],
    renderKey: 'cost',
    search: '?id=' + PID,
    routes: {
      '/projects/{projectId}/budget': FX.budget,
      '/projects/{projectId}/ledger-summary': FX.ledgerSummary,
      '/projects/{projectId}/ledger-entries': { __envelope: { data: FX.ledgerEntries } },
      '/projects/{projectId}/cost-estimates': { __envelope: { data: FX.estimates } },
      '/projects/{projectId}/budget-reservations': { __envelope: { data: FX.reservations } },
      '/pricing-versions': { __envelope: { data: FX.pricing } }
    },
    expect: function (R) {
      return {
        'cost-summary': R.renderSummaryCards(FX.ledgerSummary, FX.budget),
        'cost-truncation': R.renderTruncationWarning(FX.ledgerSummary),
        'cost-kinds': R.renderKindDistribution(FX.ledgerSummary),
        'cost-units': R.renderUnitDistribution(FX.ledgerEntries),
        'cost-ledger': R.renderLedgerTable(FX.ledgerEntries),
        'cost-estimates': R.renderEstimates(FX.estimates),
        'cost-budget': R.renderBudgetCard(FX.budget),
        'cost-reservations': R.renderReservations(FX.reservations),
        'cost-pricing': R.renderPricingVersions(FX.pricing),
        'cost-budget-mode': R.renderBudgetModeNotice(),
        'cost-attribution': R.renderAttributionUnavailable(),
        'cost-queue': R.renderQueueUnavailable(),
        'cost-doctrine': R.renderCostDoctrine(),
        'cost-reconcile': R.renderReconciliation(null)
      };
    }
  },
  {
    name: 'feedback-fix',
    page: 'feedback-fix.v2.html',
    modules: ['render/feedback-fix.js'],
    renderKey: 'feedbackFix',
    search: '?id=' + PID + '&contentId=' + CID,
    routes: {
      '/contents/{contentId}/feedback': { __envelope: { data: FX.feedback, page: { hasMore: false, nextCursor: null } } },
      '/contents/{contentId}/versions': { __envelope: { data: FX.versions, page: { hasMore: false, nextCursor: null } } }
    },
    expect: function (R) {
      const idx = R.indexVersions(FX.versions);
      return {
        'ff-fixlist': R.renderFixList(FX.feedback, idx),
        'ff-category': R.renderCategoryTally(FX.feedback),
        'ff-rejected': R.renderRejectedList(FX.feedback),
        'ff-scope': R.renderScopeCards(FX.feedback),
        'ff-versions': R.renderVersionTimeline(FX.versions),
        'ff-events': R.renderEventRecordDoctrine(FX.feedback),
        'ff-rootcause': R.renderRootCauseDoctrine(),
        'ff-round': R.renderRoundNotice()
      };
    }
  },
  {
    name: 'knowledge',
    page: 'knowledge.v2.html',
    modules: ['render/knowledge.js'],
    renderKey: 'knowledge',
    search: '',
    routes: {
      '/knowledge/assets': { __envelope: { data: FX.assets, page: { hasMore: false, nextCursor: null } } },
      '/knowledge/proposals': { __envelope: { data: FX.proposals, page: { hasMore: false, nextCursor: null } } },
      '/knowledge/assets/{assetId}': FX.assetDetail,
      '/knowledge/assets/{assetId}/versions': { __envelope: { data: FX.knowledgeVersions, page: null } },
      '/knowledge/assets/{assetId}/references': { __envelope: { data: FX.references, page: null } }
    },
    expect: function (R) {
      const f = { category: '', scope: '', state: '', mode: 'SEMANTIC', query: '' };
      return {
        'kb-filters': R.renderFilters(f),
        'kb-searchform': R.renderSearchForm(f),
        'kb-assets': R.renderAssetTable(FX.assets, FX.assets[0].id),
        'kb-tally': R.renderCategoryTally(FX.assets),
        'kb-proposals': R.renderProposalList(FX.proposals),
        'kb-detail': R.renderAssetDetail(FX.assetDetail),
        'kb-versions': R.renderVersionHistory(FX.knowledgeVersions),
        'kb-pubhistory': R.renderPublicationHistory(FX.assetDetail.publicationHistory),
        'kb-references': R.renderReferences(FX.references),
        'kb-searchresults': R.renderSearchResults(null),
        'kb-d-sixdomain': R.renderSixDomainDoctrine(),
        'kb-d-ontology': R.renderOntologyDoctrine(),
        'kb-d-sop': R.renderSopDoctrine(),
        'kb-d-strategy': R.renderStrategyStateDoctrine(),
        'kb-d-correction': R.renderCorrectionDoctrine()
      };
    }
  },
  {
    name: 'content-feedback',
    page: 'content-feedback.v2.html',
    modules: ['render/content-feedback.js'],
    renderKey: 'contentFeedback',
    search: '?id=' + PID + '&contentId=' + CID,
    routes: {
      '/projects/{projectId}/contents': { __envelope: { data: FX.contents, page: { hasMore: false, nextCursor: null } } },
      '/contents/{contentId}/preview': FX.preview,
      '/contents/{contentId}/feedback': { __envelope: { data: FX.feedback, page: { hasMore: false, nextCursor: null } } },
      '/contents/{contentId}/media': { __envelope: { data: FX.media, page: { hasMore: false, nextCursor: null } } },
      '/contents/{contentId}/versions': { __envelope: { data: FX.versions, page: { hasMore: false, nextCursor: null } } },
      '/feedback/{feedbackId}': { __envelope: { data: FX.feedback[0] } },
      '/contents/{contentId}/reviews': { __envelope: { data: {
        id: 'rv111111-1111-4111-8111-111111111111', projectId: PID, contentItemId: CID,
        subjectVersionId: VER2, decision: 'ELIMINATED', reason: '演示',
        decidedBy: 'u1', decidedAt: '2026-10-18T10:00:00+08:00', supersedesId: null } } },
      '/projects/{projectId}/content-reviews': { __envelope: { data: { results: [] } } }
    },
    /* 三个写动作各点一次，核对 method / path / body / If-Match */
    clicks: [
      { attrs: { 'data-action': 'advance', 'data-feedback-id': FX.feedback[0].id,
        'data-to-state': 'RESOLVED' } },
      { attrs: { 'data-action': 'review', 'data-decision': 'ELIMINATED' } },
      { attrs: { 'data-action': 'batch-review', 'data-decision': 'APPROVED' } }
    ],
    expectCalls: [
      { method: 'PATCH', path: '/feedback/' + FX.feedback[0].id,
        body: { toState: 'RESOLVED' }, ifMatch: '"' + FX.feedback[0].id + ':1"' },
      { method: 'POST', path: '/contents/' + CID + '/reviews',
        body: { subjectVersionId: VER2, decision: 'ELIMINATED', reason: '' }, ifMatch: 'null' },
      { method: 'POST', path: '/projects/' + PID + '/content-reviews',
        body: { contentItemIds: FX.contents.map(function (c) { return c.contentId; }),
          decision: 'APPROVED', reason: '' }, ifMatch: 'null' }
    ],
    expect: function (R) {
      const idx = R.indexVersions(FX.versions);
      return {
        'cf-counters': R.renderFeedbackCounters(FX.feedback),
        'cf-preview': R.renderPreviewPhone(FX.preview),
        'cf-edit': R.renderEditForm(FX.preview),
        'cf-selfcheck': R.renderSelfCheckGrid(FX.preview),
        'cf-feedback': R.renderFeedbackList(FX.feedback, idx),
        'cf-contents': R.renderContentsTable(FX.contents, CID),
        'cf-media': R.renderMediaStrip(FX.media),
        'cf-meta': R.renderMetaFields(FX.preview),
        'cf-gate': R.renderBlockingGate(FX.feedback),
        'cf-gates': R.renderGateChecks(FX.feedback, FX.contents),
        'cf-form': R.renderFeedbackForm(FX.preview, { anchorKind: 'WHOLE' }),
        'cf-review': R.renderReviewActions(FX.preview, FX.contents),
        'cf-anchors': R.renderParagraphAnchors(FX.preview),
        'cf-regen': R.renderRegenScopeOptions('FULL'),
        'cf-upload': R.renderUploadUnavailable(),
        'cf-direction': R.renderDirectionUnavailable(),
        'cf-doctrine': R.renderReviewDoctrine()
      };
    }
  },
  {
    name: 'effect-view',
    page: 'effect-view.v2.html',
    modules: ['render/effect-view.js'],
    renderKey: 'effectView',
    /* 本页基址是 mock 端口（页面自己设的那一行），spec 要跟着改，
       否则 fetch stub 剥 base 时对不上 */
    base: 'http://127.0.0.1:8799/api/v1',
    search: '',
    routes: {
      '/experiments': { __envelope: { data: FX.experiments, page: null } },
      '/strategy-evidence': { __envelope: { data: FX.evidence, page: null } },
      '/metric-definitions': { __envelope: { data: FX.definitions, page: null } },
      '/publications': { __envelope: { data: FX.publications, page: null } },
      '/deliveries': { __envelope: { data: FX.deliveries, page: null } },
      '/publications/{publicationId}/metric-latest': FX.metricLatest
    },
    expect: function (R) {
      return {
        'ev-counters': R.renderExperimentCounters(FX.experiments, FX.evidence),
        'ev-stages': R.renderSevenStageNav(R.stageAvailability({
          experiments: FX.experiments, evidence: FX.evidence, metrics: FX.metricLatest,
          deliveries: FX.deliveries, publications: FX.publications
        })),
        'ev-experiments': R.renderExperimentList(FX.experiments),
        'ev-execution': R.renderExecutionCheck(FX.deliveries, FX.publications),
        'ev-metrics': R.renderMetricLatest(FX.metricLatest, FX.definitions),
        'ev-evidence': R.renderEvidenceList(FX.evidence),
        'ev-redline': R.renderEvidenceRedline(FX.evidence),
        'ev-window': R.renderWindowUnavailable(FX.definitions),
        'ev-assignments': R.renderAssignmentsUnavailable(),
        'ev-purpose': R.renderPurposeScopeUnavailable(),
        'ev-statemachine': R.renderStrategyStateMachineDoctrine(),
        'ev-doctrine': R.renderEffectDoctrine()
      };
    }
  }
];

module.exports.SPECS = SPECS;
module.exports.FX = FX;

if (require.main === module) {
  const only = process.argv.slice(2);
  const picked = only.length
    ? SPECS.filter(function (s) { return only.indexOf(s.name) !== -1; })
    : SPECS;
  if (!picked.length) {
    console.log('没有匹配的 spec。可用：' + SPECS.map(function (s) { return s.name; }).join(', '));
    process.exitCode = 1;
  } else {
    run(picked);
  }
}
