"""交付与发布登记的依赖注入（WP36）。

只提供一样：`DeliveryRepoDep`。资格复验走 `export_bridge`（它从同一个 session
现场构造 `ExportRepository`），因此**不需要**把 `ExportRepoDep` 注进来 ——
注进来就等于让本包的端点签名依赖 `exports` 的装配点，接缝面会从"一个函数"
扩大到"一个依赖"。

权限：**不新建 `ProjectAction`**，四件事（建 KOC / 建交付 / 标记交付 / 登记发布）
全在既有的 `DELIVER` 里 —— `projects/permissions.py:88` 的注释原文就是
「下载、交付、发布登记、回收数据」。查看用 `VIEW`。

`ProjectAccessDep` 只回答"能不能看到这个项目"，**每个端点必须自己
`access.require(...)`**。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from rednote.modules.delivery.repository import DeliveryRepository
from rednote.modules.identity.dependencies import DBSession


def get_delivery_repository(db: DBSession) -> DeliveryRepository:
    """请求级仓储（与端点同一个 session）。"""
    return DeliveryRepository(db)


DeliveryRepoDep = Annotated[DeliveryRepository, Depends(get_delivery_repository)]

__all__ = ["DeliveryRepoDep", "get_delivery_repository"]
