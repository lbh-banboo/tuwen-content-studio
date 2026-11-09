/* ============================================================================
 * api.js —— Rednote Demo 统一取数层（线 C / C0 平台层）
 * ============================================================================
 *
 * 定位：路线 A 的唯一取数入口（定案：docs/m4/M4前端接线路线定案.md §3.1）。
 *   原生 JS、零依赖、无构建步骤：跟随 `<script src="app.js">` 一样直接 <script src="api.js">。
 *   本文件只做平台层（取数封装 + 错误 + 幂等 + 降级），**不含任何渲染逻辑**（渲染是 C1~C3 的事）。
 *
 * ---------------------------------------------------------------------------
 * 一、暴露了哪些符号（全部挂 window；内部用 IIFE 包住，避免与 app.js 的顶层
 *     const（PAGES / STEP_ORDER / FLOW_STEPS / NOTIFS / SEARCH_RECORDS）撞名）
 * ---------------------------------------------------------------------------
 *   window.RednoteAPI                 门面对象（下文简称 API）
 *     API.base                        当前 API_BASE（只读）
 *     API.configure({...})            改 baseUrl / timeoutMs / credentials / sendRequestId / autoIdempotency
 *     API.probe({force})              API_BASE 可达性探测，结果缓存（TTL 见 PROBE_TTL_MS）
 *     API.mode                        'unknown' | 'online' | 'offline'
 *     API.request(method, path, opt)  统一请求；解包后**只返回 data**
 *     API.get / post / put / patch / del / delete
 *     API.page(path, {limit,cursor,params})   游标分页 → {items, page, meta, static}
 *     API.eachPage(path, cb, opts)            逐页遍历（按 nextCursor 自动翻页）
 *     API.download(path, {etag,query})        条件下载（If-None-Match → 200 / 304）
 *     API.exportZip(projectId, groupId, opt)  WP35 固定版本 ZIP 鉴权下载
 *     API.verifyExport(projectId, groupId)    WP35 摘要核验
 *     API.reportMarkdown(reportId, {versionNo}) 报告 Markdown 导出（草案端点）
 *     API.newIdempotencyKey()         crypto.randomUUID() 幂等键
 *     API.operation(fn)               同一次用户操作内复用同一个幂等键
 *     API.registerStatic(path, v)     登记静态降级样例（v 可为值或 () => 值）
 *     API.static(path)                读取已登记的静态样例
 *     API.onDegrade(cb) / API.onModeChange(cb)   降级 / 联通性事件订阅
 *     API.auth.me() / devLogin() / logout() / setCsrfToken() / ensureCsrf()
 *     API.ENDPOINTS                   本项目已核实的端点路径常量（逐条标注来源）
 *   window.ApiError                  错误类（页面用 instanceof 区分，读 .code / .status / .retryable）
 *   window.ApiErrorCode              18 个错误码字面量（17 个 ErrorCode + INTERNAL_ERROR）
 *   window.ApiErrorCodeInfo          {ALL_CODES, STATUS_BY_CODE, RETRYABLE_BY_CODE, CODE_BY_STATUS, MESSAGE_BY_CODE}
 *
 * ---------------------------------------------------------------------------
 * 二、错误码枚举来自哪里（**不自创**，逐条对应磁盘文件 —— 红线 3）
 * ---------------------------------------------------------------------------
 *   services/backend/src/rednote/platform/errors.py:47    class ErrorCode（17 个）
 *   services/backend/src/rednote/platform/errors.py:74    _STATUS_BY_CODE（业务码 → HTTP）
 *   services/backend/src/rednote/platform/errors.py:97    _RETRYABLE_BY_CODE（只有 429 / 503 可重试）
 *   services/backend/src/rednote/platform/errors.py:104    _MESSAGE_BY_CODE（默认中文提示）
 *   services/backend/src/rednote/platform/errors.py:129    _CODE_BY_STATUS（裸 HTTP → 业务码，兜底折算）
 *   services/backend/src/rednote/platform/errors.py:355    INTERNAL_ERROR（500 专用单值字面量，不在枚举内，但属「18 个」口径）
 *   口径文档：docs/m4/M4契约与幂等口径.md §1
 *   草案枚举：contracts/draft/m4-openapi.yaml → components.schemas.ErrorCode（同一组 17 个）
 *
 *   兜底规则（与 errors.py:_handle_http_exception 一致）：响应体没有结构化错误码、
 *   或返回了枚举外的码时，按 HTTP 状态折算（>=500 → INTERNAL_ERROR；其余未映射 4xx →
 *   MALFORMED_REQUEST），**枚举外的原码只放进 details.upstreamCode，绝不当成 .code 透传**
 *   —— 这样页面侧的 switch(err.code) 永远只需要覆盖 18 个分支。
 *
 * ---------------------------------------------------------------------------
 * 三、静态降级怎么触发（红线 4：接不上后端不得白屏）
 * ---------------------------------------------------------------------------
 *   触发条件（只认「连不上」，不认业务错误）：
 *     1) navigator.onLine === false；
 *     2) fetch 直接 reject（连接被拒 / DNS 失败 / CORS 被浏览器拦 / 用 file:// 打开页面）；
 *     3) 请求超时（AbortController，默认 8000ms，可 configure）。
 *   ⇒ 服务端**返回**的结构化错误（401/403/404/409/412/422/429/503…）一律照抛 ApiError，
 *     因为那证明后端是活的；把 4xx 也降级成样例会把真错误藏起来。
 *   ⇒ 降级只在「本次请求登记过静态样例」时生效（API.registerStatic(path, v) 或调用时传 {static: v}）。
 *     未登记就抛 ApiError(DEPENDENCY_UNAVAILABLE, {offline:true})，页面据此渲染错误态 —— 仍然不白屏。
 *   ⇒ 降级返回的数据带**不可枚举**的 __static:true（数组上还带 page/meta），
 *     页面想打「演示数据」角标读 `. __static` 即可；JSON 序列化不受影响。
 *   ⇒ mode 已知为 offline 时**直接走降级分支、不再等超时**（32 页 × N 次请求都干等 8s 会让 demo 假死）；
 *     需要强发网络时传 {forceNetwork:true}。
 *
 * ---------------------------------------------------------------------------
 * 四、契约形状的两处已知分歧（本文件对两种形状都兼容 —— 磁盘上真的两种都有）
 * ---------------------------------------------------------------------------
 *   [已核实] 错误体：
 *     正式契约 → {error:{code,message,details,retryable}, meta:{requestId}}
 *                （errors.py:361 ErrorEnvelopeForOpenApi / errors.py:384 error_response）
 *     草案+mock → 裸 ErrorBody {code,message,details,retryable}
 *                （draft yaml components.schemas.ErrorBody；tools/dev/mock_m4/mock_server.py:96）
 *     ⇒ 解包先找 payload.error，找不到再当裸错误体。
 *   [已核实] 分页：
 *     正式契约 → {data:[...], page:{nextCursor,hasMore}, meta:{requestId}}
 *                （platform/pagination.py:241 PaginatedEnvelope）
 *     草案+mock → {data:[...], meta:{requestId, page:{nextCursor,hasMore,total}}}
 *                （draft yaml PaginatedEnvelope_*；mock_server.py:66）
 *     ⇒ 解包以 meta.page 优先，回落顶层 page；两种都能翻页。
 *
 * ---------------------------------------------------------------------------
 * 五、[待核实] 登记（查不到 / 有出入的，如实标出，未擅自补全）
 * ---------------------------------------------------------------------------
 *   [待核实 1] WP35 导出端点的正式契约条目：代码已装配（modules/exports/api.py；main.py:226/489），
 *              但盘上 contracts/openapi.json（77 paths）里 exports 出现 **0 次**
 *              （outputs/项目进度盘点与落地计划_Demo优先_2026-10-16.md:236 已登记此现象）。
 *              本文件的路径/ETag 语义按 modules/exports/api.py 的 handler 实现与 docstring 引用。
 *   [待核实 2] Idempotency-Key 长度约束：草案契约写 8–128（draft yaml components.parameters.IdempotencyKey），
 *              而 modules/exports/types.py:51 写 MAX_IDEMPOTENCY_KEY_CHARS = 160。本文件生成 36 字符 UUIDv4，
 *              两种口径都过；**不在前端实现长度校验**，交给服务端判 422，避免这边钉一个与服务端不一致的界。
 *   [待核实 3] /reports/{reportId}/export、/deliveries、/publications、/metric-* 等 M4 端点：
 *              仅存在于草案 m4-openapi.yaml 与 mock，正式 openapi.json 里没有（M4 未开发）。
 *              本文件只提供通用 request/page，不为其写死形状。
 *
 * ---------------------------------------------------------------------------
 * 六、三个硬约束推出的设计（不是偏好）
 * ---------------------------------------------------------------------------
 *   · 会话是 HttpOnly + SameSite=Lax 的 Cookie（platform/csrf.py:44 SESSION_COOKIE_NAME='rednote_session'），
 *     ⇒ 必须 credentials:'same-origin'（默认）才能带上 Cookie；**任何令牌都不进 localStorage**（研发计划 01 §10.1）。
 *   · 写方法（POST/PUT/PATCH/DELETE）必须带 X-CSRF-Token（双提交，csrf.py:212 CSRF_HEADER_NAME），
 *     值从**可读的** rednote_csrf Cookie 取（csrf.py:520 CSCookie 刻意不 HttpOnly），或从 /me、dev 登录响应取。
 *   · 后端没有 CORS 中间件（全仓 grep CORSMiddleware 命中 0）
 *     ⇒ 真后端场景静态页应由后端自己的 StaticFiles Mount 提供（main.py:617），同源，天然不触发 CORS；
 *       连 mock（mock_server.py:130 ACAO:*）时默认 same-origin 恰好避开「* + credentials」被浏览器拦的死局。
 *       需要跨源带 Cookie 时 API.configure({credentials:'include'})，但那时服务端必须配 CORS 白名单。
 *   · X-Request-Id（errors.py:35）默认**不发**：mock 的 Access-Control-Allow-Headers 只放行
 *     Content-Type / Idempotency-Key / If-Match（mock_server.py:131），多发一个头会让预检失败。
 *     真后端场景 configure({sendRequestId:true}) 打开即可（服务端缺该头会自动生成，main.py:128）。
 * ========================================================================== */

