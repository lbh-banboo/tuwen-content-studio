/* ============================================================================
 * render/delivery.js —— delivery 页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 因此可直接被 Node 单测（render/delivery.test.js），浏览器里只做一次 innerHTML。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./delivery.js') 得到下面这组函数
 *   浏览器          → 挂到 RednoteRender.delivery
 *
 * 数据形状（来自 GET /deliveries，api.js 解包后的数组）：
 *   { id, projectId, kocRecordId, contentItemId, versionNodeId,
 *     state: 'PREPARED' | 'DELIVERED' | 'CANCELLED', eligibilityHash,
 *     replacesDeliveryId, deliveredAt, cancelledAt }
 *
 * 分组形状（groupDeliveries 的输出，也是 renderDeliveryList 的入参）：
 *   { key, contentItemId, records: Delivery[] }
 *   同一 contentItemId 的多条交付（例如同内容分配给多个 KOC）聚合为一行。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.delivery = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var STATE_META = {
    DELIVERED: { label: '已交付', cls: 'tag-ok', order: 0 },
    PREPARED: { label: '待交付', cls: 'tag-warn', order: 1 },
    CANCELLED: { label: '已取消', cls: 'tag-danger', order: 2 }
  };

  var COLUMNS = 6;

  /* ---------------------------- 基础工具 ---------------------------- */

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

  /** UUID 太长，表格里显示首 8 + 末 4；完整值放在 title */
  function shortId(value) {
    var s = text(value);
    if (!s) return '';
    return s.length > 16 ? s.slice(0, 8) + '…' + s.slice(-4) : s;
  }

  function fmtTime(value) {
    var s = text(value);
    var m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(s);
    return m ? m[1] + '-' + m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5] : s;
  }

  function recordsOf(group) {
    if (!group || typeof group !== 'object' || !Array.isArray(group.records)) return [];
    return group.records.filter(function (row) {
      return row && typeof row === 'object';
    });
  }

  function stateSummary(records) {
    var tally = new Map();
    records.forEach(function (row) {
      var state = text(row.state) || 'UNKNOWN';
      tally.set(state, (tally.get(state) || 0) + 1);
    });

    var known = Object.keys(STATE_META)
      .filter(function (key) { return tally.has(key); })
      .sort(function (a, b) { return STATE_META[a].order - STATE_META[b].order; })
      .map(function (key) {
        return { state: key, label: STATE_META[key].label, cls: STATE_META[key].cls, count: tally.get(key) };
      });

    var unknown = Array.from(tally.keys())
      .filter(function (key) { return !STATE_META[key]; })
      .map(function (key) {
        return {
          state: key,
          label: key === 'UNKNOWN' ? '状态未标注' : truncate(key, 20),
          cls: '',
          count: tally.get(key)
        };
      });

    return known.concat(unknown);
  }

  function latestDeliveredAt(records) {
    var latest = '';
    records.forEach(function (row) {
      var at = text(row.deliveredAt);
      if (at && at > latest) latest = at; // ISO 8601 同偏移下按字面序比较即可
    });
    return latest;
  }

  function kocIds(records) {
    var seen = new Set();
    var ids = [];
    records.forEach(function (row) {
      var id = text(row.kocRecordId);
      if (id && !seen.has(id)) {
        seen.add(id);
        ids.push(id);
      }
    });
    return ids;
  }

  function rowClass(states) {
    var names = states.map(function (s) { return s.state; });
    if (names.indexOf('CANCELLED') !== -1) return ' class="tr-danger"';
    if (names.indexOf('PREPARED') !== -1) return ' class="tr-warn"';
    return '';
  }

  /* ---------------------------- 分组 ---------------------------- */

  /**
   * 按 contentItemId 聚合交付记录；保持首次出现顺序。
   * 缺 contentItemId 时退化为按单条记录成组，保证不丢行。
   */
  function groupDeliveries(rows) {
    var list = Array.isArray(rows) ? rows : [];
    var groups = new Map();

    list.forEach(function (row, index) {
      if (!row || typeof row !== 'object') return;
      var contentId = text(row.contentItemId);
      var key = contentId || 'delivery:' + (text(row.id) || 'row-' + index);
      var group = groups.get(key);
      if (!group) {
        group = { key: key, contentItemId: contentId, records: [] };
        groups.set(key, group);
      }
      group.records.push(row);
    });

    return Array.from(groups.values());
  }

  /* ---------------------------- 渲染 ---------------------------- */

  function tableHead() {
    return '<thead><tr>' +
      '<th>内容</th>' +
      '<th style="width:78px">交付记录</th>' +
      '<th style="width:112px">KOC</th>' +
      '<th style="width:150px">状态</th>' +
      '<th style="width:124px">交付时间</th>' +
      '<th class="num" style="width:74px">操作</th>' +
      '</tr></thead>';
  }

  function tableShell(body) {
    return '<div class="table-wrap"><table class="tbl tbl-compact">' +
      tableHead() + '<tbody>' + body + '</tbody></table></div>';
  }

  function renderDeliveryRow(group) {
    var records = recordsOf(group);
    var contentId = text(group && group.contentItemId) ||
      text(records[0] && records[0].contentItemId);
    var states = stateSummary(records);
    var kocs = kocIds(records);
    var deliveredAt = fmtTime(latestDeliveredAt(records));

    var idAttr = contentId ? ' title="' + escapeHtml(contentId) + '"' : '';
    var content = escapeHtml(shortId(contentId) || '（内容未标注）');

    var kocTitle = kocs.length ? ' title="' + escapeHtml(kocs.join(', ')) + '"' : '';
    var kocCell;
    if (!kocs.length) kocCell = '<span class="t-mut">—</span>';
    else if (kocs.length === 1) kocCell = escapeHtml(shortId(kocs[0]));
    else kocCell = escapeHtml(shortId(kocs[0])) +
      ' <span class="t-mut">+' + (kocs.length - 1) + '</span>';

    var stateCell = states.length
      ? states.map(function (s) {
          var cls = 'tag' + (s.cls ? ' ' + s.cls : '') + ' no-dot f10';
          return '<span class="' + cls + '">' + escapeHtml(s.label) +
            (s.count > 1 ? ' ' + s.count : '') + '</span>';
        }).join(' ')
      : '<span class="t-mut">—</span>';

    return '<tr' + rowClass(states) + '>' +
      '<td class="f11 mono"' + idAttr + '>' + content + '</td>' +
      '<td class="num mono">' + records.length + '</td>' +
      '<td class="f11 mono"' + kocTitle + '>' + kocCell + '</td>' +
      '<td>' + stateCell + '</td>' +
      '<td class="f11 mono">' +
        (deliveredAt ? escapeHtml(deliveredAt) : '<span class="t-mut">—</span>') +
      '</td>' +
      '<td class="num">' +
        '<a class="btn btn-sm btn-ghost" href="delivery-koc.html" title="查看 KOC 交付记录">' +
        '<i class="fa-solid fa-arrow-right"></i></a>' +
      '</td>' +
      '</tr>';
  }

  function renderDeliveryList(groups) {
    var list = Array.isArray(groups)
      ? groups.filter(function (g) { return g && typeof g === 'object'; })
      : [];
    return tableShell(list.length
      ? list.map(renderDeliveryRow).join('')
      : renderDeliveryEmpty());
  }

  function renderDeliveryEmpty() {
    return '<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:22px 0;">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">暂无交付记录</span>' +
      '<span class="f10 t-fai">项目产生交付后会自动出现在这里</span>' +
      '</div></td></tr>';
  }

  function renderDeliveryLoading() {
    return tableShell('<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:22px 0;">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在读取交付记录…</span>' +
      '</div></td></tr>');
  }

  /**
   * @param {object|string} err ApiError 实例或 {code,message,retryable,offline}
   *   只读取普通字段，不依赖 ApiError 类（保持本模块零依赖）。
   */
  function renderDeliveryError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取交付记录失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端（页面结构已保留）。确认 mock / 后端已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');
    var meta = (code ? code + ' · ' : '') + hint;

    return tableShell('<tr class="tr-danger"><td colspan="' + COLUMNS + '">' +
      '<div class="row" style="align-items:flex-start;gap:10px;padding:14px 0;">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px;">' +
      '<span class="f12 fw6">' + escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml(meta) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div></td></tr>');
  }

  return {
    groupDeliveries: groupDeliveries,
    renderDeliveryList: renderDeliveryList,
    renderDeliveryRow: renderDeliveryRow,
    renderDeliveryLoading: renderDeliveryLoading,
    renderDeliveryEmpty: renderDeliveryEmpty,
    renderDeliveryError: renderDeliveryError,
    escapeHtml: escapeHtml
  };
});
