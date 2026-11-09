# WP46 内网安装包与 SBOM 说明

> 对应计划：`outputs/M5研发计划_加固与上线_2026-10-05.md` §4.1 WP46
> 判据载体：`tools/quality/check_offline_bundle.py`（**29** 条）、`tools/quality/check_sbom_coverage.py`（**14** 条）
> 反向验证：`tools/quality/collect_sbom_coverage_reverse_evidence.py`（**8** 用例，R0~R6）
> 产出工具：`tools/ops/build_offline_bundle.py`、`tools/ops/gen_sbom.py`（**纯标准库**）
> 部署资产：`deploy/prod/`（8 个文件；手册见 `deploy/prod/README.md`）
> 证据目录：`evidence/m5/wp46-*.log` + `evidence/m5/wp46-sbom-snapshot.cdx.json`
> 记录更新：2026-10-18

---

## 0. 这份记录回答什么、不回答什么

WP46 的验收原文（计划 §4.1）：

> 本机：`docker compose config` 静态校验通过、无 `latest`、SBOM 产出、依赖声明判据通过。

**本机实测：这句验收单独使用时不设防。**（2026-10-18，Docker Compose 5.3.1）

```bash
$ printf 'services:\n  x:\n    image: "minio/minio@sha256:NOT_A_REAL_DIGEST_zz"\n' \
    | docker compose -f - config --quiet ; echo "EXIT=$?"
EXIT=0

$ printf 'services:\n  y:\n    image: "no-such-registry.invalid/foo:bar"\n' \
    | docker compose -f - config --quiet ; echo "EXIT=$?"
EXIT=0
```

伪造的 digest（不是 64 位十六进制）、根本不存在的 registry，全部 EXIT=0 ——
它只做 YAML 语法 + 变量插值 + schema。这与 WP47② 踩到的 `promtool check`
完全同族（`tools/quality/check_alerting_rules.py:6-13`：指标名拼错也 `SUCCESS ... EXIT=0`）。

同向第二条实测：`deploy/dev/` 下 `docker compose --env-file .env.example config --quiet`
今日 EXIT=0 —— 而那份 compose 里躺着 `minio/minio:latest`（`deploy/dev/docker-compose.yml:79`）。
**「静态校验通过」与「版本锁定了」是两件无关的事。**

⇒ 本包把 `compose config` 降级成「语法层 D1」，形态/对账/校验值全部自建判据。
本记录的一切结论都按 §6 的**三档验证层级**标注。

### 0.0 禁用句式（放在最前面，因为它决定后面每一句怎么读）

本包的任何文档与脚本输出里**都不会出现**下面四类说法，判据
`prod:no-overclaim-phrases` 逐行扫描并报红：

- ❌ 「离线安装包已验证可用」 [禁用句式示例]
- ❌ 「断网可一键安装」 [禁用句式示例]
- ❌ 「版本已锁定」（digest 还是占位时） [禁用句式示例]
- ❌ 「SBOM 覆盖全部依赖」（L-D 镜像内 OS 包层未覆盖） [禁用句式示例]

只能这样写：

- ✅ 「离线包**构建链**在本机跑通并产出清单与校验值；**断网安装未验**，见 §5 第 2 项」
- ✅ 「SBOM 覆盖 Python 直接依赖 + 传递闭包 + 镜像引用层**三层**；镜像内 OS 包层**未覆盖**，由判据 `sbom:uncovered-layers-declared` 显式声明」

### 0.1 覆盖矩阵（防"看起来什么都做了"）

对计划 §4.1 的任务项 ①~⑦ 逐条交代。

**两表互补的口径（防悬空）**：矩阵里的每一行必须落在下面三类之一，没有第四类 ——

| 标记 | 含义 | 去哪核对 |
|---|---|---|
| ✅ / 🟡 | 已做 / 部分做 | 「载体」列指到具体文件与判据名；🟡 的缺口部分另有 §5 行号 |
| ❌ **待跑** | 该做、但本机做不到 | **必须**在 §5 内网待跑清单里有对应行（行号写在「载体 / 缺口」列里） |
| ❌ **边界** | 刻意不做，不是缺口 | **刻意不进 §5**（§5 是"本机做不到"，不是"决定不做"）⇒ 理由写在「载体 / 缺口」列里；若牵涉串行装配则指到 §7 |

