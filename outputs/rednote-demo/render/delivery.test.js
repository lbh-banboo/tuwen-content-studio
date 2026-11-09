/* render/delivery.test.js —— 渲染层（方案 A 纯函数）Node 单测
 * 运行：node render/delivery.test.js   （零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./delivery.js');

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

/* 形状与 tools/dev/mock_m4/samples.json 的 deliveries 一致 */
const FIXTURE = [
  {
    id: '11111111-1111-4111-8111-111111111111',
    projectId: '00000000-0000-4000-8000-000000000001',
    kocRecordId: 'aaaaaaaa-1111-4111-8111-111111111111',
    contentItemId: 'cccccccc-1111-4111-8111-111111111111',
    versionNodeId: 'vvvvvvvv-1111-4111-8111-111111111111',
    state: 'PREPARED',
    eligibilityHash: 'sha256:abcd1234',
    replacesDeliveryId: null,
    deliveredAt: null,
    cancelledAt: null
  },
  {
    id: '22222222-2222-4222-8222-222222222222',
    projectId: '00000000-0000-4000-8000-000000000001',
    kocRecordId: 'aaaaaaaa-9999-4999-8999-999999999999',
    contentItemId: 'cccccccc-2222-4222-8222-222222222222',
    versionNodeId: 'vvvvvvvv-2222-4222-8222-222222222222',
    state: 'DELIVERED',
    eligibilityHash: 'sha256:ef567890',
    replacesDeliveryId: null,
    deliveredAt: '2026-10-07T10:00:00+08:00',
    cancelledAt: null
  },
  {
    id: '33333333-3333-4333-8333-333333333333',
    projectId: '00000000-0000-4000-8000-000000000001',
    kocRecordId: 'bbbbbbbb-1111-4111-8111-111111111111',
    contentItemId: 'cccccccc-2222-4222-8222-222222222222',
    versionNodeId: 'vvvvvvvv-3333-4333-8333-333333333333',
    state: 'DELIVERED',
    eligibilityHash: 'sha256:aa99',
    replacesDeliveryId: null,
    deliveredAt: '2026-10-08T09:30:00+08:00',
    cancelledAt: null
  }
];

/* ---------------------------- groupDeliveries ---------------------------- */

test('groupDeliveries: 按 contentItemId 聚合，保持出现顺序', () => {
  const groups = R.groupDeliveries(FIXTURE);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].contentItemId, 'cccccccc-1111-4111-8111-111111111111');
  assert.equal(groups[0].records.length, 1);
  assert.equal(groups[1].records.length, 2);
});

test('groupDeliveries: 非数组输入 / 非对象行不抛错、不丢合法行', () => {
  assert.deepEqual(R.groupDeliveries(null), []);
  assert.deepEqual(R.groupDeliveries('oops'), []);
  const groups = R.groupDeliveries([null, 42, FIXTURE[0], undefined]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].records.length, 1);
});

test('groupDeliveries: 缺 contentItemId 时按单条记录成组', () => {
  const groups = R.groupDeliveries([
    { id: 'a1', state: 'DELIVERED' },
    { id: 'a2', state: 'PREPARED' }
  ]);
  assert.equal(groups.length, 2);
});

/* ---------------------------- 正常列表 ---------------------------- */

test('renderDeliveryList: 正常列表渲染完整表格结构', () => {
  const html = R.renderDeliveryList(R.groupDeliveries(FIXTURE));
  assert.ok(html.startsWith('<div class="table-wrap">'));
  assert.ok(html.includes('<table class="tbl tbl-compact">'));
  assert.ok(html.includes('<thead>') && html.includes('<tbody>'));
  assert.ok(html.includes('<th>内容</th>'));
  assert.equal(occurrences(html, '<tr'), 3); // 1 表头 + 2 分组行
});

test('renderDeliveryRow: 单元格值来自数据（短 id / 计数 / KOC / 状态 / 时间）', () => {
  const html = R.renderDeliveryRow(R.groupDeliveries(FIXTURE)[1]);
  assert.ok(html.includes('cccccccc…2222'));              // 短 id
  assert.ok(html.includes('title="cccccccc-2222-4222-8222-222222222222"')); // 全量在 title
  assert.ok(html.includes('<td class="num mono">2</td>')); // 交付记录数
  assert.ok(html.includes('aaaaaaaa…9999 <span class="t-mut">+1</span>')); // 2 位 KOC
  assert.ok(html.includes('已交付 2'));                    // 状态 + 计数
  assert.ok(html.includes('2026-10-08 09:30'));            // 取最新 deliveredAt
});

test('renderDeliveryRow: 行色 —— 有待交付 tr-warn，有已取消 tr-danger，全部已交付无标记', () => {
  const groups = R.groupDeliveries(FIXTURE);
  assert.ok(R.renderDeliveryRow(groups[0]).startsWith('<tr class="tr-warn">'));
  assert.ok(R.renderDeliveryRow(groups[1]).startsWith('<tr>'));

  const cancelled = R.groupDeliveries([
    { id: 'x', contentItemId: 'c-x', state: 'CANCELLED' },
    { id: 'y', contentItemId: 'c-x', state: 'DELIVERED' }
  ])[0];
  assert.ok(R.renderDeliveryRow(cancelled).startsWith('<tr class="tr-danger">'));
});

