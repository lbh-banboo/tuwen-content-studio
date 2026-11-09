# WP36 DDL 草案（逐包，4 张新建表）

> 编制：2026-10-19　工作包：WP36（KOC 记录 / 交付与替换 / 发布草稿与版本映射）
> **性质：文本草案，不得据此建 `migrations/versions/`。** 建迁移是主 agent 独占动作（M4 接缝 §4.1）。
>
> ⚠️ **形态说明**：M4 用的是合订 `docs/m4/M4-DDL草案.md`，而该文件**不在「分支可写的三处」之内** ——
> 这是一处口径空洞。主会话裁定（Q7）：分支交**逐包** `docs/m4/WP36-ddl-草案.md`（取 M3 形态），
> 由主 agent 后续合并进 `M4-DDL草案.md`，并把 §3 的差异逐条登记进该文件的差异登记节。
>
> ⚠️ **下方 §1 的 SQL 是从磁盘上的 `modules/delivery/models.py` 由 SQLAlchemy 编译导出的**，
> 不是手写的 —— 手写会与模型分叉，而分叉是静默的。§2 的触发器 DDL 的唯一来源是
> `modules/delivery/ddl.py`（迁移请直接引用它的 `all_trigger_ddl()`，**不要抄 SQL 文本**）。
> 核对命令见 §4。

---

## 0. 口径

| 项 | 本包取值 | 与 `M4-DDL草案.md` §0.1 的差异 |
|---|---|---|
| 主键 | `UUID PRIMARY KEY DEFAULT gen_random_uuid()`（`uuid_pk()`） | 一致 |
| 外键 | 一律 `ON DELETE RESTRICT`，**零 CASCADE**（11 条，有判据） | 一致 |
| 时间 | `TIMESTAMPTZ`；`created_at DEFAULT now()`；可变表加 `updated_at`（带 `onupdate`） | 一致 |
| 乐观锁 | **`lock_version BIGINT NOT NULL DEFAULT 1`**（`lock_version_column()`） | ★ **不一致**。草案 §0.1（`:20`）写 `INTEGER NOT NULL DEFAULT 0`，而平台助手 `platform/base.py:79-90` 是 `BigInteger` / `server_default text("1")`，docstring 自述「从 1 开始递增，对外映射为 ETag…为什么用 bigint 而不是 int」。**以磁盘为准，草案那一行已过期**（铁律 14） |
| 状态列 | `VARCHAR(20)` + `CHECK (… IN (…))`，约束名 `*_valid`；迁移里取值域**写死字面量**（ISS-191） | 一致（草案写 TEXT，本包用 `String(20)`，与 `exports.export_kind` 同形） |
| 哈希列 | `VARCHAR(64)` + `CHECK (char_length(x) = 64 AND x ~ '^[0-9a-f]+$')` | ★ **收紧**。草案写 TEXT 无形状约束；WP35 实际落的是这个形状（`exports/models.py:94-96`、`:149-153`）。两张表的同名列**必须同形**，否则「复制过来的指纹」无法与重算值比对 |
| 不可变性 | 本包**没有整行不可变表** ⇒ 不调 `guard_immutable`、不挂 `trg_*_immutable`、**不碰** `rednote_reject_mutation()`（ISS-79）。`deliveries` 的不可变性是**列级**的（§2.1） | ★ 草案未涉及列级不可变 |

---

## 1. 四张表（磁盘导出，逐字可核）

### 1.1 `koc_records`

```sql
CREATE TABLE koc_records (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    project_id UUID NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    platform_home_url TEXT,
    home_url_canonical TEXT,
    contact_ref TEXT,
    note TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lock_version BIGINT DEFAULT 1 NOT NULL,
    CONSTRAINT pk_koc_records PRIMARY KEY (id),
    CONSTRAINT ck_koc_records_display_name_not_blank
        CHECK (char_length(btrim(display_name)) >= 1),
    CONSTRAINT fk_koc_records_project_id_projects
        FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
);
CREATE INDEX ix_koc_records_project ON koc_records (project_id);
CREATE INDEX ix_koc_records_home_canonical ON koc_records (home_url_canonical);
```

