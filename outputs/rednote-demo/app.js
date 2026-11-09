/* ============================================================
   Rednote 系统 Demo — 共享脚本
   职责：注入侧边栏/顶栏、页面间跳转、演示态交互
   ============================================================ */

/* ---------- 页面注册表：单一数据源 ----------
   flow: 该页在流程中的位置 { step, sub, of } —— 用于页尾步骤导航与流程条高亮
   next / prev: 交接页面；next 为 null 表示流程终点
   ------------------------------------------------ */
const PAGES = [
  { id: 'workbench',        file: 'workbench.html',        name: '工作台',      icon: 'fa-gauge-high',      group: 'main' },
  { id: 'projects',         file: 'projects.html',         name: '项目中心',    icon: 'fa-folder-open',     group: 'main' },
  { id: 'knowledge',        file: 'knowledge.html',        name: '知识库',      icon: 'fa-book-bookmark',   group: 'main' },
  { id: 'tasks',            file: 'tasks.html',            name: '任务与通知',  icon: 'fa-bell',            group: 'main', badge: 7 },

  { id: 'project-overview', file: 'project-overview.html', name: '项目总览',    icon: 'fa-chart-pie',       group: 'proj' },

  { id: 'brief',            file: 'brief.html',            name: 'Brief 解析',  icon: 'fa-file-lines',      group: 'flow', flow: { step: 1, sub: 1, of: 2 } },
  { id: 'brief-thicken',    file: 'brief-thicken.html',    name: '加厚输出',    icon: 'fa-wand-magic-sparkles', group: 'flow', flow: { step: 1, sub: 2, of: 2 } },

  { id: 'research',         file: 'research.html',         name: '项目研究',    icon: 'fa-magnifying-glass',group: 'flow', flow: { step: 2, sub: 1, of: 2 } },
  { id: 'research-files',   file: 'research-files.html',   name: '资料与缺口',  icon: 'fa-folder-tree',     group: 'flow', flow: { step: 2, sub: 2, of: 2 } },

  { id: 'info-review',      file: 'info-review.html',      name: '信息审核',    icon: 'fa-clipboard-check', group: 'flow', flow: { step: 3, sub: 1, of: 3 } },
  { id: 'baseline',         file: 'baseline.html',         name: '生产基线',    icon: 'fa-layer-group',     group: 'flow', flow: { step: 3, sub: 2, of: 3 } },
  { id: 'claims',           file: 'claims.html',           name: '可用主张',    icon: 'fa-quote-left',      group: 'flow', flow: { step: 3, sub: 3, of: 3 } },

  { id: 'trial-config',     file: 'trial-config.html',     name: '试产配置',    icon: 'fa-sliders',         group: 'flow', flow: { step: 4, sub: 1, of: 3 } },
  { id: 'trial-plan',       file: 'trial-plan.html',       name: '生产清单',    icon: 'fa-list-ol',         group: 'flow', flow: { step: 4, sub: 2, of: 3 } },
  { id: 'plan-detail',      file: 'plan-detail.html',      name: '逐行计划',    icon: 'fa-table-list',      group: 'flow', flow: { step: 4, sub: 3, of: 3 } },

  { id: 'content-feedback', file: 'content-feedback.html', name: '试产反馈',    icon: 'fa-comments',        group: 'flow', flow: { step: 5, sub: 1, of: 3 } },
  { id: 'feedback-list',    file: 'feedback-list.html',    name: '本轮内容',    icon: 'fa-images',          group: 'flow', flow: { step: 5, sub: 2, of: 3 } },
  { id: 'feedback-fix',     file: 'feedback-fix.html',     name: '对照与修复',  icon: 'fa-code-compare',    group: 'flow', flow: { step: 5, sub: 3, of: 3 } },

  { id: 'scale-batch',      file: 'scale-batch.html',      name: '新建扩产批次',icon: 'fa-layer-group',     group: 'flow', flow: { step: 6, sub: 1, of: 3 } },
  { id: 'batch-run',        file: 'batch-run.html',        name: '新建批产批次',icon: 'fa-clone',           group: 'flow', flow: { step: 6, sub: 2, of: 3 } },
  { id: 'batch-check',      file: 'batch-check.html',      name: '计划层检查',  icon: 'fa-list-check',      group: 'flow', flow: { step: 6, sub: 3, of: 3 } },

  { id: 'delivery',         file: 'delivery.html',         name: '交付记录',    icon: 'fa-box-open',        group: 'flow', flow: { step: 7, sub: 1, of: 2 } },
  { id: 'delivery-koc',     file: 'delivery-koc.html',     name: 'KOC 与交付',  icon: 'fa-user-tag',        group: 'flow', flow: { step: 7, sub: 2, of: 2 } },

  { id: 'data-feedback',    file: 'data-feedback.html',    name: '数据反馈',    icon: 'fa-chart-line',      group: 'flow', flow: { step: 8, sub: 1, of: 3 } },
  { id: 'data-snapshot',    file: 'data-snapshot.html',    name: '快照与汇总',  icon: 'fa-camera-retro',    group: 'flow', flow: { step: 8, sub: 2, of: 3 } },
  { id: 'data-trace',       file: 'data-trace.html',       name: '差异与会留痕',icon: 'fa-route',           group: 'flow', flow: { step: 8, sub: 3, of: 3 } },

  { id: 'report',           file: 'report.html',           name: '项目报告',    icon: 'fa-file-contract',   group: 'proj' },

  { id: 'quality-view',     file: 'quality-view.html',     name: '生产质量视图',icon: 'fa-arrows-spin',     group: 'loop' },
  { id: 'effect-view',      file: 'effect-view.html',      name: '效果学习视图',icon: 'fa-flask-vial',      group: 'loop' },

  { id: 'cost',             file: 'cost.html',             name: '费用管理',    icon: 'fa-coins',           group: 'admin' },
  { id: 'members',          file: 'members.html',          name: '成员与供应商',icon: 'fa-users-gear',      group: 'admin' },
];