(function (global) {
  'use strict';

  /* ==========================================================================
   * 1. 错误码枚举（全部派生自 errors.py，见头部第二节）
   * ======================================================================== */

  const CODES = Object.freeze([
    'MALFORMED_REQUEST',
    'SESSION_EXPIRED',
    'PERMISSION_DENIED',
    'EXTERNAL_POLICY_DENIED',
    'RESOURCE_NOT_FOUND',
    'INVALID_TRANSITION',
    'IDEMPOTENCY_CONFLICT',
    'DUPLICATE_PUBLICATION',
    'EVENT_HISTORY_EXPIRED',
    'VERSION_CONFLICT',
    'UPLOAD_TOO_LARGE',
    'VALIDATION_FAILED',
    'BUDGET_EXCEEDED',
    'DIRECTION_UNVERIFIED',
    'PRECONDITION_REQUIRED',
    'RATE_LIMITED',
    'DEPENDENCY_UNAVAILABLE',
    'INTERNAL_ERROR' // 500 专用，不在 ErrorCode 枚举内（errors.py:355），但属「18 个」口径
  ]);

  const ERROR_CODES = {};
  CODES.forEach(function (c) { ERROR_CODES[c] = c; });
  Object.freeze(ERROR_CODES);

  const CODE_SET = new Set(CODES);

  // errors.py:74 _STATUS_BY_CODE（INTERNAL_ERROR 单列为 500）
  const STATUS_BY_CODE = Object.freeze({
    MALFORMED_REQUEST: 400,
    SESSION_EXPIRED: 401,
    PERMISSION_DENIED: 403,
    EXTERNAL_POLICY_DENIED: 403,
    RESOURCE_NOT_FOUND: 404,
    INVALID_TRANSITION: 409,
    IDEMPOTENCY_CONFLICT: 409,
    DUPLICATE_PUBLICATION: 409,
    EVENT_HISTORY_EXPIRED: 410,
    VERSION_CONFLICT: 412,
    UPLOAD_TOO_LARGE: 413,
    VALIDATION_FAILED: 422,
    BUDGET_EXCEEDED: 422,
    DIRECTION_UNVERIFIED: 422,
    PRECONDITION_REQUIRED: 428,
    RATE_LIMITED: 429,
    DEPENDENCY_UNAVAILABLE: 503,
    INTERNAL_ERROR: 500
  });

  // errors.py:97 _RETRYABLE_BY_CODE —— 只有 429 与 503 可重试
  const RETRYABLE_BY_CODE = Object.freeze({
    MALFORMED_REQUEST: false,
    SESSION_EXPIRED: false,
    PERMISSION_DENIED: false,
    EXTERNAL_POLICY_DENIED: false,
    RESOURCE_NOT_FOUND: false,
    INVALID_TRANSITION: false,
    IDEMPOTENCY_CONFLICT: false,
    DUPLICATE_PUBLICATION: false,
    EVENT_HISTORY_EXPIRED: false,
    VERSION_CONFLICT: false,
    UPLOAD_TOO_LARGE: false,
    VALIDATION_FAILED: false,
    BUDGET_EXCEEDED: false,
    DIRECTION_UNVERIFIED: false,
    PRECONDITION_REQUIRED: false,
    RATE_LIMITED: true,
    DEPENDENCY_UNAVAILABLE: true,
    INTERNAL_ERROR: false
  });

  // errors.py:129 _CODE_BY_STATUS —— 无结构化错误体时按 HTTP 折算
  const CODE_BY_STATUS = Object.freeze({
    400: 'MALFORMED_REQUEST',
    401: 'SESSION_EXPIRED',
    403: 'PERMISSION_DENIED',
    404: 'RESOURCE_NOT_FOUND',
    409: 'INVALID_TRANSITION',
    410: 'EVENT_HISTORY_EXPIRED',
    412: 'VERSION_CONFLICT',
    413: 'UPLOAD_TOO_LARGE',
    422: 'VALIDATION_FAILED',
    428: 'PRECONDITION_REQUIRED',
    429: 'RATE_LIMITED',
    503: 'DEPENDENCY_UNAVAILABLE'
  });

  // errors.py:104 _MESSAGE_BY_CODE —— 兜底中文提示（页面可直接展示）
  const MESSAGE_BY_CODE = Object.freeze({
    MALFORMED_REQUEST: '请求格式不正确。',
    SESSION_EXPIRED: '会话已过期，请重新登录。',
    PERMISSION_DENIED: '没有执行该操作的权限。',
    EXTERNAL_POLICY_DENIED: '当前项目策略不允许访问外部模型。',
    RESOURCE_NOT_FOUND: '资源不存在或无权访问。',
    INVALID_TRANSITION: '当前状态不允许该操作。',
    IDEMPOTENCY_CONFLICT: '相同幂等键对应了不同的请求内容。',
    DUPLICATE_PUBLICATION: '该笔记已登记过，请勿重复计入。',
    EVENT_HISTORY_EXPIRED: '事件历史已过期，请全量同步。',
    VERSION_CONFLICT: '内容已被更新，请比较新版本后再提交。',
    UPLOAD_TOO_LARGE: '上传内容超出允许大小。',
    VALIDATION_FAILED: '字段校验未通过。',
    BUDGET_EXCEEDED: '项目预算不足，无法提交该任务。',
    DIRECTION_UNVERIFIED: '该方向缺少已验证的通过样例。',
    PRECONDITION_REQUIRED: '缺少 If-Match 等前置条件。',
    RATE_LIMITED: '请求过于频繁，请稍后重试。',
    DEPENDENCY_UNAVAILABLE: '依赖服务暂时不可用。',
    INTERNAL_ERROR: '服务内部错误。'
  });

  /* ==========================================================================
   * 2. ApiError —— 页面区分 401/403/404/409/412/422 的唯一载体
   * ======================================================================== */

  class ApiError extends Error {
    /**
     * @param {string} code     业务码，恒为 CODES 之一
     * @param {string} [message] 人类可读提示；缺省取 MESSAGE_BY_CODE
     * @param {object} [opts]   {status, details, retryable, body, requestId, url, method, path, offline}
     */
    constructor(code, message, opts) {
      opts = opts || {};
      const known = CODE_SET.has(code) ? code : 'INTERNAL_ERROR';
      super(message || MESSAGE_BY_CODE[known] || known);
      this.name = 'ApiError';
      this.code = known;                                  // 业务 code（18 枚举之一）
      this.status = opts.status !== undefined ? opts.status : (STATUS_BY_CODE[known] || 0);
      this.details = opts.details || {};
      this.retryable = opts.retryable !== undefined ? !!opts.retryable : !!RETRYABLE_BY_CODE[known];
      this.body = opts.body !== undefined ? opts.body : null;   // 原始响应体（可能是字符串）
      this.requestId = opts.requestId || '';
      this.url = opts.url || '';
      this.method = opts.method || '';
      this.path = opts.path || '';
      this.offline = !!opts.offline;                      // true = 连不上，不是服务端说的
      if (opts.upstreamCode) this.upstreamCode = opts.upstreamCode;
    }

    is(code) { return this.code === code; }

    toString() {
      return 'ApiError[' + this.code + (this.status ? ' ' + this.status : '') + '] ' + this.message;
    }
  }

  /* ==========================================================================
   * 3. 配置 + API_BASE
   * ======================================================================== */

  const DEFAULT_BASE = 'http://127.0.0.1:8787/api/v1'; // = mock_m4 的默认端口（tools/dev/mock_m4/README.md）
  const DEFAULT_TIMEOUT_MS = 8000;
  const PROBE_TTL_MS = 15000;
  const PROBE_PATH = '/health'; // 正式契约有 /api/v1/health（health/api.py:119，只答进程活着、不碰库）

  const config = {
    baseUrl: String(global.REDNOTE_API_BASE || DEFAULT_BASE).replace(/\/+$/, ''),
    timeoutMs: DEFAULT_TIMEOUT_MS,
    credentials: 'same-origin', // 见头部第六节：同源带 Cookie，且不与 mock 的 ACAO:* 冲突
    sendRequestId: false,       // 见头部第六节：默认不发，避免 mock 预检失败
    autoIdempotency: true,      // 写方法未显式给键时自动生成（定案 §3.1）
    csrf: true                  // 写方法自动带 X-CSRF-Token（取不到就不带）
  };

  let _mode = 'unknown';        // unknown | online | offline
  let _modeReason = 'init';
  let _probePromise = null;
  let _lastProbeAt = 0;
  let _lastDegrade = null;
  let _csrfToken = null;

  const degradeListeners = [];
  const modeListeners = [];

  function setMode(next, reason) {
    if (_mode === next && _modeReason === reason) return;
    const prev = _mode;
    _mode = next;
    _modeReason = reason || '';
    if (prev !== next) {
      modeListeners.slice().forEach(function (cb) {
        try { cb(_mode, _modeReason, API); } catch (e) { /* 订阅者异常不影响主流程 */ }
      });
    }
  }

  function emitDegrade(record) {
    _lastDegrade = record;
    degradeListeners.slice().forEach(function (cb) {
      try { cb(record, API); } catch (e) { /* 同上 */ }
    });
  }

  function configure(patch) {
    patch = patch || {};
    if (patch.baseUrl) config.baseUrl = String(patch.baseUrl).replace(/\/+$/, '');
    if (patch.timeoutMs != null) config.timeoutMs = Number(patch.timeoutMs) || DEFAULT_TIMEOUT_MS;
    if (patch.credentials) config.credentials = patch.credentials;
    if (patch.sendRequestId != null) config.sendRequestId = !!patch.sendRequestId;
    if (patch.autoIdempotency != null) config.autoIdempotency = !!patch.autoIdempotency;
    if (patch.csrf != null) config.csrf = !!patch.csrf;
    if (patch.redetect) { _mode = 'unknown'; _modeReason = 'reconfigured'; _lastProbeAt = 0; }
    return API;
  }

  /* ==========================================================================
   * 4. 底层 fetch（超时 + 把「连不上」与「HTTP 错误」区分开）
   * ======================================================================== */

  /**
   * fetch 只在网络层失败时 reject；HTTP 4xx/5xx 是 resolve。
   * 本函数把 reject 与超时统一打上 __offline 标记，交给上层决定降级还是抛错。
   */
  function fetchWithTimeout(url, init, timeoutMs) {
    const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    let timedOut = false;
    let timer = null;
    if (ctrl && timeoutMs > 0) {
      timer = setTimeout(function () { timedOut = true; ctrl.abort(); }, timeoutMs);
    }
    const finalInit = {
      method: init.method,
      headers: init.headers,
      body: init.body,
      credentials: init.credentials,
      cache: init.cache || 'no-store',
      redirect: init.redirect || 'follow'
    };
    if (ctrl) finalInit.signal = ctrl.signal;

    return global.fetch(url, finalInit).then(
      function (res) { if (timer) clearTimeout(timer); return res; },
      function (err) {
        if (timer) clearTimeout(timer);
        const reason = timedOut ? 'timeout' : (err && err.name === 'AbortError' ? 'aborted' : 'network_error');
        const e = new Error(
          timedOut ? ('请求超时（' + timeoutMs + 'ms）：' + url) : ('无法连接 ' + url)
        );
        e.__offline = true;
        e.reason = reason;
        e.cause = err;
        throw e;
      }
    );
  }

  /* ==========================================================================
   * 5. 解包（兼容正式契约与草案/mock 两套形状，见头部第四节）
   * ======================================================================== */

  function unwrap(payload) {
    if (payload === null || payload === undefined || typeof payload !== 'object') {
      return { data: payload === undefined ? null : payload, page: null, meta: null, envelope: false };
    }
    if (Object.prototype.hasOwnProperty.call(payload, 'data')) {
      const meta = (payload.meta && typeof payload.meta === 'object') ? payload.meta : null;
      const page = (meta && meta.page) || payload.page || null; // meta.page 优先（草案），回落顶层 page（正式）
      return { data: payload.data, page: page, meta: meta, envelope: true };
    }
    return { data: payload, page: null, meta: null, envelope: false };
  }

  /** 不可枚举地挂 page/meta/__static，避免污染页面的 for-in 与 JSON.stringify */
  function attachHidden(target, props) {
    if (target === null || typeof target !== 'object') return target;
    Object.keys(props).forEach(function (k) {
      try {
        Object.defineProperty(target, k, {
          value: props[k], enumerable: false, configurable: true, writable: true
        });
      } catch (e) { /* 冻结对象等场景忽略 */ }
    });
    return target;
  }

  /** 从响应体里取出结构化错误块；正式契约 {error:{...}}，草案/mock 是裸 ErrorBody */
  function extractErrorBody(payload) {
    if (payload && typeof payload === 'object') {
      if (payload.error && typeof payload.error === 'object') return payload.error;
      if (typeof payload.code === 'string') return payload;
    }
    return null;
  }

  function errorFromResponse(status, payload, text, ctx) {
    const eb = extractErrorBody(payload);
    const requestId = ctx.requestId || (payload && payload.meta && payload.meta.requestId) || '';
    const base = { status: status, body: payload !== null ? payload : text, requestId: requestId,
                   url: ctx.url, method: ctx.method, path: ctx.path };

    if (eb && typeof eb.code === 'string') {
      if (CODE_SET.has(eb.code)) {
        return new ApiError(eb.code, eb.message, Object.assign(base, {
          details: eb.details || {},
          retryable: eb.retryable !== undefined ? eb.retryable : undefined
        }));
      }
      // 枚举外的码：不透传成 .code（红线 3），折算到 HTTP 对应码，原码留证据
      base.upstreamCode = eb.code;
      base.details = eb.details || {};
    }
    // 无结构化错误体（例如反向代理/静态处理器吐 HTML），或码不在枚举内 → 按 HTTP 折算
    const code = CODE_BY_STATUS[status] || (status >= 500 ? 'INTERNAL_ERROR' : 'MALFORMED_REQUEST');
    const details = base.details || {};
    if (!eb && text) details.bodySnippet = String(text).slice(0, 300);
    if (!eb) details.reason = 'non_json_error_body';
    return new ApiError(code, eb && eb.message, Object.assign(base, { details: details }));
  }

  /* ==========================================================================
   * 6. 静态降级注册表 + 幂等键 + CSRF
   * ======================================================================== */

  const staticRegistry = new Map();

  function staticKey(path) {
    const s = String(path == null ? '' : path);
    const noQuery = s.split('?')[0];
    return noQuery.replace(/\/+$/, '') || '/';
  }

  function registerStatic(path, value) {
    staticRegistry.set(staticKey(path), value);
    return API;
  }

  function staticValue(path) {
    const k = staticKey(path);
    return staticRegistry.has(k) ? staticRegistry.get(k) : undefined;
  }

  function resolveStatic(path, opts) {
    if (opts && Object.prototype.hasOwnProperty.call(opts, 'static')) return opts.static; // 显式传 null 也是「不降级」
    return staticValue(path);
  }

  function newIdempotencyKey() {
    if (global.crypto && typeof global.crypto.randomUUID === 'function') return global.crypto.randomUUID();
    // 回退：非安全上下文（http:// 非 localhost）下 randomUUID 可能不存在
    return 'demo-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
  }

  /**
   * 同一次用户操作复用同一幂等键：`API.operation(key => API.post('/deliveries', body, {idempotencyKey: key}))`
   * 重试也用同一个 key —— 同键同体 ⇒ 服务端回放首次结果（M4契约与幂等口径.md §2.1）。
   */
  function operation(fn) {
    const key = newIdempotencyKey();
    return Promise.resolve().then(function () { return fn(key, API); });
  }

  function csrfFromCookie() {
    if (typeof document === 'undefined' || !document.cookie) return null;
    const m = document.cookie.match(/(?:^|;\s*)rednote_csrf=([^;]*)/); // csrf.py: CSRF_COOKIE_NAME
    return m ? decodeURIComponent(m[1]) : null;
  }

  function currentCsrf() { return _csrfToken || csrfFromCookie(); }

  /* ==========================================================================
   * 7. 请求核心
   * ======================================================================== */

  const WRITE_METHODS = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };

  function buildUrl(path, query) {
    let url;
    if (/^https?:\/\//i.test(path)) url = path;
    else url = config.baseUrl + (String(path).charAt(0) === '/' ? path : '/' + path);

    if (query) {
      const parts = [];
      Object.keys(query).forEach(function (k) {
        const v = query[k];
        if (v === null || v === undefined || v === '') return;
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
      });
      if (parts.length) url += (url.indexOf('?') === -1 ? '?' : '&') + parts.join('&');
    }
    return url;
  }

  function degradeOrThrow(path, opts, info) {
    const fb = resolveStatic(path, opts);
    if (fb !== undefined) {
      const value = (typeof fb === 'function')
        ? fb({ path: path, url: info.url, reason: info.reason, method: info.method })
        : fb;
      attachHidden(value, { __static: true, __degradeReason: info.reason });
      emitDegrade({ path: path, url: info.url, method: info.method, reason: info.reason, static: true });
      return value;
    }
    emitDegrade({ path: path, url: info.url, method: info.method, reason: info.reason, static: false });
    throw new ApiError('DEPENDENCY_UNAVAILABLE', '连不上后端（' + info.reason + '），且本页未登记静态样例。', {
      status: 0,
      offline: true,
      url: info.url,
      method: info.method,
      path: path,
      details: {
        reason: info.reason,
        clientSide: true,
        hint: 'API.registerStatic(path, 静态样例) 后即可降级渲染，避免白屏。'
      }
    });
  }

  /**
   * 统一请求。成功时**只返回解包后的 data**（定案 §3.1）；
   * 若响应带 page/meta，会以不可枚举属性挂在返回的数组/对象上，页面仍可用。
   */
  async function request(method, path, opts) {
    opts = opts || {};
    const upper = String(method || 'GET').toUpperCase();
    const isWrite = !!WRITE_METHODS[upper];
    const query = opts.query || opts.params || null;
    const url = buildUrl(path, query);
    const timeoutMs = opts.timeoutMs != null ? opts.timeoutMs : config.timeoutMs;
    const fallbackDeclared = resolveStatic(path, opts) !== undefined;

    // --- 预判：已知离线 且 本次有静态样例 ⇒ 不走网络（演示态秒开）---
    const offlineKnown = _mode === 'offline' && (Date.now() - _lastProbeAt) < PROBE_TTL_MS;
    const navigatorOffline = (typeof navigator !== 'undefined' && navigator.onLine === false);
    if (!opts.forceNetwork && fallbackDeclared && (offlineKnown || navigatorOffline)) {
      return degradeOrThrow(path, opts, { reason: navigatorOffline ? 'navigator_offline' : _modeReason, url: url, method: upper });
    }

    const headers = { Accept: 'application/json' };
    let body;
    if (opts.body !== undefined && opts.body !== null) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(opts.body);
    }
    if (config.sendRequestId) headers['X-Request-Id'] = newIdempotencyKey(); // errors.py:35 REQUEST_ID_HEADER
    if (opts.ifMatch !== undefined && opts.ifMatch !== null) headers['If-Match'] = String(opts.ifMatch); // 乐观锁
    if (opts.headers) Object.keys(opts.headers).forEach(function (k) { headers[k] = opts.headers[k]; });

    if (isWrite) {
      const explicitKey = Object.prototype.hasOwnProperty.call(opts, 'idempotencyKey');
      const key = explicitKey ? opts.idempotencyKey : (config.autoIdempotency ? newIdempotencyKey() : null);
      if (key) headers['Idempotency-Key'] = String(key);
      if (config.csrf) {
        const tok = currentCsrf();
        if (tok) headers['X-CSRF-Token'] = tok; // csrf.py CSRF_HEADER_NAME
      }
    }

    let res;
    try {
      res = await fetchWithTimeout(url, {
        method: upper, headers: headers, body: body, credentials: config.credentials
      }, timeoutMs);
    } catch (netErr) {
      _lastProbeAt = Date.now(); // 刚拿到「连不上」的新证据，供后面的短路判断使用
      setMode('offline', netErr.reason);
      return degradeOrThrow(path, opts, { reason: netErr.reason, url: url, method: upper });
    }

    _lastProbeAt = Date.now();
    setMode('online', 'http_' + res.status); // 拿到任何 HTTP 响应都说明后端活着
    const requestId = res.headers && res.headers.get ? (res.headers.get('X-Request-Id') || '') : '';
    const text = await res.text();
    let payload = null;
    if (text) { try { payload = JSON.parse(text); } catch (e) { payload = null; } }

    if (!res.ok) {
      throw errorFromResponse(res.status, payload, text, {
        url: url, method: upper, path: path, requestId: requestId
      });
    }

    if (res.status === 204 || res.status === 205) return null;

    if (opts.responseType === 'text') return text;

    if (payload === null) return text; // 2xx 但非 JSON（例如 text/markdown）——原样给回

    const u = unwrap(payload);
    return attachHidden(u.data, { page: u.page, meta: u.meta, __static: false });
  }

  function get(path, opts) { return request('GET', path, opts); }
  function post(path, body, opts) { return request('POST', path, Object.assign({}, opts, { body: body })); }
  function put(path, body, opts) { return request('PUT', path, Object.assign({}, opts, { body: body })); }
  function patch(path, body, opts) { return request('PATCH', path, Object.assign({}, opts, { body: body })); }
  function del(path, opts) { return request('DELETE', path, opts); }

  /* ==========================================================================
   * 8. 分页（游标）—— 契约参数：limit(默认20/最大100) + cursor
   * ======================================================================== */

  async function page(path, opts) {
    opts = opts || {};
    const query = Object.assign({}, opts.params, opts.query);
    if (opts.limit != null) query.limit = opts.limit;
    if (opts.cursor != null) query.cursor = opts.cursor;

    const data = await request('GET', path, Object.assign({}, opts, { query: query }));
    const isArr = Object.prototype.toString.call(data) === '[object Array]';
    return {
      items: isArr ? data : (data == null ? [] : [data]),
      page: (data && data.page) || null,
      meta: (data && data.meta) || null,
      static: !!(data && data.__static)
    };
  }

  /** 按 nextCursor 逐页遍历，cb({items, page, static}) 返回 false 可提前终止 */
  async function eachPage(path, cb, opts) {
    let cursor = null;
    const seen = new Set();
    for (;;) {
      const res = await page(path, Object.assign({}, opts, { cursor: cursor }));
      const stop = cb ? cb(res) : undefined;
      if (stop === false) return;
      if (!res.page || !res.page.hasMore || !res.page.nextCursor) return;
      if (seen.has(res.page.nextCursor)) return; // 防服务端游标不前进造成死循环
      seen.add(res.page.nextCursor);
      cursor = res.page.nextCursor;
    }
  }

  /* ==========================================================================
   * 9. 下载（If-None-Match / ETag）
   *
   * WP35「固定版本 ZIP 鉴权下载」的实测语义（modules/exports/api.py:318-368）：
   *   GET /projects/{projectId}/exports/{exportGroupId}/download
   *   响应头 ETag = "<zip_sha256>"（带引号的强校验器）；命中 If-None-Match ⇒ 304；
   *   200 时带 Content-Length / Content-Disposition: attachment / Cache-Control: private。
   *   ⚠️ 断点续传（Range）在 WP35 §2.7 已明确砍掉 —— 本文件不实现 Range。
   * ======================================================================== */

  async function download(path, opts) {
    opts = opts || {};
    const url = buildUrl(path, opts.query || opts.params || null);
    const headers = {};
    if (opts.etag) headers['If-None-Match'] = String(opts.etag);
    if (opts.headers) Object.keys(opts.headers).forEach(function (k) { headers[k] = opts.headers[k]; });

    let res;
    try {
      res = await fetchWithTimeout(url, {
        method: 'GET', headers: headers, credentials: config.credentials, cache: 'no-store'
      }, opts.timeoutMs != null ? opts.timeoutMs : config.timeoutMs);
    } catch (netErr) {
      _lastProbeAt = Date.now();
      setMode('offline', netErr.reason);
      emitDegrade({ path: path, url: url, method: 'GET', reason: netErr.reason, static: false });
      throw new ApiError('DEPENDENCY_UNAVAILABLE', '连不上后端，无法下载。', {
        status: 0, offline: true, url: url, method: 'GET', path: path,
        details: { reason: netErr.reason, clientSide: true }
      });
    }

    setMode('online', 'http_' + res.status);
    const etag = (res.headers && res.headers.get) ? (res.headers.get('ETag') || '') : '';
    const disposition = (res.headers && res.headers.get) ? (res.headers.get('Content-Disposition') || '') : '';
    const length = (res.headers && res.headers.get) ? (res.headers.get('Content-Length') || '') : '';
    const requestId = (res.headers && res.headers.get) ? (res.headers.get('X-Request-Id') || '') : '';

    if (res.status === 304) return { status: 304, notModified: true, etag: etag, blob: null, filename: '', size: 0, url: url };

    if (!res.ok) {
      const text = await res.text();
      let payload = null;
      if (text) { try { payload = JSON.parse(text); } catch (e) { payload = null; } }
      throw errorFromResponse(res.status, payload, text, { url: url, method: 'GET', path: path, requestId: requestId });
    }

    const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    const blob = (typeof res.blob === 'function') ? await res.blob() : null;
    return {
      status: res.status,
      notModified: false,
      etag: etag,
      blob: blob,
      filename: m ? decodeURIComponent(m[1]) : (opts.filename || ''),
      size: length ? Number(length) : (blob ? blob.size : 0),
      url: url
    };
  }

  /* ==========================================================================
   * 10. 端点常量（逐条标注来源；未在磁盘核实的绝不写进来）
   * ======================================================================== */

  const ENDPOINTS = Object.freeze({
    // —— 正式契约 contracts/openapi.json 里有（77 paths 内）——
    health: '/health',                 // health/api.py:119
    me: '/me',                         // identity/api.py:438
    logout: '/auth/logout',            // identity/api.py:399（POST，需 CSRF）
    login: '/auth/login',              // identity/api.py:214（GET，302 到 IdP，未在本层封装）
    // —— dev/test 专用，不进契约（devtools/api.py:135；生产路由表里不存在）——
    devLogin: '/dev/auth/login',
    // —— WP35 导出 [待核实 1]：代码已装配，但未进当前 openapi.json 快照 ——
    exportCreate: '/projects/{projectId}/exports',                                  // modules/exports/api.py:194
    exportList: '/projects/{projectId}/exports',                                    // modules/exports/api.py:265
    exportDownload: '/projects/{projectId}/exports/{exportGroupId}/download',        // modules/exports/api.py:318
    exportVerification: '/projects/{projectId}/exports/{exportGroupId}/verification',// modules/exports/api.py:376
    // —— 草案 m4-openapi.yaml 有、正式契约没有（M4 未开发）——
    reportMarkdownExport: '/reports/{reportId}/export'                              // draft m4-openapi.yaml:804 + mock
  });

  function fill(template, params) {
    return String(template).replace(/\{(\w+)\}/g, function (_, k) {
      if (params && params[k] != null) return encodeURIComponent(params[k]);
      return '{' + k + '}';
    });
  }

  function exportZip(projectId, exportGroupId, opts) {
    return download(fill(ENDPOINTS.exportDownload, { projectId: projectId, exportGroupId: exportGroupId }), opts);
  }

  function verifyExport(projectId, exportGroupId, opts) {
    return get(fill(ENDPOINTS.exportVerification, { projectId: projectId, exportGroupId: exportGroupId }), opts);
  }

  function reportMarkdown(reportId, opts) {
    opts = opts || {};
    const query = {};
    if (opts.versionNo != null) query.versionNo = opts.versionNo;
    return request('GET', fill(ENDPOINTS.reportMarkdownExport, { reportId: reportId }),
      Object.assign({}, opts, { query: query, responseType: 'text' }));
  }

  /* ==========================================================================
   * 11. 探测 + 鉴权
   * ======================================================================== */

  async function probe(opts) {
    opts = opts || {};
    const fresh = (Date.now() - _lastProbeAt) < PROBE_TTL_MS;
    if (!opts.force && fresh && _mode !== 'unknown') return _mode === 'online';
    if (!opts.force && _probePromise) return _probePromise;

    _probePromise = (async function () {
      if (typeof navigator !== 'undefined' && navigator.onLine === false) {
        _lastProbeAt = Date.now();
        setMode('offline', 'navigator_offline');
        return false;
      }
      try {
        const res = await fetchWithTimeout(
          buildUrl(PROBE_PATH, null),
          { method: 'GET', headers: { Accept: 'application/json' }, credentials: config.credentials, cache: 'no-store' },
          opts.timeoutMs != null ? opts.timeoutMs : config.timeoutMs
        );
        // 任何 HTTP 响应（含 401/404）都说明服务活着 —— 只有连不上才算离线
        _lastProbeAt = Date.now();
        setMode('online', 'http_' + res.status);
        return true;
      } catch (netErr) {
        _lastProbeAt = Date.now();
        setMode('offline', netErr.reason);
        return false;
      } finally {
        _probePromise = null;
      }
    })();

    return _probePromise;
  }

  const auth = {
    /** GET /me → {id, displayName, role, csrfToken, capabilities}（identity/api.py:125 MeBody） */
    me: function (opts) { return get(ENDPOINTS.me, opts); },
    /** POST /dev/auth/login（仅 dev/test 环境存在）→ {userId, displayName, role, csrfToken, ...} */
    devLogin: function (body, opts) {
      return post(ENDPOINTS.devLogin, Object.assign({ email: 'demo@rednote.local' }, body || {}),
        Object.assign({ idempotencyKey: null }, opts)); // 登录免幂等（devtools/api.py:166）
    },
    logout: function (opts) { return post(ENDPOINTS.logout, null, opts); },
    setCsrfToken: function (t) { _csrfToken = t || null; return API; },
    getCsrfToken: function () { return currentCsrf(); },
    /** 写操作前确保有 CSRF 令牌（没有就打一次 /me 取；csrf.py 双提交模式） */
    ensureCsrf: async function () {
      if (currentCsrf()) return currentCsrf();
      const me = await get(ENDPOINTS.me);
      if (me && me.csrfToken) _csrfToken = me.csrfToken;
      return currentCsrf();
    }
  };

  /* ==========================================================================
   * 12. 门面
   * ======================================================================== */

  // 浏览器联网状态变化时让探测缓存失效（非浏览器环境无此 API，跳过）
  if (typeof global.addEventListener === 'function') {
    global.addEventListener('online', function () { _lastProbeAt = 0; setMode('unknown', 'navigator_online'); });
    global.addEventListener('offline', function () { _lastProbeAt = Date.now(); setMode('offline', 'navigator_offline'); });
  }

  const API = {
    // 配置与联通性
    configure: configure,
    probe: probe,
    getConfig: function () { return Object.assign({}, config); },
    isOnline: function () { return _mode === 'online'; },
    isOffline: function () { return _mode === 'offline'; },
    lastDegrade: function () { return _lastDegrade; },
    onDegrade: function (cb) { degradeListeners.push(cb); return API; },
    onModeChange: function (cb) { modeListeners.push(cb); return API; },

    // 请求
    request: request,
    get: get, post: post, put: put, patch: patch,
    del: del, 'delete': del,
    page: page, eachPage: eachPage,

    // 下载 / 导出
    download: download,
    exportZip: exportZip,
    verifyExport: verifyExport,
    reportMarkdown: reportMarkdown,

    // 幂等
    newIdempotencyKey: newIdempotencyKey,
    operation: operation,

    // 静态降级
    registerStatic: registerStatic,
    static: staticValue,
    staticKeys: function () { return Array.from(staticRegistry.keys()); },

    // 鉴权
    auth: auth,

    // 工具与常量
    unwrap: unwrap,
    buildUrl: buildUrl,
    fill: fill,
    ENDPOINTS: ENDPOINTS,
    CODES: ERROR_CODES,
    CODE_INFO: Object.freeze({
      ALL_CODES: CODES,
      STATUS_BY_CODE: STATUS_BY_CODE,
      RETRYABLE_BY_CODE: RETRYABLE_BY_CODE,
      CODE_BY_STATUS: CODE_BY_STATUS,
      MESSAGE_BY_CODE: MESSAGE_BY_CODE
    })
  };

  // API_BASE 以只读属性暴露（定案 §3.1）
  Object.defineProperty(API, 'base', { get: function () { return config.baseUrl; }, enumerable: true });
  Object.defineProperty(API, 'mode', { get: function () { return _mode; }, enumerable: true });

  global.RednoteAPI = API;
  global.ApiError = ApiError;
  global.ApiErrorCode = ERROR_CODES;
  global.ApiErrorCodeInfo = API.CODE_INFO;
})(typeof window !== 'undefined' ? window : this);
