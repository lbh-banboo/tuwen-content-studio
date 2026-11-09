/* ============================================================================
 * render/delivery-koc.js —— KOC 轻量记录渲染层（方案 A：纯函数）
 * ============================================================================
 * 数据源：GET /koc-records（扁平数组，无分组）
 *   { id, projectId, displayName, platformHomeUrl, homeUrlCanonical, contactRef }
 *
 * 与 delivery.js 的差异：
 *   · 没有 contentItemId 之类的聚合键，不需要 groupXxx；
 *   · 没有状态枚举，不需要行色/状态汇总；
 *   · 主要风险是 URL（主页链接）与展示名，统一走 shared 的转义与 http(s) 白名单。
 *
 * 纯函数：输入数据、输出 HTML 字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * ========================================================================== */

(function (root, factory) {
  var shared = (typeof module === 'object' && module && module.exports)
    ? require('./shared.js')
    : (root.RednoteRender && root.RednoteRender.shared);
  if (!shared) throw new Error('render/shared.js 必须先于 render/delivery-koc.js 加载');
  var api = factory(shared);
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.deliveryKoc = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (s) {
  'use strict';

  function renderKocRow(record) {
    var row = record && typeof record === 'object' ? record : {};
    var name = s.text(row.displayName) || '未命名 KOC';
    var home = s.safeHttpUrl(row.platformHomeUrl) || s.safeHttpUrl(row.homeUrlCanonical);
    var contact = s.text(row.contactRef);
    var id = s.text(row.id);

    var avatar = '<span class="kav">' + s.escapeHtml(s.firstChar(name) || '?') + '</span>';

    var urlLine = home
      ? '<i class="fa-solid fa-link"></i> ' +
        '<a class="t-mut" href="' + s.escapeHtml(home) + '" target="_blank" rel="noopener noreferrer">' +
        s.escapeHtml(s.displayUrl(home, 52)) + '</a>'
      : '<i class="fa-solid fa-link-slash"></i> 未填写主页链接';

    var contactLine = contact
      ? ' <span class="t-fai">· 联系引用 ' + s.escapeHtml(s.truncate(contact, 48)) + '</span>'
      : '';

    var idLine = id
      ? '<span class="code f10" title="' + s.escapeHtml(id) + '">' +
        s.escapeHtml(s.shortId(id)) + '</span>'
      : '<span class="code f10">—</span>';

    return '<div class="koc">' +
      avatar +
      '<div class="f1">' +
        '<div class="row" style="gap:7px">' +
          '<span class="f12 fw6" title="' + s.escapeHtml(name) + '">' +
          s.escapeHtml(s.truncate(name, 40)) + '</span>' +
          idLine +
        '</div>' +
        '<div class="f10 t-mut mt4">' + urlLine + contactLine + '</div>' +
      '</div>' +
      '<div class="right">' +
        '<span class="tag tag-ok no-dot f10">已登记</span>' +
        '<div class="f10 t-mut mt4">KOC 记录</div>' +
      '</div>' +
      '</div>';
  }

  function renderKocList(records) {
    var list = Array.isArray(records)
      ? records.filter(function (r) { return r && typeof r === 'object'; })
      : [];
    if (!list.length) return renderKocEmpty();
    return list.map(renderKocRow).join('');
  }

  function renderKocEmpty() {
    return '<div class="koc" style="justify-content:center;flex-direction:column;gap:4px;padding:22px 13px;">' +
      '<i class="fa-solid fa-user-group t-fai"></i>' +
      '<span class="f12 t-mut">暂无 KOC 记录</span>' +
      '<span class="f10 t-fai">新增 KOC 后会出现在这里</span>' +
      '</div>';
  }

  function renderKocLoading() {
    return '<div class="koc" style="justify-content:center;flex-direction:column;gap:4px;padding:22px 13px;">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在读取 KOC 记录…</span>' +
      '</div>';
  }

  /**
   * @param {object|string} err ApiError 实例或 {code,message,retryable,offline}
   *   只读普通字段，不依赖 ApiError 类。
   */
  function renderKocError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = s.text(e.code);
    var message = s.text(e.message) || s.text(err) || '读取 KOC 记录失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端（页面结构已保留）。确认 mock / 后端已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');
    var meta = (code ? code + ' · ' : '') + hint;

    return '<div class="koc" style="align-items:flex-start;gap:10px;">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="f1">' +
        '<div class="f12 fw6">' + s.escapeHtml(message) + '</div>' +
        '<div class="f10 t-mut mt4">' + s.escapeHtml(meta) + '</div>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div>';
  }

  return {
    renderKocList: renderKocList,
    renderKocRow: renderKocRow,
    renderKocLoading: renderKocLoading,
    renderKocEmpty: renderKocEmpty,
    renderKocError: renderKocError
  };
});
