"""Outbox 事件（WP12 建表，WP13 投递）。

本模块**只有表定义**：事件由各业务模块在业务提交的同一事务里写入
（当前唯一写入方是 `modules/files/service.py` 的完成上传路径），
投递器、重试与租约属 WP13。

显式 import 模型，使 `migrations/env.py` 的 autogenerate 能看见这张表 ——
少了这一句，表不会出现在迁移里，而"忘了建表"要到运行时插入才暴露。
"""

from rednote.modules.outbox.models import OutboxEvent  # noqa: F401

__all__: list[str] = []