- `display_name` 用 `VARCHAR(200)`（草案写 TEXT 无长度）+ `btrim` 非空 CHECK：空白名在应用层 strip 后是空串，但裸 SQL 能绕过应用层。
- **不做** `platform_home_url` 唯一（草案 `:54` 明写「同名不同人、同链不同参 → 唯一约束会误拒」）。有一条断言核对同一链接可挂两位 KOC。
- `home_url_canonical` **本包只建列、恒 NULL**（WP37 规范器产）。API 层 `extra="forbid"`，客户端传它当场 422。
- **禁止**任何存放密码/令牌的列（草案 `:55` 明文）—— 有一条断言按列名机械核对。

### 1.2 `deliveries`（本包核心）

```sql
CREATE TABLE deliveries (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    project_id UUID NOT NULL,
    koc_record_id UUID,
    export_item_id UUID NOT NULL,
    content_item_id UUID NOT NULL,
    version_node_id UUID NOT NULL,
    state VARCHAR(20) NOT NULL,
    eligibility_hash VARCHAR(64) NOT NULL,
    replaces_delivery_id UUID,
    idempotency_key VARCHAR(160),
    request_hash VARCHAR(64) NOT NULL,
    delivered_at TIMESTAMP WITH TIME ZONE,
    cancelled_at TIMESTAMP WITH TIME ZONE,
    delivered_by UUID,
    created_by UUID NOT NULL,
    note TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lock_version BIGINT DEFAULT 1 NOT NULL,
    CONSTRAINT pk_deliveries PRIMARY KEY (id),
    CONSTRAINT ck_deliveries_state_valid
        CHECK (state IN ('PREPARED', 'DELIVERED', 'CANCELLED')),
    CONSTRAINT ck_deliveries_state_timestamps
        CHECK ((state = 'DELIVERED') = (delivered_at IS NOT NULL)
           AND (state = 'CANCELLED') = (cancelled_at IS NOT NULL)),
    CONSTRAINT ck_deliveries_delivered_by_shape
        CHECK ((delivered_at IS NULL) = (delivered_by IS NULL)),
    CONSTRAINT ck_deliveries_no_self_replace
        CHECK (replaces_delivery_id IS DISTINCT FROM id),
    CONSTRAINT ck_deliveries_eligibility_hash_shape
        CHECK (char_length(eligibility_hash) = 64 AND eligibility_hash ~ '^[0-9a-f]+$'),
    CONSTRAINT ck_deliveries_request_hash_shape
        CHECK (char_length(request_hash) = 64 AND request_hash ~ '^[0-9a-f]+$'),
    CONSTRAINT fk_deliveries_project_id_projects
        FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_deliveries_koc_record_id_koc_records
        FOREIGN KEY(koc_record_id) REFERENCES koc_records (id) ON DELETE RESTRICT,
    CONSTRAINT fk_deliveries_export_item_id_export_items
        FOREIGN KEY(export_item_id) REFERENCES export_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_deliveries_content_item_id_content_items
        FOREIGN KEY(content_item_id) REFERENCES content_items (id) ON DELETE RESTRICT,
    CONSTRAINT fk_deliveries_version_node_id_version_nodes
        FOREIGN KEY(version_node_id) REFERENCES version_nodes (id) ON DELETE RESTRICT,
    CONSTRAINT fk_deliveries_replaces_delivery_id_deliveries
        FOREIGN KEY(replaces_delivery_id) REFERENCES deliveries (id) ON DELETE RESTRICT,
    CONSTRAINT fk_deliveries_delivered_by_users
        FOREIGN KEY(delivered_by) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_deliveries_created_by_users
        FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_deliveries_idempotency ON deliveries (project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX uq_deliveries_replaces ON deliveries (replaces_delivery_id)
    WHERE replaces_delivery_id IS NOT NULL;
CREATE INDEX ix_deliveries_project_state ON deliveries (project_id, state);
CREATE INDEX ix_deliveries_export_item ON deliveries (export_item_id);
CREATE INDEX ix_deliveries_content ON deliveries (project_id, content_item_id);
```

