/* render/projects.test.js —— 项目中心（方案 A 纯函数）Node 单测
 * 运行：node render/projects.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./projects.js');

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

/* 形状与真后端 GET /projects 一致 */
const FIXTURE = [
  {
    id: '41474d88-daaf-55a1-a095-ce83516e8873',
    projectCode: 'PRJ-202609-6Q2ZK7',
    name: '屿见 Yujian 身体乳',
    phase: 'PRODUCTION',
    dueDate: '2026-09-15',
    targetDeliveryCount: 30,
    ownerId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    brandId: '64f93d4f-4a86-5302-b376-9d3182b0cf00',
    productId: '2ddbe922-43f7-55dc-a413-3178f1020e2c'
  },
  {
    id: '930e1700-4aa7-5c94-81f7-4694d4a51084',
    projectCode: 'PRJ-202609-2F6YV8',
    name: '光屿 Glowis 防晒霜',
    phase: 'DATA_COLLECTION',
    dueDate: '2026-09-25',
    targetDeliveryCount: 40,
    ownerId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    brandId: '640a97b6-87a1-5817-b18a-b706ff626a69',
    productId: 'd55faf6e-57f7-5e4a-9be9-0d3e87144c4a'
  },
  {
    id: '5f2d4fec-1d6d-5fa5-9260-e65cf86e0fc8',
    projectCode: 'PRJ-202609-7K3M2Q',
    name: '素与 Su& 氨基酸洁面',
    phase: 'PREPARATION',
    dueDate: '2026-10-20',
    targetDeliveryCount: 30,
    ownerId: '365bcf69-fc8b-5dc1-9144-00cc975daeb8',
    brandId: 'f82846a0-0a88-5323-9c79-b3563d5408ec',
    productId: 'a6f12884-da1b-579a-80de-2752a4f8c15c'
  },
  {
    id: 'f0e769ac-0a1e-5ac8-8f06-1ff58575f25c',
    projectCode: 'PRJ-202609-8M4DT3',
    name: '叁物 SANWU 香薰',
    phase: 'PREPARATION',
    dueDate: '2026-11-22',
    targetDeliveryCount: 18,
    ownerId: 'e3f629d1-384a-507a-8ce0-5c627c6b52f6',
    brandId: '04b0277b-953f-5db7-ae1a-2389eda1dae5',
    productId: '8eefd6fc-0df4-5782-b844-df6b4d84632f'
  }
];

/* ---------------------------- summarize ---------------------------- */

test('summarize: 总数 / 计划交付合计 / 阶段计数', () => {
  const s = R.summarize(FIXTURE);
  assert.equal(s.total, 4);
  assert.equal(s.targetSum, 118);
  assert.equal(s.withTarget, 4);
  assert.equal(s.phases.PREPARATION, 2);
  assert.equal(s.phases.PRODUCTION, 1);
  assert.equal(s.phases.DATA_COLLECTION, 1);
  assert.equal(s.phases.CLOSED, 0);
});

test('summarize: 非数组 / 非法行不抛错', () => {
  assert.equal(R.summarize(null).total, 0);
  assert.equal(R.summarize('oops').total, 0);
  const s = R.summarize([null, 42, FIXTURE[0], undefined]);
  assert.equal(s.total, 1);
});

test('renderSummary: 正常与空列表文案', () => {
  assert.ok(R.renderSummary(FIXTURE).includes('共 4 个项目'));
  assert.ok(R.renderSummary(FIXTURE).includes('计划交付合计 118 条'));
  assert.ok(R.renderSummary([]).includes('暂无可见项目'));
});

test('renderSummary: 缺 targetDeliveryCount 时如实说明', () => {
  const rows = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B', targetDeliveryCount: 10 }];
  const text = R.renderSummary(rows);
  assert.ok(text.includes('计划交付合计 10 条'));
  assert.ok(text.includes('1 项缺该字段'));
});

/* ---------------------------- 正常列表 ---------------------------- */

test('renderProjectTable: 正常列表渲染完整表格结构', () => {
  const html = R.renderProjectTable(FIXTURE);
  assert.ok(html.startsWith('<div class="table-wrap">'));
  assert.ok(html.includes('<table class="tbl">'));
  assert.ok(html.includes('<thead>') && html.includes('<tbody>'));
  assert.ok(html.includes('<th>项目 / 编号</th>'));
  assert.equal(occurrences(html, '<tr'), 5); // 1 表头 + 4 行
});

