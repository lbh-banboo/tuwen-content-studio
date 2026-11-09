/* render/data-snapshot.test.js —— 指标最新视图 / 快照历史 Node 单测
 * 运行：node render/data-snapshot.test.js（零依赖，退出码 0=全绿 1=有失败）
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const R = require('./data-snapshot.js');

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

/* 形状与 mock samples.json 的 metric_* 一致，额外补一条更早观测用于时间差 */
const DEFS = [
  { id: 'def-like', metricCode: 'LIKE_COUNT', scope: 'CUMULATIVE', valueKind: 'NUMERIC', unit: '次', label: '点赞数' },
  { id: 'def-view', metricCode: 'VIEW_COUNT', scope: 'LAST_24H', valueKind: 'NUMERIC', unit: '次', label: '近 24h 浏览量' },
  { id: 'def-url', metricCode: 'NOTE_URL', scope: 'NOT_APPLICABLE', valueKind: 'TEXT', unit: null, label: '笔记链接' }
];

const LATEST = [
  {
    publicationId: 'pub-1',
    metricDefinitionId: 'def-view',
    observationId: 'obs-view',
    valueNum: null,
    valueText: null,
    observedAt: '2026-10-07T18:00:00+08:00',
    label: 'PENDING',
    rebuiltAt: '2026-10-07T20:35:00+08:00'
  },
  {
    publicationId: 'pub-1',
    metricDefinitionId: 'def-like',
    observationId: 'obs-like',
    valueNum: 12000,
    valueText: null,
    observedAt: '2026-10-07T18:00:00+08:00',
    label: 'QUALITY',
    rebuiltAt: '2026-10-07T20:35:00+08:00'
  },
  {
    publicationId: 'pub-1',
    metricDefinitionId: 'def-url',
    observationId: 'obs-url',
    valueNum: null,
    valueText: 'https://www.xiaohongshu.com/explore/abc123',
    observedAt: '2026-10-07T16:30:00+08:00',
    label: 'PENDING',
    rebuiltAt: '2026-10-07T20:35:00+08:00'
  }
];

const OBS = [
  {
    id: 'obs-like',
    publicationId: 'pub-1',
    metricDefinitionId: 'def-like',
    observedAt: '2026-10-07T18:00:00+08:00',
    valueNum: 12000,
    valueText: null,
    rawValue: '1.2 万',
    precision: 'APPROXIMATE',
    availability: 'ACQUIRED',
    sourceType: 'MANUAL',
    evidenceRef: 'WP03-shots/F02-like-20261007.png',
    importId: null,
    recordedAt: '2026-10-07T20:30:00+08:00'
  },
  {
    id: 'obs-view',
    publicationId: 'pub-1',
    metricDefinitionId: 'def-view',
    observedAt: '2026-10-07T18:00:00+08:00',
    valueNum: null,
    valueText: null,
    rawValue: null,
    precision: null,
    availability: 'NO_PERMISSION',
    sourceType: 'MANUAL',
    evidenceRef: null,
    importId: null,
    recordedAt: '2026-10-07T20:31:00+08:00'
  },
  {
    id: 'obs-url',
    publicationId: 'pub-1',
    metricDefinitionId: 'def-url',
    observedAt: '2026-10-07T16:30:00+08:00',
    valueNum: null,
    valueText: 'https://www.xiaohongshu.com/explore/abc123',
    rawValue: null,
    precision: null,
    availability: 'ACQUIRED',
    sourceType: 'AUTO_API',
    evidenceRef: null,
    importId: null,
    recordedAt: '2026-10-07T16:31:00+08:00'
  }
];

/* ---------------------------- 最新视图 ---------------------------- */

