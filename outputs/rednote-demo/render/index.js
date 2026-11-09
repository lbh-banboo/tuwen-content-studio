/* ============================================================================
 * render/index.js —— 登录 / 导览落地页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 浏览器里只做一次 innerHTML；Node 单测见 render/index.test.js。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./index.js')
 *   浏览器          → RednoteRender.index
 *
 * 数据形状（api.js 解包后）：
 *   GET /me            → { id, displayName, role, csrfToken, sessionId,
 *                          capabilities: { enabledFormats, localModels,
 *                          allowedExternalProviders, importFormats,
 *                          limits: { trialQuantity, scaleMax, bulkMax } } }
 *   POST /dev/auth/login → { userId, displayName, role, csrfToken, sessionId, expiresAt }
 *
 * 安全约定：csrfToken / sessionId 永不渲染进 HTML（只留在 API 层）。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.index = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var ROLE_META = {
    ADMIN: { label: '管理员', cls: 'tag-purple' },
    OPERATOR: { label: '运营', cls: 'tag-info' }
  };

  var FORMAT_LABELS = {
    TEXT_ONLY: '纯文本',
    STATIC: '静态图',
    LIVE: 'Live 图',
    MIXED: '静态 + Live'
  };

  var IMPORT_LABELS = {
    CSV: 'CSV',
    XLSX: 'XLSX',
    SCREENSHOT: '截图'
  };

  /* ---------------------------- 基础工具 ---------------------------- */

  function text(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function escapeHtml(value) {
    return text(value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }

  function provided(value, fallback) {
    var s = text(value);
    return s ? escapeHtml(s) : '<span class="t-mut">' + escapeHtml(fallback || '未提供') + '</span>';
  }

  function roleMeta(role) {
    return ROLE_META[role] || { label: text(role) || '未提供', cls: '' };
  }

  function tags(list, labels) {
    var arr = Array.isArray(list) ? list : [];
    if (!arr.length) return '<span class="tag tag-muted no-dot f10">未提供</span>';
    return arr.map(function (key) {
      return '<span class="tag no-dot f10">' + escapeHtml(labels[key] || key) + '</span>';
    }).join(' ');
  }

  function capsRows(me) {
    var caps = (me && me.capabilities) || {};
    var limits = caps.limits || {};
    var rows = [
      ['可用内容形式', tags(caps.enabledFormats, FORMAT_LABELS)],
      ['导入格式', tags(caps.importFormats, IMPORT_LABELS)],
      ['产能上限', limits.trialQuantity != null || limits.scaleMax != null || limits.bulkMax != null
        ? escapeHtml('试产 ' + text(limits.trialQuantity || 0) + ' 条 · 扩产 ≤' +
            text(limits.scaleMax || 0) + ' · 批产 ≤' + text(limits.bulkMax || 0))
        : '<span class="t-mut">未提供</span>'],
      ['模型策略', caps.localModels === true
        ? '<span class="tag tag-ok no-dot f10">本地模型可用</span>'
        : '<span class="tag tag-muted no-dot f10">本地模型不可用</span>'],
      ['外部模型', Array.isArray(caps.allowedExternalProviders) && caps.allowedExternalProviders.length
        ? tags(caps.allowedExternalProviders, {})
        : '<span class="t-mut">未开放（allowedExternalProviders 为空）</span>']
    ];
    return rows.map(function (row) {
      return '<div class="row between f11" style="gap:12px">' +
        '<span class="t-mut">' + escapeHtml(row[0]) + '</span>' +
        '<span style="text-align:right">' + row[1] + '</span>' +
        '</div>';
    }).join('');
  }

  /* ---------------------------- 会话 / 登录 ---------------------------- */

  function renderSessionLoading() {
    return '<div class="row" style="justify-content:center;gap:8px;padding:26px 0">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在检查登录态…</span>' +
      '</div>';
  }

  /** GET /me 成功：展示身份与能力，不明文回显 csrfToken / sessionId */
  function renderSession(me) {
    var m = me && typeof me === 'object' ? me : {};
    var role = roleMeta(m.role);
    return '<div class="lp-login-head">' +
        '<div class="lp-login-t">已登录</div>' +
        '<div class="lp-login-d">' + provided(m.displayName, '显示名未提供') + '</div>' +
      '</div>' +
      '<div class="col" style="gap:8px">' +
        '<div class="row between f11"><span class="t-mut">角色</span>' +
          '<span class="tag ' + role.cls + ' no-dot f10">' + escapeHtml(role.label) + '</span></div>' +
        capsRows(m) +
      '</div>' +
      '<div class="lp-login-cta">' +
        '<a class="lp-cta" href="workbench.v2.html"><i class="fa-solid fa-gauge-high"></i>进入工作台</a>' +
        '<a class="lp-cta2" href="projects.v2.html"><i class="fa-solid fa-folder-open"></i>项目中心</a>' +
      '</div>' +
      '<div class="lp-login-note"><i class="fa-solid fa-circle-info"></i>' +
        '<span>会话由 HttpOnly Cookie 维持 · 本页不展示 csrfToken / sessionId</span></div>';
  }

  function renderLoginForm(opts) {
    var o = opts && typeof opts === 'object' ? opts : {};
    return '<div class="lp-login-head">' +
        '<div class="lp-login-t">成员登录</div>' +
        '<div class="lp-login-d">开发旁路 · 仅 dev 环境可用（POST /dev/auth/login）</div>' +
      '</div>' +
      '<form id="login-form" novalidate>' +
        '<div class="field">' +
          '<div class="label">企业邮箱 <span class="req">*</span></div>' +
          '<input class="input" type="email" name="email" required ' +
            'value="' + escapeAttr(o.email || '') + '" placeholder="name@company.cn" />' +
        '</div>' +
        '<button class="lp-cta" type="submit" style="width:100%;justify-content:center;border:0;cursor:pointer">' +
          '<i class="fa-solid fa-arrow-right-to-bracket"></i>开发登录并进入' +
        '</button>' +
      '</form>' +
      '<div class="lp-login-note"><i class="fa-solid fa-circle-info"></i>' +
        '<span>管理员邀请制 · 不开放外部自由注册。登录成功后可进入工作台。</span></div>';
  }

  function renderLoginBusy(email) {
    return '<div class="col" style="align-items:center;gap:8px;padding:26px 0">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在登录 ' + escapeHtml(email || '') + ' …</span>' +
      '</div>';
  }

  /** POST /dev/auth/login 成功（响应无 capabilities，不推测） */
  function renderLoginResult(data) {
    var d = data && typeof data === 'object' ? data : {};
    var role = roleMeta(d.role);
    return '<div class="lp-login-head">' +
        '<div class="lp-login-t">登录成功</div>' +
        '<div class="lp-login-d">' + provided(d.displayName, '显示名未提供') + '</div>' +
      '</div>' +
      '<div class="col" style="gap:8px">' +
        '<div class="row between f11"><span class="t-mut">角色</span>' +
          '<span class="tag ' + role.cls + ' no-dot f10">' + escapeHtml(role.label) + '</span></div>' +
        '<div class="row between f11"><span class="t-mut">会话到期</span>' +
          '<span class="mono">' + provided(d.expiresAt, '未提供') + '</span></div>' +
      '</div>' +
      '<div class="lp-login-cta">' +
        '<a class="lp-cta" href="workbench.v2.html"><i class="fa-solid fa-gauge-high"></i>进入工作台</a>' +
        '<a class="lp-cta2" href="projects.v2.html"><i class="fa-solid fa-folder-open"></i>项目中心</a>' +
      '</div>' +
      '<div class="lp-login-note"><i class="fa-solid fa-circle-info"></i>' +
        '<span>登录响应未携带能力清单；完整能力可在 GET /me 读取。</span></div>';
  }

  function renderAuthError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '登录状态读取失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端。确认 127.0.0.1:8787 已启动后重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查，或重新登录。');
    return '<div class="lp-login-head">' +
        '<div class="lp-login-t">无法读取登录状态</div>' +
        '<div class="lp-login-d">' + escapeHtml((code ? code + ' · ' : '') + hint) + '</div>' +
      '</div>' +
      '<div class="note note-danger"><i class="fa-solid fa-triangle-exclamation"></i>' +
        '<div>' + escapeHtml(message) + '</div></div>' +
      '<div class="lp-login-cta" style="margin-top:14px">' +
        '<button class="lp-cta" type="button" data-action="retry" style="border:0;cursor:pointer">' +
          '<i class="fa-solid fa-rotate"></i>重试</button>' +
      '</div>';
  }

  return {
    renderSessionLoading: renderSessionLoading,
    renderSession: renderSession,
    renderLoginForm: renderLoginForm,
    renderLoginBusy: renderLoginBusy,
    renderLoginResult: renderLoginResult,
    renderAuthError: renderAuthError,
    escapeHtml: escapeHtml
  };
});