/* ---------- 页尾步骤导航：单一数据源 ----------
   8 个业务关口 → 24 个可执行环节（一页一环节）。
   step 用小关口语义编号（1—8），sub/of 表示该关口内的第几页。
   顺序即导航顺序：next / prev 自动推导，不手写。
   ------------------------------------------------ */
const STEP_ORDER = [
  { id: 'brief',            step: 1, label: '阅读 Brief 原文与结构化拆解',   done: '确认 10 类必覆盖字段无遗漏' },
  { id: 'brief-thicken',    step: 1, label: '确认加厚输出与版本规则',       done: '四类加厚输出可追溯' },

  { id: 'research',         step: 2, label: '领取推荐研究任务并上传资料',   done: '24 份资料上传完成' },
  { id: 'research-files',   step: 2, label: '核对资料文件与覆盖缺口',       done: '缺口已标记并补充' },

  { id: 'info-review',      step: 3, label: '审核产品事实、人群与表达边界', done: '事实冲突已处置' },
  { id: 'baseline',         step: 3, label: '确认生产基线快照与组装顺序',   done: '基线 BL-v3 已批准' },
  { id: 'claims',           step: 3, label: '确认可用主张与拟用策略',       done: '每条主张有证据来源' },

  { id: 'trial-config',     step: 4, label: '配置场景比例与内容形式比例',   done: '两类比例各自合计 100%' },
  { id: 'trial-plan',       step: 4, label: '确认最终十行生产清单与费用',   done: '提交校验通过' },
  { id: 'plan-detail',      step: 4, label: '复核逐行决策问题与实验分组',   done: '每行偏离项已说明' },

  { id: 'content-feedback', step: 5, label: '逐条审核试产内容并处理意见',   done: '阻断事实问题已解决' },
  { id: 'feedback-list',    step: 5, label: '核对本轮 10 条内容与关卡校验', done: '每个方向至少一条已通过' },
  { id: 'feedback-fix',     step: 5, label: '确认对照、原因与局部修复范围', done: '修复范围已收窄到行' },

  { id: 'scale-batch',      step: 6, label: '新建扩产批次（每批 1—30 条）', done: '批次差异化检查通过' },
  { id: 'batch-run',        step: 6, label: '新建批产批次（每批 1—50 条）', done: '批次队列已提交' },
  { id: 'batch-check',      step: 6, label: '执行计划层差异化检查',         done: '无重复生产风险' },

  { id: 'delivery',         step: 7, label: '确认交付进度与下载包结构',     done: '仅批准版本可下载' },
  { id: 'delivery-koc',     step: 7, label: '登记 KOC 轻量记录与交付记录',  done: '交付记录已显式标记' },

  { id: 'data-feedback',    step: 8, label: '登记发布信息与采集状态',       done: '链接失效已标记' },
  { id: 'data-snapshot',    step: 8, label: '查看指标快照历史与项目汇总',   done: '缺失不填 0' },
  { id: 'data-trace',       step: 8, label: '核对交付稿→发布稿差异与留痕',  done: '四段留痕完整' },
];

/* 由 STEP_ORDER 推导 prev / next 与所在关口 */
const STEP_NAV = {};
STEP_ORDER.forEach((s, i) => {
  STEP_NAV[s.id] = Object.assign({}, s, {
    idx: i,
    prev: i > 0 ? STEP_ORDER[i - 1].id : null,
    next: i < STEP_ORDER.length - 1 ? STEP_ORDER[i + 1].id : null,
    stepTotal: STEP_ORDER.filter(x => x.step === s.step).length,
    sub: STEP_ORDER.filter(x => x.step === s.step).findIndex(x => x.id === s.id) + 1,
  });
});

const STEP_TOTAL = 8;

function stepFile(id) {
  const p = PAGES.find(x => x.id === id);
  return p ? p.file : '#';
}

const GROUP_TITLES = { main: '导航', proj: '项目 · 素与 Su&', loop: '双 Loop 学习闭环', admin: '管理中心' };

/* ---------- 八步流程关口状态：单一数据源 ----------
   流程导航已从固定侧边栏摘除，改为在流程页顶部内容区渲染关口状态条。
   state: done（已过关）| current（进行中）| locked（未解锁）
   每个关口可含多个环节页面（见 STEP_ORDER），流程条按关口聚合展示。
   ------------------------------------------------ */
/* ⚠️ 2026-10-17：`state` / `meta` 两个字段**已不再参与渲染** —— 流程条改为纯导航
   （原先它把 '步骤 5 当前'、'24 份 · 218 MB' 这类示例数据注入每一页，与真实的
   `GET /projects/{id}/step-approvals`（当前返回空数组）直接矛盾）。
   保留字段是为了将来若把流程条改成数据驱动时可直接复用；**新增页面不要依赖它们**。 */
