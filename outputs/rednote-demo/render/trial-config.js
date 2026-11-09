/* ============================================================================
 * render/trial-config.js —— 试产配置页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读取浏览器全局、不访问 DOM、不做 I/O。
 * 双环境导出：Node/CommonJS → require('./trial-config.js')；浏览器 → RednoteRender.trialConfig
 *
 * 数据形状（api.js 解包后，均实测自 127.0.0.1:8787）：
 *   GET /projects/{id}/plan-draft → { draftId, projectId, baselineId, state,
 *     totalQuantity, sceneItems[], formItems[], pairings[], schemaVersion,
 *     lockVersion, etag,
 *     sceneAllocation{ total, lines[], allocatedTotal, unallocated, rounds, notes[] },
 *     formAllocation{ 同左 } }
 *   GET /projects/{id}/reference-assets → { data:[{ assetId, projectId,
 *     fileObjectId, state, productModel, productColor, productPart, source,
 *     note, supersedesAssetId, createdAt, createdBy }], page }
 *   GET /pricing-versions → { data:[{ id, providerId, modelProfileId, state,
 *     currency, label, effectiveFrom, lines[] }] }
 *
 * 保存（PUT /plan-draft）需要 If-Match；本页只读展示，不自动写。
 * ========================================================================== */

(function (root, factory) {
  var shared = (typeof module === 'object' && module && module.exports)
    ? require('./shared.js')
    : (root.RednoteRender && root.RednoteRender.shared);
  var api = factory(shared);
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.trialConfig = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (S) {
  'use strict';

  var text = S.text;
  var escapeHtml = S.escapeHtml;
  var shortId = S.shortId;
  var fmtTime = S.fmtTime;

  var ITEM_STATE = {
    ENABLED: { label: '启用', cls: 'tag-ok' },
    DISABLED: { label: '停用', cls: 'tag-muted' }
  };

  function listOf(rows) {
    return Array.isArray(rows)
      ? rows.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function draftOf(draft) {
    return draft && typeof draft === 'object' ? draft : null;
  }

  function permille(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }

  function pctOf(value) {
    return (permille(value) / 10).toFixed(1) + '%';
  }

  function itemState(state) {
    return ITEM_STATE[state] || { label: text(state) || '状态未提供', cls: 'tag-muted' };
  }

  function allocationByKey(allocation) {
    var index = {};
    var alloc = allocation && typeof allocation === 'object' ? allocation : {};
    listOf(alloc.lines).forEach(function (line) {
      if (text(line.key)) index[text(line.key)] = line;
    });
    return index;
  }

  /* ---------------------------- 汇总 ---------------------------- */

  function summarize(draft, assets, pricing, baselines) {
    var d = draftOf(draft) || {};
    var scenes = listOf(d.sceneItems);
    var forms = listOf(d.formItems);
    var sa = d.sceneAllocation && typeof d.sceneAllocation === 'object' ? d.sceneAllocation : null;
    var fa = d.formAllocation && typeof d.formAllocation === 'object' ? d.formAllocation : null;
    var baselinesList = listOf(baselines);
    return {
      scenes: scenes.length,
      forms: forms.length,
      sceneDisabled: scenes.filter(function (i) { return i.state === 'DISABLED'; }).length,
      formDisabled: forms.filter(function (i) { return i.state === 'DISABLED'; }).length,
      pairings: listOf(d.pairings).length,
      totalQuantity: d.totalQuantity,
      baselineId: text(d.baselineId),
      allocated: (sa ? Number(sa.allocatedTotal) || 0 : 0) + (fa ? Number(fa.allocatedTotal) || 0 : 0),
      assets: listOf(assets).length,
      pricing: listOf(pricing).length,
      baselines: baselinesList.length,
      activeBaseline: baselinesList.filter(function (b) { return b.isActive === true; }).length
    };
  }

  /* ---------------------------- 页头 / 指标 ---------------------------- */

  function renderHead(project, draft) {
    var p = project && typeof project === 'object' ? project : {};
    var d = draftOf(draft) || {};
    var state = text(d.state) || '未提供';
    return '<div>' +
      '<div class="row mb6">' +
        '<span class="tag tag-purple no-dot"><i class="fa-solid fa-4"></i> 步骤 4 / 8</span>' +
        '<span class="code">' + (text(p.projectCode) ? escapeHtml(p.projectCode) : '编号未提供') + '</span>' +
        '<span class="tag ' + (state === 'SUBMITTED' ? 'tag-ok' : 'tag-warn') + ' no-dot">草稿 ' + escapeHtml(state) + '</span>' +
        '<span class="tag tag-muted no-dot">总量 ' +
          (d.totalQuantity == null ? '未提供' : escapeHtml(d.totalQuantity)) + ' 条</span>' +
      '</div>' +
      '<h1 class="page-title">配置场景比例与内容形式比例</h1>' +
      '<p class="page-desc">' + (text(p.name) ? escapeHtml(p.name) : '项目名称未提供') +
        ' · 场景与形式以千分比（‰）保存；保存需 PUT /plan-draft（If-Match）。' +
        '本页只读展示当前草稿。</p></div>';
  }

  function renderMetrics(draft, assets, pricing, baselines) {
    var s = summarize(draft, assets, pricing, baselines);
    return [
      ['fa-users', '场景条目', s.scenes, s.scenes ? 't-ok' : 't-warn', 'sceneItems · 停用 ' + s.sceneDisabled],
      ['fa-images', '形式条目', s.forms, s.forms ? 't-ok' : 't-warn', 'formItems · 停用 ' + s.formDisabled],
      ['fa-photo-film', '参考素材', s.assets, s.assets ? 't-ok' : 't-warn', 'reference-assets · 只列可用'],
      ['fa-coins', '价目版本', s.pricing, s.pricing ? 't-ok' : 't-warn', 'pricing-versions · 只读']
    ].map(function (row) {
      return '<div class="card metric">' +
        '<div class="metric-label"><i class="fa-solid ' + row[0] + '"></i>' + row[1] + '</div>' +
        '<div class="metric-value ' + row[3] + '">' + escapeHtml(row[2]) + '</div>' +
        '<div class="metric-foot">' + escapeHtml(row[4]) + '</div></div>';
    }).join('');
  }

  /* ---------------------------- 比例卡 ---------------------------- */

  function renderDimensionRow(item, line) {
    var state = itemState(item.state);
    var disabled = text(item.disabledReason);
    return '<tr' + (item.state === 'DISABLED' ? ' style="opacity:0.6"' : '') + '>' +
      '<td class="strong">' + (text(item.name) ? escapeHtml(item.name) : escapeHtml(text(item.key) || '键未提供')) +
        '<div class="f10 t-fai mono">' + escapeHtml(text(item.key) || '键未提供') + '</div>' +
        (disabled ? '<div class="f10 t-warn">停用原因：' + escapeHtml(disabled) + '</div>' : '') + '</td>' +
      '<td><span class="tag ' + state.cls + ' no-dot f10">' + escapeHtml(state.label) + '</span></td>' +
      '<td class="num mono">' + escapeHtml(text(item.weightPermille) || '0') + '‰' +
        '<div class="f10 t-fai">' + pctOf(item.weightPermille) + '</div></td>' +
      '<td class="num mono">' + (line ? escapeHtml(line.allocated) + ' 条' : '<span class="t-mut">—</span>') + '</td>' +
      '<td class="num mono">' + (item.capacity == null ? '<span class="t-mut">—</span>' : escapeHtml(item.capacity)) + '</td>' +
      '<td class="f10 t-fai mono">' + (line
        ? 'floor ' + escapeHtml(text(line.floorPart)) + ' · 余数 ' + escapeHtml(text(line.remainder)) + ' · 次序 ' + escapeHtml(text(line.tieRank))
        : '—') + '</td>' +
      '</tr>';
  }

  function renderDimensionCard(title, label, items, allocation) {
    var list = listOf(items);
    var alloc = allocation && typeof allocation === 'object' ? allocation : null;
    var index = allocationByKey(alloc);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">' + escapeHtml(title) + '</span>' +
        '<span class="card-sub">' + list.length + ' 项 · ' + escapeHtml(label) + '</span></div>' +
      (list.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>条目</th><th style="width:70px">状态</th>' +
            '<th class="num" style="width:96px">配比</th><th class="num" style="width:76px">分配</th>' +
            '<th class="num" style="width:66px">容量</th><th style="width:196px">最大余数换算</th></tr></thead>' +
            '<tbody>' + list.map(function (item) {
              return renderDimensionRow(item, index[text(item.key)]);
            }).join('') + '</tbody></table></div>'
        : renderEmpty(title + '未配置', label + ' 为空；需 PUT /plan-draft 写入 ' + label + ' 行')) +
      (alloc
        ? '<div class="card-foot"><span class="f12 t-mut">' +
            '分配 total=' + escapeHtml(text(alloc.total)) + ' · 已分配 ' + escapeHtml(text(alloc.allocatedTotal)) +
            ' · 未分配 ' + escapeHtml(text(alloc.unallocated)) + ' · rounds ' + escapeHtml(text(alloc.rounds)) + '</span></div>'
        : '<div class="card-foot"><span class="f12 t-mut">分配读数未提供</span></div>') +
      (alloc && Array.isArray(alloc.notes) && alloc.notes.length
        ? '<div class="f10 t-warn" style="padding:0 18px 12px">' +
          alloc.notes.map(function (n) { return escapeHtml(n); }).join('<br>') + '</div>' : '') +
      '</section>';
  }

  /* ---------------------------- 基线 / 素材 / 价目 ---------------------------- */

  function renderBaselineCard(baselines, draft) {
    var list = listOf(baselines);
    var bound = text(draftOf(draft) && draftOf(draft).baselineId);
    var active = list.filter(function (b) { return b.isActive === true; });
    var boundRow = bound
      ? '<tr><td class="t-mut">绑定基线</td><td class="mono f11">' + escapeHtml(shortId(bound)) + '</td></tr>'
      : '<tr><td class="t-mut">绑定基线</td><td><span class="t-warn">null — 保存 plan-draft 需指定 baselineId</span></td></tr>';
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">基线绑定</span>' +
        '<span class="card-sub">GET /baselines · ' + list.length + ' 条</span></div>' +
      '<div class="card-body" style="padding-top:8px;padding-bottom:8px">' +
        (list.length
          ? '<table class="tbl tbl-compact" style="font-size:12px"><tbody>' +
              boundRow +
              list.map(function (b) {
                return '<tr><td class="t-mut" style="width:96px">r' + escapeHtml(b.revisionNo == null ? '—' : b.revisionNo) + '</td>' +
                  '<td class="strong f11">' + (text(b.rulesetVersion) ? escapeHtml(b.rulesetVersion) : '规则集未提供') +
                  ' <span class="f10 t-fai">纳入 ' + escapeHtml(b.itemCount == null ? '—' : b.itemCount) +
                  ' · 排除 ' + escapeHtml(b.excludedCount == null ? '—' : b.excludedCount) + '</span></td></tr>';
              }).join('') + '</tbody></table>'
          : renderEmpty('尚无基线', 'GET /baselines 返回空数组')) +
        '<div class="f10 t-fai mt10">' +
          (active.length ? '生效基线：r' + escapeHtml(text(active[0].revisionNo)) : '当前无生效基线') +
          '（projects.active_baseline_id 为准）</div>' +
      '</div></section>';
  }

  function renderAssetsCard(assets) {
    var list = listOf(assets);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">参考素材</span>' +
        '<span class="card-sub">GET /reference-assets · ' + list.length + ' 条 · 只列可用</span></div>' +
      (list.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>素材</th><th style="width:150px">型号 / 颜色 / 部位</th>' +
            '<th style="width:86px">状态</th><th style="width:120px">登记时间</th></tr></thead>' +
            '<tbody>' + list.map(function (asset) {
              return '<tr>' +
                '<td class="strong mono f11">' + escapeHtml(shortId(asset.assetId) || 'ID 未提供') +
                  (text(asset.source) ? '<div class="f10 t-fai">来源 ' + escapeHtml(asset.source) + '</div>' : '') + '</td>' +
                '<td class="f11">' + escapeHtml([asset.productModel, asset.productColor, asset.productPart]
                  .filter(function (v) { return text(v); }).join(' · ') || '未提供') + '</td>' +
                '<td><span class="tag no-dot f10 ' + (asset.state === 'ACTIVE' ? 'tag-ok' : 'tag-muted') + '">' +
                  escapeHtml(text(asset.state) || '未提供') + '</span></td>' +
                '<td class="f11 mono">' + (text(asset.createdAt) ? escapeHtml(fmtTime(asset.createdAt)) : '未提供') + '</td>' +
                '</tr>';
            }).join('') + '</tbody></table></div>'
        : renderEmpty('暂无参考素材', '登记需 POST /projects/{id}/reference-assets（fileObjectId 必填）')) +
      '</section>';
  }

  function renderPricingCard(pricing) {
    var list = listOf(pricing);
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">价目版本</span>' +
        '<span class="card-sub">GET /pricing-versions · ' + list.length + ' 条 · 全局只读</span></div>' +
      (list.length
        ? '<div class="table-wrap"><table class="tbl tbl-compact">' +
            '<thead><tr><th>价目</th><th style="width:110px">供应商</th><th style="width:70px">状态</th>' +
            '<th style="width:60px">币种</th><th style="width:86px">计量项</th>' +
            '<th style="width:116px">生效时间</th></tr></thead>' +
            '<tbody>' + list.map(function (v) {
              var lines = Array.isArray(v.lines) ? v.lines : [];
              return '<tr>' +
                '<td class="strong">' + (text(v.label) ? escapeHtml(v.label) : '名称未提供') + '</td>' +
                '<td class="f11 mono">' + escapeHtml(shortId(v.providerId) || '未提供') + '</td>' +
                '<td><span class="tag no-dot f10 tag-muted">' + escapeHtml(text(v.state) || '未提供') + '</span></td>' +
                '<td class="f11">' + escapeHtml(text(v.currency) || '未提供') + '</td>' +
                '<td class="num mono">' + lines.length + '</td>' +
                '<td class="f11 mono">' + (text(v.effectiveFrom) ? escapeHtml(fmtTime(v.effectiveFrom)) : '未提供') + '</td>' +
                '</tr>';
            }).join('') + '</tbody></table></div>'
        : renderEmpty('暂无价目版本', 'pricing-versions 为空；写路径属运维脚本，不在本页')) +
      '</section>';
  }

  /* ---------------------------- 通用块 ---------------------------- */

  function renderEmpty(title, sub) {
    return '<div class="col" style="align-items:center;gap:6px;padding:22px 0">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(title || '暂无数据') + '</span>' +
      (sub ? '<span class="f10 t-fai">' + escapeHtml(sub) + '</span>' : '') + '</div>';
  }

  function renderCardLoading(message) {
    return '<section class="card"><div class="card-head"><span class="card-title">读取中</span></div>' +
      '<div class="card-body"><div class="row" style="gap:8px">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(message || '正在读取…') + '</span></div></div></section>';
  }

  function renderCardError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上后端。确认 127.0.0.1:8787 已启动后可重试。'
      : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。');
    return '<section class="card"><div class="card-head"><span class="card-title">读取失败</span></div>' +
      '<div class="card-body"><div class="row" style="align-items:flex-start;gap:10px">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px">' +
      '<span class="f12 fw6">' + escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml((code ? code + ' · ' : '') + hint) + '</span></div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button></div></div></section>';
  }

  return {
    summarize: summarize,
    renderHead: renderHead,
    renderMetrics: renderMetrics,
    renderDimensionCard: renderDimensionCard,
    renderBaselineCard: renderBaselineCard,
    renderAssetsCard: renderAssetsCard,
    renderPricingCard: renderPricingCard,
    renderEmpty: renderEmpty,
    renderCardLoading: renderCardLoading,
    renderCardError: renderCardError,
    escapeHtml: escapeHtml
  };
});
