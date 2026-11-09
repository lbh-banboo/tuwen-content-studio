/* ============================================================================
 * render/data-feedback.js —— 发布登记渲染层（方案 A：纯函数）
 * ============================================================================
 * 数据源：GET /publications（扁平数组，无分组）
 *   { id, projectId, deliveryId, publicationKey, noteId, noteUrl, urlCanonical,
 *     state: DRAFT | PUBLISHED | UNKNOWN,
 *     contentState: VERIFIED | UNVERIFIED,
 *     publishedAt }
 *
 * 与 delivery.js 的差异：
 *   · 扁平列表，不需要 groupXxx；
 *   · 两个枚举（state / contentState）决定 tag 与行色，但没有聚合计数；
 *   · 需要 URL 白名单与发布时间格式化（走 shared）。
 *
 * 原页面静态表里的「KOC / 版本 / 是否改写 / 投流信息」在 Publication 契约中
 * 没有对应字段（KOC 与版本需再关联 delivery），因此不伪造，改为展示真实字段。
 *
 * 纯函数：输入数据、输出 HTML 字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * ========================================================================== */

(function (root, factory) {
  var shared = (typeof module === 'object' && module && module.exports)
    ? require('./shared.js')
    : (root.RednoteRender && root.RednoteRender.shared);
  if (!shared) throw new Error('render/shared.js 必须先于 render/data-feedback.js 加载');
  var api = factory(shared);
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.dataFeedback = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (s) {
  'use strict';

  var STATE_META = {
    PUBLISHED: { label: '已发布', cls: 'tag-ok' },
    DRAFT: { label: '待发布', cls: 'tag-muted' },
    UNKNOWN: { label: '待确认', cls: 'tag-warn' }
  };

  var CONTENT_META = {
    VERIFIED: { label: '已核对', cls: 'tag-ok' },
    UNVERIFIED: { label: '未核对', cls: 'tag-warn' }
  };

  var COLUMNS = 6;

  function tagOf(meta, raw, fallback) {
    var value = s.text(raw);
    if (!value) return '<span class="t-mut">' + fallback + '</span>';
    var item = meta[value];
    if (!item) {
      return '<span class="tag no-dot f10">' + s.escapeHtml(s.truncate(value, 20)) + '</span>';
    }
    return '<span class="tag ' + item.cls + ' no-dot f10">' + item.label + '</span>';
  }

  function rowClass(state, contentState) {
    if (contentState === 'UNVERIFIED' || state === 'UNKNOWN') return ' class="tr-warn"';
    if (state === 'DRAFT') return ' class="tr-off"';
    return '';
  }

  function renderPublicationRow(record) {
    var row = record && typeof record === 'object' ? record : {};
    var key = s.text(row.publicationKey);
    var deliveryId = s.text(row.deliveryId);
    var noteId = s.text(row.noteId);
    var canonical = s.safeHttpUrl(row.urlCanonical);
    var raw = s.safeHttpUrl(row.noteUrl);
    var href = canonical || raw;
    var publishedAt = s.fmtTime(row.publishedAt);
    var state = s.text(row.state);
    var contentState = s.text(row.contentState);

    var keyCell = key
      ? '<span class="code code-purple" title="' + s.escapeHtml(key) + '">' +
        s.escapeHtml(s.truncate(key, 26)) + '</span>'
      : '<span class="t-mut">—</span>';

    var deliveryCell = deliveryId
      ? '<span class="code f10" title="' + s.escapeHtml(deliveryId) + '">' +
        s.escapeHtml(s.shortId(deliveryId)) + '</span>'
      : '<span class="t-mut">—</span>';

    var linkCell;
    if (href) {
      var sub = noteId
        ? '标识 <span class="mono">' + s.escapeHtml(s.truncate(noteId, 32)) + '</span>'
        : '已登记链接';
      linkCell = '<div><a class="mono" href="' + s.escapeHtml(href) +
        '" target="_blank" rel="noopener noreferrer">' +
        s.escapeHtml(s.displayUrl(href, 48)) + '</a></div>' +
        '<div class="f10 t-mut mt4">' + sub + '</div>';
    } else {
      linkCell = '<div class="mono t-mut">未登记链接</div>' +
        (noteId ? '<div class="f10 t-mut mt4">标识 ' +
          s.escapeHtml(s.truncate(noteId, 32)) + '</div>' : '');
    }

    return '<tr' + rowClass(state, contentState) + '>' +
      '<td>' + keyCell + '</td>' +
      '<td>' + deliveryCell + '</td>' +
      '<td class="f11">' + linkCell + '</td>' +
      '<td class="f11 mono">' +
        (publishedAt ? s.escapeHtml(publishedAt) : '<span class="t-mut">—</span>') +
      '</td>' +
      '<td>' + tagOf(STATE_META, state, '—') + '</td>' +
      '<td>' + tagOf(CONTENT_META, contentState, '—') + '</td>' +
      '</tr>';
  }

  function tableHead() {
    return '<thead><tr>' +
      '<th style="width:132px">发布记录</th>' +
      '<th style="width:124px">交付记录</th>' +
      '<th>笔记链接 / 标识</th>' +
      '<th style="width:124px">发布时间</th>' +
      '<th style="width:110px">发布状态</th>' +
      '<th style="width:126px">内容核对</th>' +
      '</tr></thead>';
  }

  function tableShell(body) {
    return '<div class="table-wrap"><table class="tbl">' +
      tableHead() + '<tbody>' + body + '</tbody></table></div>';
  }

  function renderPublicationList(rows) {
    var list = Array.isArray(rows)
      ? rows.filter(function (r) { return r && typeof r === 'object'; })
      : [];
    return tableShell(list.length
      ? list.map(renderPublicationRow).join('')
      : renderPublicationEmpty());
  }

  function renderPublicationEmpty() {
    return '<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:22px 0;">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">暂无发布登记</span>' +
      '<span class="f10 t-fai">登记发布后会出现在这里</span>' +
      '</div></td></tr>';
  }

  function renderPublicationLoading() {
    return tableShell('<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:22px 0;">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在读取发布登记…</span>' +
      '</div></td></tr>');
  }

  /**
   * @param {object|string} err ApiError 实例或 {code,message,retryable,offline}
   *   只读普通字段，不依赖 ApiError 类。
   */
  function renderPublicationError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = s.text(e.code);
    var message = s.text(e.message) || s.text(err) || '读取发布登记失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端（页面结构已保留）。确认 mock / 后端已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');
    var meta = (code ? code + ' · ' : '') + hint;

    return tableShell('<tr class="tr-danger"><td colspan="' + COLUMNS + '">' +
      '<div class="row" style="align-items:flex-start;gap:10px;padding:14px 0;">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px;">' +
      '<span class="f12 fw6">' + s.escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + s.escapeHtml(meta) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div></td></tr>');
  }

  return {
    renderPublicationList: renderPublicationList,
    renderPublicationRow: renderPublicationRow,
    renderPublicationLoading: renderPublicationLoading,
    renderPublicationEmpty: renderPublicationEmpty,
    renderPublicationError: renderPublicationError
  };
});