**逐条理由**

| 对象 | 为什么 |
|---|---|
| `export_item_id NOT NULL` | 主会话裁定 Q1。方向由 WP35 写死（`exports/models.py:21-25`）。NOT NULL 的代价是「无导出不能交付」，收益是①「固定导出版本」结构上不可能错版；②本包**零「当前版本」读法实现** |
| `koc_record_id` 可空 | 草案即如此（允许先建交付后指派 KOC）。⚠️ 但它是**冻结列**（§2.1）—— 一旦落行就不能换人，换人 = 另一次交付事实 |
| `ck_deliveries_state_timestamps` 是**双向等式** | 单向判会放过「`DELIVERED` 却没时间戳」。这条 CHECK 是「下载不计交付」的**结构性**载体之一：想置 `delivered_at` 就必须同时推 `state` |
| `ck_deliveries_delivered_by_shape` | 本方案新增（与 `project_closures.reopen_shape` 同形）。原型要「操作人」列，而「有交付时间却没有操作人」是半截数据 |
| `ck_deliveries_no_self_replace` 用 `IS DISTINCT FROM` | `replaces_delivery_id` 可空；`NULL <> id` 求值为 NULL ⇒ CHECK 放行。此处恰好无害，但**口径必须与触发器一致**（混用两种写法迟早在某处咬人） |
| `uq_deliveries_idempotency` **两列** | WP35 的同名索引是三列（`(project_id, idempotency_key, content_item_id)`），因为那是「一键一组 N 行」；`deliveries` 是「一键一行」⇒ 两列即可 |
| `uq_deliveries_replaces` **部分唯一** | ★ **草案没有，本方案新增**。一条旧交付最多被替换一次 —— 否则「替换链」变「替换树」，「当前有效的那一版」就有两个答案 |
| **刻意不做** `(content_item_id, koc_record_id)` 唯一 | 一条内容可分配给多位 KOC（原型 `static/delivery-koc.html:148`），交付还可超额（`static/delivery.html:42`「52 / 50 已交付（超额）」）。加唯一会误拒合法业务 |

### 1.3 `publications`

```sql
CREATE TABLE publications (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    project_id UUID NOT NULL,
    delivery_id UUID,
    publication_key VARCHAR(64) NOT NULL,
    note_id TEXT,
    note_url TEXT,
    url_canonical TEXT,
    state VARCHAR(20) NOT NULL,
    content_state VARCHAR(20) NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE,
    idempotency_key VARCHAR(160),
    request_hash VARCHAR(64) NOT NULL,
    created_by UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    lock_version BIGINT DEFAULT 1 NOT NULL,
    CONSTRAINT pk_publications PRIMARY KEY (id),
    CONSTRAINT ck_publications_state_valid
        CHECK (state IN ('DRAFT', 'PUBLISHED', 'UNKNOWN')),
    CONSTRAINT ck_publications_content_state_valid
        CHECK (content_state IN ('VERIFIED', 'UNVERIFIED')),
    CONSTRAINT ck_publications_publication_key_format
        CHECK (publication_key ~ '^[A-Za-z0-9_.\-]{1,64}$'),
    CONSTRAINT ck_publications_published_shape
        CHECK ((state = 'PUBLISHED') = (published_at IS NOT NULL)),
    CONSTRAINT ck_publications_request_hash_shape
        CHECK (char_length(request_hash) = 64 AND request_hash ~ '^[0-9a-f]+$'),
    CONSTRAINT uq_publications_project_key UNIQUE (project_id, publication_key),
    CONSTRAINT fk_publications_project_id_projects
        FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_publications_delivery_id_deliveries
        FOREIGN KEY(delivery_id) REFERENCES deliveries (id) ON DELETE RESTRICT,
    CONSTRAINT fk_publications_created_by_users
        FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_publications_idempotency ON publications (project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX ix_publications_project_state ON publications (project_id, state);
CREATE INDEX ix_publications_canonical ON publications (url_canonical);
CREATE INDEX ix_publications_delivery ON publications (delivery_id);
```

