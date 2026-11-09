/* api.js —— 前端唯一取数入口（M4 路线 A，见 docs/m4/M4前端接线路线定案.md §3.1）
 *
 * 零依赖。页面只拿 data；列表另带 page。错误统一抛 ApiError。
 * 全局导出：window.api、window.ApiError。
 *
 * 基址：window.REDNOTE_API_BASE（显式覆盖）
 *       否则 http(s) 同源提供 -> '/api/v1'；file:// 打开 -> 'http://127.0.0.1:8787/api/v1'
 */
(function (global) {
  'use strict';

  // 后端 platform/errors.py::ErrorCode 的 17 个成员（逐条照抄；文档口径里写的“18”含 500 的 INTERNAL_ERROR，
  // 它不在 ErrorCode 内，见 errors.py 模块说明）。仅用于 isKnownCode() 判断，不用于改写后端返回的 code。
  var BACKEND_CODES = [
    'MALFORMED_REQUEST', 'SESSION_EXPIRED', 'PERMISSION_DENIED', 'EXTERNAL_POLICY_DENIED',
    'RESOURCE_NOT_FOUND', 'INVALID_TRANSITION', 'IDEMPOTENCY_CONFLICT', 'DUPLICATE_PUBLICATION',
    'EVENT_HISTORY_EXPIRED', 'VERSION_CONFLICT', 'UPLOAD_TOO_LARGE', 'VALIDATION_FAILED',
    'BUDGET_EXCEEDED', 'DIRECTION_UNVERIFIED', 'PRECONDITION_REQUIRED', 'RATE_LIMITED',
    'DEPENDENCY_UNAVAILABLE'
  ];

  /* kind 区分错误来源，避免客户端侧错误冒充后端错误码：
   *   'BACKEND'  后端返回了标准错误体；code/message/details/retryable 全部来自后端原值
   *   'NETWORK'  请求没有得到任何 HTTP 响应（后端未启动、断网、跨源被拦、超时）；code 恒为 null
   *   'PROTOCOL' 有 HTTP 响应但不是约定的 JSON 信封（如代理返回 HTML、204 以外的空体）；code 恒为 null
   */
  function ApiError(init) {
    var e = init || {};
    this.name = 'ApiError';
    this.kind = e.kind || 'BACKEND';
    this.code = e.code === undefined ? null : e.code;
    this.message = e.message || '';
    this.details = e.details || {};
    this.retryable = !!e.retryable;
    this.status = e.status === undefined ? null : e.status;
    this.requestId = e.requestId || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, ApiError);
    else this.stack = (new Error(this.message)).stack;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;
  ApiError.BACKEND_CODES = BACKEND_CODES.slice();
  ApiError.prototype.isKnownCode = function () { return BACKEND_CODES.indexOf(this.code) >= 0; };

  function resolveBase() {
    if (global.REDNOTE_API_BASE) return String(global.REDNOTE_API_BASE).replace(/\/+$/, '');
    var proto = global.location && global.location.protocol;
    if (proto === 'http:' || proto === 'https:') return '/api/v1';
    return 'http://127.0.0.1:8787/api/v1';
  }

  function uuid() {
    var c = global.crypto;
    if (c && typeof c.randomUUID === 'function') return c.randomUUID();
    var b = new Uint8Array(16);
    c.getRandomValues(b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    var h = Array.prototype.map.call(b, function (x) { return ('0' + x.toString(16)).slice(-2); }).join('');
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }

  var WRITE_METHODS = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };
  var csrfToken = null;
  var me = null;

  function buildUrl(path, query) {
    var url = resolveBase() + path;
    if (query) {
      var qs = [];
      Object.keys(query).forEach(function (k) {
        var v = query[k];
        if (v === undefined || v === null || v === '') return;
        qs.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
      });
      if (qs.length) url += '?' + qs.join('&');
    }
    return url;
  }

  // 底层：发请求并解包。返回 {data, page, meta}；204 时 data 为 null。
  function send(method, path, opts, csrfRetried) {
    opts = opts || {};
    method = method.toUpperCase();
    var isWrite = !!WRITE_METHODS[method];

    var prep = Promise.resolve();
    if (isWrite && !csrfToken && !opts.skipCsrf) {
      prep = loadMe().catch(function () { /* 取不到令牌就不带，让后端给出权威的 401/403 */ });
    }

    return prep.then(function () {
      var headers = { Accept: 'application/json' };
      var init = { method: method, headers: headers, credentials: 'include' };
      if (opts.body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(opts.body);
      }
      if (isWrite) {
        // 同一次用户操作 = 同一个 opts 对象（或显式传入同一个 idempotencyKey）：键在首次发送时固化回 opts，重试复用。
        if (!opts.idempotencyKey) opts.idempotencyKey = uuid();
        headers['Idempotency-Key'] = opts.idempotencyKey;
        if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
      }
      if (opts.ifMatch !== undefined && opts.ifMatch !== null) headers['If-Match'] = String(opts.ifMatch);

      return fetch(buildUrl(path, opts.query), init).then(function (res) {
        return parse(res);
      }, function (err) {
        throw new ApiError({
          kind: 'NETWORK',
          message: '无法连接后端：' + (err && err.message ? err.message : '网络错误'),
          retryable: true
        });
      });
    }).catch(function (err) {
      // 写请求遇到 403 且本地令牌可能已过期（登录态变化）：刷新 /me 后用同一 Idempotency-Key 重试一次。
      if (isWrite && !csrfRetried && err instanceof ApiError && err.kind === 'BACKEND' &&
          err.code === 'PERMISSION_DENIED' && csrfToken) {
        var old = csrfToken;
        csrfToken = null;
        return loadMe().then(function () {
          if (csrfToken && csrfToken !== old) return send(method, path, opts, true);
          throw err;
        }, function () { throw err; });
      }
      throw err;
    });
  }

  function parse(res) {
    if (res.status === 204) return { data: null, page: undefined, meta: undefined };
    return res.text().then(function (text) {
      var body = null;
      if (text) { try { body = JSON.parse(text); } catch (e) { body = undefined; } }
      if (!res.ok) {
        if (body && body.error && typeof body.error === 'object') {
          var er = body.error;
          throw new ApiError({
            kind: 'BACKEND', code: er.code, message: er.message, details: er.details,
            retryable: er.retryable, status: res.status, requestId: body.meta && body.meta.requestId
          });
        }
        throw new ApiError({
          kind: 'PROTOCOL', status: res.status, retryable: res.status >= 502,
          message: '后端返回了非标准错误响应（HTTP ' + res.status + '）'
        });
      }
      if (!body || typeof body !== 'object' || !('data' in body)) {
        throw new ApiError({
          kind: 'PROTOCOL', status: res.status, message: '响应不是 {data, meta} 信封（可能被代理改写）'
        });
      }
      // 契约里分页块是信封顶层的 page（PaginatedEnvelope: data/page/meta）；兼容 meta.page 写法。
      var page = body.page !== undefined ? body.page : (body.meta ? body.meta.page : undefined);
      return { data: body.data, page: page, meta: body.meta };
    });
  }

  // 读 /me：缓存当前用户与 CSRF 令牌。登录态变化后调用 refreshSession() 重新拉取。
  function loadMe() {
    return send('GET', '/me', {}).then(function (r) {
      me = r.data;
      csrfToken = (me && me.csrfToken) || null;
      return me;
    }, function (err) {
      if (err instanceof ApiError && err.kind === 'BACKEND' && err.code === 'SESSION_EXPIRED') {
        me = null; csrfToken = null;
      }
      throw err;
    });
  }

  var api = {
    get base() { return resolveBase(); },

    // 只返回 data
    get: function (path, query) { return send('GET', path, { query: query }).then(function (r) { return r.data; }); },
    // 返回 {data, page, meta}（列表用）
    list: function (path, query) { return send('GET', path, { query: query }); },

    // 写操作：opts = { query, idempotencyKey, ifMatch }。同一次用户操作重试时传入同一个 opts 对象即复用同一键。
    post: function (path, body, opts) { return write('POST', path, body, opts); },
    put: function (path, body, opts) { return write('PUT', path, body, opts); },
    patch: function (path, body, opts) { return write('PATCH', path, body, opts); },
    del: function (path, opts) { return write('DELETE', path, undefined, opts); },

    // 低层：返回完整 {data, page, meta}
    request: function (method, path, opts) { return send(method, path, opts); },

    // 会话：/me 的 data（含 csrfToken）。登录/登出后调用 refreshSession()。
    me: function () { return loadMe(); },
    refreshSession: function () { me = null; csrfToken = null; return loadMe(); },
    currentUser: function () { return me; },
    clearSession: function () { me = null; csrfToken = null; },

    // 探测：永不 reject。
    // 返回 {state:'ok', me} | {state:'unauthenticated', error} | {state:'unreachable', error}
    probe: function () {
      return loadMe().then(function (u) { return { state: 'ok', me: u }; }, function (err) {
        if (err instanceof ApiError && err.kind === 'BACKEND' && err.code === 'SESSION_EXPIRED') {
          return { state: 'unauthenticated', error: err };
        }
        return { state: err instanceof ApiError && err.kind === 'NETWORK' ? 'unreachable' : 'error', error: err };
      });
    },

    newIdempotencyKey: uuid
  };

  function write(method, path, body, opts) {
    opts = opts || {};
    opts.body = body;
    return send(method, path, opts).then(function (r) { return r.data; });
  }

  global.ApiError = ApiError;
  global.api = api;
})(typeof window !== 'undefined' ? window : globalThis);