| WP46 任务项 | 状态 | 载体 / 缺口 |
|---|---|---|
| ① 补全 compose：C1 PG / C2 RabbitMQ / C3 MinIO / C4 Keycloak / C8 反代+双 API+Worker | ✅ **已做**（形态层） | `deploy/prod/docker-compose.yml`（9 服务）+ `nginx/rednote.conf`。判据 `prod:compose-syntax-d1` / `prod:image-refs-externalized` |
| ① C6 监控接入 | ✅ **已做**（只读挂载，不复制第二份） | compose 的 `prometheus` 服务挂 `../monitoring:/etc/prometheus:ro`；判据 `prod:monitoring-not-duplicated` 断言本目录下无第二份规则 |
| ① C5 Harbor 的安装编排 | ❌ **边界** | Harbor 是**宿主设施**（ADR-11 C5 / S1），装它的那一刻还没有离线包（鸡生蛋）⇒ 属运维手册第 0 步，本包只在手册里写顺序。⚠️ **另一件事**：Harbor 的推送/拉取**链路**确实是待跑项 → §5 第 5 项 |
| ① ansible / IaC 工具链 | ❌ **边界**（刻意不进 §5） | `outputs/项目部署方式与发布节奏说明_2026-10-15.md:53` 不建议完整 K8s；2~3 节点下 ansible 收益低于它自身的离线安装与维护成本（ansible 本身也要离线装）。ADR-11「安装即 IaC 化，产出 ansible/compose 清单」——**compose 清单已满足这句话** |
| ② 所有镜像换 digest / 精确补丁 tag，清除 `latest` | 🟡 **部分**：形态 + 登记 + 计数已落地；**digest 实值 7 条全部待回填** | `deploy/prod/images.loc​k.json`（7 条）。判据 5 条：`prod:no-floating-image-ref` / `prod:pending-registered` / `prod:pin-state-matches-ref` / `prod:pending-digests-monotonic`（==7）/ `prod:unresolved-refs-monotonic`（==3）。缺口见 §5 第 4 项 |
| ③ 离线包：`docker save` 打包 + 校验清单（含 digest） | 🟡 **部分**：构建链 4/5 个子命令本机跑通；`images` 子命令本机**不可执行** | `tools/ops/build_offline_bundle.py`（5 子命令）。本机产出 54 文件 / 70 MB / 清单自校验通过。`images` 缺口见 §5 第 1 项 |
| ④ SBOM：应用 Python 依赖 + 镜像层 | 🟡 **部分**：L-A 直接依赖 + L-B 传递闭包 + L-C 镜像引用**三层已覆盖**；**L-D 镜像内 OS 包层未覆盖** | `tools/ops/gen_sbom.py`（CycloneDX 1.5，51 library + 7 container）。L-D 由判据 `sbom:uncovered-layers-declared` 显式声明为未覆盖，缺口见 §5 第 11 项 |
| ⑤ 首次启动不下载任何依赖（G0-20） | 🟡 **只做静态侧**：脚本正文零联网动作 + data 网 `internal: true` | 判据 `prod:scripts-offline-shape`。⚠️ 只能读作「脚本里没写联网动作」。**断网实测**见 §5 第 2 项 |
| ⑤ G0-20 的另一半：前端无公网 CDN | ❌ **待跑**（本包新发现，当前不满足） | 实测本仓 `services/backend/static/` 的 **34 个文件**引用 `cdnjs.cloudflare.com` / `fonts.googleapis.com` / `fonts.gstatic.com`。本包**不改前端赛道的文件**，只把现状钉住（`frontend:external-origin-hosts-declared` + `frontend:external-origin-files-monotonic`）并登记。见 §4 与 §5 第 14 项 |
| ⑥ 一键安装 / 升级 / 卸载脚本 | 🟡 **部分**：脚本齐全、`bash -n` 通过、`--dry-run` 逻辑路径走通且输出确定；**真机幂等未验** | `install.sh` / `upgrade.sh` / `uninstall.sh`。判据 5 条（严格模式 / 零联网 / 语法 / 删卷闸口 ×2 / 备份委托）。缺口见 §5 第 6 项 |
| ⑦ CI 新增门：镜像无 `latest`、SBOM 可生成 | ❌ **边界**（串行装配，刻意不进 §5） | 判据脚本已就绪且本机全绿，但 `.github/workflows/ci.yml` / `REQUIRED_CI_STEPS` / `MIN_SCRIPTS` / `README.md` 四处同步是接缝表 #12/#13/#14/#6 的**禁改共享文件** ⇒ 本包不碰。这不是「本机做不到」而是「本包无权改」⇒ 归 **§7 第 1 项**，不进 §5 |
| — 应用镜像的 Dockerfile | ❌ **待跑 / 待拍板（本包新发现）** | 本仓**没有 Dockerfile** ⇒ `IMAGE_APP=rednote/backend:0.1.0` 现在造不出来，`api-1`/`api-2`/`worker` 三个服务没有可用镜像。方案 §3.1 未列此项、计划 §4.1 也未点名 ⇒ 本包不自行扩范围。见 §5 第 13 项与 §7 第 2 项 |
| — 制品不入库 | ✅ **已做** | 制品落 `dist/`（`.gitignore:43` 已忽略）；判据 `prod:artifacts-not-tracked` 断言 `git ls-files` 无 `dist/` |
| — 零第三方依赖引入 | ✅ **已做** | `gen_sbom.py` / `build_offline_bundle.py` / 三个判据脚本**全部只 import 标准库**；`git diff --stat services/backend/pyproject.toml` 为空 |