test('buildMetricView: 合并 definitions + latest + observations，按定义顺序排序', () => {
  const view = R.buildMetricView(DEFS, LATEST, OBS);
  assert.equal(view.length, 3);
  assert.equal(view[0].label, '点赞数');
  assert.equal(view[1].label, '近 24h 浏览量');
  assert.equal(view[2].label, '笔记链接');
  assert.equal(view[0].availability, 'ACQUIRED');
  assert.equal(view[0].precision, 'APPROXIMATE');
  assert.equal(view[0].rawValue, '1.2 万');
  assert.equal(view[1].availability, 'NO_PERMISSION');
});

test('renderMetricTable: 正常行展示值 / 状态 / 时间 / 时间差 / 来源 / 口径', () => {
  const html = R.renderMetricTable(R.buildMetricView(DEFS, LATEST, OBS), { id: 'pub-1' });
  assert.ok(html.includes('点赞数'));
  assert.ok(html.includes('12,000'));
  assert.ok(html.includes('约数 · 原值 &quot;1.2 万&quot;'));
  assert.ok(html.includes('已获取'));
  assert.ok(html.includes('人工录入'));
  assert.ok(html.includes('2026-10-07 18:00'));
  assert.ok(html.includes('累计值 · 数值 · 单位 次'));
  assert.ok(html.includes('-1 小时 30 分'));
  assert.equal(occurrences(html, '<tr'), 4); // 表头 + 3 行
});

test('renderMetricTable: 缺失状态显示 — 而不是 0', () => {
  const html = R.renderMetricTable(R.buildMetricView(DEFS, LATEST, OBS), { id: 'pub-1' });
  assert.ok(html.includes('无权限'));
  assert.ok(html.includes('<div class="f14 fw6 mono t-warn">—</div>'));
  const view = R.buildMetricView(DEFS, LATEST, OBS);
  assert.notEqual(view[1].valueNum, 0);
});

test('renderMetricTable: 业务标签映射（QUALITY → 优质）', () => {
  const html = R.renderMetricTable(R.buildMetricView(DEFS, LATEST, OBS), { id: 'pub-1' });
  assert.ok(html.includes('优质'));
});

test('renderMetricTable: 空视图 / 非数组输入落到空态', () => {
  assert.ok(R.renderMetricTable([], null).includes('暂无指标快照'));
  assert.ok(R.renderMetricTable(null, null).includes('暂无指标快照'));
  assert.ok(R.renderMetricTable(null, { id: 'pub-1' }).includes('该发布记录暂无最新指标投影'));
});

test('renderMetricLoading / renderMetricError: 加载与错误态', () => {
  assert.ok(R.renderMetricLoading().includes('正在读取指标最新视图…'));
  const err = R.renderMetricError({ code: 'DEPENDENCY_UNAVAILABLE', message: '连不上后端。', offline: true });
  assert.ok(err.includes('DEPENDENCY_UNAVAILABLE'));
  assert.ok(err.includes('data-action="retry"'));
  assert.ok(R.renderMetricError(null).includes('读取指标快照失败'));
});

/* ---------------------------- 快照历史 ---------------------------- */

test('groupObservationsByTime: 按 observedAt 分组并按时间倒序', () => {
  const groups = R.groupObservationsByTime(OBS);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].observedAt, '2026-10-07T18:00:00+08:00');
  assert.equal(groups[0].observations.length, 2);
  assert.equal(groups[1].observations.length, 1);
});

test('renderSnapshotHistory: 最新 / 历史两栏与指标值', () => {
  const groups = R.groupObservationsByTime(OBS);
  const html = R.renderSnapshotHistory(groups, DEFS);
  assert.ok(html.includes('最新快照'));
  assert.ok(html.includes('历史快照'));
  assert.ok(html.includes('1 次写入'));
  assert.ok(html.includes('点赞数'));
  assert.ok(html.includes('12,000'));
  assert.ok(html.includes('无权限'));
  assert.ok(html.includes('人工录入'));
  assert.ok(html.includes('自动采集'));
});

test('renderSnapshotHistory: 只有一组时历史栏显示暂无更早快照', () => {
  const one = [OBS[0]];
  const html = R.renderSnapshotHistory(R.groupObservationsByTime(one), DEFS);
  assert.ok(html.includes('暂无更早快照'));
});

