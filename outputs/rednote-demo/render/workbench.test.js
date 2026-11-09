/* render/workbench.test.js —— 工作台（方案 A 纯函数）Node 单测
 * 运行：node render/workbench.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./workbench.js');

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('[PASS] ' + name);
  } catch (err) {
    failures.push({ name, err });
    console.log('[FAIL] ' + name + '\n       ' + err.message);
  }
}

function occurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

/* 形状与真后端 GET /work-items 一致 */
const WORK_ITEMS = [
  {
    workItemId: 'f7489eb9-8c09-5814-8950-42aa5101ac49',
    projectId: 'f0e769ac-0a1e-5ac8-8f06-1ff58575f25c',
    assigneeId: 'e3f629d1-384a-507a-8ce0-5c627c6b52f6',
    kind: 'FACT_CORRECTION',
    subjectType: 'PROJECT',
    subjectId: 'f0e769ac-0a1e-5ac8-8f06-1ff58575f25c',
    state: 'OPEN',
    dueAt: '2026-10-05T19:00:00+08:00',
    createdAt: '2026-09-28T10:45:55.113561+08:00'
  },
  {
    workItemId: 'd467d2e1-b8dc-56d7-8b51-c3c54ff09b55',
    projectId: '1b31ccaa-fab8-5577-8acb-0ebd1f2f031d',
    assigneeId: null,
    kind: 'TASK_COMPLETED',
    subjectType: 'PROJECT',
    subjectId: '1b31ccaa-fab8-5577-8acb-0ebd1f2f031d',
    state: 'OPEN',
    dueAt: '2026-10-08T19:00:00+08:00',
    createdAt: '2026-09-28T10:45:55.113561+08:00'
  },
  {
    workItemId: '955e2bc7-2c6c-5e55-b806-563920da5036',
    projectId: '41474d88-daaf-55a1-a095-ce83516e8873',
    assigneeId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    kind: 'DUE_SOON',
    subjectType: 'PROJECT',
    subjectId: '41474d88-daaf-55a1-a095-ce83516e8873',
    state: 'OPEN',
    dueAt: '2026-09-15T19:00:00+08:00',
    createdAt: '2026-09-28T10:45:55.113561+08:00'
  },
  {
    workItemId: '8c44b2c0-6a5e-552c-b6d2-5a956f6dc52f',
    projectId: '930e1700-4aa7-5c94-81f7-4694d4a51084',
    assigneeId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    kind: 'DATA_COLLECTION_BLOCKED',
    subjectType: 'PROJECT',
    subjectId: '930e1700-4aa7-5c94-81f7-4694d4a51084',
    state: 'OPEN',
    dueAt: '2026-09-30T19:00:00+08:00',
    createdAt: '2026-09-28T10:45:55.113561+08:00'
  },
  {
    workItemId: '546dc74b-362a-536f-84f1-9a6279a481bb',
    projectId: 'f0c119af-9ab4-56c2-8101-961012ab88a0',
    assigneeId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    kind: 'TASK_FAILED',
    subjectType: 'FILE',
    subjectId: '0be9b7b2-a1c0-5b6e-bc5c-5b727b2d073f',
    state: 'OPEN',
    dueAt: '2026-09-29T19:00:00+08:00',
    createdAt: '2026-09-28T10:45:55.113561+08:00'
  },
  {
    workItemId: '3c34aa85-2028-59d1-a1da-1e0361a733d0',
    projectId: '41474d88-daaf-55a1-a095-ce83516e8873',
    assigneeId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    kind: 'MANUAL_CONFIRM',
    subjectType: 'PROJECT',
    subjectId: '41474d88-daaf-55a1-a095-ce83516e8873',
    state: 'OPEN',
    dueAt: '2026-09-28T19:00:00+08:00',
    createdAt: '2026-09-28T10:45:55.113561+08:00'
  }
];

/* 形状与真后端 GET /projects 一致（截取 3 个） */
const PROJECTS = [
  {
    id: '41474d88-daaf-55a1-a095-ce83516e8873',
    projectCode: 'PRJ-202609-6Q2ZK7',
    name: '屿见 Yujian 身体乳',
    phase: 'PRODUCTION',
    dueDate: '2026-09-15',
    targetDeliveryCount: 30
  },
  {
    id: '930e1700-4aa7-5c94-81f7-4694d4a51084',
    projectCode: 'PRJ-202609-2F6YV8',
    name: '光屿 Glowis 防晒霜',
    phase: 'DATA_COLLECTION',
    dueDate: '2026-09-25',
    targetDeliveryCount: 40
  },
  {
    id: 'f0e769ac-0a1e-5ac8-8f06-1ff58575f25c',
    projectCode: 'PRJ-202609-8M4DT3',
    name: '叁物 SANWU 香薰',
    phase: 'PREPARATION',
    dueDate: '2026-11-22',
    targetDeliveryCount: 18
  }
];

