/* ============================================================================
 * render/data-snapshot.js —— 指标最新视图 + 快照历史（方案 A：纯函数）
 * ============================================================================
 * 数据源：
 *   GET /metric-definitions
 *   GET /metric-observations          （mock 有 GET；草案只声明了 POST，见报告）
 *   GET /publications
 *   GET /publications/{id}/metric-latest
 *     （顶层 GET /metric-latest 在 mock 与草案中都不存在，404）
 *
 * 分组需求：最新视图是按指标扁平行；快照历史需要按 observedAt 分组，
 * 且每组内是多个指标观测 —— 与 delivery 的 contentItemId 聚合不同。
 *
 * 纯函数：输入 data、输出 HTML 字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * ========================================================================== */

(function (root, factory) {
  var shared = (typeof module === 'object' && module && module.exports)
    ? require('./shared.js')
    : (root.RednoteRender && root.RednoteRender.shared);
  if (!shared) throw new Error('render/shared.js 必须先于 render/data-snapshot.js 加载');
  var api = factory(shared);
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.dataSnapshot = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (s) {
  'use strict';

  var AVAILABILITY = {
    ACQUIRED: { label: '已获取', cls: 'fs-got' },
    NOT_PROVIDED: { label: '未提供', cls: 'fs-none' },
    NO_PERMISSION: { label: '无权限', cls: 'fs-noauth' },
    NOT_APPLICABLE: { label: '不适用', cls: 'fs-na' },
    FETCH_FAILED: { label: '获取失败', cls: 'fs-fail' },
    PENDING: { label: '待确认', cls: 'fs-todo' }
  };

  var SOURCE_TYPE = {
    MANUAL: '人工录入',
    CSV_IMPORT: 'CSV 导入',
    XLSX_IMPORT: 'XLSX 导入',
    SCREENSHOT_OCR: '截图识别',
    AUTO_API: '自动采集'
  };

  var SCOPE = {
    CUMULATIVE: '累计值',
    LAST_24H: '近 24 小时',
    LAST_7D: '近 7 天',
    LAST_30D: '近 30 天',
    LAST_90D: '近 90 天',
    NOT_APPLICABLE: '不适用',
    UNKNOWN: '未知口径'
  };

  var BUSINESS_LABEL = {
    QUALITY: { label: '优质', cls: 'tag-ok' },
    POTENTIAL: { label: '潜力', cls: 'tag-warn' },
    BELOW_THRESHOLD: { label: '未达阈值', cls: 'tag-muted' },
    PENDING: { label: '待确认', cls: 'tag-warn' }
  };

  var COLUMNS = 7;

  function isObj(v) { return v && typeof v === 'object'; }

  function indexById(rows) {
    var map = new Map();
    (Array.isArray(rows) ? rows : []).forEach(function (row) {
      if (isObj(row) && row.id) map.set(row.id, row);
    });
    return map;
  }

  function fmtNum(value) {
    var n = typeof value === 'number' ? value : Number(value);
    if (!isFinite(n)) return '';
    var parts = String(n).split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return parts.join('.');
  }

  function availabilityMeta(value) {
    var v = s.text(value);
    return AVAILABILITY[v] || null;
  }

  function availabilityTag(value) {
    var v = s.text(value);
    var meta = AVAILABILITY[v];
    if (!v) return '<span class="fs fs-none">未知</span>';
    if (!meta) return '<span class="fs fs-none">' + s.escapeHtml(s.truncate(v, 20)) + '</span>';
    return '<span class="fs ' + meta.cls + '">' + meta.label + '</span>';
  }

  function availabilityText(value) {
    var meta = availabilityMeta(value);
    return meta ? meta.label : (s.text(value) || '未知');
  }

  function sourceLabel(value) {
    var v = s.text(value);
    return SOURCE_TYPE[v] || v || '—';
  }

  function scopeLabel(value) {
    var v = s.text(value);
    return SCOPE[v] || v || '—';
  }

  function kindLabel(value) {
    return s.text(value) === 'TEXT' ? '文本' : (s.text(value) === 'NUMERIC' ? '数值' : (s.text(value) || '—'));
  }

  function businessLabelTag(value) {
    var v = s.text(value);
    var meta = BUSINESS_LABEL[v];
    if (!meta) return v ? '<span class="tag no-dot f10">' + s.escapeHtml(v) + '</span>' : '';
    return '<span class="tag ' + meta.cls + ' no-dot f10">' + meta.label + '</span>';
  }

  function timeDiff(maxAt, at) {
    var a = s.text(at);
    var m = s.text(maxAt);
    if (!a) return '—';
    if (!m || a === m) return '最新';
    var diff = Date.parse(m) - Date.parse(a);
    if (!isFinite(diff) || diff === 0) return '最新';
    var sign = diff < 0 ? '+' : '-';
    var mins = Math.round(Math.abs(diff) / 60000);
    if (mins < 60) return sign + mins + ' 分钟';
    var hours = Math.floor(mins / 60);
    var rem = mins % 60;
    if (hours < 24) return sign + hours + ' 小时' + (rem ? ' ' + rem + ' 分' : '');
    var days = Math.floor(hours / 24);
    var rest = hours % 24;
    return sign + days + ' 天' + (rest ? ' ' + rest + ' 小时' : '');
  }

  function valueCell(row) {
    var num = row.valueNum;
    var hasNum = num !== null && num !== undefined && num !== '' && isFinite(Number(num));
    var hasText = s.text(row.valueText) !== '';
    if (row.availability !== 'ACQUIRED' || (!hasNum && !hasText)) {
      return '<div class="f14 fw6 mono t-warn">—</div>' +
        '<div class="f10 t-mut mt4">' + s.escapeHtml(availabilityText(row.availability)) + '</div>';
    }
    var main = hasNum ? fmtNum(num) : s.escapeHtml(s.truncate(row.valueText, 60));
    var notes = [];
    if (row.precision === 'APPROXIMATE') notes.push('约数');
    if (row.precision === 'EXACT') notes.push('精确值');
    if (row.rawValue) notes.push('原值 "' + row.rawValue + '"');
    var sub = notes.length ? '<div class="f10 t-warn mt4">' + s.escapeHtml(notes.join(' · ')) + '</div>' : '';
    return '<div class="f14 fw6 mono">' + main + '</div>' + sub;
  }

  /**
   * 合并 definitions + latest + observations 成最新视图行。
   * latest 决定“有哪些指标”，observations 补充 availability / precision / 来源。
   */
  function buildMetricView(definitions, latest, observations) {
    var defs = indexById(definitions);
    var obs = indexById(observations);
    var order = new Map();
    (Array.isArray(definitions) ? definitions : []).forEach(function (row, index) {
      if (isObj(row) && row.id) order.set(row.id, index);
    });

    return (Array.isArray(latest) ? latest : [])
      .filter(isObj)
      .map(function (row) {
        var def = defs.get(row.metricDefinitionId) || {};
        var ob = obs.get(row.observationId) || {};
        return {
          metricDefinitionId: s.text(row.metricDefinitionId),
          label: s.text(def.label) || s.text(def.metricCode) || '未命名指标',
          metricCode: s.text(def.metricCode),
          scope: s.text(def.scope),
          valueKind: s.text(def.valueKind),
          unit: s.text(def.unit),
          valueNum: row.valueNum,
          valueText: row.valueText,
          observedAt: s.text(row.observedAt),
          latestLabel: s.text(row.label),
          rebuiltAt: s.text(row.rebuiltAt),
          availability: s.text(ob.availability),
          precision: s.text(ob.precision),
          rawValue: s.text(ob.rawValue),
          sourceType: s.text(ob.sourceType),
          evidenceRef: s.text(ob.evidenceRef),
          recordedAt: s.text(ob.recordedAt),
          hasObservation: !!ob.id
        };
      })
      .sort(function (a, b) {
        var ai = order.has(a.metricDefinitionId) ? order.get(a.metricDefinitionId) : 9999;
        var bi = order.has(b.metricDefinitionId) ? order.get(b.metricDefinitionId) : 9999;
        if (ai !== bi) return ai - bi;
        return a.metricDefinitionId < b.metricDefinitionId ? -1 : 1;
      });
  }

  function metricHead() {
    return '<thead><tr>' +
      '<th style="width:170px">指标</th>' +
      '<th style="width:160px">最新有效值</th>' +
      '<th style="width:104px">字段状态</th>' +
      '<th style="width:126px">观测时间</th>' +
      '<th style="width:96px">时间差</th>' +
      '<th style="width:110px">来源</th>' +
      '<th>口径说明</th>' +
      '</tr></thead>';
  }

  function tableShell(body) {
    return '<div class="table-wrap"><table class="tbl">' +
      metricHead() + '<tbody>' + body + '</tbody></table></div>';
  }

  function renderMetricRow(row, maxObservedAt) {
    var note = [];
    if (row.scope) note.push(scopeLabel(row.scope));
    if (row.valueKind) note.push(kindLabel(row.valueKind));
    if (row.unit) note.push('单位 ' + row.unit);
    if (row.evidenceRef) note.push('证据 ' + row.evidenceRef);
    var labelTag = businessLabelTag(row.latestLabel);

    return '<tr' + (row.availability === 'ACQUIRED' ? '' : ' class="tr-warn"') + '>' +
      '<td>' +
        '<div class="strong">' + s.escapeHtml(row.label) + '</div>' +
        '<div class="f10 t-mut mono">' + s.escapeHtml(row.metricCode || '—') + '</div>' +
        (labelTag ? '<div class="mt4">' + labelTag + '</div>' : '') +
      '</td>' +
      '<td>' + valueCell(row) + '</td>' +
      '<td>' + availabilityTag(row.availability) + '</td>' +
      '<td class="f11 mono">' +
        (row.observedAt ? s.escapeHtml(s.fmtTime(row.observedAt)) : '<span class="t-mut">—</span>') +
      '</td>' +
      '<td class="f11 ' + (timeDiff(maxObservedAt, row.observedAt) === '最新' ? 't-ok' : 't-warn') + '">' +
        s.escapeHtml(timeDiff(maxObservedAt, row.observedAt)) +
      '</td>' +
      '<td class="f11">' + s.escapeHtml(sourceLabel(row.sourceType)) + '</td>' +
      '<td class="f11 t-mut">' + s.escapeHtml(note.join(' · ') || '—') + '</td>' +
      '</tr>';
  }

  function renderMetricTable(rows, publication) {
    var list = Array.isArray(rows) ? rows.filter(isObj) : [];
    if (!list.length) return renderMetricEmpty(publication);

    var maxObservedAt = '';
    list.forEach(function (row) {
      if (row.observedAt && row.observedAt > maxObservedAt) maxObservedAt = row.observedAt;
    });

    return tableShell(list.map(function (row) {
      return renderMetricRow(row, maxObservedAt);
    }).join(''));
  }

  function renderMetricEmpty(publication) {
    var hint = publication
      ? '该发布记录暂无最新指标投影'
      : '尚无可关联的发布记录';
    return tableShell('<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:22px 0;">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">暂无指标快照</span>' +
      '<span class="f10 t-fai">' + s.escapeHtml(hint) + '</span>' +
      '</div></td></tr>');
  }

  function renderMetricLoading() {
    return tableShell('<tr><td colspan="' + COLUMNS + '">' +
      '<div class="col" style="align-items:center;gap:6px;padding:22px 0;">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">正在读取指标最新视图…</span>' +
      '</div></td></tr>');
  }

  function renderMetricError(err) {
    var e = isObj(err) ? err : {};
    var code = s.text(e.code);
    var message = s.text(e.message) || s.text(err) || '读取指标快照失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端（页面结构已保留）。确认 mock / 后端已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');

    return tableShell('<tr class="tr-danger"><td colspan="' + COLUMNS + '">' +
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

  /** 按 observedAt 精确分组，最新在前 */
  function groupObservationsByTime(observations) {
    var map = new Map();
    (Array.isArray(observations) ? observations : []).forEach(function (row) {
      if (!isObj(row)) return;
      var at = s.text(row.observedAt);
      if (!at) return;
      var group = map.get(at);
      if (!group) {
        group = { observedAt: at, observations: [] };
        map.set(at, group);
      }
      group.observations.push(row);
    });
    return Array.from(map.values()).sort(function (a, b) {
      return a.observedAt < b.observedAt ? 1 : -1;
    });
  }

  function historyRows(group, defs, order) {
    return group.observations.slice().sort(function (a, b) {
      var ai = order.has(a.metricDefinitionId) ? order.get(a.metricDefinitionId) : 9999;
      var bi = order.has(b.metricDefinitionId) ? order.get(b.metricDefinitionId) : 9999;
      return ai - bi;
    }).map(function (row) {
      var def = defs.get(row.metricDefinitionId) || {};
      var label = s.text(def.label) || s.text(def.metricCode) || '未命名指标';
      var value;
      var num = row.valueNum;
      var hasNum = num !== null && num !== undefined && num !== '' && isFinite(Number(num));
      if (row.availability === 'ACQUIRED' && hasNum) {
        value = '<span class="mono">' + fmtNum(num) + '</span> ' + availabilityTag(row.availability) +
          (row.precision === 'APPROXIMATE' ? ' <span class="f10 t-warn">约数</span>' : '');
      } else if (row.availability === 'ACQUIRED' && s.text(row.valueText)) {
        value = '<span class="mono">' + s.escapeHtml(s.truncate(row.valueText, 60)) + '</span> ' +
          availabilityTag(row.availability);
      } else {
        value = '<span class="mono t-warn">—</span> ' + availabilityTag(row.availability);
      }
      return '<tr><td style="width:130px">' + s.escapeHtml(label) + '</td><td>' + value + '</td></tr>';
    }).join('');
  }

  function renderHistoryGroup(group, defs, order, old) {
    var recorded = '';
    var sources = new Set();
    var evidence = new Set();
    group.observations.forEach(function (row) {
      if (row.recordedAt && row.recordedAt > recorded) recorded = row.recordedAt;
      if (row.sourceType) sources.add(sourceLabel(row.sourceType));
      if (row.evidenceRef) evidence.add(row.evidenceRef);
    });

    return '<div class="snap-i' + (old ? ' old' : '') + '">' +
      '<div class="row wrap mb6" style="gap:7px">' +
      '<span class="tag tag-purple no-dot f10">观测</span>' +
      '<span class="f12 fw6">' + s.escapeHtml(s.fmtTime(group.observedAt)) + '</span>' +
      '<span class="f10 t-mut">' +
        (recorded ? '录入 ' + s.escapeHtml(s.fmtTime(recorded)) + ' · ' : '') +
        s.escapeHtml(Array.from(sources).join(' / ') || '来源未标注') +
      '</span>' +
      '</div>' +
      '<div class="table-wrap"><table class="tbl tbl-compact"><tbody>' +
      historyRows(group, defs, order) +
      '</tbody></table></div>' +
      (evidence.size
        ? '<div class="f10 t-mut mt4">证据：' + s.escapeHtml(Array.from(evidence).join(' / ')) + '</div>'
        : '') +
      '</div>';
  }

  function renderSnapshotHistory(groups, definitions) {
    var list = Array.isArray(groups) ? groups.filter(isObj) : [];
    if (!list.length) return renderSnapshotHistoryEmpty();

    var defs = indexById(definitions);
    var order = new Map();
    (Array.isArray(definitions) ? definitions : []).forEach(function (row, index) {
      if (isObj(row) && row.id) order.set(row.id, index);
    });

    var latest = list[0];
    var older = list.slice(1);
    return '<div class="grid g-3-2 card-split">' +
      '<div class="cs-main">' +
        '<div class="sec-title" style="margin-top:0"><i class="fa-solid fa-camera-retro"></i>最新快照' +
        '<span class="sec-sub">观测 ' + s.escapeHtml(s.fmtTime(latest.observedAt)) + '</span></div>' +
        '<div class="snap">' + renderHistoryGroup(latest, defs, order, false) + '</div>' +
      '</div>' +
      '<div class="cs-side">' +
        '<div class="sec-title" style="margin-top:0"><i class="fa-solid fa-clock-rotate-left"></i>历史快照' +
        '<span class="sec-sub">' + older.length + ' 次写入 · 不覆盖较新数据</span></div>' +
        '<div class="snap">' +
          (older.length
            ? older.map(function (g) { return renderHistoryGroup(g, defs, order, true); }).join('')
            : '<div class="f11 t-mut">暂无更早快照</div>') +
        '</div>' +
      '</div>' +
      '</div>';
  }

  function renderSnapshotHistoryEmpty() {
    return '<div class="f11 t-mut" style="padding:12px 0;">暂无快照记录</div>';
  }

  function renderSnapshotHistoryLoading() {
    return '<div class="f11 t-mut" style="padding:12px 0;">正在读取快照历史…</div>';
  }

  function renderSnapshotHistoryError(err) {
    var e = isObj(err) ? err : {};
    var message = s.text(e.message) || s.text(err) || '读取快照历史失败。';
    return '<div class="warnrow">' +
      '<i class="fa-solid fa-triangle-exclamation"></i> ' +
      s.escapeHtml(message) +
      ' <button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div>';
  }

  return {
    buildMetricView: buildMetricView,
    renderMetricTable: renderMetricTable,
    renderMetricLoading: renderMetricLoading,
    renderMetricEmpty: renderMetricEmpty,
    renderMetricError: renderMetricError,
    groupObservationsByTime: groupObservationsByTime,
    renderSnapshotHistory: renderSnapshotHistory,
    renderSnapshotHistoryLoading: renderSnapshotHistoryLoading,
    renderSnapshotHistoryEmpty: renderSnapshotHistoryEmpty,
    renderSnapshotHistoryError: renderSnapshotHistoryError
  };
});
