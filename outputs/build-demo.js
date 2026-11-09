#!/usr/bin/env node
/**
 * build-demo.js — 把 outputs/rednote-demo/ 打成可直接用浏览器跑的 bundle。
 *
 * 规则（幂等，零依赖）：
 *  1. 全量重建 outputs/demo-build/（先清空，防陈旧文件残留）。
 *  2. 拷贝原版 .html、app.js、api.js、*.css、render/（仅运行时 .js）、assets/。
 *  3. 对每个存在 <page>.v2.html 的页面，用 v2 内容覆盖同名 <page>.html。
 *     v2 内容里的 "*.v2.html" 引用（href / 文案 / render 模块里的链接）统一改写为 "*.html"，
 *     否则 bundle 内导航会指向已被排除的 v2 文件。
 *  4. 排除 *.v2.html 本身、render/*.test.js、以下划线开头的内部文件（_gen_index.js 等）。
 *  5. 从 demo-build-extras/ 复制 WALKTHROUGH.md 与 start-demo.sh 到 bundle 根目录。
 *
 * 用法：node build-demo.js
 */

'use strict';

const fs = require('fs');
const path = require('path');

const OUTPUTS = __dirname;
const SRC = path.join(OUTPUTS, 'rednote-demo');
const DST = path.join(OUTPUTS, 'demo-build');
const EXTRAS = path.join(OUTPUTS, 'demo-build-extras');

function fail(message) {
  console.error('[build] FAIL ' + message);
  process.exit(1);
}

function listDir(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
}

function copyFile(from, to) {
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.copyFileSync(from, to);
}

function transformV2Refs(text) {
  return text.replace(/\.v2\.html/g, '.html');
}

function copyRenderModules() {
  const fromDir = path.join(SRC, 'render');
  const toDir = path.join(DST, 'render');
  let copied = 0;
  let skippedTests = 0;
  function walk(from, to) {
    for (const entry of listDir(from)) {
      const fromPath = path.join(from, entry.name);
      const toPath = path.join(to, entry.name);
      if (entry.isDirectory()) {
        walk(fromPath, toPath);
      } else if (entry.isFile()) {
        if (/\.test\.js$/.test(entry.name)) { skippedTests += 1; continue; }
        if (!entry.name.endsWith('.js')) continue;
        fs.mkdirSync(path.dirname(toPath), { recursive: true });
        fs.writeFileSync(toPath, transformV2Refs(fs.readFileSync(fromPath, 'utf8')));
        copied += 1;
      }
    }
  }
  walk(fromDir, toDir);
  return { copied, skippedTests };
}

function copyAssets() {
  const fromDir = path.join(SRC, 'assets');
  const toDir = path.join(DST, 'assets');
  let files = 0;
  (function walk(from, to) {
    fs.mkdirSync(to, { recursive: true });
    for (const entry of listDir(from)) {
      const fromPath = path.join(from, entry.name);
      const toPath = path.join(to, entry.name);
      if (entry.isDirectory()) walk(fromPath, toPath);
      else if (entry.isFile()) { copyFile(fromPath, toPath); files += 1; }
    }
  })(fromDir, toDir);
  return files;
}

function main() {
  if (!fs.existsSync(SRC)) fail('源目录不存在：' + SRC);
  if (path.resolve(SRC) === path.resolve(DST)) fail('源目录与产物目录相同，拒绝执行');
  if (DST.startsWith(SRC + path.sep)) fail('产物目录不能位于源目录内，拒绝执行');

  // 1) 全量重建
  fs.rmSync(DST, { recursive: true, force: true });
  fs.mkdirSync(DST, { recursive: true });

  const srcEntries = listDir(SRC);
  const v2Pages = srcEntries
    .filter((e) => e.isFile() && e.name.endsWith('.v2.html'))
    .map((e) => e.name.slice(0, -'.v2.html'.length));

  // 2) 基础文件
  let copiedHtml = 0;
  let copiedCss = 0;
  let copiedJs = 0;
  for (const entry of srcEntries) {
    const fromPath = path.join(SRC, entry.name);
    if (entry.name.startsWith('_')) continue;            // 内部生成物
    if (entry.isDirectory()) {
      if (entry.name === 'assets') continue;              // 单独拷贝
      if (entry.name === 'render') continue;              // 单独拷贝（排除 test）
      continue;
    }
    if (entry.name.endsWith('.v2.html')) continue;        // 不直接拷 v2
    if (entry.name.endsWith('.html')) {
      copyFile(fromPath, path.join(DST, entry.name));
      copiedHtml += 1;
    } else if (entry.name.endsWith('.css')) {
      copyFile(fromPath, path.join(DST, entry.name));
      copiedCss += 1;
    } else if (entry.name === 'api.js' || entry.name === 'app.js') {
      copyFile(fromPath, path.join(DST, entry.name));
      copiedJs += 1;
    }
  }

  const render = copyRenderModules();
  const assetCount = copyAssets();

  // 3) v2 覆盖同名原版
  const replaced = [];
  for (const page of v2Pages) {
    const fromPath = path.join(SRC, page + '.v2.html');
    const toPath = path.join(DST, page + '.html');
    const html = transformV2Refs(fs.readFileSync(fromPath, 'utf8'));
    fs.writeFileSync(toPath, html);
    replaced.push(page);
  }

  // 4) 额外文件（走查手册 / 启动脚本）
  const extrasCopied = [];
  for (const name of ['WALKTHROUGH.md', 'start-demo.sh']) {
    const fromPath = path.join(EXTRAS, name);
    if (!fs.existsSync(fromPath)) fail('缺少 extras 文件：' + fromPath);
    copyFile(fromPath, path.join(DST, name));
    if (name.endsWith('.sh')) fs.chmodSync(path.join(DST, name), 0o755);
    extrasCopied.push(name);
  }

  // 5) 自检：bundle 里不允许出现 v2
  const leftovers = fs.readdirSync(DST).filter((n) => n.endsWith('.v2.html'));
  if (leftovers.length) fail('bundle 里残留 v2 文件：' + leftovers.join(', '));

  console.log('[build] source        ' + SRC);
  console.log('[build] bundle        ' + DST);
  console.log('[build] 原版页面      ' + copiedHtml + ' 个');
  console.log('[build] CSS/JS        ' + (copiedCss + copiedJs) + ' 个');
  console.log('[build] render 模块   ' + render.copied + ' 个（跳过测试 ' + render.skippedTests + ' 个）');
  console.log('[build] assets        ' + assetCount + ' 个文件');
  console.log('[build] 接线页覆盖    ' + replaced.length + ' 个：' + replaced.join(', '));
  console.log('[build] extras        ' + extrasCopied.join(', '));
  console.log('[build] OK 可重复运行，产物已全量重建');
}

main();
