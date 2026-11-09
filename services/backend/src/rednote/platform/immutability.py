"""不可变记录的守卫（数据结构文档 02 §1：历史版本和事件表不允许原地改写）。

**为什么"不可变"必须在数据库层也拦一道**：

只在 ORM 层实现（`before_update` 事件）能拦住应用的正常写入路径，但拦不住：
  1. 排障时的 `psql` 手工 UPDATE；
  2. Alembic 数据回填脚本（它走原生 SQL）；
  3. 将来的批处理 / 运维工具；
  4. 某个新人写了一句 `session.execute(update(VersionNode).values(...))`。

第 4 条容易被误认为"ORM 层还能兜住"，其实**兜不住**：
mapper 级 `before_update` 只在 flush 流程里触发，而 ORM-enabled DML
（`session.execute(update(...))`）走的是另一条执行路径，
**根本不触发该事件** —— SQLAlchemy `MapperEvents.before_update` 文档开头
就写明它 "does **not** apply to the ORM DML operations"。要拦这一类
只能改用 `SessionEvents.do_orm_execute`；本模块刻意不这么做，
因为**数据库触发器已经把这条路径管住了**，再加一层只会多一处要同步的规则。

这四条路径**都没有代码评审**。而"版本被改过"的失败模式是静默的：
没有任何异常、没有任何日志，只在事后对账时才发现溯源不可信 ——
那时已经无法分辨"被改过"与"本来如此"。

因此本模块只负责应用层守卫，数据库层的守卫（`BEFORE UPDATE OR DELETE`
触发器）与表在同一个迁移里建立。两层缺一不可：
  - ORM 事件：给出可读的领域错误，让问题在开发期就暴露；
  - 触发器：最后一道，管住所有绕过 ORM 的写入。

**触发器命名集中在 `immutable_trigger_ddl` 里生成**，不允许迁移脚本或验证脚本
各自手写名字 —— 否则会出现"迁移建的触发器叫 A、验证脚本找的是 B"这种
**看起来通过、实际没保护**的情况。

**为什么用 `restrict_violation` 作为 SQLSTATE**：调用方（测试、排障脚本）需要
把"被不可变守卫拒绝"与"其他任何约束失败"区分开。统一用 23001 后，
`except DBAPIError as e: e.orig.sqlstate == "23001"` 就是一个精确判据，
不必去匹配错误文案（文案会随本地化变化）。
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import event
from sqlalchemy.orm import object_session

IMMUTABLE_RECORD_SQLSTATE: Final[str] = "23001"  # PostgreSQL restrict_violation

# 所有不可变表共用一个触发器函数：行为完全一致（一律拒绝 UPDATE/DELETE），
# 分成多个函数只会让"DBA 要改一处行为得改 N 个函数"。
TRIGGER_FUNCTION: Final[str] = "rednote_reject_mutation"


class ImmutableRecordError(RuntimeError):
    """试图修改或删除不可变记录。

    刻意**不继承 ApiError**：这是程序缺陷（写代码的人搞错了对象类型），
    不是用户可以纠正的请求问题。让它以未捕获异常的形式变成 500，
    才能被监控告警捞到 —— 若折算成 4xx，这类 bug 会被当成"用户操作不对"
    而长期潜伏。

    数据库层对应的异常是 `sqlalchemy.exc.DBAPIError`，
    `orig.sqlstate == IMMUTABLE_RECORD_SQLSTATE`。
    """

    def __init__(self, table_name: str, operation: str) -> None:
        self.table_name = table_name
        self.operation = operation
        super().__init__(
            f"{table_name} 是不可变记录，不允许 {operation}。"
            "版本内容不可变（数据结构文档 02 §5）："
            "归档、撤销批准、影响标记通过事件改变可用性，不修改已固化内容。"
        )


def _guard(model: type) -> type:
    """给模型挂 before_update / before_delete 守卫。

    用 `propagate=True` 让未来基于该表的继承映射也被覆盖 ——
    不这样做的话，子类实体的修改会绕过守卫（SQLAlchemy 的事件默认不向下传播）。
    """

    def _reject_update(mapper: object, connection: object, target: object) -> None:
        """拒绝真正会发出 UPDATE 的修改；净变化为零时放行。

        **为什么必须判净变化，不能直接 raise**：
        `before_update` 是**按"实例是否 dirty"触发**的，而 SQLAlchemy 把
        "任何属性被 set 过"（**哪怕赋的是与库中完全相同的值**）都算 dirty。
        这一点在 SQLAlchemy 2.0 的 `MapperEvents.before_update` 文档里是明写的：
        "called for all instances that are marked as dirty, *even those which
        have no net changes to their column-based attributes*"。
        净变化为零时它根本不会发出 UPDATE 语句。

        所以只要不判净变化，下面这种完全无害的写法就会炸：
            row = session.get(VersionNode, node_id)
            row.revision_no = row.revision_no      # 赋同样的值
            session.flush()                        # ← 这里抛 ImmutableRecordError
        而 `ImmutableRecordError` 继承自 `RuntimeError`（刻意不是 ApiError），
        会以 500 的形式冒到用户面前 —— 把一个"什么都没改"的请求打成服务故障。

        官方给出的判据（`events.py` before_update 文档原句，也是 `Session`
        自己文档里"在 dirty 里但净变化为零"那段所指）：
            object_session(instance).is_modified(instance, include_collections=False)
        `include_collections=False` 表示只看会影响本行 UPDATE 的标量列 ——
        两者口径一致，正是我们要的那个"会不会真的发 UPDATE"。

        **已知保守方向**：若某标量列在赋值时尚未加载/已 expired，
        SQLAlchemy 会**假定它有变化**（它拿不到旧值来做比较）。
        那种情况下本函数会拦下 —— 这是可接受的方向：此时 SQLAlchemy
        确实会发出 UPDATE，数据库触发器也会拦，两层结论一致。
        """
        session = object_session(target)
        if session is not None and not session.is_modified(
            target, include_collections=False
        ):
            return
        raise ImmutableRecordError(model.__tablename__, "UPDATE")

    def _reject_delete(mapper: object, connection: object, target: object) -> None:
        # DELETE 不判净变化：`session.delete()` 是显式意图，没有"净变化为零"这种状态。
        raise ImmutableRecordError(model.__tablename__, "DELETE")

    event.listen(model, "before_update", _reject_update, propagate=True)
    event.listen(model, "before_delete", _reject_delete, propagate=True)
    return model


def guard_immutable(*models: type) -> None:
    """把一批模型登记为不可变。

    在模型模块的**模块级**调用（模型定义之后），这样只要 import 了模型，
    守卫就已经生效 —— 不依赖任何初始化顺序或应用启动钩子。

    **注意 `before_update` 的触发口径**：它按"实例是否 dirty"触发，
    而"给属性赋一个相同的值"也会让实例进入 dirty。因此守卫内部
    **必须**再判一次净变化（见 `_guard`），否则"读了不改"会误报。
    """
    for model in models:
        _guard(model)


def immutable_trigger_name(table: str) -> str:
    """某个表的不可变触发器名。迁移与验证脚本都必须用它取名。"""
    return f"trg_{table}_immutable"


def immutable_trigger_ddl(table: str) -> tuple[str, str, str, str]:
    """返回 (函数 DDL, 触发器 DDL, 触发器 DROP, 函数 DROP)。

    **为什么把 DDL 生成放在这里而不是迁移文件里**：
    验证脚本要按名字回查 `pg_trigger`。若名字由迁移文件手写，
    验证脚本也得手写一遍同样的字符串，两处一旦不一致，
    "找回触发器"的检查就会**假失败**或**假通过**。名字只有一个来源才靠得住。

    RAISE 用 `USING MESSAGE = ...` 拼接而**不用 `%` 占位符**：
    psycopg 的 paramstyle 会把 `%` 当参数占位符，未提供参数时直接报
    "unsupported format character"。字符串拼接没有这个坑。
    """
    function_ddl = (
        f"CREATE OR REPLACE FUNCTION {TRIGGER_FUNCTION}() RETURNS trigger AS $$\n"
        "BEGIN\n"
        "    RAISE EXCEPTION USING\n"
        "        MESSAGE = TG_TABLE_NAME || ' 是不可变记录，不允许 ' || TG_OP\n"
        "                  || '（数据结构文档 02 §1）',\n"
        f"        ERRCODE = '{IMMUTABLE_RECORD_SQLSTATE}';\n"
        "END;\n"
        "$$ LANGUAGE plpgsql"
    )
    trigger_ddl = (
        f"CREATE TRIGGER {immutable_trigger_name(table)}\n"
        f"BEFORE UPDATE OR DELETE ON {table}\n"
        f"FOR EACH ROW EXECUTE FUNCTION {TRIGGER_FUNCTION}()"
    )
    drop_trigger = f"DROP TRIGGER IF EXISTS {immutable_trigger_name(table)} ON {table}"
    drop_function = f"DROP FUNCTION IF EXISTS {TRIGGER_FUNCTION}()"
    return function_ddl, trigger_ddl, drop_trigger, drop_function


__all__ = [
    "IMMUTABLE_RECORD_SQLSTATE",
    "TRIGGER_FUNCTION",
    "ImmutableRecordError",
    "guard_immutable",
    "immutable_trigger_ddl",
    "immutable_trigger_name",
]