const FLOW_STEPS = [
  { id: 'brief',            step: 1, label: 'Brief 解析与加厚', state: 'done',    meta: 'v2 已批准 · 09-11', enters: ['brief','brief-thicken'] },
  { id: 'research',         step: 2, label: '项目研究与上传',   state: 'done',    meta: '24 份 · 218 MB',    enters: ['research','research-files'] },
  { id: 'info-review',      step: 3, label: '信息审核与基线',   state: 'done',    meta: 'BL-v3 已批准',      enters: ['info-review','baseline','claims'] },
  { id: 'trial-config',     step: 4, label: '素材试产配置',     state: 'done',    meta: '10 条 · 方案 P-v2',  enters: ['trial-config','trial-plan','plan-detail'] },
  { id: 'content-feedback', step: 5, label: '试产反馈',         state: 'current', meta: '6 通过 · 8 待处置',  enters: ['content-feedback','feedback-list','feedback-fix'] },
  { id: 'scale-batch',      step: 6, label: '扩产 / 批产',      state: 'locked',  meta: '每批 1—30',         enters: ['scale-batch','batch-run','batch-check'] },
  { id: 'delivery',         step: 7, label: '批量产出与交付',   state: 'locked',  meta: '每批 1—50',         enters: ['delivery','delivery-koc'] },
  { id: 'data-feedback',    step: 8, label: '发布登记与数据',   state: 'locked',  meta: '持续工作区',        enters: ['data-feedback','data-snapshot','data-trace'] },
];

/* 流程页外的项目级页面（也需要流程条以便进入各步骤） */
const FLOW_EXTRA = ['project-overview', 'report'];

/* ---------- 注入页内八步流程条 ----------
   在 .canvas 内 .page-head 之后插入，随页面滚动，不吸顶。
   传入的 activeId 可以是关口 id，也可以是关口下的某个环节页 id；
   命中后该关口加 .is-here，并在关口内显示「环节 2/3」。
   关口的跳转目标 = 该关口下第一个环节页（enters[0]）。
   ---------------------------------------- */
function gateOf(activeId) {
  return FLOW_STEPS.find(s => s.id === activeId)
      || FLOW_STEPS.find(s => (s.enters || []).includes(activeId))
      || null;
}

function renderFlowBar(activeId) {
  const inFlow = !!gateOf(activeId) || FLOW_EXTRA.includes(activeId);
  if (!inFlow) return '';

  const here = gateOf(activeId);
  // ⚠️ 2026-10-17：流程条**只做导航，不再声称任何状态**。
  // FLOW_STEPS 里的 `state` / `meta`（'v2 已批准 · 09-11'、'6 通过 · 8 待处置' 等）
  // 是 09-11 的示例数据，与真实 `GET /projects/{id}/step-approvals` 无关
  // ——实测该接口当前返回**空数组**（八步全部未开始），而本流程条却在每页
  // 显示「步骤 5 当前」，页面自己跟自己矛盾。
  // 真实状态由各 v2 页从接口数据自行渲染（见 project-overview.v2 的关口中枢）。
  const items = FLOW_STEPS.map(s => {
    const isHere = here && s.id === here.id ? ' is-here' : '';
    const sub = (isHere && here) ? `<div class="step-meta">环节 ${here.sub}/${here.stepTotal}</div>` : '';
    return `<a class="step${isHere}" href="${(s.enters && s.enters[0]) || s.id}.html">
        <div class="step-n">0${s.step} <i class="fa-solid fa-circle-dot"></i></div>
        <div class="step-name">${s.label}</div>
        ${sub}
      </a>`;
  }).join('');

  const curTag = here
    ? `<span class="tag tag-purple f10">当前：步骤 ${here.step} ${here.label}</span>`
    : `<span class="tag tag-mut f10">八步流程导航</span>`;

  return `
  <section class="card flowbar mb14">
    <div class="card-body" style="padding:10px 14px 11px">
      <div class="row between mb8">
        <div class="row">
          <i class="fa-solid fa-diagram-project t-pur f11"></i>
          <span class="f11 fw6">八步流程</span>
          ${curTag}
        </div>
        <span class="f10 t-mut">首次生产必须按 1—8 顺序完成关口，后端不允许绕过</span>
      </div>
      <div class="steps">${items}</div>
    </div>
  </section>`;
}

/* ---------- 注入页尾步骤导航 ----------
   一页一环节，线性推进。页尾给出「上一步 / 下一步」。
   activeId 不是流程环节页时返回 ''（项目管理类页面不注入）。
   ---------------------------------------- */
function renderStepNav(activeId) {
  const me = STEP_NAV[activeId];
  if (!me) return '';

  const pips = Array.from({ length: me.stepTotal }, (_, i) =>
    `<span class="sn-pip ${i + 1 < me.sub ? 'done' : i + 1 === me.sub ? 'now' : ''}"></span>`).join('');

  const prevBtn = me.prev
    ? `<a class="btn btn-ghost" href="${stepFile(me.prev)}"><i class="fa-solid fa-arrow-left"></i>上一步</a>`
    : `<a class="btn btn-ghost" href="project-overview.html"><i class="fa-solid fa-arrow-left"></i>返回项目总览</a>`;

  const isLast = !me.next;
  const nextBtn = isLast
    ? `<a class="btn btn-primary" href="report.html">生成项目报告 <i class="fa-solid fa-file-contract"></i></a>`
    : `<a class="btn btn-primary" href="${stepFile(me.next)}">下一步：${STEP_NAV[me.next].label.split('（')[0]} <i class="fa-solid fa-arrow-right"></i></a>`;

  // 关口交接提示：跨关口时点明下一关口序号
  let crossTip = '';
  if (me.next && STEP_NAV[me.next].step !== me.step) {
    crossTip = `<span class="sn-d" style="color:var(--warn)"><i class="fa-solid fa-circle-check"></i> 步骤 ${me.step} 完成，进入步骤 ${STEP_NAV[me.next].step}</span>`;
  }

  return `
  <nav class="stepnav${isLast ? ' is-last' : ''}" aria-label="步骤导航">
    <div class="sn-left">
      <div class="sn-pips" title="步骤 ${me.step} · 环节 ${me.sub}/${me.stepTotal}">${pips}</div>
      <div class="sn-txt">
        <div class="sn-t">步骤 ${me.step} · 环节 ${me.sub}/${me.stepTotal}<span class="code">${me.label.split('（')[0]}</span></div>
        <div class="sn-d"><i class="fa-solid fa-circle-check"></i> 完成条件：${me.done}</div>
        ${crossTip}
      </div>
    </div>
    <div class="sn-act">${prevBtn}${nextBtn}</div>
  </nav>`;
}