test('renderSnapshotHistory: 空 / 加载 / 错误态', () => {
  assert.ok(R.renderSnapshotHistory([], DEFS).includes('暂无快照记录'));
  assert.ok(R.renderSnapshotHistoryLoading().includes('正在读取快照历史…'));
  const err = R.renderSnapshotHistoryError({ message: '<script>alert(1)</script>' });
  assert.ok(!err.includes('<script>'));
  assert.ok(err.includes('&lt;script&gt;'));
  assert.ok(err.includes('data-action="retry"'));
});

/* ---------------------------- 边界 ---------------------------- */

test('buildMetricView: 缺定义 / 缺观测 / 缺字段不抛错', () => {
  assert.doesNotThrow(() => R.buildMetricView(null, null, null));
  const view = R.buildMetricView([], [{ metricDefinitionId: 'missing', observationId: 'none' }], []);
  assert.equal(view.length, 1);
  assert.equal(view[0].label, '未命名指标');
  assert.equal(view[0].availability, '');
  assert.equal(view[0].hasObservation, false);
});

test('renderMetricTable: 未知 availability / sourceType / scope 降级为原始文本', () => {
  const latest = [{ metricDefinitionId: 'd', observationId: 'o', valueNum: 1, observedAt: '2026-10-07T18:00:00+08:00', label: 'WHAT' }];
  const obs = [{ id: 'o', availability: 'STRANGE', sourceType: 'ROBOT', precision: null }];
  const defs = [{ id: 'd', label: '指标', metricCode: 'X', scope: 'WEIRD', valueKind: 'NUMERIC', unit: null }];
  const html = R.renderMetricTable(R.buildMetricView(defs, latest, obs), null);
  assert.ok(html.includes('STRANGE'));
  assert.ok(html.includes('ROBOT'));
  assert.ok(html.includes('WEIRD'));
  assert.ok(html.includes('WHAT'));
});

test('renderMetricTable / history: 超长文本与 XSS 被转义 / 截断', () => {
  const longText = 'A'.repeat(200);
  const latest = [{ metricDefinitionId: 'd', observationId: 'o', valueNum: null, valueText: longText, observedAt: '2026-10-07T18:00:00+08:00', label: null }];
  const obs = [{ id: 'o', availability: 'ACQUIRED', sourceType: 'MANUAL', valueText: longText, precision: null }];
  const defs = [{ id: 'd', label: '<img src=x onerror=alert(1)>', metricCode: 'X', scope: 'CUMULATIVE', valueKind: 'TEXT', unit: null }];

  const table = R.renderMetricTable(R.buildMetricView(defs, latest, obs), null);
  assert.ok(!table.includes('<img'));
  assert.ok(table.includes('&lt;img'));

  const malicious = [{
    id: 'o2', publicationId: 'p', metricDefinitionId: 'd',
    observedAt: '2026-10-07T18:00:00+08:00',
    valueNum: null, valueText: '<script>alert(1)</script>',
    rawValue: '<b>x</b>', precision: 'APPROXIMATE',
    availability: 'ACQUIRED', sourceType: 'MANUAL',
    evidenceRef: '<script>alert(2)</script>', recordedAt: '2026-10-07T18:01:00+08:00'
  }];
  const history = R.renderSnapshotHistory(R.groupObservationsByTime(malicious), defs);
  assert.ok(!history.includes('<script>'));
  assert.ok(history.includes('&lt;script&gt;'));
});

test('源码不含 DOM / 浏览器全局读取（data-snapshot.js + shared.js）', () => {
  ['data-snapshot.js', 'shared.js'].forEach(function (file) {
    const src = fs.readFileSync(path.join(__dirname, file), 'utf8');
    assert.equal(/document\.|window\./.test(src), false, file + ' 命中了 DOM 全局');
  });
});

console.log('');
console.log('RESULT: ' + passed + '/' + (passed + failures.length) + ' PASS');
if (failures.length) process.exitCode = 1;