---

## 1. 三件产物的确切形态

| # | 产物 | 形态 | 为什么是这个形态 |
|---|---|---|---|
| A | 生产编排 | `deploy/prod/` 下的 **docker compose**（非 ansible、非 helm）+ 3 个 shell 脚本 + 反代配置 + **镜像锁定清单** | 现有资产就是 compose（`deploy/dev/docker-compose.yml`，203 行带完整房规注释），顺着它长成本最低；节点数 2~3 时 ansible 的收益低于它自身的离线安装成本 |
| B | 离线安装包 | **`docker save` 的镜像 tar + Python wheelhouse + 源码 tar + `SHA256SUMS.txt` + 机读 `MANIFEST.json`**，产物落 `dist/`（**不入库**） | 形态是 ADR-11 的验证表定的：「可出网机器 `docker pull` + `docker save` → 内网 `docker load` → **断网**后 `up -d`」。制品数 GB ⇒ 入库会毁掉仓库 ⇒ **入库的是「怎么造」和「造出来对不对」** |
| C | SBOM | **CycloneDX 1.5 JSON**，`tools/ops/gen_sbom.py` **纯 stdlib** 生成 | ① CycloneDX 的 `purl` + `hashes[SHA-256]` 两个字段天然就是"装什么 + 校验什么"，而本包的 SBOM 要同时承担离线包校验职责；② 不引入 syft/cyclonedx-py/trivy —— 本机实测四个全缺，且新增 Python 依赖会触碰 `pyproject.toml`（接缝表 #17 禁改）⇒ **只用 stdlib 是设计约束而非巧合** |

### 1.1 为什么镜像引用走 `${IMAGE_*}` 而不是写死在 compose 里

不是风格偏好。内网走 Harbor（ADR-11 C5），镜像**名字前缀本身会变**
（`registry.example.com/rednote/pgvector...`）。写死 `image:` 的话内网第一步就得改 compose，
而**改过的 compose 不再被任何判据守着**。

取值的唯一权威是 `images.lock.json`，`.env` 只是它的投影，判据
`prod:image-refs-three-way` 做 compose ⇄ `.env.example` ⇄ `images.lock.json`
三方双向对账，`prod:lock-env-refs-equal` 再逐条比 ref 值。

### 1.2 `pin_state` 三态（与 ref 形态双向对账）

| 状态 | 含义 | 当前条数 |
|---|---|---|
| `digest` | ref 形如 `name@sha256:<64hex>`。真正锁死 | **0** |
| `tag-channel` | 带真实上游 tag，但是通道/次版本（`pg17` / `4-management` / `26.0` / `0.1.0`），未锁 digest ⇒ 同一 tag 不同时间可能拉到不同内容 | **4** |
| `unresolved` | 连 tag 都不知道，ref 里写字面 `PENDING`，`docker pull` 必失败 | **3** |

`unresolved` 是**刻意的**：此处若凭印象填一个版本号，就是 OP-14 备注里点名禁止的那件事
（登记册 `:393` 原话：「当前**刻意留空，不凭印象填**」）。
判据 `prod:pin-state-matches-ref` 做状态 ⇄ ref 形态的双向推导，防「把 unresolved 标成 tag-channel」这类自我放行。

⇒ **这份编排在回填前起不来**。起不来比「悄悄拉到一个别的版本」安全。

---

## 2. SBOM 的覆盖四层与判据

### 2.1 「覆盖了全部依赖」拆成可判的四层

| 层 | 事实来源 | 本机能判？ | 今日读数 |
|---|---|---|---|
| L-A **Python 直接依赖** | `services/backend/pyproject.toml` 的 `dependencies` + optional 各组 | ✅ | **19** 条 |
| L-B **Python 传递闭包** | `dist/bundle/wheelhouse/*.whl` 文件名（pip 真解析出来的结果） | ✅ | **51** 个 wheel（+32 个传递依赖） |
| L-C **镜像引用层** | `deploy/prod/images.lock.json` | ✅ | **7** 条 |
| L-D **镜像内 OS 包层** | 需 syft/trivy 解析镜像层与发行版包数据库 | ❌ **不做** | 显式登记为未覆盖层 |

**为什么 L-B 必须取 wheelhouse 文件名，不能把 pyproject 抄一遍**：
`pyproject.toml` 只有**直接**依赖。`fastapi==0.115.0` 会拖进
starlette / anyio / sniffio / idna / typing-extensions…
抄 pyproject = 「列了几个就算数」，而内网装机缺的恰恰是传递的那些
（`docs/m3/工程债-依赖声明一致性说明.md:30` 记过同族实测：缺 `cryptography` 时
`alembic upgrade head` 直接 ModuleNotFoundError 崩掉）。
wheelhouse 的文件名集合是 **pip 的真实解析结果**，人改不动它。