/* ---------- 注入侧边栏 ---------- */
function renderSidebar(activeId) {
  const groups = ['main', 'proj', 'loop', 'admin'];

  // 流程页在侧栏已无独立入口，归属高亮到「项目总览」
  const isFlow = !!gateOf(activeId);
  const navActive = isFlow ? 'project-overview' : activeId;

  let html = `
    <a class="brand" href="index.html" title="返回导览页">
      <span class="brand-mark">R</span>
      <span class="brand-text">
        <span class="brand-name">Rednote 内容生产</span>
        <span class="brand-sub">AI Native 系统 V1.0</span>
      </span>
    </a>`;

  groups.forEach(g => {
    const items = PAGES.filter(p => p.group === g);
    if (!items.length) return;
    html += `<div class="nav-group"><div class="nav-title">${GROUP_TITLES[g]}</div>`;
    items.forEach(p => {
      const on = p.id === navActive ? ' is-active' : '';
      const badge = p.badge ? `<span class="n-badge">${p.badge}</span>` : '';
      html += `<a class="nav-item${on}" href="${p.file}"><i class="fa-solid ${p.icon}"></i>${p.name}${badge}</a>`;
    });
    html += `</div>`;
  });

  html += `
    <div class="sidebar-foot">
      <a class="nav-item" href="index.html"><i class="fa-solid fa-table-cells-large"></i>页面导览</a>
      <div class="who mt6" data-pop="user">
        <span class="who-av">林</span>
        <span class="f1">
          <span class="who-name">林知远</span><br>
          <span class="who-role">项目负责人 · 运营组</span>
        </span>
        <i class="fa-solid fa-chevron-up f10 t-fai"></i>
      </div>
    </div>`;

  return html;
}

/* ---------- 注入顶栏 ---------- */
function renderTopbar(opts) {
  const crumbs = (opts.crumb || []).map((c, i, arr) =>
    i === arr.length - 1 ? `<b>${c}</b>` : `<span>${c}</span><span class="sep">/</span>`
  ).join('');

  return `
    <div class="crumb">${crumbs}</div>
    <div class="topbar-right">
      <button class="search" data-pop="cmd" title="全局搜索（⌘K）">
        <i class="fa-solid fa-magnifying-glass"></i>
        <span class="search-ph">搜索项目、内容编号、知识资产…</span>
        <kbd>⌘K</kbd>
      </button>
      <button class="icon-btn" data-pop="notif" title="任务与通知" aria-label="任务与通知"><i class="fa-solid fa-bell"></i><span class="dot"></span></button>
      <button class="icon-btn" data-pop="help" title="帮助" aria-label="帮助"><i class="fa-solid fa-circle-question"></i></button>
    </div>`;
}

/* ============================================================
   全局外壳层 —— 搜索（⌘K）/ 通知中心 / 帮助 / 用户菜单
   演示态：结果与通知均为静态样例，点击只做页面跳转或轻提示。
   ============================================================ */

const SEARCH_RECORDS = [
  { g: '项目',   i: 'fa-folder-open',   n: '素与 Su& 氨基酸洁面',        s: '进行中 · 步骤 5 试产反馈 · 负责人 林知远', f: 'project-overview.html' },
  { g: '项目',   i: 'fa-folder-open',   n: '除湿机选购说明',             s: '进行中 · Loop 2 实验中 EXP-007',          f: 'effect-view.html' },
  { g: '项目',   i: 'fa-folder-open',   n: '露营电源对比',               s: '已结项 · 结项报告 v2',                    f: 'report.html' },
  { g: '项目',   i: 'fa-folder-open',   n: '儿童保温杯',                 s: '待启动 · 未创建生产基线',                 f: 'projects.html' },

  { g: '内容编号', i: 'fa-file-lines',  n: 'C-0037-B01-001',            s: 'T01-04 · 当前稿 v3 · 已通过',             f: 'content-feedback.html' },
  { g: '内容编号', i: 'fa-file-lines',  n: 'C-0037-B01-004',            s: 'T01-07 · 发布稿删段 · 执行不足',          f: 'effect-view.html' },
  { g: '内容编号', i: 'fa-file-lines',  n: 'C-0037-B02-006',            s: 'T01-09 · 未产出 · 未发布',                f: 'scale-batch.html' },
  { g: '内容编号', i: 'fa-layer-group', n: 'BATCH-T01',                 s: '试产批次 · 10 条 · 6 通过',               f: 'content-feedback.html' },

  { g: '知识资产', i: 'fa-flask-vial',  n: 'KP-A02 夜间使用顾虑说明前置', s: '候选 · 未验证 · 不可标记已验证',          f: 'effect-view.html' },
  { g: '知识资产', i: 'fa-book-bookmark', n: 'FAC-03 人群适用范围',      s: '已批准 · 来源 R-014 · 仅覆盖非敏感肌',    f: 'knowledge.html' },
  { g: '知识资产', i: 'fa-book-bookmark', n: 'STY-07 素与 Su& 账号风格', s: '已批准 · v2 · 冷启动配置',                f: 'knowledge.html' },
  { g: '知识资产', i: 'fa-book-bookmark', n: 'KP-B03 无依据体验主张',    s: '待复核 · 由 ISS-017 提出',                f: 'quality-view.html' },

  { g: '审核与修复', i: 'fa-circle-question', n: 'ISS-014 人群扩大主张',  s: '待处置 · 根因已确认 · 生成约束',          f: 'quality-view.html' },
  { g: '审核与修复', i: 'fa-lightbulb',  n: 'PRP-118 修复提案',         s: '待采纳 · 回归 2/4 已出结果',              f: 'quality-view.html' },
  { g: '审核与修复', i: 'fa-flask',      n: 'EXP-007 证据前置实验',     s: '实验中 · 3d 窗口已到 · 长尾未满',         f: 'effect-view.html' },
];

