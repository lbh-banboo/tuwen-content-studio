/* ============================================================================
 * render/workbench.js —— 工作台渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./workbench.js')
 *   浏览器          → RednoteRender.workbench
 *
 * 数据形状（api.js 解包后）：
 *   GET /work-items    → [{ workItemId, projectId, assigneeId, kind, subjectType,
 *                          subjectId, state, dueAt, createdAt }]
 *   GET /projects      → [{ id, projectCode, name, phase, dueDate, targetDeliveryCount, ... }]
 *   GET /audit-events  → [{ auditId, actorKind, actorId, action, objectType,
 *                          objectId, projectId, changed, reason, createdAt }]
 *
 * 说明：GET /events 是 SSE 事件流（无历史列表），近期动态改由 /audit-events 读取；
 * 本模块不做任何姓名/标题推断，缺字段标「未提供」。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.workbench = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var KIND_META = {
    FACT_CORRECTION: { label: '事实修正', cls: 'danger', icon: 'fa-shield-halved' },
    TASK_FAILED: { label: '任务失败', cls: 'danger', icon: 'fa-triangle-exclamation' },
    DATA_COLLECTION_BLOCKED: { label: '数据采集受阻', cls: 'danger', icon: 'fa-link-slash' },
    DUE_SOON: { label: '临近截止', cls: 'warn', icon: 'fa-clock-rotate-left' },
    MANUAL_CONFIRM: { label: '人工确认', cls: 'warn', icon: 'fa-hand' },
    TASK_COMPLETED: { label: '任务完成确认', cls: 'info', icon: 'fa-circle-check' }
  };

  var STATE_META = {
    OPEN: { label: '待处理', cls: 'tag-warn' },
    DONE: { label: '已完成', cls: 'tag-ok' }
  };

  var ACTION_LABELS = {
    'project.created': '项目创建',
    'project.member.added': '新增项目成员',
    'parse.scheduled': '资料解析已排期',
    'file.linked': '资料文件已关联'
  };

  var PHASE_META = {
    PREPARATION: { label: '准备期', cls: 'tag-purple' },
    PRODUCTION: { label: '投产期', cls: 'tag-info' },
    DATA_COLLECTION: { label: '数据回收期', cls: 'tag-ok' },
    CLOSED: { label: '已结束', cls: 'tag-muted' }
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

  function listOf(rows) {
    return Array.isArray(rows)
      ? rows.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function phaseMeta(phase) {
    return PHASE_META[phase] || { label: text(phase) || '未提供', cls: '' };
  }

  function kindMeta(kind) {
    return KIND_META[kind] || { label: '其他待办', cls: '', icon: 'fa-circle-dot' };
  }

  function projectIndex(projects) {
    var index = {};
    listOf(projects).forEach(function (p) {
      if (text(p.id)) index[text(p.id)] = p;
    });
    return index;
  }

  function projectName(projectId, byId) {
    var p = byId && byId[text(projectId)];
    return p && text(p.name) ? text(p.name) : '';
  }

  function projectLabel(projectId, byId) {
    var name = projectName(projectId, byId);
    if (name) return escapeHtml(name);
    var id = text(projectId);
    return id
      ? '<span class="mono f10" title="' + escapeHtml(id) + '">' + escapeHtml(shortId(id)) + '</span>'
      : '<span class="t-mut">项目未提供</span>';
  }

  function projectLink(projectId) {
    if (!text(projectId)) return '';
    return '<a class="btn btn-sm" href="project-overview.v2.html?id=' +
      encodeURIComponent(text(projectId)) + '">查看</a>';
  }

  /* ---------------------------- 指标 ---------------------------- */

  function renderMetrics(data) {
    var d = data && typeof data === 'object' ? data : {};
    var items = Array.isArray(d.workItems) ? d.workItems : null;
    var projects = Array.isArray(d.projects) ? d.projects : null;
    var events = Array.isArray(d.events) ? d.events : null;

    var open = items ? items.filter(function (i) { return i && i.state === 'OPEN'; }).length : null;
    var targetSum = projects ? projects.reduce(function (sum, p) {
      var n = Number(p && p.targetDeliveryCount);
      return sum + (isFinite(n) ? n : 0);
    }, 0) : null;
    var missingTarget = projects ? projects.filter(function (p) {
      var n = Number(p && p.targetDeliveryCount);
      return !isFinite(n);
    }).length : 0;

    var cards = [
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-list-check"></i>待办事项</div>' +
        '<div class="metric-value' + (open ? ' t-warn' : '') + '">' +
          (items ? escapeHtml(open) : '<span class="na">—</span>') + '</div>' +
        '<div class="metric-foot">' +
          (items ? 'OPEN ' + open + ' · 共 ' + items.length + ' 项（GET /work-items）' : '读取失败') +
        '</div></div>',
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-folder-open"></i>可见项目</div>' +
        '<div class="metric-value">' + (projects ? escapeHtml(projects.length) : '<span class="na">—</span>') + '</div>' +
        '<div class="metric-foot">' +
          (projects
            ? '计划交付合计 ' + targetSum + ' 条' +
              (missingTarget ? '（' + missingTarget + ' 项缺该字段）' : '') + '（GET /projects）'
            : '读取失败') +
        '</div></div>',
      '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid fa-clock-rotate-left"></i>近期动态</div>' +
        '<div class="metric-value">' + (events ? escapeHtml(events.length) : '<span class="na">—</span>') + '</div>' +
        '<div class="metric-foot">' +
          (events ? '最近 8 条（GET /audit-events）' : '读取失败') +
        '</div></div>'
    ];
    return cards.join('');
  }

  /* ---------------------------- 待办 ---------------------------- */

  function renderWorkItem(item, byId) {
    var i = item && typeof item === 'object' ? item : {};
    var kind = kindMeta(i.kind);
    var state = STATE_META[i.state] || { label: text(i.state) || '状态未提供', cls: 'tag-muted' };
    var title = kind.label + (KIND_META[i.kind] ? '' : '（' + text(i.kind || '未提供') + '）');
    var sub = [
      projectLabel(i.projectId, byId),
      '截止 ' + (text(i.dueAt) ? escapeHtml(fmtTime(i.dueAt)) : '未提供'),
      text(i.assigneeId)
        ? '指派 <span class="mono" title="' + escapeHtml(i.assigneeId) + '">' + escapeHtml(shortId(i.assigneeId)) + '</span>'
        : '未指派'
    ].join(' · ');

    return '<div class="list-item">' +
      '<span class="li-icon ' + kind.cls + '"><i class="fa-solid ' + kind.icon + '"></i></span>' +
      '<div class="li-main">' +
        '<div class="li-title">' + escapeHtml(title) + '</div>' +
        '<div class="li-sub">' + sub + '</div>' +
      '</div>' +
      '<div class="li-right">' +
        '<span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span>' +
        projectLink(i.projectId) +
      '</div></div>';
  }

  function renderWorkItems(items, byId) {
    var list = listOf(items);
    if (!list.length) {
      return renderBlockEmpty('暂无待办事项', '需要人工处理的事项会出现在这里');
    }
    return list.map(function (item) { return renderWorkItem(item, byId); }).join('');
  }

  /* ---------------------------- 项目 ---------------------------- */

  function renderProjectRow(project) {
    var p = project && typeof project === 'object' ? project : {};
    var phase = phaseMeta(p.phase);
    return '<tr>' +
      '<td class="strong">' + (text(p.name) ? escapeHtml(p.name) : '<span class="t-mut">名称未提供</span>') +
        (text(p.projectCode) ? '<div class="f10 t-fai mono">' + escapeHtml(p.projectCode) + '</div>' : '') + '</td>' +
      '<td><span class="tag ' + phase.cls + ' no-dot f10">' + escapeHtml(phase.label) + '</span></td>' +
      '<td class="num">' + (p.targetDeliveryCount == null ? '<span class="t-mut">—</span>' : escapeHtml(p.targetDeliveryCount)) + '</td>' +
      '<td class="f11 mono">' + (text(p.dueDate) ? escapeHtml(p.dueDate) : '<span class="t-mut">未提供</span>') + '</td>' +
      '<td class="num"><div class="row-act">' +
        (text(p.id)
          ? '<a class="btn btn-sm" href="project-overview.v2.html?id=' + encodeURIComponent(text(p.id)) + '">进入</a>'
          : '<span class="btn btn-sm is-disabled"><i class="fa-solid fa-lock"></i></span>') +
      '</div></td></tr>';
  }

  function renderProjectsTable(projects) {
    var list = listOf(projects);
    var body = list.length
      ? list.map(renderProjectRow).join('')
      : '<tr><td colspan="5"><div class="col" style="align-items:center;gap:6px;padding:20px 0">' +
        '<i class="fa-solid fa-folder-open t-fai"></i>' +
        '<span class="f12 t-mut">暂无可见项目</span></div></td></tr>';

    return '<div class="table-wrap"><table class="tbl tbl-compact">' +
      '<thead><tr><th>项目 / 编号</th><th style="width:120px">阶段</th>' +
      '<th class="num" style="width:88px">计划交付</th><th style="width:116px">截止</th>' +
      '<th class="num" style="width:76px">操作</th></tr></thead>' +
      '<tbody>' + body + '</tbody></table></div>';
  }

  /* ---------------------------- 动态 ---------------------------- */

  function activityActor(event) {
    var kind = text(event.actorKind);
    var label = kind === 'USER' ? '成员' : kind === 'SYSTEM' ? '系统' : (kind || '操作者未提供');
    var id = text(event.actorId);
    return escapeHtml(label) + (id
      ? ' <span class="mono f10" title="' + escapeHtml(id) + '">' + escapeHtml(shortId(id)) + '</span>'
      : '');
  }

  function renderActivityItem(event, byId) {
    var e = event && typeof event === 'object' ? event : {};
    var action = ACTION_LABELS[e.action] || text(e.action) || '动作未提供';
    var desc = [
      projectLabel(e.projectId, byId),
      activityActor(e)
    ];
    if (text(e.reason)) desc.push(escapeHtml(e.reason));
    var cls = e.actorKind === 'USER' ? 'purple' : (e.actorKind === 'SYSTEM' ? '' : 'warn');

    return '<div class="tl-item ' + cls + '">' +
      '<div class="tl-time">' + (text(e.createdAt) ? escapeHtml(fmtTime(e.createdAt)) : '时间未提供') + '</div>' +
      '<div class="tl-title">' + escapeHtml(action) + '</div>' +
      '<div class="tl-desc">' + desc.join(' · ') + '</div>' +
      '</div>';
  }

  function renderActivity(events, byId) {
    var list = listOf(events);
    if (!list.length) {
      return renderBlockEmpty('暂无近期动态', 'GET /audit-events 未返回记录');
    }
    return '<div class="timeline">' +
      list.map(function (event) { return renderActivityItem(event, byId); }).join('') +
      '</div>';
  }

  /* ---------------------------- 通用块 ---------------------------- */

  function renderBlockLoading(message) {
    return '<div class="row" style="justify-content:center;gap:8px;padding:22px 0">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(message || '正在读取…') + '</span>' +
      '</div>';
  }

  function renderBlockEmpty(title, sub) {
    return '<div class="col" style="align-items:center;gap:6px;padding:22px 0">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(title || '暂无数据') + '</span>' +
      (sub ? '<span class="f10 t-fai">' + escapeHtml(sub) + '</span>' : '') +
      '</div>';
  }

  function renderBlockError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端。确认 127.0.0.1:8787 已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');

    return '<div class="row" style="align-items:flex-start;gap:10px;padding:12px 0">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px">' +
      '<span class="f12 fw6">' + escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml((code ? code + ' · ' : '') + hint) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div>';
  }

  return {
    KIND_META: KIND_META,
    projectIndex: projectIndex,
    renderMetrics: renderMetrics,
    renderWorkItems: renderWorkItems,
    renderWorkItem: renderWorkItem,
    renderProjectsTable: renderProjectsTable,
    renderActivity: renderActivity,
    renderActivityItem: renderActivityItem,
    renderBlockLoading: renderBlockLoading,
    renderBlockEmpty: renderBlockEmpty,
    renderBlockError: renderBlockError,
    escapeHtml: escapeHtml
  };
});