**L-D 不做的理由不是"来不及"**：需 syft/trivy（本机四个工具实测零命中、内网也装不了），
自己写「解析镜像层 tar + dpkg/rpm 数据库」是另一个工作包的体量。
它与另外三条未覆盖项一起由判据 `sbom:uncovered-layers-declared` 做
**SBOM ⇄ 判据侧常量双向对账** —— 「没覆盖」必须是一句**会被判据核对**的话。

四条未覆盖项：`image-os-packages` / `frontend-assets` / `ocr-model-weights`（OP-137）/ `dynamic-imports`。

### 2.2 两条最关键的判据，以及它们各自挡住了什么

#### ★ `sbom:closure-strictly-larger` —— 挡 wheelhouse 退化成 `--no-deps`

`sbom:python-closure-equals-wheelhouse` 断言 SBOM 的 `(name, version)` 集合与
wheelhouse 解析结果**双向相等**。这条很有用，但它**单独使用时有一条致命的恒真路径**：

若有人把 wheelhouse 构建成 `pip download --no-deps`，那么 wheelhouse 里只有直接依赖、
SBOM 也只有直接依赖 ⇒ **双向相等，全绿**，而传递闭包根本没取到。
这与 WP47④ 队列表那次 `0 == 0` 完全同族
（`docs/m5/WP47-演练记录.md:167-180`：备份把队列内容全丢了照样报"行数相等"）。

⇒ 必须另有一条断言**严格大于**：`51 > 19`。
**反向验证 R2 实测证明了这一点**：把 wheelhouse 缩成只有 19 个直接依赖的 wheel、
重新生成 SBOM 之后，`sbom:python-closure-equals-wheelhouse` 的
**集合差仍然是 `SBOM-only=[] / wheelhouse-only=[]`**（即集合依然相等）。
⇒ 「集合相等」在这种退化下**毫无信息**。

#### ★ `sbom:hashes-recomputed` —— 挡「字段齐全但值是编的」

判据对 wheelhouse 里每个 `.whl` **重新算一遍** sha256，与 SBOM 里写的逐个比，
**不读清单里写的数**（读它等于自己断言自己）。
反向验证 R4 把一个组件的 sha256 改一位，该条当场报红。

### 2.3 fail-closed：不许"跑不了所以绿"

wheelhouse 不存在或为空时，`check_sbom_coverage.py` **输出 14 条全红并打印
「无法构造对账」**，不会 SKIP。本机实测过这条路径（`--wheelhouse /tmp/empty_wh`
→ `RESULT=FAIL checks=14/14 failures=14`）。

依据：`check_alerting_rules.py:47-48`「一个『跑不了所以绿』的判据等于没有判据」。
本包的具体风险：CI runner 上若不构建 wheelhouse，S3/S3a/S7 会**在 CI 上永远不生效**
而输出是绿的。

> ⚠️ **留给主会话拍板**：CI 侧要不要真构建 wheelhouse？真构建 ⇒ 门 3 变慢
> （本机实测 51 wheel / 64 MB / 38 秒，CI 网络条件未知）且依赖网络；
> 不构建 ⇒ 本脚本在 CI 上必红（fail-closed 的必然结果）。
> 两条路都要人拍板，理由已写进 `check_sbom_coverage.py` 的 docstring
> （照 `ci.yml:157-167` 的纪律：理由必须写下来，否则就是 ISS-95 形态的空白）。

---

## 3. 反向验证读数（8/8）

`tools/quality/collect_sbom_coverage_reverse_evidence.py`，证据落
`evidence/m5/wp46-sbom-reverse.log`。

| 编号 | 注入 | 期望报红的判据 | 实测 |
|---|---|---|---|
| R0 | 基线未变异 | 两脚本全绿 | ✅ `29/29` + `14/14` |
| R1 | wheelhouse 副本删掉 `fastapi` 的 wheel，重新生成 SBOM | `sbom:python-direct-superset` | ✅ 报红（连带 `sbom:direct-flag-matches-pyproject`） |
| R2 | wheelhouse 只留 19 个直接依赖的 wheel（模拟 `--no-deps`） | **`sbom:closure-strictly-larger`**，且 S3 的集合差仍为空 | ✅ 报红；S3 集合**仍相等** |
| R3 | deploy 副本把 `pgvector/pgvector:pg17` 改成 `:latest`（env 与 lock 两侧同改） | `prod:no-floating-image-ref` | ✅ 报红 |
| R3b | 对**同一份**带 `:latest` 的副本跑 `docker compose config --quiet` | **EXIT 仍为 0** | ✅ `exit=0` —— 把计划原文验收不设防钉成常驻读数 |
| R4 | SBOM 副本把一个组件的 sha256 改一位 | `sbom:hashes-recomputed` | ✅ 报红 |
| R5 | SBOM 副本删掉一条未覆盖层声明 | `sbom:uncovered-layers-declared` | ✅ 报红 |
| R6 | 原文件 sha256 与开跑前逐字相同 | 零污染 | ✅ 7 个受保护文件全部未变 |

