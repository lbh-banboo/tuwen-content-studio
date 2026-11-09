# M4 DDL 草案（19 张新建表）

> 编制日：2026-10-08　对应：M4 = WP36~WP45（W11~W14，出口 G4）
> 依据：`outputs/M4研发计划_WP36-45_2026-10-07.md` §5（19 表清单）/ §7（逐包任务）、
> `docs/m4/M4并行开发接缝冻结.md` §4（迁移纪律）、
> `templates/metric_import_field_guide.md`（WP38/39 口径，**权威**）。
> **性质：文本草案。不得据此建 `migrations/versions/`。** 建迁移是主 agent 独占动作（接缝 §4.1）。

---

## 0. 口径与约定（全表通用）

### 0.1 列约定（与既有 22 模块一致）

| 项 | 约约定 |
|---|---|
| 主键 | `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`（对应 `uuid_pk()`） |
| 外键 | 一律 `ON DELETE RESTRICT`（**不用 CASCADE** —— 删除须经状态机，不由 DB 连锁） |
| 时间 | `TIMESTAMPTZ`；`created_at TIMESTAMPTZ NOT NULL DEFAULT now()`；可变的加 `updated_at` |
| 乐观锁 | 可并发改的表加 `lock_version INTEGER NOT NULL DEFAULT 0` |
| 状态列 | `TEXT` + `CHECK (state IN (…))`，约束名 `state_valid`；取值域**写死字面量**（ISS-191） |
| 金额 | `NUMERIC(20,8)`（`AMOUNT_TYPE`，WP25 口径，**有符号**，非负性由值对象 + CHECK 守） |
| 数量 | `NUMERIC(30,10)` |
| 计数 | `BIGINT` / `INTEGER`（**非负**加 `CHECK (x >= 0)`） |
| 名称 | 表名复数下划线；索引 `ix_<表>_<列>`；唯一索引 `uq_<表>_<语义>` |

### 0.2 三条全局硬约束

1. **缺失不填 0**：指标值缺失时 `value` 为 `NULL`，由 `availability` 说明原因。
   ⇒ 观测值列**必须可空**，且 `CHECK` 不得把它默认成 0。
2. **只追加**：`metric_observations` / `usage_ledger` 类**只 INSERT，不 UPDATE/DELETE**。
   ⇒ 用 `rednote_reject_mutation()` 触发器（**复用**，不重建；`downgrade` 不删）。
3. **可重建缓存**：`metric_latest` 是投影缓存，**不是**历史权威来源 —— 可整表重建。

---

## 1. KOC 与交付（3 表）

### 1.1 `koc_records`（WP36）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `display_name` | TEXT | NOT NULL | 展示名 |
| `platform_home_url` | TEXT | NULL | 主页链接（**去重提示**用，不做硬唯一） |
| `home_url_canonical` | TEXT | NULL | 规范化后的主页 URL（WP37 规范器产） |
| `contact_ref` | TEXT | NULL | 联系方式**引用**（不存平台密码、不存明文凭据） |
| `note` | TEXT | NULL | |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |
| `lock_version` | INTEGER | NOT NULL DEFAULT 0 | |

- **索引**：`ix_koc_records_project`(`project_id`)、`ix_koc_records_home_canonical`(`home_url_canonical`)
- **不做**：`platform_home_url` 唯一（同名不同人、同链不同参 → 唯一约束会误拒）
- **禁止**：任何存放密码/令牌的列（04 明文）

### 1.2 `deliveries`（WP36）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `koc_record_id` | UUID | FK koc_records RESTRICT, NULL | |
| `content_item_id` | UUID | FK 既有内容实体 RESTRICT, NOT NULL | 交付对象 |
| `version_node_id` | UUID | FK version_nodes RESTRICT, NOT NULL | **固定导出版本**，不随改稿变 |
| `state` | TEXT | NOT NULL, `state_valid` | `PREPARED` / `DELIVERED` / `CANCELLED` |
| `eligibility_hash` | TEXT | NOT NULL | 导出资格指纹（导出后再次验证） |
| `replaces_delivery_id` | UUID | FK deliveries RESTRICT, NULL | **替换链（追加，不覆盖）** |
| `idempotency_key` | TEXT | NULL | **交付创建幂等键**（契约 `POST /deliveries`、`/deliveries/{id}/replace` 声明了 `Idempotency-Key` ⇒ 必须有载体，2026-10-11 补） |
| `delivered_at` | TIMESTAMPTZ | NULL | |
| `cancelled_at` | TIMESTAMPTZ | NULL | |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |
| `lock_version` | INTEGER | NOT NULL DEFAULT 0 | |

