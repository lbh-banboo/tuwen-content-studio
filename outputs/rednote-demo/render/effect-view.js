/* ============================================================================
 * render/effect-view.js —— effect-view 页渲染层（方案 A：数据 → HTML 字符串）
 * ============================================================================
 * 纯函数模块：输入数据、输出字符串；不读 DOM、不读浏览器全局、不做 I/O、
 * 不取「现在几点」。因此可直接 Node 单测。
 *
 * 双环境导出：
 *   Node/CommonJS  → require('./effect-view.js')
 *   浏览器          → RednoteRender.effectView
 *
 * ---------------------------------------------------------------------------
 * ⚠️ 数据源是 **mock（M4 草案）**，不是正式契约
 * ---------------------------------------------------------------------------
 * 正式契约 contracts/openapi.json（86 paths）里 metric / effect / experiment
 * **全部零命中**；publication 只命中 /knowledge/assets/{id}/publications 与
 * /prompts/{id}/publications，都不是内容发布。
 * ⇒ 本页数据只能来自 contracts/draft/m4-openapi.yaml + tools/dev/mock_m4/samples.json
 *   （与已交付的 P3 六页同源，沿用「把 API 基址指向 mock 端口」的既有做法）。
 *   页面显著位置必须标「mock 数据源 · 正式契约未定义」。
 *
 * ---------------------------------------------------------------------------
 * 数据来源（草案 + mock，逐条核过行号）：
 *
 *   GET /experiments                 draft m4-openapi.yaml:580（?projectId&limit&cursor）
 *     → Experiment（yaml 的 components.schemas.Experiment）
 *       { id, projectId, name, hypothesis, state: DRAFT|RUNNING|CLOSED,
 *         startedAt, closedAt }
 *       mock 样本 1 条（samples.json 的 experiments）
 *
 *   GET /strategy-evidence           draft m4-openapi.yaml:633（?projectId&limit&cursor）
 *     → StrategyEvidence
 *       { id, projectId, experimentId, strategyRef,
 *         state: CANDIDATE|SUPPORTED|UNSUPPORTED|INCONCLUSIVE,
 *         evidenceGrade: EXPERIMENT|OBSERVATIONAL|INSUFFICIENT, note }
 *       mock 样本 1 条（INCONCLUSIVE + INSUFFICIENT）
 *       **草案 description 原文的产品红线**：缺失条件 ⇒ INCONCLUSIVE +
 *       evidenceGrade='INSUFFICIENT'，**不得自动 SUPPORTED**；
 *       不得把高点赞当因果证据。⇒ 这条红线可以用真数据演示。
 *
 *   GET /metric-definitions          draft m4-openapi.yaml:328
 *     → MetricDefinition { id, metricCode, scope, valueKind, unit, label }
 *       metricCode 枚举 14 个（VIEW_COUNT / IMPRESSION_COUNT / LIKE_COUNT /
 *         FAVORITE_COUNT / COMMENT_COUNT / LINK_CLICK_PV / LINK_CLICK_UV /
 *         IS_PROMOTED / COMMENT_TEXT / NOTE_URL / NOTE_ID / PUBLISHED_AT /
 *         PROMOTION_INFO / PUBLISH_STATUS）
 *       scope 枚举 7 个（CUMULATIVE / LAST_24H / LAST_7D / LAST_30D /
 *         LAST_90D / NOT_APPLICABLE / UNKNOWN）
 *       valueKind NUMERIC / TEXT
 *
 *   GET /publications/{publicationId}/metric-latest  draft m4-openapi.yaml:367
 *     → MetricLatest { publicationId, metricDefinitionId, observationId,
 *                      valueNum, valueText, observedAt, label, rebuiltAt }
 *       排序按 observed_at 最大化（不是 recorded_at）；metric_latest 是
 *       **可重建缓存**，不是历史权威来源（yaml description 原文）。
 *       ⚠️ 顶层 GET /metric-latest **不存在**，只有这条带 publicationId 的。
 *
 *   GET /publications/{publicationId}/labels         draft m4-openapi.yaml:385
 *     → PublicationLabel { publicationId, metricDefinitionId,
 *                          label: POTENTIAL|QUALITY|BELOW_THRESHOLD|PENDING,
 *                          thresholdVersion }
 *       缺失 → PENDING（**不归零、不进 BELOW_THRESHOLD**，yaml description 原文）
 *
 *   GET /publications                （用于拿一个 publicationId）
 *   GET /deliveries / GET /publications（第 4 段执行核对，与 delivery / delivery-koc 同源）
 *
 * ---------------------------------------------------------------------------
 * 降级登记（原型 12 处硬编码声称逐条处置）：
 *   D1 「EXP-007 · 除湿机选购」—— 实验编号无字段（Experiment 只有 id/name），
 *      改为显示真实 name + 短 id。
 *   D2 **「窗口未满 · 3d 已出 · 长尾未到」** —— 本页叙事主线，但：
 *      · 「窗口维度」**部分存在**：MetricDefinition.scope 有
 *        CUMULATIVE / LAST_24H / LAST_7D / LAST_30D / LAST_90D 五个真值；
 *      · 「3d」这个窗口**不在枚举里**；
 *      · 「窗口是否已满 / 长尾是否到齐」**完全没有字段**：没有 windowDays、
 *        没有 windowComplete、没有任何表示「还要等多久」的量。
 *      ⇒ 如实渲染每个指标定义的 scope（真），再用一张专门的卡写清
 *        「窗口是否已满」无字段（降级）。**不画「窗口未满」的判定结论**。
 *   D3 第 3 段「分组与预登记」两行表 + 任务数 18 / 17 ——
 *      草案只有 **POST** /experiments/{id}/assignments，**没有 GET**
 *      （yaml:611 只有 post）⇒ 分组列表无读源，整块降级。
 *   D4 4 个顶部指标里的「待核对执行」「窗口未满」—— 无源；
 *      「实验中假设」←experiments 里 state==='RUNNING' 计数（真）、
 *      「范围内支持」←strategy_evidence 里 state==='SUPPORTED' 计数（真）。
 *   D5 第 7 段「用途范围」—— StrategyEvidence 只有 note，没有用途范围字段。
 *   D6 「策略六态」双维状态机 —— 正式契约与草案**都不存在**六态；
 *      草案 StrategyEvidence.state 只有 4 态 ⇒ 静态 + 标注并注明「草案只有 4 态」。
 *   D7 「分开回答三个效果问题」「先诊断，再给策略」—— 教义，静态 + 标注。
 *   D8 第 4 段「实际执行核对」的结论（「执行不足」判据）—— deliveries /
 *      publications 可读，但「执行是否充分」的判据字段不存在 ⇒ 只列事实，不下结论。
 *   D9 「KOC 删掉了实验段落，这篇就不能再算策略执行成功」—— 教义，保留。
 * ========================================================================== */