### 3.1 与 WP47④ 那份反向验证的一处**刻意不同**

`collect_queue_reconcile_reverse_evidence.py` 的做法是「原地改 → 跑 → 逐字还原」，
它的 docstring 自己记了风险：共享工作树，并行会话可能在窗口内改同一文件，
还原失败就会让磁盘停在变异状态。

本包的被变异对象里有 **`services/backend/pyproject.toml`** ——
接缝表 #17 的禁改文件，且本包的收口条件之一正是它的 `git diff` 为空。
在它上面做"改了再还原"，脚本一旦在窗口内被中断，收口条件就被破坏了。

⇒ 本包**全部变异都在仓库外的临时副本上做**（被测脚本提供
`--pyproject` / `--wheelhouse` / `--sbom` / `--images-lock` / `--deploy-dir` 覆盖参数）。
R6 核对的是**「原文件 sha256 与开跑前逐字相同」**，比"还原成功"更强：它证明的是根本没动过。

### 3.2 锚点纪律

每处文本变异满足 `src.count(old) == 1`，否则 fail-fast。
「恰好 1」而不是「至少 1」：命中 0 次意味着变异根本没改上，而那时
"判据没报红"与"变异没生效"**同形**（`docs/m5/WP47-演练记录.md:225-228` 的教训）。
文件级变异（删 wheel）用等价形式：glob 命中数必须恰好 1。

---

## 4. 本包实测发现的一个真缺口：本仓前端的公网 CDN 外链

```bash
$ grep -rhoE 'https?://[A-Za-z0-9.-]+' services/backend/static/ | sort | uniq -c | sort -rn
  70 https://fonts.googleapis.com
  35 https://fonts.gstatic.com
  35 https://cdnjs.cloudflare.com
```

**本仓 `services/backend/static/` 的 34 个已追踪文件依赖 cdnjs 与 Google Fonts。**
内网断网后：图标全变方框、字体回退 —— 这正是 G0-20 要挡的东西。

处置纪律：
- 前端是**另一条并行赛道**的写入面 ⇒ WP46 **不去改这些文件**；
- WP46 做两件事：① 判据把现状**钉住**
  （`frontend:external-origin-hosts-declared` 做主机集合双向对账，
  `frontend:external-origin-files-monotonic` 做文件数计数、**只许减不许增**）；
  ② 登记为待处置（需主会话串行分配 OP 号，见 §7 第 3 项）。
- **不做的事**：不在本文档里写「前端已满足 G0-20」。它现在不满足。

> 口径修正：本包的开工方案 §5.2 曾写「**当前本仓命中 0** ⇒ 这条是为前端赛道提前立的桩」。
> 那是因为方案只 grep 了仓库外的 `outputs/rednote-demo/`。
> **今日实测推翻了这句话**：本仓自己的 `services/backend/static/` 就有 34 个文件命中。
> 这不是"提前立的桩"，是**现存缺口**。

---

## 5. 内网待跑清单（本机无法验证，不得冒充生产证据）