- `CHECK state_valid`: `state IN ('PREPARED','DELIVERED','CANCELLED')`
- `CHECK state_timestamps`: `(state='DELIVERED') = (delivered_at IS NOT NULL)` 且 `(state='CANCELLED') = (cancelled_at IS NOT NULL)`
- **部分唯一（幂等）**：`uq_deliveries_idempotency`(`idempotency_key`) `WHERE idempotency_key IS NOT NULL`
- **索引**：`ix_deliveries_project_state`(`project_id`,`state`)、`ix_deliveries_replaces`(`replaces_delivery_id`)
- **验收断言**：① 下载不计交付（`delivered_at` 只由显式交付动作置）② 原交付版本不随改稿变（`version_node_id` 不可变）③ 替换用新行 + `replaces_delivery_id`，**不 UPDATE 旧行 state**

### 1.3 `export_items`（**WP35**，2026-10-14 自 WP36 挪出）

> ⚠️ **归属变更**：本表由 **WP35** 建（Q2 拍板）。下方列定义是原草案，**阶段 4 落地时以 `docs/m3/WP35-固定版本ZIP与鉴权下载说明.md` §2.4 为准**：
> ① 粒度由「单内容」改为「项目内一组 content_id」，需新增 `export_group_id`（形态 A'，待老大在说明文档 §6 确认）；
> ② **`delivery_id` 列须删除**：`deliveries` 属 WP36，晚于本表建立，WP35 迁移里 FK 无处可指（先后顺序倒置）。WP36 建 `deliveries` 时反向引用 `export_items`（`deliveries.export_item_id`），方向与原草案相反。

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `delivery_id` | UUID | FK deliveries RESTRICT, NULL | |
| `content_item_id` | UUID | FK RESTRICT, NOT NULL | |
| `version_node_id` | UUID | FK version_nodes RESTRICT, NOT NULL | 固定版本 |
| `eligibility_hash` | TEXT | NOT NULL | 导出时的资格快照 |
| `export_kind` | TEXT | NOT NULL, `export_kind_valid` | `SINGLE` / `BATCH_ZIP` |
| `artifact_ref` | TEXT | NULL | 产物引用（MinIO key） |
| `idempotency_key` | TEXT | NULL | **导出幂等键**（契约 `POST /export-items` 声明了 `Idempotency-Key` ⇒ 必须有载体，2026-10-11 补） |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK export_kind_valid`: `export_kind IN ('SINGLE','BATCH_ZIP')`
- **部分唯一（幂等）**：`uq_export_items_idempotency`(`idempotency_key`) `WHERE idempotency_key IS NOT NULL`
- **索引**：`ix_export_items_delivery`(`delivery_id`)、`ix_export_items_version`(`version_node_id`)

---

## 2. 发布与映射（2 表）

### 2.1 `publications`（WP36 登记 / WP37 链接与状态）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `delivery_id` | UUID | FK deliveries RESTRICT, NULL | 草稿 → 实发映射 |
| `publication_key` | TEXT | NOT NULL | **业务主键**（导入模板 §1，长度 1–64，`[A-Za-z0-9_\-.]`，不含逗号） |
| `note_id` | TEXT | NULL | 平台笔记 ID（可得时） |
| `note_url` | TEXT | NULL | 原文 URL |
| `url_canonical` | TEXT | NULL | canonical URL（WP37 规范器产） |
| `state` | TEXT | NOT NULL, `state_valid` | `DRAFT` / `PUBLISHED` / `UNKNOWN` |
| `content_state` | TEXT | NOT NULL, `content_state_valid` | `VERIFIED` / `UNVERIFIED`（**与实际可达性分离**） |
| `published_at` | TIMESTAMPTZ | NULL | |
| `idempotency_key` | TEXT | NULL | **发布登记幂等键**（契约 `POST /publications` 声明了 `Idempotency-Key`；业务键 `uq_publications_project_key` 已挡重复登记，本列用于同键异体判 409，2026-10-11 补） |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |
| `lock_version` | INTEGER | NOT NULL DEFAULT 0 | |

- `CHECK state_valid`: `state IN ('DRAFT','PUBLISHED','UNKNOWN')`
- `CHECK content_state_valid`: `content_state IN ('VERIFIED','UNVERIFIED')`
- `CHECK publication_key_format`: `publication_key ~ '^[A-Za-z0-9_.\-]{1,64}$'`
- **唯一**：`uq_publications_project_key`(`project_id`,`publication_key`)
- **部分唯一（幂等）**：`uq_publications_idempotency`(`idempotency_key`) `WHERE idempotency_key IS NOT NULL`
- **索引**：`ix_publications_project_state`(`project_id`,`state`)、`ix_publications_canonical`(`url_canonical`)
- **关键**：**`content_state=UNVERIFIED` ≠ 已删除**。抓取失败只落 `UNVERIFIED`，**不得**推成删除（04 点名反例）。

### 2.2 `publication_links`（WP36 建 / WP37 用）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `publication_id` | UUID | FK publications RESTRICT, NOT NULL | |
| `raw_url` | TEXT | NOT NULL | 原始链接（短链/长链/带参） |
| `canonical_url` | TEXT | NOT NULL | 规范化后 |
| `link_kind` | TEXT | NOT NULL, `link_kind_valid` | `SHORT` / `LONG` / `OTHER` |
| `first_seen_at` | TIMESTAMPTZ | NOT NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK link_kind_valid`: `link_kind IN ('SHORT','LONG','OTHER')`
- **唯一**：`uq_publication_links_canonical`(`canonical_url`) —— **短链与长链规范化后不得重复**（WP37 核心）
- **索引**：`ix_publication_links_publication`(`publication_id`)、`ix_publication_links_raw`(`raw_url`)