**逐条理由**

| 对象 | 为什么 |
|---|---|
| `uq_publications_project_key` **必须显式写全名** | `platform/base.py:31` 的 `uq` 约定是 `uq_%(table_name)s_%(column_0_N_name)s`（**不含** `constraint_name` 令牌）⇒ 不传名会自动变成 `uq_publications_project_id_publication_key`。WP35 为此留了告警（`exports/models.py:160`）。有一条判据按**精确名**查 `pg_constraint` |
| 业务键与幂等键**并存** | 审计 `:62` 原文「业务键保留」。两条各管一头：业务键挡「同一条笔记登记两次」（409 `DUPLICATE_PUBLICATION`），幂等键挡「同键异体」（409 `IDEMPOTENCY_CONFLICT`） |
| `publication_key` 格式**不得自行放宽** | WP39 的指标导入按这个键找发布。格式与契约草案 `:1103` 的 pattern 同源，`types.py` 另有 Python 侧同义正则 + 对账断言 |
| `ck_publications_published_shape` | 本方案新增，与 `deliveries` 的时间戳 CHECK 同形 |
| `content_state` 取值域**只有两个值** | 草案 `:132` 的红线：「`content_state=UNVERIFIED` **≠ 已删除**。抓取失败只落 `UNVERIFIED`，**不得**推成删除」。本包不做抓取 ⇒ 一律落 `UNVERIFIED`，且取值域里**结构上不存在**任何「删除」取值（有一条断言机械核对取值域里不含 `DELET*`）。API 层 `extra="forbid"`，客户端不能自己声称 `VERIFIED` |
| `url_canonical` 只建列恒 NULL | WP37 规范器产。API 层 `extra="forbid"` |
| `created_by NOT NULL` | ★ 草案缺。登记是一次人工动作，没有「谁登记的」就无法审计 |

### 1.4 `publication_links`（本包只建表，逻辑归 WP37）

```sql
CREATE TABLE publication_links (
    id UUID DEFAULT gen_random_uuid() NOT NULL,
    publication_id UUID NOT NULL,
    raw_url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    link_kind VARCHAR(20) NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_publication_links PRIMARY KEY (id),
    CONSTRAINT ck_publication_links_link_kind_valid
        CHECK (link_kind IN ('SHORT', 'LONG', 'OTHER')),
    CONSTRAINT fk_publication_links_publication_id_publications
        FOREIGN KEY(publication_id) REFERENCES publications (id) ON DELETE RESTRICT
);
CREATE INDEX ix_publication_links_publication ON publication_links (publication_id);
CREATE INDEX ix_publication_links_raw ON publication_links (raw_url);
CREATE INDEX ix_publication_links_canonical ON publication_links (canonical_url);
```

★ **两处对草案的实质偏离**（主会话裁定 Q3 / Q4）：

1. **`uq_publication_links_canonical`（全局唯一）不建**，只建普通索引 `ix_publication_links_canonical`。
   草案 `:147` 要「短链与长链规范化后不得重复」，而 WP37 的验收写「**跨项目同笔记需管理者确认**」—— 一条**全局**唯一会让第二个项目**物理上**无法登记同一条笔记，确认分支永远不可达。两者不能同时为真。
   唯一性判定留给 WP37 在服务层按「项目内唯一 + 跨项目需确认」实现，届时由 WP37 的迁移补**项目内**部分唯一。**先宽后紧只需 ADD，先紧后宽要写 DROP INDEX 迁移**（改他人已落库约束，代价更高）。
   有一条判据核对 `publication_links` 上**只有主键**是唯一索引，另有一条真跑用例插入两行同 `canonical_url` 并断言放行。
