"""跨表/跨包外键闭包的纯函数（WP-DEBT-05 从 `services/backend/tests/conftest.py` 迁出）。

**为什么挪到 `platform/` 而不是留在 `tests/conftest.py`**：`tools/dev/reset_shared_test_db.py`
（手动清空共享测试库数据的工具）需要同一套"引用闭包"算法，而 `tools/dev/` 不该反向
import 测试层的 `conftest.py`（那会让 `tools/**` 对 pytest/测试 fixture 产生依赖）。
`rednote.platform` 两边都能原生 import（测试层已有 `PYTHONPATH=src`，`tools/dev/` 按
`seed_demo_data.py` 的先例注入 backend `src`），因此把纯函数本体放这里，
`conftest.py` 改为从这里 import 再 re-export，21 个既有 `from conftest import
truncate_targets, cross_package_foreign_keys` 的测试文件不必改一行。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine


def bare_table_name(raw: str) -> str:
    """`regclass::text` 在 search_path 不含该表时会给 `schema.table`，统一取裸名比较。"""
    return raw.rsplit(".", 1)[-1].strip('"').lower()


def cross_package_foreign_keys(engine: Engine, names: Sequence[str]) -> set[str]:
    """别的包建在本包某张表上的外键**约束名**集合（只读，供夹具前后核对）。

    `pg_constraint.confrelid` 是"被引用的表"，`conrelid` 是"引用方表"。
    `names` 传本包的表清单；引用方也在 `names` 里的是**包内**外键，不算跨包。
    """
    own = {name.lower() for name in names}
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "select conrelid::regclass::text as ref_table, conname"
                " from pg_constraint"
                " where contype = 'f' and confrelid::regclass::text = any(:names)"
                " order by 1, 2"
            ),
            {"names": list(own)},
        )
        return {row[1] for row in rows if row[0].lower() not in own}


def truncate_targets(engine: Engine, names: Sequence[str]) -> list[str]:
    """TRUNCATE 本包表的**完整目标清单** —— 含所有入向引用者（含跨包）。

    ## 为什么不能只 TRUNCATE 自己的表

    PG 对 `TRUNCATE` 的要求与 `DELETE` 不同：**被引用的表不能单独 TRUNCATE**，
    且**不看 `ondelete` 动作**（TRUNCATE 走独立路径）。报错形态
    （WP27 的 `media_items → content_items` 落库后实测）：

        ERROR: cannot truncate a table referenced in a foreign key constraint
        DETAIL: Table "media_items" references "content_items".
        HINT: Truncate table "media_items" at the same time, or use TRUNCATE ... CASCADE.

    ⚠️ **要"整条链"，不是"直接引用方"**：只把 `media_items` 加进去**仍会失败** ——
    `media_versions` 又引用 `media_items`，逐层都要列（本机连续三次实测：
    ① 缺 `media_items` 报错 → ② 补上后缺 `media_versions` 报错 → ③ 补齐才过）。
    `test_billing_wp25.py` 的 docstring 记过同一性质。

    ## 为什么返回闭包而不是写死名单

    "哪些表引用了我"是**数据库的事实**，不是某个包的属性：WP27 落库前它是空集，
    落库后是 4 张 media 表。写死名单会在下次跨包引用出现时**静默漏掉** ——
    而漏掉的表现是**夹具每个用例崩一次**（WP27 实测 93 errors），
    不是一条清晰的断言。所以这里沿 `confrelid` 递归展开到**不动点**。
    ⚠️ 闭包算法与 `drop_own_tables`（`tests/conftest.py`）的 `kept` **同源、方向相反**，
    但**取边范围不同**：那条的保留集合只在**本包表之间**传播（包外表本来就不删）⇒ 可按
    `confrelid = any(:names)` 过滤；本函数要往外走到**包外**的引用方
    （`media_versions` 就是包外的）⇒ 必须**先取全量 FK 边**再在内存里闭包 ——
    少这一步闭包只走一层，而表现是"补了 2 张还是报同一个错"。

    ## 为什么不用 `TRUNCATE ... CASCADE`

    同 `drop_own_tables` 的论证（ISS-106）：`CASCADE` 会在"将来真有跨包引用"时
    **静默清空引用方表**。这里显式列出，让"多清了谁"永远可见。

    顺序：**引用方在前**（与 `DROP_ORDER` 的口径一致），本包表按传入顺序在后。
    ⚠️ TRUNCATE **本身不要求**顺序（PG 一次性处理整条链）；这个顺序是给人读的，
    与 `DROP_ORDER` 保持同一心智模型。
    """
    own = {name.lower() for name in names}
    # ⚠️ **必须取全量 FK 边，不能按 `confrelid = any(:names)` 过滤。**
    # 闭包要往外走到**包外**的引用方（`media_versions → media_generation_contexts`），
    # 而按 own 过滤后那条边（confrelid = `media_generation_contexts` ∉ own）
    # 根本不在结果集里 ⇒ 闭包只走一层就停。首版照抄了 `drop_own_tables` 的
    # 过滤条件，真库实测就是「列表里只多出 media_items 与 media_generation_contexts、
    # 缺 media_versions ⇒ 仍然报同一个错」。
    with engine.begin() as conn:
        edges = [
            (
                bare_table_name(ref),
                bare_table_name(target),
            )
            for ref, target in conn.execute(
                text(
                    "select conrelid::regclass::text as ref_table,"
                    "       confrelid::regclass::text as target"
                    " from pg_constraint"
                    " where contype = 'f'"
                )
            ).fetchall()
        ]

    closure = set(own)
    while True:
        grown = closure | {ref for ref, target in edges if target in closure}
        if grown == closure:
            break
        closure = grown

    return sorted(closure - own) + list(names)