---

## 3. 指标（5 表，WP38）

### 3.1 `metric_definitions`（字段字典）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `metric_code` | TEXT | NOT NULL | 取自词表（模板 §2）：`VIEW_COUNT` 等 14 个 |
| `scope` | TEXT | NOT NULL | `CUMULATIVE` / `LAST_24H` / … / `NOT_APPLICABLE` / `UNKNOWN` |
| `value_kind` | TEXT | NOT NULL, `value_kind_valid` | `NUMERIC` / `TEXT` |
| `unit` | TEXT | NULL | |
| `label` | TEXT | NOT NULL | 展示名 |
| `is_active` | BOOLEAN | NOT NULL DEFAULT true | |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK value_kind_valid`: `value_kind IN ('NUMERIC','TEXT')`
- **唯一**：`uq_metric_definitions_code_scope`(`metric_code`,`scope`)
- **注意**：**累计值与窗口值不可比较、不可相加** ⇒ 定义颗粒度必须含 `scope`，不能只按 `metric_code` 定义。

### 3.2 `metric_observations`（**只追加，权威历史**）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `publication_id` | UUID | FK publications RESTRICT, NOT NULL | |
| `metric_definition_id` | UUID | FK metric_definitions RESTRICT, NOT NULL | |
| `observed_at` | TIMESTAMPTZ | NOT NULL | **观测时刻**（非录入时刻，模板 §4） |
| `value_num` | NUMERIC(30,10) | **NULL 允许** | 数值类用；缺失 → NULL（**不填 0**） |
| `value_text` | TEXT | NULL | 文本类用（`COMMENT_TEXT`/`NOTE_URL` 等） |
| `raw_value` | TEXT | NULL | 平台原样显示值（保留约数，模板 §6） |
| `precision` | TEXT | NULL, `precision_valid` | `EXACT` / `APPROXIMATE` |
| `availability` | TEXT | NOT NULL, `availability_valid` | 六态（模板 §8） |
| `source_type` | TEXT | NOT NULL, `source_type_valid` | 五态（模板 §9） |
| `evidence_ref` | TEXT | NULL | 证据引用 |
| `import_id` | UUID | FK metric_imports RESTRICT, NULL | 来自导入时为导入批次 |
| `recorded_at` | TIMESTAMPTZ | NOT NULL DEFAULT now() | **录入时刻**（与 `observed_at` 不同，WP38 断言点） |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK availability_valid`: `availability IN ('ACQUIRED','NOT_PROVIDED','NO_PERMISSION','NOT_APPLICABLE','FETCH_FAILED','PENDING')`
- `CHECK source_type_valid`: `source_type IN ('MANUAL','CSV_IMPORT','XLSX_IMPORT','SCREENSHOT_OCR','AUTO_API')`
- `CHECK precision_valid`: `precision IS NULL OR precision IN ('EXACT','APPROXIMATE')`
- `CHECK value_shape`：`availability='ACQUIRED'` 时（数值类须 `value_num IS NOT NULL`；文本类须 `value_text IS NOT NULL`）—— 由服务层与 CHECK 共同守（跨表判定 `value_kind`，**CHECK 不能跨表** ⇒ 只有本表可判的写入 CHECK，其余进服务层 + 判据）
- **唯一（幂等）**：`uq_metric_observations_natural`(`publication_id`,`metric_definition_id`,`observed_at`,`source_type`) —— **重复导入的幂等键**
- **索引**：`ix_metric_observations_pub_obs`(`publication_id`,`observed_at`)、`ix_metric_observations_import`(`import_id`)
- **触发器**：挂 `rednote_reject_mutation()`（**只追加**，UPDATE/DELETE 被拒）