| # | 项 | 为什么本机做不了 | 对应 §0.1 的哪一行 |
|---|---|---|---|
| 1 | `docker save` / `docker load` | daemon 不可达（实测 `docker info` → `dial unix /Users/<user>/.orbstack/run/docker.sock: no such file`）+ 受控出网拉不到镜像（ISS-01） | ③ |
| 2 | **断网 `up -d` 全绿、无 DNS/超时重试** | 本机无 daemon、无隔离网络 | ⑤ |
| 3 | 内网 Linux 上 `pip install --no-index --find-links wheelhouse` 成功 | 本机 **darwin/arm64**，manylinux wheel 在本机根本装不上 | ③ |
| 4 | 每个镜像的**真 digest** 回填（7 条，其中 3 条连 tag 都未定） | 需可出网机 `docker pull` + 内网 Harbor | ② |
| 5 | Harbor 推送 / 拉取链路、Harbor 自身安装 | 需内网 Harbor（ADR-11 C5 / S1，离线包托管的前提件） | ① C5 |
| 6 | 一键安装 / 升级 / 卸载的**真机幂等** | 需真机、真卷、真迁移（本机只验到 `--dry-run` 输出确定） | ⑥ |
| 7 | 反向代理 + HTTPS + 内部 CA（C8） | 依赖 E2 内部域名 / DNS / CA 未落地。`nginx/rednote.conf` 末尾留了要加的 HTTPS 段，**刻意不提前写进生效配置** | ① C8 |
| 8 | 双 API 实例 + **RabbitMQ 3 节点 quorum** | 需 ≥3 节点。本编排是单节点 RabbitMQ，ADR-11 明写「单节点验证不得当生产证据」 | ① C2/C8 |
| 9 | 组件隔离（用户网段不可达 PG / AMQP / MinIO） | 需企业网络（WP01 N5）。本编排已做到「除 gateway 外无 `ports:` 段」，但那只是结构性保障 | ① |
| 10 | `/metrics` 仅运维面可达的**部署层回执** | 需网关。**OP-191**（🔴 P0）的关闭条件是「从外网实测一次应返回 403/404 而非 200」 | ① C6 |
| 11 | 镜像内 OS 包级 SBOM（L-D） | 无 syft/trivy（本机实测四个工具零命中），自己写是另一个工作包的体量 | ④ |
| 12 | 介质完整性（U 盘拷贝后 sha256） | 无介质。`install.sh` 步骤 1 的双路径校验逻辑（`sha256sum` / `shasum -a 256`）已就位但未在真介质上跑过 | ③ |
| 13 | **应用镜像的构建**（`IMAGE_APP`） | ⚠️ 这不是"本机做不到"，是**还没做**：本仓无 Dockerfile。归属待拍板 | — Dockerfile 行 |
| 14 | 前端去掉公网 CDN 外链后的断网渲染 | 需先由前端赛道处置 34 个文件；且断网渲染本机验不了 | ⑤ 前端那行 |
| 15 | OCR（PaddleOCR）权重进离线包 | **OP-137** 未定版本与权重来源，离线包体积未知 | ④（L-D 的 `ocr-model-weights`） |

---

## 6. 验证层级（本节是本包诚实度的主战场）

照 `docs/m5/WP47-演练记录.md:20-21` 的写法：
> 「ADR-11 明文写着『单节点验证不得当生产证据』—— 所以下面的耗时数字**一律只能读作"机制可用"，不能读作"RTO 达标"**。」

本包的对应句式是：**「只能读作构建链可跑、清单可核对，不能读作内网装得上」**。

### 6.1 本机环境能力（每次复跑重新实测，落 `evidence/m5/wp46-env-baseline.log`）

> ⚠️ 这些读数**不写死在本文档里当结论**。环境变了（比如哪天 daemon 起来了），
> 文档不会自己更新，而那时文档里每一句"本机做不到"都成了无依据的话。
> ⇒ 结论回到那份每次复跑都重新实测的基线。下面是 **2026-10-18 的快照**。

| 能力 | 命令 | 结果 | 它决定了什么 |
|---|---|---|---|
| Docker CLI | `docker --version` | ✅ `/opt/homebrew/bin/docker` | 有 CLI 才能跑语法层 D1 |
| Docker Compose | `docker compose version` | ✅ **5.3.1** | — |
| Docker **daemon** | `docker info` | ❌ **EXIT=1**：`dial unix /Users/<user>/.orbstack/run/docker.sock: connect: no such file or directory` | ★ `docker save`/`load`/`up -d` 全部降为 **L3** |
| `compose config` | `cd deploy/prod && docker compose --env-file .env.example config --quiet` | ✅ EXIT=0（**无需 daemon**） | 语法层 D1 可用 |
| `compose config` 的**鉴别力** | 见 §0 两段 printf | ❌ 伪 digest / 不存在的 registry **全部 EXIT=0** | ★ 形态判据必须自建，不依赖它 |
| SBOM 工具链 | `command -v syft trivy cyclonedx-py pip-audit` | ❌ **四个全部无命中** | ★ L-D 无法覆盖；SBOM 只能自己写（且不许引入第三方依赖） |
| shell 工具 | `command -v shellcheck shfmt yq` | ❌ 全部无命中 | ★ shell 静态检查只能到 `bash -n` + `--dry-run` 行为核对 + 自建正则；compose 只能用窄行扫描器解析 |
| nginx | `command -v nginx` | ❌ 无命中 | `nginx -t` 跑不了 ⇒ 反代配置只能做形态层核对 |
| promtool | `command -v promtool` | ✅ 存在（WP47② 在用） | 本包不碰监控规则，只读挂载 |
| Python | `python3 -V` | ✅ **3.11.9**（本 shell 实测） | `tomllib` 可用（3.11+）⇒ 零第三方依赖解析 TOML。⚠️ 与 CI 的 `PYTHON_VERSION: "3.13"` **不同**，见 §7 第 5 项 |
| pip 索引 | `pip config list` | 🟡 `global.index-url = https://mirrors.aliyun.com/pypi/simple/` | ★ wheelhouse 是从**镜像站**下来的，不是 pypi.org 直连。「镜像站与上游内容一致」是**假设**不是事实 ⇒ 索引地址写进 `MANIFEST.json` |
| PyPI 直连 | `curl --max-time 12 https://pypi.org/simple/` | 🟡 HTTP 200 但 12s 只收到 677 KB / 47 MB | 直连可达但慢；实际构建走镜像站（38 秒拉完 51 个 wheel） |
| PostgreSQL | `psql -At -c 'select version()'` | ✅ **PostgreSQL 17.10 (Homebrew) on aarch64-apple-darwin25.6.0** | ★ SBOM 的 runtime facts 真库来源 |
| pgvector | `psql -At -c 'select extname,extversion from pg_extension'` | ✅ **vector=0.8.6**（+ plpgsql=1.0） | 同上。与 ADR-11 写的「pgvector 0.8 系列」一致 —— 但那是**核对结果**，不是前提 |
| 构建机平台 | `uname -sm` | **Darwin arm64** | ★ manylinux wheel 在本机无法 `pip install` 验证 |

