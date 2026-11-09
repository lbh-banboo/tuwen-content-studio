/* ============================================================================
 * render/shared.js —— 渲染层共享工具（方案 A，纯函数）
 * ============================================================================
 * 只放跨页重复的纯工具：字符串安全、截断、时间格式化、URL 白名单。
 * 不读取浏览器全局、不访问 DOM、不做 I/O。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./shared.js')
 *   浏览器          → RednoteRender.shared（必须先于页面渲染模块加载）
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.shared = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function text(value) {
    return value === null || value === undefined ? '' : String(value);
  }

  function escapeHtml(value) {
    return text(value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function truncate(value, max) {
    var s = text(value);
    return s.length > max ? s.slice(0, max) + '…' : s;
  }

  /** UUID 太长，列表里显示首 8 + 末 4；完整值由调用方放进 title */
  function shortId(value) {
    var s = text(value);
    if (!s) return '';
    return s.length > 16 ? s.slice(0, 8) + '…' + s.slice(-4) : s;
  }

  /** ISO 8601 → YYYY-MM-DD HH:mm；非 ISO 原样返回（由调用方转义） */
  function fmtTime(value) {
    var s = text(value);
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(s);
    return m ? m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : s;
  }

  /** 只允许 http(s) 绝对地址，挡掉 javascript: / data: / 协议相对地址 */
  function safeHttpUrl(value) {
    var s = text(value).trim();
    return /^https?:\/\//i.test(s) ? s : '';
  }

  /** 展示用短链接：去掉协议与末尾斜杠，再截断 */
  function displayUrl(value, max) {
    var s = safeHttpUrl(value);
    if (!s) return '';
    s = s.replace(/^https?:\/\//i, '').replace(/\/+$/, '');
    return truncate(s, max || 48);
  }

  function firstChar(value) {
    var s = text(value);
    return s ? Array.from(s)[0] : '';
  }

  return {
    text: text,
    escapeHtml: escapeHtml,
    truncate: truncate,
    shortId: shortId,
    fmtTime: fmtTime,
    safeHttpUrl: safeHttpUrl,
    displayUrl: displayUrl,
    firstChar: firstChar
  };
});