### 3.3 `metric_resolutions`（冲突处理）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `publication_id` | UUID | FK publications RESTRICT, NOT NULL | |
| `metric_definition_id` | UUID | FK metric_definitions RESTRICT, NOT NULL | |
| `observed_at` | TIMESTAMPTZ | NOT NULL | |
| `chosen_observation_id` | UUID | FK metric_observations RESTRICT, NOT NULL | 采信值 |
| `superseded_observation_id` | UUID | FK metric_observations RESTRICT, NULL | 被取代值（**保留，不删**） |
| `resolution_rule` | TEXT | NOT NULL, `rule_valid` | `LATEST_WINS` / `MANUAL` / `SOURCE_PRIORITY` |
| `resolved_by` | UUID | FK users RESTRICT, NULL | |
| `note` | TEXT | NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK rule_valid`: `resolution_rule IN ('LATEST_WINS','MANUAL','SOURCE_PRIORITY')`
- **唯一**：`uq_metric_resolutions_natural`(`publication_id`,`metric_definition_id`,`observed_at`)
- **关键**：**旧快照与原始来源保留**（`superseded_observation_id` 指向真行，非标记删除）

### 3.4 `metric_latest`（**可重建投影缓存**）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `publication_id` | UUID | FK publications RESTRICT, PK(复合) | |
| `metric_definition_id` | UUID | FK metric_definitions RESTRICT, PK(复合) | |
| `observation_id` | UUID | FK metric_observations RESTRICT, NOT NULL | 当前采信行 |
| `value_num` | NUMERIC(30,10) | NULL | 冗余，便于查询 |
| `value_text` | TEXT | NULL | |
| `observed_at` | TIMESTAMPTZ | NOT NULL | |
| `label` | TEXT | NULL | 阈值标签（见 3.5） |
| `rebuilt_at` | TIMESTAMPTZ | NOT NULL | 最近重建时刻 |

- **PK**：(`publication_id`,`metric_definition_id`)
- **排序口径**：按 `observed_at` **最大化**（**不是** `recorded_at`）—— WP38 核心断言「观测时间与录入时间不同时的排序」
- **可重建**：`TRUNCATE` + 从 `metric_resolutions`/`metric_observations` 重算；**不作历史权威**

### 3.5 `publication_labels`（阈值标签）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `publication_id` | UUID | FK publications RESTRICT, NOT NULL | |
| `metric_definition_id` | UUID | FK metric_definitions RESTRICT, NOT NULL | |
| `label` | TEXT | NOT NULL, `label_valid` | `POTENTIAL` / `QUALITY` / `BELOW_THRESHOLD` / `PENDING` |
| `threshold_version` | TEXT | NOT NULL | 阈值口径版本 |
| `computed_at` | TIMESTAMPTZ | NOT NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK label_valid`: `label IN ('POTENTIAL','QUALITY','BELOW_THRESHOLD','PENDING')`
- **唯一**：`uq_publication_labels_natural`(`publication_id`,`metric_definition_id`,`threshold_version`)
- **边界断言**：50/51/100/101 边界；**缺失 → `PENDING`，不归零、不进 `BELOW_THRESHOLD`**

---

## 4. 评论（1 表，WP41）