### 6.2 三档层级（每条产出都标明它属哪档）

#### L1 —— 本机可真验（能写"已验证"三个字的，只有这一档）

| # | 验证项 | 怎么验 | 读法 |
|---|---|---|---|
| 1 | compose 语法与变量插值 | `docker compose --env-file .env.example config --quiet` EXIT=0 | 只能读作「YAML 合法」。**不能**读作「镜像引用有效」（§0 实测） |
| 2 | 无 `latest` / 引用形态 / 三方清单双向相等 / 占位计数 | `check_offline_bundle.py` 的 7 条镜像类判据 | 可读作「镜像引用的**形态**已落地、未锁 digest 的条目逐条登记且被计数」。**不能**读作版本锁死（0 条 digest 形态） |
| 3 | SBOM 14 条判据全绿 + 反向验证 8/8 | `check_sbom_coverage.py` + `collect_sbom_coverage_reverse_evidence.py` | 可读作「SBOM 覆盖 L-A/L-B/L-C 三层，且判据自己会报红」 |
| 4 | 每个 wheel 的 sha256 | 现场重算 51 个，与 SBOM 逐个比 | 可读作「校验值可独立重算」 |
| 5 | 离线包清单自校验 | `SHA256SUMS.txt` 54 条现场重算 + 清单 ⇄ 磁盘双向相等 | 可读作「构建机上这些文件存在且校验值可重算」 |
| 6 | 脚本零联网形态 | `bash -n` + 判据扫联网 token + `pip install` 必带 `--no-index` | 可读作「脚本里没写联网动作」。**不能**读作「运行时不联网」 |
| 7 | 卸载默认不删卷 | **跑 `--dry-run` 核对打出的 `down` 行**（行为核对，不是文本扫描）+ `--purge` 对照组 | 可读作「默认路径不含 `--volumes`，且 `--purge` 路径确实含」 |
| 8 | 制品不入库 | `git ls-files dist` 为空 | 可读作「仓库不会被制品污染」 |
| 9 | PG / pgvector 版本事实 | 原生 PG17 只读查询 | 可读作「**本机**实测版本」。**不能**读作「内网版本」 |
| 10 | 零第三方依赖 | `git diff --stat services/backend/pyproject.toml` 为空 | 可读作「只用 stdlib 这句话是真的」 |

#### L2 —— 本机只能到"脚本能跑、产物存在"（必须逐字写限定句）

| # | 验证项 | 本机能得到什么 | **不能**读作什么 |
|---|---|---|---|
| 1 | Python wheelhouse 构建 | `pip download --platform manylinux_2_28/2_17/2014_x86_64 --python-version 3.13 --only-binary=:all:` 把 **51 个 linux wheel / 64 MB** 拉下来、算 sha256、进 SBOM | ❌ **不能**读作「目标平台装得上」。本机是 darwin/arm64，那些 manylinux wheel 在本机**根本无法 `pip install` 验证**。**原句式**：「只能读作 wheelhouse 清单可复现，不能读作内网 Linux 可安装」 |
| 2 | `MANIFEST.json` + `SHA256SUMS.txt` | 文件存在、格式正确、54 条自校验通过 | ❌ 不能读作「介质拷贝后仍完整」（那要在内网校验一遍） |
| 3 | `install.sh` 的**逻辑路径** | `--dry-run` 走通 6 步、参数解析、前置检查逐项列出缺口 | ❌ 不能读作「安装成功」。没有 daemon，`docker load` / `up -d` 这两步**一步都没执行** |
| 4 | 脚本幂等 | 三个脚本各连跑两遍 `--dry-run`，输出逐字相同 | ❌ 不能读作「真机重复执行幂等」—— 真幂等要看卷、容器、迁移三者的重入 |
| 5 | 源码 tar | `git archive HEAD` 产出成功，并把**工作树脏文件数**写进 MANIFEST | ❌ 不能读作「包里有最新代码」：tar 打的是 HEAD，不含未提交改动（本次构建时脏文件 36 个） |

