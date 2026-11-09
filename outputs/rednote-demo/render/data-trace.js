/* ============================================================================
 * render/data-trace.js —— 发布登记核对 + 四段留痕降级（方案 A：纯函数）
 * ============================================================================
 * 数据源：GET /publications（扁平数组，无分组）
 *   { id, projectId, deliveryId, publicationKey, noteId, noteUrl, urlCanonical,
 *     state: DRAFT | PUBLISHED | UNKNOWN,
 *     contentState: VERIFIED | UNVERIFIED,
 *     publishedAt }
 *
 * 四段留痕（机器稿 → 人工稿 → 交付稿 → 发布稿）在 mock 中没有数据源：
 *   · GET /audit-events 在正式契约存在，但本 mock 返回 404；
 *   · 草案 m4-openapi 没有该端点；
 *   · GET /events 是 SSE 流，不是列表，不能当数据源。
 * 因此该区块渲染明确的「无数据源」提示，不展示示例数据。
 *
 * 纯函数：输入 data、输出 HTML 字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * ========================================================================== */

(function (root, factory) {
  var shared = (typeof module === 'object' && module && module.exports)
    ? require('./shared.js')
    : (root.RednoteRender && root.RednoteRender.shared);
  if (!shared) throw new Error('render/shared.js 必须先于 render/data-trace.js 加载');
  var api = factory(shared);
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.dataTrace = api;
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

  function isObj(v) { return v && typeof v === 'object'; }

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

  function renderPublicationCheckRow(record) {
    var row = isObj(record) ? record : {};
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

  function checkHead() {
    return '<thead><tr>' +
      '<th style="width:132px">发布记录</th>' +
      '<th style="width:124px">交付记录</th>' +
      '<th>笔记链接 / 标识</th>' +
      '<th style="width:124px">发布时间</th>' +
      '<th style="width:110px">发布状态</th>' +
      '<th style="width:126px">内容核对</th>' +
      '</tr></thead>';
  }

  function checkShell(body) {
    return '<div class="table-wrap"><table class="tbl tbl-compact" style="min-width:520px">' +
      checkHead() + '<tbody>' + body + '</tbody></table></div>';
  }

  function renderPublicationCheckList(rows) {
    var list = Array.isArray(rows) ? rows.filter(isObj) : [];
    return checkShell(list.length
      ? list.map(renderPublicationCheckRow).join('')
      : renderPublicationCheckEmpty());
  }

  function renderPublicationCheckEmpty() {
    return '<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:22px 0;">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">暂无发布登记</span>' +
      '<span class="f10 t-fai">登记发布后会出现在这里</span>' +
      '</div></td></tr>';
  }

  function renderPublicationCheckLoading() {
    return checkShell('<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:22px 0;">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在读取发布登记…</span>' +
      '</div></td></tr>');
  }

  function renderPublicationCheckError(err) {
    var e = isObj(err) ? err : {};
    var code = s.text(e.code);
    var message = s.text(e.message) || s.text(err) || '读取发布登记失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端（页面结构已保留）。确认 mock / 后端已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');

    return checkShell('<tr class="tr-danger"><td colspan="' + COLUMNS + '">' +
      '<div class="row" style="align-items:flex-start;gap:10px;padding:14px 0;">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px;">' +
      '<span class="f12 fw6">' + s.escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + s.escapeHtml((code ? code + ' · ' : '') + hint) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div></td></tr>');
  }

  /**
   * 发布身份核对汇总：绑定 = deliveryId + noteUrl + publishedAt 三项齐全。
   * 全部由 GET /publications 真实字段推导，不展示示例数字。
   */
  function renderIdentitySummary(rows) {
    var list = Array.isArray(rows) ? rows.filter(isObj) : [];
    var bound = 0;
    var unverified = 0;
    list.forEach(function (row) {
      var hasIdentity = !!s.text(row.deliveryId) && !!s.safeHttpUrl(row.noteUrl) && !!s.text(row.publishedAt);
      if (hasIdentity) bound += 1;
      if (s.text(row.contentState) === 'UNVERIFIED') unverified += 1;
    });
    var missing = list.length - bound;

    function item(cls, icon, title, sub) {
      return '<div class="list-item">' +
        '<span class="li-icon ' + cls + '"><i class="fa-solid ' + icon + '"></i></span>' +
        '<div class="li-main">' +
        '<div class="li-title">' + title + '</div>' +
        '<div class="li-sub">' + sub + '</div>' +
        '</div>' +
        '</div>';
    }

    return '<div class="sec-title" style="margin-top:0"><i class="fa-solid fa-id-badge"></i>发布身份核对</div>' +
      '<div class="col" style="gap:9px">' +
      item('ok', 'fa-check',
        bound + ' / ' + list.length + ' 已绑定真实发布版本',
        '交付记录 + 笔记链接 + 发布时间三项齐全，进入窗口统计') +
      item(missing ? 'warn' : 'ok', missing ? 'fa-exclamation' : 'fa-check',
        missing + ' 条待补发布身份',
        '不进入窗口统计，也不以 0 计入 —— 缺失值不得填 0') +
      item(unverified ? 'danger' : 'ok', unverified ? 'fa-ban' : 'fa-check',
        unverified + ' 条发布稿未核对',
        '保留在发布记录中，但排除出主张类效果分析') +
      '</div>' +
      '<div class="sec-title"><i class="fa-solid fa-code-branch"></i>变动来源</div>' +
      '<div class="note note-info f11">' +
      '<i class="fa-solid fa-circle-info"></i>' +
      '<div><b>变动来源无数据源。</b>需要 GET /audit-events（正式契约存在、本 mock 404），' +
      '草案 m4-openapi 也未定义该端点，因此不展示示例计数。</div>' +
      '</div>';
  }

  /**
   * 四段留痕区块：无数据源，明确降级，不展示示例数据。
   */
  function renderTraceUnavailable(detail) {
    var reason = s.text(detail) ||
      '需要 GET /audit-events（正式契约存在、本 mock 404）；草案 m4-openapi 未定义该端点。';
    return '<div class="card-body">' +
      '<div class="note note-warn f11">' +
      '<i class="fa-solid fa-triangle-exclamation"></i>' +
      '<div>' +
      '<b>该区块无数据源。</b>四段留痕（机器稿 → 人工稿 → 交付稿 → 发布稿）' +
      '当前没有可用的 mock 端点。' + s.escapeHtml(reason) +
      ' 本页面只保留区块结构，不展示示例数据；接入后端后按 /audit-events 填充。' +
      '</div>' +
      '</div>' +
      '</div>';
  }

  return {
    renderPublicationCheckList: renderPublicationCheckList,
    renderPublicationCheckRow: renderPublicationCheckRow,
    renderPublicationCheckLoading: renderPublicationCheckLoading,
    renderPublicationCheckEmpty: renderPublicationCheckEmpty,
    renderPublicationCheckError: renderPublicationCheckError,
    renderIdentitySummary: renderIdentitySummary,
    renderTraceUnavailable: renderTraceUnavailable
  };
});
