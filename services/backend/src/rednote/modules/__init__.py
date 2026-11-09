"""业务模块包。

M1 骨架期只含 `health`。研发计划 01 §5 规划的模块
（identity/projects/assets/knowledge/production/review/publication/billing/reports）
按工作包逐个加入，每个模块自带 api.py / service.py / repository.py / models.py，
通过服务方法交互，不允许跨模块直接读表。
"""

__all__: list[str] = []