### 4.1 `comment_records`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `publication_id` | UUID | FK publications RESTRICT, NOT NULL | |
| `platform_comment_id` | TEXT | NULL | 平台评论 ID（**存在则唯一**） |
| `content_digest` | TEXT | NULL | 内容摘要哈希（弱去重用） |
| `content_text` | TEXT | NULL | |
| `like_count` | BIGINT | NULL, `CHECK >= 0` | **不做因果证据**（04 红线） |
| `posted_at` | TIMESTAMPTZ | NULL | |
| `dedup_mode` | TEXT | NOT NULL, `dedup_mode_valid` | `PLATFORM_ID` / `WEAK_DIGEST` |
| `weak_dedup_at` | TIMESTAMPTZ | NULL | 弱去重的时间窗锚 |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK dedup_mode_valid`: `dedup_mode IN ('PLATFORM_ID','WEAK_DIGEST')`
- **部分唯一**：`uq_comment_records_platform_id`(`platform_comment_id`) `WHERE platform_comment_id IS NOT NULL`（**弱去重行不参与唯一**，且必须 `dedup_mode='WEAK_DIGEST'` **标记**）
- **索引**：`ix_comment_records_pub`(`publication_id`)、`ix_comment_records_digest`(`content_digest`,`posted_at`)
- **禁止**：把 `like_count` 高当作因果证据（`strategy_evidence` 里不得据此自动 `SUPPORTED`）

---

## 5. 导入（2 表，WP39）

### 5.1 `metric_imports`（批次）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `import_kind` | TEXT | NOT NULL, `kind_valid` | `CSV` / `XLSX` / `SCREENSHOT_OCR` |
| `state` | TEXT | NOT NULL, `state_valid` | `DRAFT` / `PREVIEWED` / `CONFIRMED` / `FAILED` |
| `source_file_id` | UUID | FK files RESTRICT, NULL | |
| `template_version` | TEXT | NOT NULL | 模板版本（`1.0`） |
| `row_count` | INTEGER | NOT NULL DEFAULT 0 | |
| `error_row_count` | INTEGER | NOT NULL DEFAULT 0 | |
| `idempotency_key` | TEXT | NULL | **重复导入幂等键** |
| `confirmed_at` | TIMESTAMPTZ | NULL | |
| `created_by` | UUID | FK users RESTRICT, NULL | |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |
| `lock_version` | INTEGER | NOT NULL DEFAULT 0 | |

- `CHECK state_valid`: `state IN ('DRAFT','PREVIEWED','CONFIRMED','FAILED')`
- `CHECK kind_valid`: `import_kind IN ('CSV','XLSX','SCREENSHOT_OCR')`
- `CHECK counts_non_negative`: `row_count >= 0 AND error_row_count >= 0 AND error_row_count <= row_count`
- **部分唯一（幂等）**：`uq_metric_imports_idempotency`(`idempotency_key`) `WHERE idempotency_key IS NOT NULL`
- **状态机**：**预览确认后才写正式观测** ⇒ 只有 `CONFIRMED` 才允许把行写入 `metric_observations`

### 5.2 `metric_import_rows`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `import_id` | UUID | FK metric_imports RESTRICT, NOT NULL（ON DELETE RESTRICT） | |
| `row_no` | INTEGER | NOT NULL, `CHECK >= 1` | |
| `raw_line` | TEXT | NULL | 原始行（留证） |
| `payload` | JSONB | NOT NULL | 解析后的 11 列 |
| `status` | TEXT | NOT NULL, `status_valid` | `OK` / `ERROR` / `SKIPPED` |
| `warnings` | JSONB | NOT NULL DEFAULT '[]' | 校验告警码（见模板 §12） |
| `errors` | JSONB | NOT NULL DEFAULT '[]' | |
| `confidence` | NUMERIC(5,4) | NULL | **OCR 专用**（截图识别置信度） |
| `observation_id` | UUID | FK metric_observations RESTRICT, NULL | 确认后回填 |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK status_valid`: `status IN ('OK','ERROR','SKIPPED')`
- `CHECK confidence_range`: `confidence IS NULL OR (confidence >= 0 AND confidence <= 1)`
- **唯一**：`uq_metric_import_rows_no`(`import_id`,`row_no`)
- **索引**：`ix_metric_import_rows_status`(`import_id`,`status`)
- **口径**：校验规则**必须复用** `tools/m0/wp03_data/validate_import.py` 的词表与告警码，**不得另写一套**

---