const NOTIFS = [
  { ic: 'fa-circle-question', c: 'warn', t: '需人工确认 · 修复提案待采纳', d: 'PRP-118「人群主张绑定批准证据」回归已出 2/4 类结果，等你确认是否生效', time: '12 分钟前', unread: true, f: 'quality-view.html' },
  { ic: 'fa-triangle-exclamation', c: 'danger', t: '事实纠错 · 已定位根因', d: 'ISS-014 初稿写「敏感肌可放心使用」，最小对照已确认根因在生成约束', time: '38 分钟前', unread: true, f: 'quality-view.html' },
  { ic: 'fa-circle-xmark', c: 'danger', t: '任务失败 · 可重试', d: 'T01-07 媒体生成失败。重试不新建内容编号，原编号保留', time: '1 小时前', unread: true, f: 'tasks.html' },
  { ic: 'fa-link-slash', c: 'warn', t: '数据回收受阻', d: '2 条笔记链接失效，采集暂停。失效不等于零，窗口内保留缺失标记', time: '2 小时前', f: 'data-feedback.html' },
  { ic: 'fa-coins', c: 'warn', t: '预算提醒', d: '素与 Su& 项目本月费用已达预算 82%，剩余可支撑约 40 条', time: '昨天 18:40', f: 'cost.html' },
  { ic: 'fa-user-pen', c: 'info', t: '负责人变更', d: '露营电源对比 主负责人由 张弛 转交 许南嘉，五步流程已完成', time: '昨天 15:02', f: 'members.html' },
  { ic: 'fa-clock', c: 'info', t: '接近截止', d: '素与 Su& 试产反馈 截止 09-21，尚有 3 条待处置（含 1 条阻断）', time: '昨天 09:00', f: 'content-feedback.html' },
  { ic: 'fa-circle-check', c: 'ok', t: '任务完成 · 同批次已合并', d: 'BATCH-T01 生成完成 10 条。同批次结果合并为 1 条，不逐张通知', time: '09-17 20:11', f: 'scale-batch.html' },
];

const HELP_ITEMS = [
  { i: 'fa-diagram-project', t: '八步生产流程', d: '首次生产必须按 1—8 顺序完成关口，后端不允许绕过', f: 'project-overview.html' },
  { i: 'fa-arrows-spin', t: '双 Loop 学习闭环', d: '生产质量视图管「改生成」，效果学习视图管「改策略」，两者互不越界', f: 'quality-view.html' },
  { i: 'fa-book-bookmark', t: '术语与口径', d: 'issue_code 与 root_cause 分离 · 混合暴露 · 三值判优 · 缺失不填 0', toast: '演示环境：术语表含 24 条口径定义与反例' },
  { i: 'fa-keyboard', t: '键盘快捷键', d: '⌘K / Ctrl+K 全局搜索 · Esc 关闭 · ↑↓ 选择 · Enter 打开', toast: '演示环境：搜索结果支持 ↑↓ 选择与 Enter 直接打开' },
  { i: 'fa-comment-dots', t: '反馈与支持', d: '演示环境不连接真实数据；结构问题请反馈给内容运营组', toast: '演示环境：反馈入口不落库' },
];

const NOTIF_CLASS = { ok: 'li-icon ok', warn: 'li-icon warn', danger: 'li-icon danger', info: 'li-icon info' };