/* 形状与真后端 GET /audit-events 一致 */
const EVENTS = [
  {
    auditId: '87bec143-72df-5881-882b-a4cd6a30dda6',
    actorKind: 'USER',
    actorId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    action: 'project.member.added',
    objectType: 'PROJECT',
    objectId: 'f0e769ac-0a1e-5ac8-8f06-1ff58575f25c',
    projectId: 'f0e769ac-0a1e-5ac8-8f06-1ff58575f25c',
    changed: true,
    reason: '新增协作者以承接扩产批次。',
    createdAt: '2026-09-20T09:00:00+08:00'
  },
  {
    auditId: 'd3c21f3e-3b62-5007-8ef9-71b6403952f7',
    actorKind: 'SYSTEM',
    actorId: null,
    action: 'parse.scheduled',
    objectType: 'PARSE_RUN',
    objectId: '5f2d4fec-1d6d-5fa5-9260-e65cf86e0fc8',
    projectId: '5f2d4fec-1d6d-5fa5-9260-e65cf86e0fc8',
    changed: false,
    reason: null,
    createdAt: '2026-09-18T17:00:00+08:00'
  }
];

/* ---------------------------- 指标 ---------------------------- */

test('renderMetrics: 待办 OPEN 数 / 可见项目 / 计划交付合计 / 动态数', () => {
  const html = R.renderMetrics({ workItems: WORK_ITEMS, projects: PROJECTS, events: EVENTS });
  assert.equal(occurrences(html, 'class="card metric"'), 3);
  assert.ok(html.includes('待办事项'));
  assert.ok(html.includes('>6<'));
  assert.ok(html.includes('OPEN 6 · 共 6 项'));
  assert.ok(html.includes('可见项目'));
  assert.ok(html.includes('计划交付合计 88 条'));
  assert.ok(html.includes('最近 8 条（GET /audit-events）'));
  assert.ok(html.includes('>2<')); // 动态条数
});

test('renderMetrics: 某类数据缺失时该卡显示 —，不抛错', () => {
  const html = R.renderMetrics({ workItems: null, projects: null, events: null });
  assert.equal(occurrences(html, '<span class="na">—</span>'), 3);
  assert.equal(occurrences(html, '读取失败'), 3);
  assert.doesNotThrow(() => R.renderMetrics(null));
});

test('renderMetrics: 空数组是合法数据（0），不是读取失败', () => {
  const html = R.renderMetrics({ workItems: [], projects: [], events: [] });
  assert.ok(html.includes('OPEN 0 · 共 0 项'));
  assert.ok(html.includes('计划交付合计 0 条'));
  assert.equal(html.includes('读取失败'), false);
});

test('renderMetrics: 缺 targetDeliveryCount 时如实标注缺字段项数', () => {
  const html = R.renderMetrics({
    workItems: [],
    projects: [{ id: 'a', name: 'A' }, { id: 'b', name: 'B', targetDeliveryCount: 10 }],
    events: []
  });
  assert.ok(html.includes('计划交付合计 10 条（1 项缺该字段）'));
});

/* ---------------------------- 待办 ---------------------------- */

test('renderWorkItems: 六类 kind 都映射为中文，不丢行', () => {
  const byId = R.projectIndex(PROJECTS);
  const html = R.renderWorkItems(WORK_ITEMS, byId);
  assert.equal(occurrences(html, 'class="list-item"'), 6);
  ['事实修正', '任务完成确认', '临近截止', '数据采集受阻', '任务失败', '人工确认']
    .forEach((label) => assert.ok(html.includes(label), label));
});

test('renderWorkItem: 项目名 / 截止时间 / 状态 / 指派人 / 入口', () => {
  const byId = R.projectIndex(PROJECTS);
  const html = R.renderWorkItem(WORK_ITEMS[0], byId);
  assert.ok(html.includes('叁物 SANWU 香薰'));
  assert.ok(html.includes('2026-10-05 19:00'));
  assert.ok(html.includes('待处理') && html.includes('tag-warn'));
  assert.ok(html.includes('e3f629d1…52f6'));
  assert.ok(html.includes('project-overview.v2.html?id=f0e769ac-0a1e-5ac8-8f06-1ff58575f25c'));
});

test('renderWorkItem: 未分配 → 「未指派」，未知项目 → 短 ID', () => {
  const html = R.renderWorkItem(WORK_ITEMS[1], {});
  assert.ok(html.includes('未指派'));
  assert.ok(html.includes('1b31ccaa…031d'));
});