2. **不挂不可变触发器**。表形态上是只追加（没有 `updated_at`/`lock_version`），但 WP37 要往里写、可能要改 `link_kind` —— 挂上会卡住它。

---

## 2. 触发器（唯一来源 `modules/delivery/ddl.py`）

> ⚠️ **迁移请直接引用 `delivery_ddl.all_trigger_ddl()`，不要抄下面的 SQL 文本。**
> 五元组形状 `(表名, 函数DDL, 触发器DDL, 触发器DROP, 函数DROP)`，与
> `modules/exports/ddl.py` / `modules/feedback/ddl.py` 同形（有判据核对）。
> `downgrade` 按 `consistency_function_names()` 删函数（3 个），**不含**
> `rednote_reject_mutation`（跨包共用，ISS-79；本包压根没用它）。

### 2.1 `trg_deliveries_frozen_columns`（列级冻结，★ 本包最核心）

```
CREATE TRIGGER trg_deliveries_frozen_columns
BEFORE UPDATE ON deliveries
FOR EACH ROW
EXECUTE FUNCTION rednote_guard_delivery_frozen_columns()
```

函数体**由 `models.frozen_delivery_columns()` 逐列生成**，形状为：

```plpgsql
CREATE OR REPLACE FUNCTION rednote_guard_delivery_frozen_columns() RETURNS trigger AS $$
BEGIN
    IF NEW.<冻结列> IS DISTINCT FROM OLD.<冻结列> THEN
        RAISE EXCEPTION USING
            MESSAGE = '交付记录 ' || OLD.id::text || ' 的 <冻结列> 是冻结列，不可改写（…）',
            ERRCODE = '23514';
    END IF;
    -- …每个冻结列一段，共 12 段…
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
```

**冻结列（12，由磁盘派生，不是手写名单）**：
`id` / `project_id` / `koc_record_id` / `export_item_id` / `content_item_id` /
`version_node_id` / `eligibility_hash` / `replaces_delivery_id` /
`idempotency_key` / `request_hash` / `created_by` / `created_at`

**可变列白名单（7，`models.MUTABLE_DELIVERY_COLUMNS`）**：
`state` / `delivered_at` / `delivered_by` / `cancelled_at` / `note` / `updated_at` / `lock_version`

| 细节 | 为什么 |
|---|---|
| 名单 = **全部列 − 白名单**，由 `frozen_delivery_columns()` 派生 | 手写名单在**加列**时会静默漏掉那一列 ⇒ 表上多出一扇没人知道的后门，而库全绿、测试全绿（铁律 14 的本体形态）。新增一列若不显式进白名单，**默认被冻结**（fail-closed）。`modules/content/models.py:18` 原文：「『只许改某一列』的触发器不好写…写错的后果是历史事实可以被整行改写 —— 那是一张『看起来不可变、实际有一扇后门』的表」 |
| 全部用 `IS DISTINCT FROM`，**零 `<>`** | 可空列上 `NULL <> NULL` 求值为 **NULL**，`IF NULL THEN` 不进分支 ⇒ 对 `koc_record_id` / `replaces_delivery_id` / `idempotency_key` 这三列，`<>` 写法的防线**对空值全面失效**。本仓已为此在四份 docstring 里留过告警（`modules/baseline/ddl.py:69-78`、`modules/content/ddl.py:167-171`、`:291`、`modules/knowledge/ddl.py:120`）。有两条判据盯这一点：①DDL 文本里逐列有 `IS DISTINCT FROM` 且 `count == len(冻结列)` 且零 `<>`；②真库用例把一个非空的冻结可空列改成 NULL 并断言被拒（**这是 `IS DISTINCT FROM` 与 `<>` 的唯一区分点**） |
| `RETURN NEW`，不是 `RETURN NULL` | **BEFORE 触发器返回 NULL 会取消这次 UPDATE** —— 那会把「拒绝」变成「静默什么都没发生」，调用方拿到 `rowcount = 0` 却不知道为什么 |
| 是**普通**触发器，不是 `CONSTRAINT TRIGGER` | 它守的是「这一次 UPDATE 改了不该改的列」，当场就能判，不需要延迟；延迟反而会让错误在 COMMIT 才爆 |
| 建触发器前**必须先 DROP** | `CREATE TRIGGER` 不幂等（实测 `DuplicateObject`）。`all_trigger_ddl()` 的第 4 元就是 DROP 语句，夹具与迁移都先执行它 |