/* ---------- 渲染外壳浮层 ---------- */
function renderShell() {
  const pages = PAGES.map(p => ({ g: '页面', i: p.icon, n: p.name, s: '跳转到「' + p.name + '」', f: p.file }));
  const idx = SEARCH_RECORDS.concat(pages);
  const data = JSON.stringify(idx).replace(/</g, '\\u003c');

  const notifRows = NOTIFS.map(n => `
      <a class="nf-it${n.unread ? ' unread' : ''}" href="${n.f}">
        <span class="${NOTIF_CLASS[n.c]}"><i class="fa-solid ${n.ic}"></i></span>
        <span class="nf-main">
          <span class="nf-t">${n.t}</span>
          <span class="nf-d">${n.d}</span>
        </span>
        <span class="nf-time">${n.time}</span>
      </a>`).join('');

  const helpRows = HELP_ITEMS.map(h => {
    const tag = h.f ? 'a' : 'div';
    const attr = h.f ? ` href="${h.f}"` : ` data-toast="${h.toast}"`;
    return `
      <${tag} class="help-it"${attr}>
        <span class="help-ic"><i class="fa-solid ${h.i}"></i></span>
        <span class="f1">
          <span class="help-t">${h.t}</span>
          <span class="help-d">${h.d}</span>
        </span>
      </${tag}>`;
  }).join('');

  return `
  <div class="ovl" id="shOvl"></div>

  <div class="cmd" id="shCmd" role="dialog" aria-label="全局搜索">
    <div class="cmd-in">
      <i class="fa-solid fa-magnifying-glass"></i>
      <input type="text" id="shCmdInput" placeholder="搜索项目、内容编号、知识资产、页面…" autocomplete="off" />
      <span class="kbd">Esc</span>
    </div>
    <div class="cmd-body" id="shCmdBody"></div>
    <div class="cmd-foot">
      <span><span class="kbd">↑</span> <span class="kbd">↓</span> 选择</span>
      <span><span class="kbd">Enter</span> 打开</span>
      <span class="ml-auto">演示环境 · 结果不来自真实数据</span>
    </div>
  </div>

  <div class="pop" id="shNotif" role="dialog" aria-label="任务与通知">
    <div class="pop-head">
      <i class="fa-solid fa-bell t-pur f12"></i>
      <span class="f12 fw6">任务与通知</span>
      <span class="tag tag-danger f10">3 未读</span>
      <span class="ml-auto f10 t-mut">7 类事件 · 同批次合并</span>
    </div>
    <div class="pop-body">${notifRows}</div>
    <div class="pop-foot">
      <a class="btn btn-sm btn-ghost f1" href="tasks.html"><i class="fa-solid fa-list-check"></i>全部待办</a>
      <button class="btn btn-sm btn-ghost f1" data-toast="演示环境：全部标记为已读，不落库"><i class="fa-solid fa-check-double"></i>全部已读</button>
    </div>
  </div>

  <div class="pop" id="shHelp" role="dialog" aria-label="帮助">
    <div class="pop-head">
      <i class="fa-solid fa-circle-question t-pur f12"></i>
      <span class="f12 fw6">帮助</span>
      <span class="ml-auto f10 t-mut">AI Native 系统 V1.0</span>
    </div>
    <div class="pop-body">${helpRows}</div>
  </div>

  <div class="pop pop-sm" id="shUser" role="dialog" aria-label="用户菜单">
    <div class="pop-head">
      <span class="who-av">林</span>
      <span>
        <span class="who-name">林知远</span><br>
        <span class="who-role">项目负责人 · 运营组</span>
      </span>
    </div>
    <div class="menu-it" data-toast="演示环境：个人设置不落库"><i class="fa-solid fa-user-gear"></i>个人设置</div>
    <div class="menu-it" data-toast="演示环境：可切换的项目共 14 个"><i class="fa-solid fa-folder-tree"></i>切换项目</div>
    <div class="menu-it" data-toast="演示环境：通知偏好按 7 类事件单独配置"><i class="fa-solid fa-sliders"></i>通知偏好</div>
    <div class="menu-sep"></div>
    <a class="menu-it" href="index.html"><i class="fa-solid fa-table-cells-large"></i>返回页面导览</a>
    <a class="menu-it" href="index.html"><i class="fa-solid fa-arrow-right-from-bracket"></i>退出登录</a>
  </div>

  <script type="application/json" id="shIdx">${data}</script>`;
}