## 6. 实验与策略（3 表，WP41）

### 6.1 `experiments`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `name` | TEXT | NOT NULL | |
| `hypothesis` | TEXT | NULL | |
| `state` | TEXT | NOT NULL, `state_valid` | `DRAFT` / `RUNNING` / `CLOSED` |
| `started_at` / `closed_at` | TIMESTAMPTZ | NULL | |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |
| `lock_version` | INTEGER | NOT NULL DEFAULT 0 | |

- `CHECK state_valid`: `state IN ('DRAFT','RUNNING','CLOSED')`

### 6.2 `experiment_assignments`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `experiment_id` | UUID | FK experiments RESTRICT, NOT NULL | |
| `content_item_id` | UUID | FK RESTRICT, NOT NULL | |
| `arm` | TEXT | NOT NULL | 实验组标识 |
| `assigned_at` | TIMESTAMPTZ | NOT NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- **唯一**：`uq_experiment_assignments_item`(`experiment_id`,`content_item_id`)

### 6.3 `strategy_evidence`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `experiment_id` | UUID | FK experiments RESTRICT, NULL | |
| `strategy_ref` | TEXT | NOT NULL | 候选策略引用 |
| `state` | TEXT | NOT NULL, `state_valid` | `CANDIDATE` / `SUPPORTED` / `UNSUPPORTED` / `INCONCLUSIVE` |
| `evidence_grade` | TEXT | NOT NULL, `grade_valid` | `EXPERIMENT` / `OBSERVATIONAL` / `INSUFFICIENT` |
| `note` | TEXT | NULL | |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK state_valid`: `state IN ('CANDIDATE','SUPPORTED','UNSUPPORTED','INCONCLUSIVE')`
- `CHECK grade_valid`: `evidence_grade IN ('EXPERIMENT','OBSERVATIONAL','INSUFFICIENT')`
- **产品红线**：**缺失条件 → `INCONCLUSIVE` + `evidence_grade='INSUFFICIENT'`，不得自动 `SUPPORTED`**（04 原文）

---

## 7. 报告（2 表，WP44）

### 7.1 `reports`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `report_kind` | TEXT | NOT NULL, `kind_valid` | `OVERVIEW` / `STAGE` / `CLOSURE` |
| `template_version` | TEXT | NOT NULL | |
| `current_version_no` | INTEGER | NOT NULL DEFAULT 1, `CHECK >= 1` | 指向 `report_versions` |
| `created_at` / `updated_at` | TIMESTAMPTZ | NOT NULL | |
| `lock_version` | INTEGER | NOT NULL DEFAULT 0 | |

- `CHECK kind_valid`: `report_kind IN ('OVERVIEW','STAGE','CLOSURE')`

### 7.2 `report_versions`（**不可变**）

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `report_id` | UUID | FK reports RESTRICT, NOT NULL | |
| `version_no` | INTEGER | NOT NULL, `CHECK >= 1` | |
| `cutoff_at` | TIMESTAMPTZ | NOT NULL | **截止快照时刻** |
| `metrics_snapshot` | JSONB | NOT NULL | **固化快照**（非渲染时现算） |
| `body_markdown` | TEXT | NOT NULL | |
| `content_hash` | TEXT | NOT NULL | |
| `generated_at` | TIMESTAMPTZ | NOT NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- **唯一**：`uq_report_versions_no`(`report_id`,`version_no`)
- **触发器**：挂 `rednote_reject_mutation()`（**旧报告不因新指标改写**，AC-29）
- **关键**：`metrics_snapshot` **必须固化** —— 若渲染时现算，历史报告会随新指标漂移

---

## 8. 结项（1 表，WP45）

### 8.1 `project_closures`

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | UUID | PK | |
| `project_id` | UUID | FK projects RESTRICT, NOT NULL | |
| `closure_type` | TEXT | NOT NULL, `type_valid` | `COMPLETED` / `TERMINATED` |
| `outstanding_snapshot` | JSONB | NOT NULL DEFAULT '{}' | 结项时的未完成项快照 |
| `closed_at` | TIMESTAMPTZ | NOT NULL | |
| `closed_by` | UUID | FK users RESTRICT, NULL | |
| `reopened_at` | TIMESTAMPTZ | NULL | **另起一行记录，不覆盖** |
| `reopen_reason` | TEXT | NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL | |

- `CHECK type_valid`: `closure_type IN ('COMPLETED','TERMINATED')`
- `CHECK reopen_shape`: `(reopened_at IS NULL) = (reopen_reason IS NULL)`
- **索引**：`ix_project_closures_project`(`project_id`,`closed_at`)
- **关键**：**重开是追加新行**（新 `project_closures` 行 + `reopened_at`），**不 UPDATE 旧行** —— 抹掉旧结项事实即违规（AC-27 / 02 明文）

---

## 9. 复用清单（**不新建**，重复建表是 M4 最易犯的错）

| 表 | 已由 | M4 谁复用 |
|---|---|---|
| `knowledge_proposals` | WP23 | WP42（AI 产出 → 人工确认 → 回写） |
| `knowledge_publications` | M2 | WP42 |
| `version_nodes` | WP11 | WP36/37 版本映射 |
| `provider_calls` | WP16 | WP40 适配器调用 |
| `cost_estimates` | WP25 | WP43 |
| `usage_ledger` | WP25 | WP43（对账，**已有 `uq_usage_ledger_idempotency`**） |
| `project_budgets` / `budget_reservations` | WP25 | WP43 预算接口 |
| `projects`（`target_delivery_count`/`target_publication_count`） | M1 | WP44 数量口径 |

---

## 10. 与设计基线的差异登记

⚠️ **`Rednote研发计划_02_数据结构与一致性.md` 不在当前磁盘上**（编制时全盘未找到）。
M4 计划 §5 的 19 表清单是该文档的转述。**本草案的列定义由以下三源推导**：

1. M4 计划 §5（表名与归属）与 §7（逐包任务与验收断言）；
2. `templates/metric_import_field_guide.md`（指标 11 列与全部词表，**权威**）；
3. 既有 22 模块的 ORM 约定（§0.1）。

**⇒ 待 02 文档重新可得时，须逐字段对账本草案，差异写入本节。** 在此之前，
**本草案的列级细节属推导，不是 04/02 原文** —— 引用时须标明。

---

## 11. 迁移归属与顺序（主 agent 独占）

| 序 | 包 | 表 | 备注 |
|---|---|---|---|
| 0 | **WP35**（M3） | export_items | **1 表**。2026-10-14 老大拍板（Q2）自 WP36 挪出：WP36 硬依赖 WP35，而 WP35 需要冻结快照的载体，原归属形成循环依赖。M4 总数仍 19 |
| 1 | WP36 | koc_records, deliveries, publications, publication_links | 4 表（原 5 表，`export_items` 已挪出）。`deliveries` 对 `export_items` 的引用见 §1.3 |
| 2 | WP37 | （无新表；用 publication_links + publications 的列） | **纯逻辑包** |
| 3 | WP38 | metric_definitions, metric_observations, metric_resolutions, metric_latest, publication_labels | 5 表（枢纽） |
| 4 | WP39 | metric_imports, metric_import_rows | 2 表 |
| 5 | WP41 | comment_records, experiments, experiment_assignments, strategy_evidence | 4 表 |
| 6 | WP44 | reports, report_versions | 2 表 |
| 7 | WP45 | project_closures | 1 表 |

**合计 19 表**（1 + 4 + 5 + 2 + 4 + 2 + 1 = 19；WP40 无新表，纯适配器逻辑）。每个迁移并入后过 §4.3 往返判据。

---

## 12. 自检清单（建迁移前逐条打勾）

- [ ] 表数 = **19**，与 §11 逐条对齐（**磁盘核对，不数文档**）
- [ ] 每表主键为 UUID；每外键 `ON DELETE RESTRICT`
- [ ] 状态列有 `CHECK state_valid`，取值域为**字面量**（ISS-191）
- [ ] 金额列用 `NUMERIC(20,8)`；数量列用 `NUMERIC(30,10)`
- [ ] 观测值列**可空**且无「默认 0」
- [ ] `metric_observations` / `report_versions` 挂 `rednote_reject_mutation()`，且**不重建函数**
- [ ] 幂等索引为**部分唯一**（`WHERE … IS NOT NULL`）
- [ ] `downgrade` 按 `owned_functions()` 删函数，**不用** `all_trigger_ddl()` 并集
- [ ] 复用清单 §9 的 8 张表**一张都没新建**
- [ ] 每个迁移过 `upgrade head → downgrade base → upgrade head`
