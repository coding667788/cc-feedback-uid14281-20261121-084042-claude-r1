"""版本迁移校验器（安全分类词表）。

子模块：
- errors:   结构化错误码
- models:   CASE.json 数据模型与结构校验
- planner:  plan 命令的掩码 + 剪枝求解器
- audit:    audit 命令的逐对象逐角色核对
- storage:  SQLite 持久化与 update 原子切换
- cli:      命令行入口
"""

__all__ = ["errors", "models", "planner", "audit", "storage", "cli"]