test('renderWorkItem: 未知 kind 显示「其他待办」+ 原始取值', () => {
  const html = R.renderWorkItem({ kind: 'NEW_KIND', state: 'OPEN', projectId: 'x' }, {});
  assert.ok(html.includes('其他待办（NEW_KIND）'));
});

test('renderWorkItems: 空数组 / 非数组 → 空态', () => {
  [null, [], 'oops'].forEach((input) => {
    assert.ok(R.renderWorkItems(input, {}).includes('暂无待办事项'));
  });
});

/* ---------------------------- 项目 ---------------------------- */

test('renderProjectsTable: 名称 / 编号 / 阶段 / 目标 / 截止 / 入口', () => {
  const html = R.renderProjectsTable(PROJECTS);
  assert.ok(html.includes('<table class="tbl tbl-compact">'));
  assert.ok(html.includes('屿见 Yujian 身体乳'));
  assert.ok(html.includes('PRJ-202609-6Q2ZK7'));
  assert.ok(html.includes('数据回收期'));
  assert.ok(html.includes('2026-11-22'));
  assert.ok(html.includes('project-overview.v2.html?id=930e1700-4aa7-5c94-81f7-4694d4a51084'));
});

test('renderProjectsTable: 空列表 / 非数组 → 空态', () => {
  [null, [], 42].forEach((input) => {
    assert.ok(R.renderProjectsTable(input).includes('暂无可见项目'));
  });
});

/* ---------------------------- 动态 ---------------------------- */

test('renderActivity: action 中文映射 / 时间 / 项目名 / 操作者 / 理由', () => {
  const byId = R.projectIndex(PROJECTS);
  const html = R.renderActivity(EVENTS, byId);
  assert.equal(occurrences(html, 'class="tl-item '), 2);
  assert.ok(html.includes('新增项目成员'));
  assert.ok(html.includes('2026-09-20 09:00'));
  assert.ok(html.includes('叁物 SANWU 香薰'));
  assert.ok(html.includes('成员'));
  assert.ok(html.includes('新增协作者以承接扩产批次。'));
  assert.ok(html.includes('资料解析已排期'));
  assert.ok(html.includes('系统'));
});

test('renderActivity: 未知 action 原样展示，不吞事件', () => {
  const html = R.renderActivity([{ action: 'custom.action', actorKind: 'USER', createdAt: '2026-10-01T10:00:00+08:00' }], {});
  assert.ok(html.includes('custom.action'));
});

test('renderActivity: 空数组 / 非数组 → 空态', () => {
  [null, [], undefined].forEach((input) => {
    assert.ok(R.renderActivity(input, {}).includes('暂无近期动态'));
  });
});

/* ---------------------------- 加载 / 错误 / 边界 ---------------------------- */

test('renderBlockLoading / renderBlockEmpty: 文案', () => {
  assert.ok(R.renderBlockLoading('读取待办…').includes('读取待办…'));
  const empty = R.renderBlockEmpty('空', '副标题');
  assert.ok(empty.includes('空') && empty.includes('副标题'));
});

test('renderBlockError: 离线 / 可重试 / 不可重试 / null', () => {
  assert.ok(R.renderBlockError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true }).includes('127.0.0.1:8787'));
  assert.ok(R.renderBlockError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderBlockError({ code: 'PERMISSION_DENIED', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderBlockError(null).includes('读取失败'));
  assert.ok(R.renderBlockError('裸字符串错误').includes('裸字符串错误'));
  assert.equal(occurrences(R.renderBlockError(null), 'data-action="retry"'), 1);
});

test('renderBlockError: 错误文案里的 HTML 被转义', () => {
  const html = R.renderBlockError({ message: '<script>alert(1)</script>' });
  assert.equal(html.includes('<script>'), false);
  assert.ok(html.includes('&lt;script&gt;'));
});

test('renderWorkItem: 标题 / 指派 ID 里的 HTML 被转义', () => {
  const html = R.renderWorkItem({
    kind: '<img src=x>',
    state: 'OPEN',
    projectId: '"><script>x</script>',
    assigneeId: '"><b>y</b>'
  }, {});
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<script>'), false);
  assert.equal(html.includes('<b>y</b>'), false);
});

test('projectIndex: 只索引有 id 的对象，非数组安全', () => {
  const index = R.projectIndex([PROJECTS[0], null, {}, { id: 'x', name: 'X' }]);
  assert.equal(Object.keys(index).length, 2);
  assert.equal(index['x'].name, 'X');
  assert.deepEqual(R.projectIndex(null), {});
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'workbench.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
