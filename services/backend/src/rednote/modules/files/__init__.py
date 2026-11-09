"""文件、配额与分片上传（WP12）。

分层与其他业务模块一致：
  · `models.py`      —— 6 张表的定义与受控取值域
  · `storage.py`     —— 对象存储适配器（本机文件系统 / 内网 MinIO）
  · `repository.py`  —— 存储访问（**配额的原子预占在这里**）
  · `service.py`     —— 业务规则（本模块对外唯一入口）
  · `api.py`         —— 协议 §4.2 的 6 个端点

显式 import 模型，使 autogenerate 能看见这 6 张表。
"""

from rednote.modules.files import models  # noqa: F401
from rednote.modules.files.api import router  # noqa: F401

__all__: list[str] = ["router"]
