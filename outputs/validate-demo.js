#!/usr/bin/env node
/**
 * validate-demo.js — bundle 前置校验器（零依赖）。
 *
 * 逐项输出 [PASS]/[FAIL]，全部通过才 exit 0：
 *  1. 链接完整性：每个 .html 的 href/src/poster 指向的本地文件都存在。
 *  2. 资源存在性：api.js、render 模块、assets 文件都在；render 里没有测试文件。
 *  3. 纯度抽查：bundle 里每个 render/*.js 的 document. / window. 零命中。
 *  4. 接线页替换：源目录每个 <page>.v2.html 都在 bundle 里变成了 <page>.html（逐字节比对）。
 *  5. bundle 里没有 *.v2.html。
 *
 * 用法：node validate-demo.js
 */

'use strict';

const fs = require('fs');
const path = require('path');

const OUTPUTS = __dirname;
const SRC = path.join(OUTPUTS, 'rednote-demo');
const BUNDLE = path.join(OUTPUTS, 'demo-build');

let pass = 0;
let fail = 0;
const failures = [];

function ok(label, detail) {
  pass += 1;
  console.log('[PASS] ' + label + (detail ? ' — ' + detail : ''));
}

function bad(label, detail) {
  fail += 1;
  failures.push(label + (detail ? ' — ' + detail : ''));
  console.log('[FAIL] ' + label + (detail ? ' — ' + detail : ''));
}

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(p, out);
    else if (entry.isFile()) out.push(p);
  }
  return out;
}

function rel(p) {
  return path.relative(BUNDLE, p).split(path.sep).join('/');
}

function isExternal(ref) {
  return /^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i.test(ref);
}

function transformV2Refs(text) {
  return text.replace(/\.v2\.html/g, '.html');
}

function main() {
  if (!fs.existsSync(BUNDLE)) {
    console.log('[FAIL] bundle 不存在：' + BUNDLE + '（先运行 node build-demo.js）');
    process.exit(1);
  }

  const allFiles = walk(BUNDLE);
  const htmlFiles = allFiles.filter((f) => f.endsWith('.html'));
  const renderFiles = allFiles.filter((f) => {
    const r = rel(f);
    return r.startsWith('render/') && r.endsWith('.js');
  });

  // ---------------------------------------------------------------- 1. 链接完整性
  const dangling = [];
  const localRefs = [];
  const attrRe = /(?:href|src|poster)\s*=\s*"([^"]*)"/gi;
  for (const file of htmlFiles) {
    const html = fs.readFileSync(file, 'utf8');
    let m;
    while ((m = attrRe.exec(html)) !== null) {
      const ref = m[1].trim();
      if (!ref || isExternal(ref)) continue;
      const clean = ref.split('#')[0].split('?')[0];
      if (!clean) continue;
      const target = path.resolve(path.dirname(file), clean);
      localRefs.push({ file, ref, target });
      if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
        dangling.push(rel(file) + ' -> ' + ref);
      }
    }
  }
  if (dangling.length) bad('链接完整性', dangling.length + ' 条悬空引用：' + dangling.join(' | '));
  else ok('链接完整性', htmlFiles.length + ' 个 HTML / ' + localRefs.length + ' 条本地引用，零悬空');

  // ---------------------------------------------------------------- 2. 资源存在性
  const apiJs = path.join(BUNDLE, 'api.js');
  if (fs.existsSync(apiJs) && fs.statSync(apiJs).isFile()) ok('资源存在性 · api.js');
  else bad('资源存在性 · api.js', 'bundle 根目录缺少 api.js');

  const referencedRender = new Set(
    localRefs
      .map((r) => rel(r.target))
      .filter((r) => r.startsWith('render/'))
  );
  const missingRender = [];
  for (const modulePath of referencedRender) {
    if (!fs.existsSync(path.join(BUNDLE, modulePath))) missingRender.push(modulePath);
  }
  if (missingRender.length) bad('资源存在性 · render 模块', '缺少：' + missingRender.join(', '));
  else if (referencedRender.size === 0) bad('资源存在性 · render 模块', '没有任何页面引用 render/*.js');
  else ok('资源存在性 · render 模块', referencedRender.size + ' 个被引用模块全部存在');

  const renderTests = renderFiles.map(rel).filter((r) => /\.test\.js$/.test(r));
  if (renderTests.length) bad('资源存在性 · render 测试文件', 'bundle 不应包含：' + renderTests.join(', '));
  else ok('资源存在性 · render 测试文件', '0 个 .test.js');
  if (renderFiles.length) ok('资源存在性 · render 运行时模块', renderFiles.length + ' 个 .js');

  const assetRefs = localRefs.map((r) => rel(r.target)).filter((r) => r.startsWith('assets/'));
  const missingAssets = assetRefs.filter((r) => !fs.existsSync(path.join(BUNDLE, r)));
  if (missingAssets.length) bad('资源存在性 · assets', '缺少：' + missingAssets.join(', '));
  else ok('资源存在性 · assets', assetRefs.length + ' 个被引用资源全部存在');

  // ---------------------------------------------------------------- 3. 纯度抽查
  const impure = [];
  let purityHits = 0;
  for (const file of renderFiles) {
    const src = fs.readFileSync(file, 'utf8');
    const hits = (src.match(/\b(?:document|window)\s*\./g) || []).length;
    if (hits > 0) {
      impure.push(rel(file) + ' (' + hits + ')');
      purityHits += hits;
    }
  }
  if (impure.length) bad('纯度抽查 · render/*.js', 'document./window. 命中：' + impure.join(', '));
  else ok('纯度抽查 · render/*.js', renderFiles.length + ' 个文件，document./window. 零命中');

  // ---------------------------------------------------------------- 4. 接线页替换
  const srcV2 = fs.readdirSync(SRC)
    .filter((n) => n.endsWith('.v2.html'))
    .map((n) => n.slice(0, -'.v2.html'.length))
    .sort();
  const mismatch = [];
  for (const page of srcV2) {
    const expected = transformV2Refs(fs.readFileSync(path.join(SRC, page + '.v2.html'), 'utf8'));
    const target = path.join(BUNDLE, page + '.html');
    if (!fs.existsSync(target)) mismatch.push(page + '.html 缺失');
    else if (fs.readFileSync(target, 'utf8') !== expected) mismatch.push(page + '.html 内容不一致');
  }
  if (mismatch.length) bad('接线页替换', mismatch.join(' | '));
  else ok('接线页替换', srcV2.length + ' 个 v2 页面已逐字节替换同名原版');

  // ---------------------------------------------------------------- 5. 无 v2 残留
  const leftovers = allFiles.map(rel).filter((r) => r.endsWith('.v2.html'));
  if (leftovers.length) bad('无 v2 残留', leftovers.join(', '));
  else ok('无 v2 残留', '0 个 *.v2.html');

  // ---------------------------------------------------------------- 6. extras
  for (const name of ['WALKTHROUGH.md', 'start-demo.sh']) {
    if (fs.existsSync(path.join(BUNDLE, name))) ok('extras · ' + name);
    else bad('extras · ' + name, 'bundle 根目录缺少 ' + name);
  }

  console.log('\nRESULT: ' + pass + ' PASS / ' + fail + ' FAIL');
  process.exit(fail === 0 ? 0 : 1);
}

main();