(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module && module.exports) {
    module.exports = api;
  } else {
    root.RednoteRender = root.RednoteRender || {};
    root.RednoteRender.effectView = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  /* ---------------------------- 受控取值域（草案） ---------------------------- */

  var EXPERIMENT_STATES = ['DRAFT', 'RUNNING', 'CLOSED'];
  var EXPERIMENT_STATE_META = {
    DRAFT: { label: '草稿', cls: '' },
    RUNNING: { label: '实验中', cls: 'tag-purple' },
    CLOSED: { label: '已结束', cls: 'tag-muted' }
  };

  var EVIDENCE_STATES = ['CANDIDATE', 'SUPPORTED', 'UNSUPPORTED', 'INCONCLUSIVE'];
  var EVIDENCE_STATE_META = {
    CANDIDATE: { label: '候选', cls: '' },
    SUPPORTED: { label: '范围内支持', cls: 'tag-ok' },
    UNSUPPORTED: { label: '不支持', cls: 'tag-danger' },
    INCONCLUSIVE: { label: '无法判定', cls: 'tag-warn' }
  };

  var EVIDENCE_GRADES = ['EXPERIMENT', 'OBSERVATIONAL', 'INSUFFICIENT'];
  var EVIDENCE_GRADE_META = {
    EXPERIMENT: { label: '实验证据', cls: 'tag-ok' },
    OBSERVATIONAL: { label: '观察证据', cls: 'tag-warn' },
    INSUFFICIENT: { label: '证据不足', cls: 'tag-danger' }
  };

  /* MetricDefinition.scope —— 这是草案里**唯一**与「窗口」有关的真字段。
     注意：它是「这个指标按什么窗口统计」，不是「窗口满了没有」。 */
  var METRIC_SCOPES = ['CUMULATIVE', 'LAST_24H', 'LAST_7D', 'LAST_30D', 'LAST_90D',
    'NOT_APPLICABLE', 'UNKNOWN'];
  var METRIC_SCOPE_LABEL = {
    CUMULATIVE: '累计', LAST_24H: '近 24 小时', LAST_7D: '近 7 天',
    LAST_30D: '近 30 天', LAST_90D: '近 90 天',
    NOT_APPLICABLE: '不适用', UNKNOWN: '窗口未知'
  };

  var LABELS = ['POTENTIAL', 'QUALITY', 'BELOW_THRESHOLD', 'PENDING'];
  var LABEL_META = {
    POTENTIAL: { label: '潜力', cls: 'tag-purple' },
    QUALITY: { label: '优质', cls: 'tag-ok' },
    BELOW_THRESHOLD: { label: '未达阈值', cls: 'tag-warn' },
    PENDING: { label: '待定（数据缺失）', cls: 'tag-muted' }
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

  function truncate(value, max) {
    var s = text(value);
    return s.length > max ? s.slice(0, max) + '…' : s;
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

  function rows(value) {
    return Array.isArray(value)
      ? value.filter(function (row) { return row && typeof row === 'object'; })
      : [];
  }

  function card(title, sub, body, extraHead) {
    return '<section class="card">' +
      '<div class="card-head"><span class="card-title">' + escapeHtml(title) + '</span>' +
      (sub ? '<span class="card-sub">' + escapeHtml(sub) + '</span>' : '') +
      (extraHead || '') +
      '</div><div class="card-body">' + body + '</div></section>';
  }

  function doctrineBadge() {
    return '<span class="tag tag-muted no-dot f10">标准文本 · 非系统数据</span>';
  }

  function noSourceBadge() {
    return '<span class="tag tag-muted no-dot f10">无数据源</span>';
  }

  /** mock 数据源标记 —— 本页每个数据卡都该带它。 */
  function mockBadge() {
    return '<span class="tag tag-warn no-dot f10">mock 数据源 · 正式契约未定义</span>';
  }

  function note(cls, strong, body) {
    return '<div class="note note-' + cls + '">' +
      '<i class="fa-solid fa-' + (cls === 'warn' ? 'triangle-exclamation' : 'circle-info') + '"></i>' +
      '<div>' + (strong ? '<b>' + escapeHtml(strong) + '</b> ' : '') + escapeHtml(body) + '</div></div>';
  }

  function emptyBlock(message, hint) {
    return '<div class="col" style="align-items:center;gap:6px;padding:20px 0;">' +
      '<i class="fa-solid fa-inbox t-fai"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(message) + '</span>' +
      (hint ? '<span class="f10 t-fai">' + escapeHtml(hint) + '</span>' : '') +
      '</div>';
  }

  function tag(label, cls) {
    return '<span class="tag' + (cls ? ' ' + cls : '') + ' no-dot f10">' + escapeHtml(label) + '</span>';
  }

  /* ---------------------------- 纯计算 ---------------------------- */

  function tallyEvidence(evidence) {
    var list = rows(evidence);
    var byState = {}, byGrade = {};
    EVIDENCE_STATES.forEach(function (s) { byState[s] = 0; });
    EVIDENCE_GRADES.forEach(function (g) { byGrade[g] = 0; });
    var unknownStates = [], unknownGrades = [];
    list.forEach(function (e) {
      var s = text(e.state), g = text(e.evidenceGrade);
      if (EVIDENCE_STATES.indexOf(s) !== -1) byState[s] += 1;
      else unknownStates.push(s || '(未标注)');
      if (EVIDENCE_GRADES.indexOf(g) !== -1) byGrade[g] += 1;
      else unknownGrades.push(g || '(未标注)');
    });
    return {
      total: list.length,
      byState: byState,
      byGrade: byGrade,
      unknownStates: unknownStates,
      unknownGrades: unknownGrades
    };
  }

  /**
   * 七段各自「有数据 / 无数据源」。
   * 这是本页最有价值的一张导航：它让读者一眼看出哪几段是真的。
   * 判据只看「有没有读端点 + 这次取数拿到了几条」，不看内容质量。
   */
  function stageAvailability(data) {
    var d = data && typeof data === 'object' ? data : {};
    var experiments = rows(d.experiments);
    var evidence = rows(d.evidence);
    var metrics = rows(d.metrics);
    var deliveries = rows(d.deliveries);
    var publications = rows(d.publications);

    return [
      { stage: 1, label: '假设', status: experiments.length ? 'live' : 'empty',
        reason: experiments.length
          ? 'GET /experiments 返回 ' + experiments.length + ' 条，hypothesis 是真字段'
          : 'GET /experiments 返回空' },
      { stage: 2, label: '适用任务', status: 'none',
        reason: 'Experiment 只有 id/projectId/name/hypothesis/state/startedAt/closedAt，无「适用任务」字段' },
      { stage: 3, label: '分组与预登记', status: 'none',
        reason: '草案只有 POST /experiments/{id}/assignments，**没有 GET** ⇒ 分组列表读不到' },
      { stage: 4, label: '实际执行核对',
        status: (deliveries.length || publications.length) ? 'live' : 'empty',
        reason: (deliveries.length || publications.length)
          ? '交付 ' + deliveries.length + ' 条 / 发布 ' + publications.length + ' 条可列，但「执行是否充分」的判据无字段'
          : 'deliveries 与 publications 都为空' },
      { stage: 5, label: '窗口数据', status: metrics.length ? 'partial' : 'empty',
        reason: metrics.length
          ? '指标数值可读（' + metrics.length + ' 条），但「窗口是否已满」无字段'
          : '该发布还没有指标投影' },
      { stage: 6, label: '结论分级', status: evidence.length ? 'live' : 'empty',
        reason: evidence.length
          ? 'GET /strategy-evidence 返回 ' + evidence.length + ' 条，state × evidenceGrade 二维都是真字段'
          : 'GET /strategy-evidence 返回空' },
      { stage: 7, label: '用途范围', status: 'none',
        reason: 'StrategyEvidence 只有 note，没有「用途范围」字段' }
    ];
  }

  /** 把 metricDefinitionId 映射成定义本身。 */
  function indexDefinitions(definitions) {
    var index = {};
    rows(definitions).forEach(function (d) {
      var id = text(d.id);
      if (id) index[id] = d;
    });
    return index;
  }

  /* ---------------------------- 渲染 ---------------------------- */

  function renderSevenStageNav(stages) {
    var list = Array.isArray(stages) ? stages : [];
    if (!list.length) {
      return card('七段链路', '', emptyBlock('未计算可用性。'));
    }

    var meta = {
      live: { label: '有数据', cls: 'tag-ok' },
      partial: { label: '部分可接', cls: 'tag-warn' },
      empty: { label: '端点可用但为空', cls: 'tag-muted' },
      none: { label: '无数据源', cls: 'tag-muted' }
    };

    var body = list.map(function (s) {
      var m = meta[s.status] || meta.none;
      return '<div class="row between" style="gap:8px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<span class="row" style="gap:7px;align-items:center">' +
          '<span class="code f10">第 ' + escapeHtml(text(s.stage)) + ' 段</span>' +
          '<span class="f12">' + escapeHtml(text(s.label)) + '</span></span>' +
        '<span class="row" style="gap:8px;align-items:center;flex:1;justify-content:flex-end">' +
          '<span class="f10 t-mut" style="text-align:right">' + escapeHtml(text(s.reason)) + '</span>' +
          tag(m.label, m.cls) +
        '</span></div>';
    }).join('');

    var liveCount = list.filter(function (s) { return s.status === 'live'; }).length;
    return card('七段链路 · 逐段标注数据可得性',
      liveCount + ' / ' + list.length + ' 段有数据',
      body +
      note('info', '这张表就是本页的诚实声明。',
        '标「无数据源」的三段（适用任务、分组与预登记、用途范围）不是「这次没取到」，' +
        '而是**草案里就没有这些字段或端点**。原型在这三段里写的具体数字与结论已全部删除。'),
      mockBadge());
  }

  function renderExperimentList(experiments) {
    var list = rows(experiments);
    if (!list.length) {
      return card('第 1 段 · 假设', 'GET /experiments',
        emptyBlock('还没有实验。', 'mock 的 experiments 为空数组'), mockBadge());
    }

    var body = list.map(function (e) {
      var meta = EXPERIMENT_STATE_META[text(e.state)];
      return '<div class="col" style="gap:5px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<div class="row" style="gap:6px;flex-wrap:wrap;align-items:center">' +
          '<span class="f12 fw6">' + escapeHtml(truncate(text(e.name) || '（无名实验）', 48)) + '</span>' +
          (meta ? tag(meta.label, meta.cls) : tag(text(e.state) || '状态未标注', 'tag-muted')) +
          '<span class="code f10" title="' + escapeHtml(text(e.id)) + '">' +
            escapeHtml(shortId(e.id)) + '</span>' +
        '</div>' +
        '<span class="f11 t-mut">假设：' +
          (text(e.hypothesis)
            ? escapeHtml(truncate(text(e.hypothesis), 180))
            : '<span class="t-fai">（hypothesis 为空）</span>') + '</span>' +
        '<span class="f10 t-fai mono">开始 ' + escapeHtml(fmtTime(e.startedAt) || '—') +
          ' · 结束 ' + escapeHtml(fmtTime(e.closedAt) || '未结束') + '</span>' +
        '</div>';
    }).join('');

    return card('第 1 段 · 假设', 'GET /experiments · ' + list.length + ' 条',
      body +
      note('warn', '第 2 段「适用任务」没有字段。',
        'Experiment 的全部字段是 id / projectId / name / hypothesis / state / ' +
        'startedAt / closedAt —— 「这条假设适用于哪些任务」在草案里没有载体。' +
        '原型里的实验编号（EXP-xxx）也没有字段，这里显示的是真实 name + 短 id。'),
      mockBadge());
  }

  function renderExperimentCounters(experiments, evidence) {
    var exps = rows(experiments);
    var running = exps.filter(function (e) { return text(e.state) === 'RUNNING'; }).length;
    var t = tallyEvidence(evidence);

    function metric(label, value, foot, badge) {
      return '<div class="card metric">' +
        '<div class="metric-label">' + escapeHtml(label) + (badge ? ' ' + badge : '') + '</div>' +
        '<div class="metric-value">' + escapeHtml(text(value)) + '</div>' +
        '<div class="metric-foot f10 t-mut">' + escapeHtml(foot) + '</div></div>';
    }

    return '<div class="grid g4">' +
      metric('实验中的假设', running, "experiments 里 state === 'RUNNING'") +
      metric('范围内支持的策略', t.byState.SUPPORTED,
        "strategy_evidence 里 state === 'SUPPORTED'") +
      metric('待核对执行', '—', '「执行是否充分」的判据无字段', noSourceBadge()) +
      metric('窗口未满', '—', '没有 windowDays / windowComplete 任何字段', noSourceBadge()) +
      '</div>';
  }

  /** 第 6 段 · 结论分级：state × evidenceGrade 二维，本页最有价值的真数据。 */
  function renderEvidenceList(evidence) {
    var list = rows(evidence);
    if (!list.length) {
      return card('第 6 段 · 结论分级', 'GET /strategy-evidence',
        emptyBlock('还没有策略证据。', 'mock 的 strategy_evidence 为空数组'), mockBadge());
    }

    var body = list.map(function (e) {
      var sMeta = EVIDENCE_STATE_META[text(e.state)];
      var gMeta = EVIDENCE_GRADE_META[text(e.evidenceGrade)];
      var insufficient = text(e.evidenceGrade) === 'INSUFFICIENT';
      return '<div class="col" style="gap:5px;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.05)">' +
        '<div class="row" style="gap:6px;flex-wrap:wrap;align-items:center">' +
          '<span class="code f10">' + escapeHtml(truncate(text(e.strategyRef) || '（无 strategyRef）', 40)) + '</span>' +
          (sMeta ? tag(sMeta.label, sMeta.cls) : tag(text(e.state) || '状态未标注', 'tag-muted')) +
          (gMeta ? tag(gMeta.label, gMeta.cls) : tag(text(e.evidenceGrade) || '等级未标注', 'tag-muted')) +
        '</div>' +
        '<span class="f11 t-mut">' +
          (text(e.note)
            ? escapeHtml(truncate(text(e.note), 200))
            : '<span class="t-fai">（note 为空）</span>') + '</span>' +
        '<span class="f10 t-fai mono">实验 ' +
          (text(e.experimentId) ? escapeHtml(shortId(e.experimentId)) : '未关联实验') + '</span>' +
        (insufficient
          ? '<div class="note note-warn" style="margin-top:4px"><i class="fa-solid fa-triangle-exclamation"></i>' +
            '<div><b>证据不足。</b>这一条**不得**被当作策略依据使用，也不得据此声称因果。</div></div>'
          : '') +
        '</div>';
    }).join('');

    var t = tallyEvidence(evidence);
    return card('第 6 段 · 结论分级',
      'GET /strategy-evidence · ' + list.length + ' 条 · state × evidenceGrade 二维',
      body +
      (t.unknownStates.length || t.unknownGrades.length
        ? note('warn', '出现受控取值域外的取值：',
            Array.from(new Set(t.unknownStates.concat(t.unknownGrades))).join('、') +
            '，原样显示，不归并到已知取值。')
        : ''),
      mockBadge());
  }

  /** 产品红线卡：用真数据印证「缺失条件不得自动 SUPPORTED」。 */
  function renderEvidenceRedline(evidence) {
    var t = tallyEvidence(evidence);
    var insufficient = t.byGrade.INSUFFICIENT;
    var inconclusive = t.byState.INCONCLUSIVE;
    var supported = t.byState.SUPPORTED;

    return card('结论分级的产品红线', '草案 description 原文',
      '<div class="col f12 t-mut" style="gap:8px">' +
      '<span>· 缺失条件 ⇒ state = INCONCLUSIVE 且 evidenceGrade = INSUFFICIENT，' +
        '<b>不得自动 SUPPORTED</b>。</span>' +
      '<span>· <b>不得把高点赞当因果证据</b>：点赞是观察量，观察量最多支持 OBSERVATIONAL。</span>' +
      '<span>· 样本量不足时不标「已验证」—— 对应的就是 INSUFFICIENT 这一级。</span>' +
      '</div>' +
      '<div class="col" style="gap:6px;margin-top:12px">' +
      '<div class="row between f12"><span class="t-mut">当前 INSUFFICIENT（证据不足）</span>' +
        '<span class="mono">' + insufficient + ' 条</span></div>' +
      '<div class="row between f12"><span class="t-mut">当前 INCONCLUSIVE（无法判定）</span>' +
        '<span class="mono">' + inconclusive + ' 条</span></div>' +
      '<div class="row between f12"><span class="t-mut">当前 SUPPORTED（范围内支持）</span>' +
        '<span class="mono">' + supported + ' 条</span></div>' +
      '</div>' +
      note('info', '这三个数是真的，红线也是真的。',
        '这是本页唯一一处「教义」与「数据」能互相印证的地方：' +
        '红线写在草案的 POST /strategy-evidence 的 description 里，' +
        '上面三个计数来自 GET /strategy-evidence 的真实返回。'),
      mockBadge());
  }

  /** 第 5 段 · 窗口数据。数值真接；窗口维度来自定义的 scope；窗口是否已满无字段。 */
  function renderMetricLatest(metrics, definitions) {
    var list = rows(metrics);
    var index = indexDefinitions(definitions);
    if (!list.length) {
      return card('第 5 段 · 窗口数据', 'GET /publications/{publicationId}/metric-latest',
        emptyBlock('该发布还没有指标投影。',
          '注意：顶层 GET /metric-latest 不存在，只有带 publicationId 的这一条'),
        mockBadge());
    }

    var head = '<thead><tr>' +
      '<th>指标</th>' +
      '<th style="width:104px">统计窗口</th>' +
      '<th class="num" style="width:96px">数值</th>' +
      '<th style="width:104px">阈值标签</th>' +
      '<th style="width:124px">观测时间</th>' +
      '<th style="width:124px">重建时间</th>' +
      '</tr></thead>';

    var body = list.map(function (m) {
      var def = index[text(m.metricDefinitionId)];
      var scope = def ? text(def.scope) : '';
      var lblMeta = LABEL_META[text(m.label)];
      /* 缺失值：valueNum 与 valueText 都为 null ⇒ 显示「未采集」而不是 0。
         草案 description 原文：缺失不填 0；NULL / 0 / 约数三者分离。 */
      var hasNum = m.valueNum !== null && m.valueNum !== undefined;
      var hasText = text(m.valueText) !== '';
      var valueCell = hasNum
        ? escapeHtml(text(m.valueNum)) + (def && text(def.unit) ? ' ' + escapeHtml(text(def.unit)) : '')
        : (hasText
          ? escapeHtml(truncate(text(m.valueText), 24))
          : '<span class="t-mut">未采集</span>');

      return '<tr' + (!hasNum && !hasText ? ' class="tr-warn"' : '') + '>' +
        '<td class="f11">' +
          (def
            ? escapeHtml(text(def.label) || text(def.metricCode))
            : '<span class="t-mut">未知指标 ' + escapeHtml(shortId(m.metricDefinitionId)) + '</span>') +
          (def ? ' <span class="mono f10 t-fai">' + escapeHtml(text(def.metricCode)) + '</span>' : '') +
        '</td>' +
        '<td class="f11">' +
          (scope
            ? escapeHtml(METRIC_SCOPE_LABEL[scope] || scope) +
              (METRIC_SCOPES.indexOf(scope) === -1 ? '<span class="t-fai">（取值域外）</span>' : '')
            : '<span class="t-mut">—</span>') + '</td>' +
        '<td class="num mono f11">' + valueCell + '</td>' +
        '<td>' + (lblMeta ? tag(lblMeta.label, lblMeta.cls)
          : (text(m.label) ? tag(truncate(text(m.label), 18), 'tag-muted') : '<span class="t-mut">—</span>')) + '</td>' +
        '<td class="f11 mono">' + escapeHtml(fmtTime(m.observedAt) || '—') + '</td>' +
        '<td class="f11 mono">' + escapeHtml(fmtTime(m.rebuiltAt) || '—') + '</td>' +
        '</tr>';
    }).join('');

    var pending = list.filter(function (m) { return text(m.label) === 'PENDING'; }).length;
    return card('第 5 段 · 窗口数据',
      'GET /publications/{publicationId}/metric-latest · ' + list.length + ' 个指标',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      note('info', 'metric_latest 是可重建缓存，不是历史权威来源。',
        '排序按 observedAt 最大化（不是 recordedAt）。' +
        '「重建时间」列就是 rebuiltAt —— 它说明这份投影是什么时候算出来的。' +
        '历史权威来源是 metric_observations（只追加）。') +
      (pending
        ? note('warn', '有 ' + pending + ' 个指标的标签是 PENDING。',
            'PENDING 的含义是**数据缺失**，不是「未达阈值」—— ' +
            '草案明确要求缺失 → PENDING，不归零、不进 BELOW_THRESHOLD。')
        : ''),
      mockBadge());
  }

  /** 降级 D2：窗口判定无字段。这是本页最需要说清的一处。 */
  function renderWindowUnavailable(definitions) {
    var defs = rows(definitions);
    var scopes = [];
    defs.forEach(function (d) {
      var s = text(d.scope);
      if (s && scopes.indexOf(s) === -1) scopes.push(s);
    });

    return card('窗口是否已满 / 长尾是否到齐', '无字段',
      note('warn', '「窗口未满」「3d 已出」「长尾未到」这三句都没有数据支撑。',
        '把能查到的和查不到的分开说：' +
        '① **窗口维度是真的** —— MetricDefinition.scope 有 CUMULATIVE / LAST_24H / ' +
        'LAST_7D / LAST_30D / LAST_90D / NOT_APPLICABLE / UNKNOWN 七个受控取值，' +
        '上面「第 5 段」表里的「统计窗口」列就是它；' +
        '② **「3d」这个窗口不在枚举里** —— 草案里只有 24h / 7d / 30d / 90d；' +
        '③ **「窗口是否已满」完全没有字段** —— 没有 windowDays、没有 windowComplete、' +
        '没有任何表示「还要等多久」的量。所以本页不渲染「窗口未满」这个判定结论，' +
        '只如实展示每个指标的 observedAt（观测时间）与 rebuiltAt（投影重建时间）。') +
      (scopes.length
        ? '<div class="row" style="gap:6px;flex-wrap:wrap;margin-top:10px">' +
          scopes.map(function (s) {
            return tag((METRIC_SCOPE_LABEL[s] || s) + '（' + s + '）',
              METRIC_SCOPES.indexOf(s) === -1 ? 'tag-muted' : '');
          }).join('') + '</div>'
        : ''),
      noSourceBadge());
  }

  /** 降级 D3：分组与预登记无 GET 端点。 */
  function renderAssignmentsUnavailable() {
    return card('第 3 段 · 分组与预登记', '无读端点',
      note('warn', '草案只有 POST，没有 GET。',
        'contracts/draft/m4-openapi.yaml:611 的 /experiments/{experimentId}/assignments ' +
        '**只定义了 post**（operationId: assign_experiment），没有 get。' +
        'mock 里的 experiment_assignments 样本也只能通过 POST 回显。' +
        '⇒ 对照组 / 实验组两行表、每组的任务数，都读不到。' +
        '原型里写的任务数（两个具体数字）与「证据正确」结论已删除。' +
        'ExperimentAssignment 的字段本身是有的（id / experimentId / contentItemId / ' +
        'arm / assignedAt），缺的只是列表端点。') +
      '<div class="col f11 t-mut" style="gap:4px;margin-top:10px">' +
      '<span>· 完整接线的前置：草案补 GET /experiments/{id}/assignments</span>' +
      '<span>· 「预登记」本身是纪律（先登记指标再看数据），不是字段</span>' +
      '</div>',
      noSourceBadge());
  }

  /** 第 4 段 · 实际执行核对。列事实，不下「执行不足」的结论（降级 D8）。 */
  function renderExecutionCheck(deliveries, publications) {
    var dels = rows(deliveries);
    var pubs = rows(publications);
    if (!dels.length && !pubs.length) {
      return card('第 4 段 · 实际执行核对', 'GET /deliveries + GET /publications',
        emptyBlock('还没有交付或发布记录。'), mockBadge());
    }

    var head = '<thead><tr>' +
      '<th style="width:112px">发布</th>' +
      '<th style="width:104px">发布状态</th>' +
      '<th style="width:112px">内容核对状态</th>' +
      '<th style="width:124px">发布时间</th>' +
      '<th>规范化链接</th>' +
      '</tr></thead>';

    var body = pubs.length
      ? pubs.map(function (p) {
          return '<tr>' +
            '<td class="f11 mono" title="' + escapeHtml(text(p.id)) + '">' +
              escapeHtml(text(p.publicationKey) || shortId(p.id) || '—') + '</td>' +
            '<td class="f11">' + escapeHtml(truncate(text(p.state) || '未标注', 18)) + '</td>' +
            '<td class="f11">' + escapeHtml(truncate(text(p.contentState) || '未标注', 18)) + '</td>' +
            '<td class="f11 mono">' + escapeHtml(fmtTime(p.publishedAt) || '—') + '</td>' +
            '<td class="f11 mono">' +
              (text(p.urlCanonical)
                ? escapeHtml(truncate(text(p.urlCanonical).replace(/^https?:\/\//i, ''), 44))
                : '<span class="t-mut">—</span>') + '</td>' +
            '</tr>';
        }).join('')
      : '<tr><td colspan="5">' + emptyBlock('没有发布记录。') + '</td></tr>';

    return card('第 4 段 · 实际执行核对',
      '交付 ' + dels.length + ' 条 / 发布 ' + pubs.length + ' 条',
      '<div class="table-wrap"><table class="tbl tbl-compact">' + head +
      '<tbody>' + body + '</tbody></table></div>' +
      note('warn', '这里只列事实，不下「执行不足」的结论。',
        '「实际执行是否符合预登记」需要三样东西：预登记的分组（无 GET 端点）、' +
        '发布稿与交付稿的差异判据（无字段）、以及「符合 / 不符合」的阈值（无字段）。' +
        '三样都没有，所以这张表只回答「发了几篇、什么状态、什么时间」。' +
        '教义仍然成立：KOC 若删掉了实验段落，这篇就不能再算策略执行成功 —— ' +
        '但这个判断目前只能人工做。'),
      mockBadge());
  }

  /** 降级 D5：第 7 段用途范围无字段。 */
  function renderPurposeScopeUnavailable() {
    return card('第 7 段 · 用途范围', '无字段',
      note('warn', 'StrategyEvidence 只有一个自由文本 note。',
        '「这条结论能用在哪些主体 / 哪些品类 / 多长时间内有效」在草案里没有结构化字段。' +
        'strategyRef 是策略的引用标识、note 是自由文本 —— ' +
        '把 note 解析出用途范围属于编造，本页不做。' +
        '原型里第 7 段写的具体用途范围已删除；note 的原文照常展示在第 6 段每条证据下面。'),
      noSourceBadge());
  }

  /** 降级 D6：策略双维状态机 —— 六态在正式契约与草案里都不存在。 */
  function renderStrategyStateMachineDoctrine() {
    return card('策略双维状态机', '方法论标准',
      '<div class="col f12 t-mut" style="gap:7px">' +
      '<span>· 维度一（证据强度）：无证据 → 观察证据 → 实验证据</span>' +
      '<span>· 维度二（生命周期）：候选 → 实验中 → 已验证 → 失效归档</span>' +
      '<span>· 状态机必须双向：没有降级与回退就会积累僵尸策略</span>' +
      '</div>' +
      note('warn', '页面原本画的「策略六态」在数据源里不存在。',
        '正式契约 contracts/openapi.json（86 paths）里 metric / effect / experiment / ' +
        'strategy 全部零命中；m4 草案的 StrategyEvidence.state 只有 **4 态**' +
        '（CANDIDATE / SUPPORTED / UNSUPPORTED / INCONCLUSIVE），' +
        'evidenceGrade 只有 **3 级**（EXPERIMENT / OBSERVATIONAL / INSUFFICIENT）。' +
        '上面这两条维度是方法论描述，与那 4 态 / 3 级**不是一一对应**，' +
        '所以这张卡保持静态，真实的二维分布看「第 6 段 · 结论分级」。'),
      doctrineBadge());
  }

  /** 降级 D7：教义两块。 */
  function renderEffectDoctrine() {
    return card('效果口径纪律', '',
      '<div class="col f12 t-mut" style="gap:8px">' +
      '<span>· 三个效果问题要分开回答：有没有发出去（执行）、数据长什么样（观测）、能不能得出结论（证据）。混在一起答就会把「数据好」说成「策略对」。</span>' +
      '<span>· 先诊断，再给策略：没弄清是执行问题还是策略问题之前，换策略只会换掉一个没验证过的东西。</span>' +
      '<span>· 不得由相关性推因果；不得把单条高互动当策略证据。</span>' +
      '<span>· 窗口未满时不下结论 —— 这条纪律成立，但「窗口满没满」目前没有字段可判，只能人工看 observedAt。</span>' +
      '</div>', doctrineBadge());
  }

  /* ---------------------------- 加载 / 空 / 错误 ---------------------------- */

  function renderLoading(label) {
    return '<section class="card"><div class="card-head"><span class="card-title">读取中</span></div>' +
      '<div class="card-body"><div class="row" style="gap:8px">' +
      '<i class="fa-solid fa-circle-notch fa-spin t-pur"></i>' +
      '<span class="f12 t-mut">' + escapeHtml(text(label) || '正在读取实验与证据…') + '</span>' +
      '</div></div></section>';
  }

  function renderEmpty(message) {
    return card('暂无数据', '', emptyBlock(text(message) || '没有可展示的效果数据。'));
  }

  function renderError(err) {
    var e = err && typeof err === 'object' ? err : {};
    var code = text(e.code);
    var message = text(e.message) || text(err) || '读取效果数据失败。';
    var offline = !!e.offline || code === 'DEPENDENCY_UNAVAILABLE';
    var hint = offline
      ? '连不上 mock（页面结构已保留）。本页数据源是 tools/dev/mock_m4，' +
        '正式契约里没有 metric / experiment / strategy-evidence 这三组端点 —— ' +
        '确认 mock 已启动后可重试。'
      : (code === 'RESOURCE_NOT_FOUND'
        ? '端点或资源不存在。本页用的是 M4 草案端点，真后端上这些路径不存在。'
        : (e.retryable ? '该错误可重试，请稍后再试。' : '请按错误码排查后重试。'));

    return '<section class="card"><div class="card-head">' +
      '<span class="card-title">读取失败</span></div>' +
      '<div class="card-body"><div class="row" style="align-items:flex-start;gap:10px">' +
      '<i class="fa-solid fa-triangle-exclamation t-dan"></i>' +
      '<div class="col" style="flex:1;gap:4px">' +
      '<span class="f12 fw6">' + escapeHtml(message) + '</span>' +
      '<span class="f10 t-mut">' + escapeHtml((code ? code + ' · ' : '') + hint) + '</span>' +
      '</div>' +
      '<button class="btn btn-sm btn-ghost" data-action="retry">' +
      '<i class="fa-solid fa-rotate"></i> 重试</button>' +
      '</div></div></section>';
  }

  return {
    /* 纯计算 */
    tallyEvidence: tallyEvidence,
    stageAvailability: stageAvailability,
    indexDefinitions: indexDefinitions,
    /* 渲染 */
    renderSevenStageNav: renderSevenStageNav,
    renderExperimentList: renderExperimentList,
    renderExperimentCounters: renderExperimentCounters,
    renderEvidenceList: renderEvidenceList,
    renderEvidenceRedline: renderEvidenceRedline,
    renderMetricLatest: renderMetricLatest,
    renderWindowUnavailable: renderWindowUnavailable,
    renderAssignmentsUnavailable: renderAssignmentsUnavailable,
    renderExecutionCheck: renderExecutionCheck,
    renderPurposeScopeUnavailable: renderPurposeScopeUnavailable,
    renderStrategyStateMachineDoctrine: renderStrategyStateMachineDoctrine,
    renderEffectDoctrine: renderEffectDoctrine,
    renderLoading: renderLoading,
    renderEmpty: renderEmpty,
    renderError: renderError,
    mockBadge: mockBadge,
    escapeHtml: escapeHtml,
    EXPERIMENT_STATES: EXPERIMENT_STATES,
    EVIDENCE_STATES: EVIDENCE_STATES,
    EVIDENCE_GRADES: EVIDENCE_GRADES,
    METRIC_SCOPES: METRIC_SCOPES,
    LABELS: LABELS
  };
});