test('renderProjectRow: 名称 / 编号 / 阶段 / 目标 / 截止 / 负责人 ID', () => {
  const html = R.renderProjectRow(FIXTURE[0]);
  assert.ok(html.includes('屿见 Yujian 身体乳'));
  assert.ok(html.includes('PRJ-202609-6Q2ZK7'));
  assert.ok(html.includes('投产期') && html.includes('tag-info'));
  assert.ok(html.includes('>30<'));
  assert.ok(html.includes('2026-09-15'));
  assert.ok(html.includes('365bcf69…aeb8'));
  assert.ok(html.includes('title="365bcf69-fc8b-5dc1-9144-00cc975daeb8"'));
  assert.ok(html.includes('project-overview.v2.html?id=41474d88-daaf-55a1-a095-ce83516e8873'));
});

test('renderProjectRow: 四种阶段标签映射', () => {
  const cases = [
    ['PREPARATION', '准备期', 'tag-purple'],
    ['PRODUCTION', '投产期', 'tag-info'],
    ['DATA_COLLECTION', '数据回收期', 'tag-ok'],
    ['CLOSED', '已结束', 'tag-muted']
  ];
  cases.forEach(([phase, label, cls]) => {
    const html = R.renderProjectRow({ id: 'x', name: 'n', phase });
    assert.ok(html.includes(label), phase);
    assert.ok(html.includes(cls), phase);
  });
});

/* ---------------------------- 空 / 加载 / 错误 ---------------------------- */

test('renderEmpty: 空态跨列 + 文案', () => {
  const html = R.renderEmpty();
  assert.ok(html.includes('colspan="6"'));
  assert.ok(html.includes('暂无可见项目'));
});

test('renderProjectTable: 空数组 / null / undefined / 非数组都落到空态', () => {
  [null, undefined, [], 'oops'].forEach((input) => {
    assert.ok(R.renderProjectTable(input).includes('暂无可见项目'));
  });
});

test('renderLoading: 保留表头骨架与加载提示', () => {
  const html = R.renderLoading();
  assert.ok(html.includes('<table class="tbl">'));
  assert.ok(html.includes('正在读取项目列表…'));
});

test('renderError: 离线 / 可重试 / 不可重试分支', () => {
  assert.ok(R.renderError({ code: 'DEPENDENCY_UNAVAILABLE', offline: true })
    .includes('127.0.0.1:8787'));
  assert.ok(R.renderError({ code: 'RATE_LIMITED', retryable: true }).includes('该错误可重试'));
  assert.ok(R.renderError({ code: 'PERMISSION_DENIED', retryable: false }).includes('请按错误码排查'));
  assert.ok(R.renderError(null).includes('读取项目列表失败'));
  assert.ok(R.renderError('裸字符串错误').includes('裸字符串错误'));
});

test('renderError: 错误文案与 code 被转义', () => {
  const html = R.renderError({ code: '<b>C</b>', message: '<script>alert(1)</script>' });
  assert.equal(html.includes('<script>'), false);
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(html.includes('&lt;b&gt;C&lt;/b&gt;'));
});

/* ---------------------------- 边界 ---------------------------- */

test('renderProjectRow: 缺字段标「未提供」/「—」，不编造', () => {
  const html = R.renderProjectRow({ id: 'x' });
  assert.ok(html.includes('名称未提供'));
  assert.ok(html.includes('编号未提供'));
  assert.ok(html.includes('未提供')); // 阶段 / 截止
  assert.equal(occurrences(html, '—') >= 2, true); // 计划交付 / 负责人
});

test('renderProjectRow: 未知阶段原样展示，不映射成已有阶段', () => {
  const html = R.renderProjectRow({ id: 'x', name: 'n', phase: 'FROZEN' });
  assert.ok(html.includes('FROZEN'));
  assert.equal(html.includes('tag-purple'), false);
  assert.equal(html.includes('tag-info'), false);
});

test('renderProjectRow: 名称 / 编号里的 HTML 被转义', () => {
  const html = R.renderProjectRow({
    id: 'x',
    name: '<img src=x onerror=alert(1)>',
    projectCode: '"><script>x</script>',
    ownerId: '<b>o</b>'
  });
  assert.equal(html.includes('<img'), false);
  assert.equal(html.includes('<script>'), false);
  assert.ok(html.includes('&lt;img'));
});

test('renderProjectRow: 超长 id 在可见区截断、全量进 title', () => {
  const longId = 'p'.repeat(200);
  const html = R.renderProjectRow({ id: 'x', name: 'n', ownerId: longId });
  assert.ok(!html.includes('>' + longId + '<'));
  assert.ok(html.includes('title="' + longId + '"'));
  assert.ok(html.includes('pppppppp…pppp'));
});

test('renderProjectRow: null / 空对象不抛错', () => {
  assert.doesNotThrow(() => R.renderProjectRow(null));
  assert.doesNotThrow(() => R.renderProjectRow({}));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'projects.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