### 2.2 `trg_deliveries_consistency`（延迟约束触发器，四条判据）

```
CREATE CONSTRAINT TRIGGER trg_deliveries_consistency
AFTER INSERT ON deliveries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_delivery_consistency()
```

| # | 判据 | FK 为什么管不了 |
|---|---|---|
| 1 | `content_item_id` 指向的内容必须属于 `NEW.project_id` | FK 只保证「被引用的行存在」，不保证归属（`exports/ddl.py:67` 对同一类问题的原话） |
| 2 | ★ `project_id` / `content_item_id` / `version_node_id` / `eligibility_hash` **必须逐列等于** `export_item_id` 那一行 | 三个冻结字段是**复制**来的。没有这条判据时，裸 SQL 可以插出「声称交付了 v3，而被引的导出行冻结的是 v1」—— 「固定导出版本」当场落空 |
| 3 | `koc_record_id`（给了才判）必须属于 `NEW.project_id` | 同 1 |
| 4 | `replaces_delivery_id`（给了才判）指向的旧行必须**同项目、同内容** | 替换链跨内容会让可追溯性变成噪声 |

**为什么必须延迟**：被替换的旧行与新行可能在同一事务里按任意顺序插入，逐行即时检查会误报。
⚠️ 探针要看到它拒绝，必须在事务里 `SET CONSTRAINTS ALL IMMEDIATE`（否则「坏值写进去了」会被误读成「防线失效」）。

### 2.3 `trg_publications_consistency`（延迟约束触发器，一条判据）

```
CREATE CONSTRAINT TRIGGER trg_publications_consistency
AFTER INSERT ON publications
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION rednote_check_publication_consistency()
```

`delivery_id`（给了才判）指向的交付必须属于 `NEW.project_id`。「草稿 → 实发」的映射跨项目没有任何业务含义，而 FK 不管归属。

---

## 3. 与 `M4-DDL草案.md` 的差异登记（主 agent 回填用，共 11 条）