#### L3 —— 本机完全不可验，必须内网窗口

见 §5 内网待跑清单 15 项（与 §0.1 覆盖矩阵的每一个 ❌ / 🟡 缺口一一对应）。

---

## 7. 留给主会话的待拍板 / 串行事项

| # | 事项 | 为什么本包不能自己做 |
|---|---|---|
| 1 | **CI 装配**：`ci.yml` 门 3 新增 3 步（`check_offline_bundle.py` / `check_sbom_coverage.py` / `collect_sbom_coverage_reverse_evidence.py`）+ `REQUIRED_CI_STEPS` 追加 3 条 + `MIN_SCRIPTS` **+2**（`collect_sbom_coverage_reverse_evidence.py`、`tools/m5/collect_wp46_evidence.py` 两个落在 `SCOPE_PATTERNS` 上）+ `README.md` 目录树补 `deploy/prod/` | 接缝表 **#12 / #13 / #14 / #6** 全是禁改共享文件，由主会话串行装配 |
| 2 | **应用镜像的 Dockerfile 归属**：没有它 `IMAGE_APP` 造不出来，`api-1`/`api-2`/`worker` 三个服务没有可用镜像 | 方案 §3.1 与计划 §4.1 都没列这一项 ⇒ 本包不自行扩范围。它要么归 WP46 的第二轮，要么归 WP50 发布 |
| 3 | **OP 编号串行申领**（7 处，判据 `prod:unallocated-op-monotonic` 逐条列出）：5 条镜像 digest（postgres / rabbitmq / app / gateway / prometheus）+ `target_registry`（Harbor 前缀）+ `target_platform`（三元组）；另加**本仓前端 CDN 外链**（34 文件 / 3 外站） | 登记册与 `issue-log.jsonl` 是接缝表 #5 / #11，编号串行分配 |
| 4 | **CI 侧要不要真构建 wheelhouse** | 牵涉 CI 时间预算与"CI 能否出网"。两条路的后果都已写进 `check_sbom_coverage.py` 的 docstring |
| 5 | **目标平台三元组仍是 `[待核实]`** | 内网 CPU 架构（x86_64 / 信创 ARM）、libc、OS 发行版、Python 版本全未定。`MANIFEST.json` 当前的 `platform_tags` 是 **x86_64 口径的演示值**，若目标是 ARM 则 wheelhouse 全部需重建。另：`pyproject.toml:5` 写 `>=3.12`、CI 用 3.13、本机这个 shell 是 3.11.9 —— 内网装哪个未定 |
| 6 | **`MIN_SCRIPTS` 是否趁本包改成磁盘推导** | 接缝文档已记「A 段同步时把它改成由磁盘推导，别再往上加数字」。本包**不夹带**这个架构改动，按现行口径 +2 |
| 7 | **本包的证据是否要做逐份溯源表** | OP-193 的同一个决定。本包**刻意选了"不做逐份溯源表"**：证据面是「固定名的 4 个文件 + 判据输出」而不是带时间戳累积的 JSON ⇒ 不会出现 WP47 那种"磁盘 35 份、文档引用 13 份而无判据报红"的脱钩（`WP47-演练记录.md:345-349` 自己预言过）。若主会话要求做表，须**同批**建双向对账判据 —— 不许做了表却没有判据 |

---

## 8. 证据文件

| 文件 | 内容 | 产出者 |
|---|---|---|
| `evidence/m5/wp46-env-baseline.log` | §6.1 的 15 项环境能力实测，每项带命令、退出码、「它决定了什么」 | `tools/m5/collect_wp46_evidence.py` |
| `evidence/m5/wp46-offline-bundle.log` | `check_offline_bundle.py` 全量输出 + 三个脚本的 `--dry-run` 实跑（各两遍）+ compose 语法层 D1 读数 | 同上 |
| `evidence/m5/wp46-sbom-coverage.log` | `check_sbom_coverage.py` 全量输出 + SBOM 结构摘要 | 同上 |
| `evidence/m5/wp46-sbom-snapshot.cdx.json` | **入库的 SBOM 快照**（制品本体落 `dist/`，不入库） | 同上 |
| `evidence/m5/wp46-sbom-reverse.log` | R0~R6 逐条红/绿 + 每次变异的注入内容与失败项名单 | `tools/quality/collect_sbom_coverage_reverse_evidence.py` 自落盘 |

**固定文件名而不带时间戳**是刻意的：`WP47-演练记录.md:345-349` 预言过
（已登记 OP-193）带时间戳累积落盘会导致「磁盘 35 份、文档引用 13 份而无判据报红」。
「跑过了」这件事由 **mtime 前进**体现，不由文件变多体现。
