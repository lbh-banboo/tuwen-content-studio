/* ============================================================================
 * render/report.js —— 报告版本历史 + 结项 / 重开事件（方案 A：纯函数）
 * ============================================================================
 * 数据源：
 *   GET /reports
 *   GET /reports/{id}
 *   GET /reports/{id}/versions    （/reports/{id} 本身不含版本，必须再取这个）
 *   GET /project-closures
 *
 * 形状：两组都是扁平数组，无分组需求。
 *   ReportVersion.metricsSnapshot 是任意对象，按 key=value 原样展示，不现算。
 *   ProjectClosure 一条记录可展开为「结项」与「重开」两个时间线事件。
 *
 * 纯函数：输入 data、输出 HTML 字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * ========================================================================== */

(function (root, factory) {
  var shared = (typeof module === 'object' && module && module.exports)
    ? require('./shared.js')
    : (root.RednoteRender && root.RednoteRender.shared);
  if (!shared) throw new Error('render/shared.js 必须先于 render/report.js 加载');
  var api = factory(shared);
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.report = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (s) {
  'use strict';

  var KIND = {
    OVERVIEW: '项目概览',
    STAGE: '阶段报告',
    CLOSURE: '结项报告'
  };

  var CLOSURE_TYPE = {
    COMPLETED: '完整结项',
    TERMINATED: '提前终止'
  };

  function isObj(v) { return v && typeof v === 'object'; }

  function kindLabel(value) {
    var v = s.text(value);
    return KIND[v] || v || '报告';
  }

  function describeObject(value) {
    if (!isObj(value)) return s.text(value) || '—';
    var keys = Object.keys(value).sort();
    if (!keys.length) return '无固化指标';
    var parts = keys.map(function (key) {
      var item = value[key];
      var val = isObj(item) ? JSON.stringify(item) : s.text(item);
      return key + '=' + (val === '' ? '—' : val);
    });
    return s.truncate(parts.join(' · '), 160);
  }

  function renderReportVersions(report, versions) {
    var list = Array.isArray(versions) ? versions.filter(isObj) : [];
    var head = isObj(report)
      ? '<div class="f11 t-mut mb10">报告 <span class="mono">' +
        s.escapeHtml(s.shortId(report.id)) + '</span> · 当前 v' +
        s.escapeHtml(s.text(report.currentVersionNo) || '—') + ' · 模板 ' +
        s.escapeHtml(s.text(report.templateVersion) || '—') + '</div>'
      : '';

    if (!list.length) return head + renderReportVersionsEmpty();

    list = list.slice().sort(function (a, b) {
      return (Number(b.versionNo) || 0) - (Number(a.versionNo) || 0);
    });

    return head + list.map(function (version) {
      var metrics = describeObject(version.metricsSnapshot);
      return '<div class="rv">' +
        '<div class="rv-n">v' + s.escapeHtml(s.text(version.versionNo) || '—') + '</div>' +
        '<div class="f1">' +
          '<div class="row between mb4">' +
            '<span class="f12 fw6">' +
              s.escapeHtml(kindLabel(report && report.reportKind)) + ' · 已生成</span>' +
            '<span class="f10 t-fai mono">' +
              s.escapeHtml(version.generatedAt ? s.fmtTime(version.generatedAt) : '—') + '</span>' +
          '</div>' +
          '<div class="f11 t-mut">截止 ' +
            s.escapeHtml(version.cutoffAt ? s.fmtTime(version.cutoffAt) : '—') +
            ' · 模板 ' + s.escapeHtml(s.text(report && report.templateVersion) || '—') +
            ' · 固化快照 ' + s.escapeHtml(metrics) +
            ' · hash ' + s.escapeHtml(s.truncate(s.text(version.contentHash) || '—', 24)) +
          '</div>' +
        '</div>' +
        '</div>';
    }).join('');
  }

  function renderReportVersionsEmpty() {
    return '<div class="f11 t-mut" style="padding:12px 0;">暂无报告版本</div>';
  }

  function renderReportVersionsLoading() {
    return '<div class="f11 t-mut" style="padding:12px 0;">正在读取报告版本…</div>';
  }

  function renderReportVersionsError(err) {
    var e = isObj(err) ? err : {};
    var message = s.text(e.message) || s.text(err) || '读取报告版本失败。';
    return '<div class="warnrow">' +
      '<i class="fa-solid fa-triangle-exclamation"></i> ' + s.escapeHtml(message) +
      ' <button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button></div>';
  }

  /** 一条结项记录可展开为「结项」+「重开」两个事件；按时间倒序 */
  function buildClosureEvents(closures) {
    var events = [];
    (Array.isArray(closures) ? closures : []).forEach(function (row) {
      if (!isObj(row)) return;
      var type = s.text(row.closureType);
      events.push({
        time: s.text(row.closedAt),
        title: '结项 · ' + (CLOSURE_TYPE[type] || type || '未知类型'),
        desc: '项目 ' + s.shortId(row.projectId) +
          ' · 未完成快照 ' + describeObject(row.outstandingSnapshot)
      });
      if (s.text(row.reopenedAt)) {
        events.push({
          time: s.text(row.reopenedAt),
          title: '项目重开',
          desc: '原因：' + (s.text(row.reopenReason) || '未填写') +
            ' · 原结项 ' + s.fmtTime(row.closedAt) + ' 保留'
        });
      }
    });
    return events.filter(function (e) { return e.time; }).sort(function (a, b) {
      return a.time < b.time ? 1 : -1;
    });
  }

  function renderClosureTimeline(closures) {
    var events = buildClosureEvents(closures);
    if (!events.length) return renderClosureEmpty();
    return '<div class="timeline">' + events.map(function (event) {
      return '<div class="tl-item">' +
        '<div class="tl-time mono">' + s.escapeHtml(s.fmtTime(event.time)) + '</div>' +
        '<div class="f1">' +
          '<div class="tl-title">' + s.escapeHtml(event.title) + '</div>' +
          '<div class="tl-desc">' + s.escapeHtml(event.desc) + '</div>' +
        '</div>' +
        '</div>';
    }).join('') + '</div>';
  }

  function renderClosureEmpty() {
    return '<div class="f11 t-mut" style="padding:12px 0;">暂无结项 / 重开记录</div>';
  }

  function renderClosureLoading() {
    return '<div class="f11 t-mut" style="padding:12px 0;">正在读取结项记录…</div>';
  }

  function renderClosureError(err) {
    var e = isObj(err) ? err : {};
    var message = s.text(e.message) || s.text(err) || '读取结项记录失败。';
    return '<div class="warnrow">' +
      '<i class="fa-solid fa-triangle-exclamation"></i> ' + s.escapeHtml(message) +
      ' <button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button></div>';
  }

  function summarizeClosures(closures) {
    var list = Array.isArray(closures) ? closures.filter(isObj) : [];
    return {
      total: list.length,
      reopened: list.filter(function (row) { return !!s.text(row.reopenedAt); }).length
    };
  }

  return {
    renderReportVersions: renderReportVersions,
    renderReportVersionsLoading: renderReportVersionsLoading,
    renderReportVersionsEmpty: renderReportVersionsEmpty,
    renderReportVersionsError: renderReportVersionsError,
    buildClosureEvents: buildClosureEvents,
    renderClosureTimeline: renderClosureTimeline,
    renderClosureLoading: renderClosureLoading,
    renderClosureEmpty: renderClosureEmpty,
    renderClosureError: renderClosureError,
    summarizeClosures: summarizeClosures
  };
});