| # | 位置 | 草案原文 | 本包落地 | 性质 |
|---|---|---|---|---|
| 1 | §0.1 乐观锁 | `lock_version INTEGER NOT NULL DEFAULT 0` | **`BIGINT NOT NULL DEFAULT 1`** | **草案过期**（平台助手为准，铁律 14） |
| 2 | §1.2 列清单 | 无 `export_item_id` | 新增 `export_item_id UUID NOT NULL FK export_items` | 方向由 WP35 写死（`exports/models.py:21-25`）；主会话 Q1 定 NOT NULL |
| 3 | §1.2 / §2.1 列清单 | 无 `request_hash` | 两张表各新增 `request_hash VARCHAR(64) NOT NULL` + 形状 CHECK | **草案与契约都缺**。`M4契约与幂等口径.md:83` 的口径存在、载体不存在 ⇒ 不补这列幂等就是假的 |
| 4 | §1.2 列清单 | 无 `delivered_by` / `note` / `created_by` | 三列全补（`delivered_by` 可空 + 同形 CHECK；`created_by` NOT NULL） | 原型 `static/delivery-koc.html:317` 要「操作人」与「备注」 |
| 5 | §1.2 `eligibility_hash` | `TEXT NOT NULL`（无形状约束） | `VARCHAR(64)` + `char_length = 64 AND ~ '^[0-9a-f]+$'` | **收紧**，与 WP35 同名列同形 |
| 6 | §1.2 索引 | `uq_deliveries_idempotency(idempotency_key)` | **`(project_id, idempotency_key)`** 部分唯一 | 幂等键按项目隔离（与 WP35 同口径）。有判据核对同键可用于两个项目 |
| 7 | §1.2 索引 | 无 | 新增 `uq_deliveries_replaces(replaces_delivery_id)` 部分唯一 | **本方案新增**：挡「替换树」 |
| 8 | §1.2 索引 | `ix_deliveries_replaces(replaces_delivery_id)` | ★ **未建**（被 #7 的部分唯一索引**完全覆盖**：`WHERE replaces_delivery_id = :id` 只查非空值，恰在部分索引范围内）。另新增 `ix_deliveries_export_item`、`ix_deliveries_content` | **偏离，需主会话确认**。保留它会是一条可证明为死的冗余索引 |
| 9 | §1.2 约束 | 只有 `state_valid` / `state_timestamps` | 另加 `delivered_by_shape` / `no_self_replace` / 两条 hash 形状 | 本方案新增 |
| 10 | §2.1 列清单 | 无 `created_by` | 新增 `created_by UUID NOT NULL` | 登记是人工动作，须可审计 |
| 11 | §2.2 唯一 | `uq_publication_links_canonical(canonical_url)` 全局唯一 | ★ **不建**，改普通索引 `ix_publication_links_canonical` | **实质偏离**（主会话 Q3）：与 WP37「跨项目同笔记需管理者确认」互斥 |

另两条**不是 DDL 差异但须同步回填**：

- `M4契约与幂等口径.md` §2 的载体现状三行：`POST /export-items` 那行**已由 WP35 闭合**；`POST /deliveries` 与 `POST /publications` 两行由本包闭合（列 + 部分唯一索引 + `request_hash`，真库验证见证据脚本 I1~I3）。
- `M4三件套一致性审计.md` §2.1 的 `export_items` 那一行**已过期**（WP35 已补齐 `exports/models.py:112-116`、`:169-176`）。

---

## 4. 核对命令（任何人都能重跑）

```bash
# ① 本节 §1 的 SQL 是不是磁盘上模型的忠实导出
cd services/backend && NO_PROXY='*' PYTHONPATH=src .venv/bin/python -c "
from sqlalchemy.schema import CreateTable, CreateIndex
from sqlalchemy.dialects import postgresql
from rednote.platform.base import Base
import rednote.modules.content.models, rednote.modules.exports.models
import rednote.modules.files.models, rednote.modules.identity.models
import rednote.modules.projects.models, rednote.modules.versions.models
import rednote.modules.media.models, rednote.modules.jobs.models
import rednote.modules.gateway.models, rednote.modules.prompts.models
from rednote.modules.delivery.models import OWN_TABLES
d = postgresql.dialect()
for n in OWN_TABLES:
    t = Base.metadata.tables[n]
    print(str(CreateTable(t).compile(dialect=d)).strip() + ';')
    for ix in t.indexes:
        print(str(CreateIndex(ix).compile(dialect=d)).strip() + ';')
"

# ② 冻结列名单与触发器 DDL
cd services/backend && NO_PROXY='*' PYTHONPATH=src .venv/bin/python -c "
from rednote.modules.delivery import ddl, models
print(models.frozen_delivery_columns())
print(models.MUTABLE_DELIVERY_COLUMNS)
print(ddl.trigger_names()); print(ddl.consistency_function_names())
print(ddl.frozen_columns_ddl()[0])
"

# ③ 库内活性（4 表 / 3 触发器 / 逐列变异 / 一致性 / 幂等载体 / 零残留）
python tools/m4/collect_wp36_evidence.py        # ⇒ RESULT=53 PASS / 0 FAIL

# ④ 两条产品规则的 AST 判据
python tools/quality/check_download_not_delivery.py   # ⇒ RESULT=PASS
```