/* ---------- 外壳交互 ---------- */
function bindShell() {
  const ovl = document.getElementById('shOvl');
  const cmd = document.getElementById('shCmd');
  const input = document.getElementById('shCmdInput');
  const body = document.getElementById('shCmdBody');
  const pops = { notif: document.getElementById('shNotif'), help: document.getElementById('shHelp'), user: document.getElementById('shUser') };
  let idx = [];
  try { idx = JSON.parse(document.getElementById('shIdx').textContent); } catch (e) { idx = []; }
  let sel = 0, cur = [];

  // 浮层定位：顶栏项贴右上角，侧栏用户块贴左下角
  const place = (el, anchor) => {
    el.style.top = el.style.right = el.style.left = el.style.bottom = '';
    const r = anchor.getBoundingClientRect();
    if (anchor.closest('.sidebar')) {
      el.style.left = Math.max(12, r.left) + 'px';
      el.style.bottom = Math.max(12, window.innerHeight - r.top + 8) + 'px';
    } else {
      el.style.right = Math.max(12, window.innerWidth - r.right) + 'px';
      el.style.top = (r.bottom + 10) + 'px';
    }
  };

  const closePops = () => {
    Object.values(pops).forEach(p => p.classList.remove('on'));
    document.querySelectorAll('[data-pop]').forEach(b => { if (b.dataset.pop !== 'cmd') b.classList.remove('is-on'); });
  };

  const closeAll = () => {
    closePops();
    cmd.classList.remove('on');
    ovl.classList.remove('on');
    document.querySelectorAll('[data-pop]').forEach(b => b.classList.remove('is-on'));
  };

  const drawResults = () => {
    const q = input.value.trim().toLowerCase();
    cur = q ? idx.filter(x => (x.n + ' ' + x.s + ' ' + x.g).toLowerCase().includes(q)) : idx.slice(0, 12);
    sel = 0;
    if (!cur.length) {
      body.innerHTML = '<div class="cmd-empty"><i class="fa-solid fa-magnifying-glass"></i><br>没有匹配「' + input.value.trim() + '」的项目、编号或知识资产</div>';
      return;
    }
    let html = '', g = '';
    cur.forEach((x, i) => {
      if (x.g !== g) { g = x.g; html += '<div class="cmd-grp">' + g + '</div>'; }
      html += '<div class="cmd-it' + (i === sel ? ' is-on' : '') + '" data-i="' + i + '">' +
        '<span class="cmd-ci"><i class="fa-solid ' + x.i + '"></i></span>' +
        '<span class="cmd-cm"><span class="cmd-cn">' + x.n + '</span><span class="cmd-cs">' + x.s + '</span></span>' +
        '<i class="fa-solid fa-arrow-right f10 t-fai"></i></div>';
    });
    body.innerHTML = html;
  };

  const move = d => {
    if (!cur.length) return;
    sel = (sel + d + cur.length) % cur.length;
    body.querySelectorAll('.cmd-it').forEach(el => el.classList.toggle('is-on', +el.dataset.i === sel));
    const on = body.querySelector('.cmd-it.is-on');
    if (on) on.scrollIntoView({ block: 'nearest' });
  };

  const go = x => { if (x && x.f) window.location.href = x.f; };

  // 搜索为模态：带全屏遮罩
  const openCmd = anchor => {
    closeAll();
    cmd.classList.add('on'); ovl.classList.add('on');
    if (anchor) anchor.classList.add('is-on');
    input.value = ''; drawResults();
    setTimeout(() => input.focus(), 30);
  };

  // 通知/帮助/用户菜单为非模态浮层：不加遮罩，点外部关闭，便于在顶栏各按钮间直接切换
  const openPop = (key, anchor) => {
    const p = pops[key];
    if (!p) return;
    const wasOn = p.classList.contains('on');
    closeAll();
    if (wasOn) return;
    place(p, anchor); p.classList.add('on'); anchor.classList.add('is-on');
  };

  document.querySelectorAll('[data-pop]').forEach(b => {
    b.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation();
      if (b.dataset.pop === 'cmd') openCmd(b); else openPop(b.dataset.pop, b);
    });
  });

  input.addEventListener('input', drawResults);
  body.addEventListener('click', e => {
    const it = e.target.closest('.cmd-it');
    if (it) go(cur[+it.dataset.i]);
  });
  body.addEventListener('mousemove', e => {
    const it = e.target.closest('.cmd-it');
    if (it && +it.dataset.i !== sel) {
      sel = +it.dataset.i;
      body.querySelectorAll('.cmd-it').forEach(x => x.classList.toggle('is-on', +x.dataset.i === sel));
    }
  });

  cmd.addEventListener('mousedown', e => e.stopPropagation());
  Object.values(pops).forEach(p => {
    p.addEventListener('click', e => { if (e.target.closest('a')) closePops(); });
  });
  ovl.addEventListener('mousedown', closeAll);

  // 非模态浮层的「点外部关闭」
  document.addEventListener('mousedown', e => {
    if (e.target.closest('.pop') || e.target.closest('[data-pop]')) return;
    closePops();
  });

  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault(); openCmd(document.querySelector('[data-pop="cmd"]')); return;
    }
    if (e.key === 'Escape') { closeAll(); return; }
    if (!cmd.classList.contains('on')) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter') { e.preventDefault(); go(cur[sel]); }
  });

  window.addEventListener('resize', closeAll);
}

/* ---------- 页面初始化 ---------- */
function initPage(opts) {
  const sb = document.querySelector('.sidebar');
  const tb = document.querySelector('.topbar');
  if (sb) sb.innerHTML = renderSidebar(opts.active);
  if (tb) tb.innerHTML = renderTopbar(opts);

  // 外壳浮层：搜索（⌘K）/ 通知 / 帮助 / 用户菜单，挂在 body 末尾
  if (!document.getElementById('shCmd')) {
    const sh = document.createElement('div');
    sh.innerHTML = renderShell();
    while (sh.firstElementChild) document.body.appendChild(sh.firstElementChild);
  }

  // 注入页内八步流程条：在 .page-head 之后（无 page-head 则置于 canvas 顶部）
  const bar = renderFlowBar(opts.active);
  if (bar) {
    const canvas = document.querySelector('.canvas');
    if (canvas && !canvas.querySelector('.flowbar')) {
      const head = canvas.querySelector('.page-head');
      const holder = document.createElement('div');
      holder.innerHTML = bar.trim();
      const node = holder.firstElementChild;
      if (head) head.insertAdjacentElement('afterend', node);
      else canvas.insertAdjacentElement('afterbegin', node);
    }
  }

  // 注入页尾步骤导航：一页一环节，线性推进
  const nav = renderStepNav(opts.active);
  if (nav) {
    const canvas = document.querySelector('.canvas');
    if (canvas && !canvas.querySelector('.stepnav')) {
      const holder = document.createElement('div');
      holder.innerHTML = nav.trim();
      canvas.appendChild(holder.firstElementChild);
    }
  }

  // 入场动画错峰
  document.querySelectorAll('[data-stagger] > *').forEach((el, i) => {
    el.classList.add('anim');
    el.style.setProperty('--d', (0.04 * i).toFixed(2) + 's');
  });

  bindDemo();
  bindShell();
}