/* ---------------------------- 空态 / 加载 / 错误态 ---------------------------- */

test('renderDeliveryEmpty: 空态跨列 + 文案', () => {
  const html = R.renderDeliveryEmpty();
  assert.ok(html.includes('colspan="6"'));
  assert.ok(html.includes('暂无交付记录'));
});

test('renderDeliveryList: 空数组与非法输入都落到空态', () => {
  assert.ok(R.renderDeliveryList([]).includes('暂无交付记录'));
  assert.ok(R.renderDeliveryList(null).includes('暂无交付记录'));
  assert.ok(R.renderDeliveryList(undefined).includes('暂无交付记录'));
});

test('renderDeliveryLoading: 加载态含表格与提示', () => {
  const html = R.renderDeliveryLoading();
  assert.ok(html.includes('<table class="tbl tbl-compact">'));
  assert.ok(html.includes('正在读取交付记录…'));
});

test('renderDeliveryError: 离线错误展示 code / message / 重试按钮', () => {
  const html = R.renderDeliveryError({
    code: 'DEPENDENCY_UNAVAILABLE',
    message: '连不上后端（network_error），且本页未登记静态样例。',
    offline: true
  });
  assert.ok(html.includes('tr-danger'));
  assert.ok(html.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(html.includes('连不上后端'));
  assert.ok(html.includes('data-action="retry"'));
});

test('renderDeliveryError: 可重试 / 不可重试提示分支', () => {
  assert.ok(R.renderDeliveryError({ code: 'RATE_LIMITED', retryable: true })
    .includes('该错误可重试'));
  assert.ok(R.renderDeliveryError({ code: 'RESOURCE_NOT_FOUND', retryable: false })
    .includes('请按错误码排查'));
  assert.ok(R.renderDeliveryError('裸字符串错误').includes('裸字符串错误'));
  assert.ok(R.renderDeliveryError(null).includes('读取交付记录失败'));
});

test('renderDeliveryError: 错误信息里的 HTML 被转义（防注入）', () => {
  const html = R.renderDeliveryError({ message: '<script>alert(1)</script>' });
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;'));
});

/* ---------------------------- 边界 ---------------------------- */

test('renderDeliveryRow: 空分组 / 空记录不抛错，计数为 0，时间显示 —', () => {
  const html = R.renderDeliveryRow({ contentItemId: 'c-empty', records: [] });
  assert.ok(html.includes('<td class="num mono">0</td>'));
  assert.ok(html.includes('—'));
  assert.doesNotThrow(() => R.renderDeliveryRow(null));
  assert.doesNotThrow(() => R.renderDeliveryRow({}));
});

test('renderDeliveryRow: 超长 id 截断显示、全量进 title', () => {
  const longId = 'x'.repeat(200);
  const group = R.groupDeliveries([{ id: 'z', contentItemId: longId, state: 'DELIVERED' }])[0];
  const html = R.renderDeliveryRow(group);
  assert.ok(!html.includes('>' + longId + '<')); // 可见区不输出全量
  assert.ok(html.includes('title="' + longId + '"'));
  assert.ok(html.includes('xxxxxxxx…xxxx'));
});

test('renderDeliveryRow: id 字段里的 HTML 特殊字符被转义', () => {
  const group = R.groupDeliveries([{
    id: 'z',
    contentItemId: '"><img src=x onerror=alert(1)>',
    state: 'DELIVERED'
  }])[0];
  const html = R.renderDeliveryRow(group);
  assert.ok(!html.includes('"><img'));
  assert.ok(html.includes('&quot;&gt;&lt;img'));
});

test('renderDeliveryRow: 未知状态降级为中性 tag，不映射成已有状态', () => {
  const html = R.renderDeliveryRow(R.groupDeliveries([
    { id: 'z', contentItemId: 'c-u', state: 'FROZEN' }
  ])[0]);
  assert.ok(html.includes('FROZEN'));
  assert.ok(!html.includes('tag-ok') && !html.includes('tag-warn') && !html.includes('tag-danger'));
  assert.ok(html.startsWith('<tr>')); // 未知状态不带行色
});

test('renderDeliveryRow: 状态缺失显示「状态未标注」', () => {
  const html = R.renderDeliveryRow(R.groupDeliveries([
    { id: 'z', contentItemId: 'c-n' }
  ])[0]);
  assert.ok(html.includes('状态未标注'));
});

/* ---------------------------- 纯度（机械证明） ---------------------------- */

test('模块源码不含任何 DOM / 浏览器全局读取（document. / window. 零命中）', () => {
  const src = fs.readFileSync(path.join(__dirname, 'delivery.js'), 'utf8');
  assert.equal(/document\.|window\./.test(src), false);
});

/* ---------------------------- 汇总 ---------------------------- */

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) {
  process.exitCode = 1;
}