/* ---------- 演示态交互 ---------- */
function bindDemo() {
  // 开关
  document.querySelectorAll('.switch').forEach(s => {
    s.addEventListener('click', () => s.classList.toggle('is-on'));
  });

  // 复选
  document.querySelectorAll('.check').forEach(c => {
    if (!c.innerHTML.trim()) c.innerHTML = '<i class="fa-solid fa-check"></i>';
    c.addEventListener('click', () => c.classList.toggle('is-on'));
  });

  // 分段控制 / 筛选 chip 组
  document.querySelectorAll('.seg').forEach(seg => {
    seg.querySelectorAll('.seg-item').forEach(it => {
      it.addEventListener('click', () => {
        seg.querySelectorAll('.seg-item').forEach(x => x.classList.remove('is-active'));
        it.classList.add('is-active');
      });
    });
  });
  document.querySelectorAll('[data-chipgroup]').forEach(grp => {
    grp.querySelectorAll('.chip').forEach(ch => {
      ch.addEventListener('click', () => {
        grp.querySelectorAll('.chip').forEach(x => x.classList.remove('is-active'));
        ch.classList.add('is-active');
      });
    });
  });

  // 标签页
  document.querySelectorAll('[data-tabs]').forEach(host => {
    const btns = host.querySelectorAll('[data-tab]');
    btns.forEach(b => {
      b.addEventListener('click', () => {
        const key = b.dataset.tab;
        btns.forEach(x => x.classList.remove('is-active'));
        b.classList.add('is-active');
        document.querySelectorAll(`[data-panel]`).forEach(p => {
          if (p.closest('[data-tabs]') !== host && !host.dataset.tabsScope) {
            // 允许面板在 host 外部
          }
          if (p.dataset.panelGroup === host.dataset.tabs) {
            p.hidden = p.dataset.panel !== key;
          }
        });
      });
    });
  });

  // 数字滚动
  document.querySelectorAll('[data-count]').forEach(el => {
    const target = parseFloat(el.dataset.count);
    const dec = parseInt(el.dataset.dec || '0', 10);
    const pre = el.dataset.pre || '';
    const suf = el.dataset.suf || '';
    let cur = 0;
    const t0 = performance.now();
    const dur = 900;
    function tick(now) {
      const p = Math.min(1, (now - t0) / dur);
      const e = 1 - Math.pow(1 - p, 3);
      cur = target * e;
      el.textContent = pre + cur.toLocaleString('zh-CN', {
        minimumFractionDigits: dec, maximumFractionDigits: dec
      }) + suf;
      if (p < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  });

  // 进度条动画
  document.querySelectorAll('.bar-fill[data-w]').forEach(f => {
    const w = f.dataset.w;
    f.style.width = '0%';
    f.style.transition = 'width 1s cubic-bezier(0.22,1,0.36,1)';
    requestAnimationFrame(() => requestAnimationFrame(() => { f.style.width = w; }));
  });

  // 折叠
  document.querySelectorAll('[data-collapse]').forEach(btn => {
    btn.addEventListener('click', () => {
      const t = document.querySelector(btn.dataset.collapse);
      if (!t) return;
      t.hidden = !t.hidden;
      const ic = btn.querySelector('i.fa-chevron-down, i.fa-chevron-up');
      if (ic) ic.classList.toggle('fa-rotate-180');
    });
  });

  // 演示态按钮：给出明确的“Demo 不落库”反馈
  document.querySelectorAll('[data-toast]').forEach(b => {
    b.addEventListener('click', e => {
      e.preventDefault();
      toast(b.dataset.toast);
    });
  });
}

/* ---------- 轻提示 ---------- */
let toastTimer = null;
function toast(msg) {
  let el = document.querySelector('.toast-host');
  if (!el) {
    el = document.createElement('div');
    el.className = 'toast-host';
    document.body.appendChild(el);
  }
  el.innerHTML = `<div class="toast"><i class="fa-solid fa-circle-info"></i>${msg}</div>`;
  el.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('on'), 2600);
}

/* ---------- 401 登录守卫 ----------
   任何 API 调用拿到 SESSION_EXPIRED ⇒ 跳回登录页。

   为什么放在 app.js 而不是 api.js：api.js 刻意保持 DOM-free
   （`document.` / `window.` 零命中，是它可被 Node 单测的前提），
   而导航是纯 UI 行为。app.js 每个页面都加载、且在所有挂载脚本之前，
   所以在这里包一层既能覆盖全部页面，又不污染接口层。

   为什么包所有方法而不是只包 request：页面用的是 get/post/page/download
   这些门面方法，它们直接闭包调用内部实现，**不经过 API.request** ——
   只包 request 会静默漏掉绝大多数调用。

   防重定向环：登录页自身拿到 401 是正常态（那就是"未登录"），直接放行。
   ---------------------------------------- */
(function installAuthGuard() {
  var API = window.RednoteAPI;
  if (!API || API.__authGuard) return;
  var NAMES = ['request', 'get', 'post', 'put', 'patch', 'del', 'delete',
               'page', 'eachPage', 'download', 'exportZip', 'verifyExport', 'reportMarkdown'];
  NAMES.forEach(function (name) {
    var fn = API[name];
    if (typeof fn !== 'function') return;
    API[name] = function () {
      var p = fn.apply(API, arguments);
      if (!p || typeof p.catch !== 'function') return p;
      return p.catch(function (err) {
        if (err && err.code === 'SESSION_EXPIRED') {
          var here = window.location.pathname.split('/').pop() || 'index.html';
          if (here !== 'index.html') window.location.replace('index.html');
        }
        throw err;
      });
    };
  });
  API.__authGuard = true;
})();
