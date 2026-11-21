"""结构化错误码。

所有结构/业务错误统一抛出 :class:`ValidationError`，CLI 层将其渲染为
``{"error": {"code": ..., "message": ..., "path": ...}}`` 的 UTF-8 JSON。
``path`` 为 JSONPath 风格的字段位置（如 ``$.objects[2].labels[0]``），
保证错误可定位；同一逻辑输入永远得到同一结果。
"""

from __future__ import annotations


class ValidationError(Exception):
    """可定位的结构/业务错误。"""

    def __init__(self, code: str, message: str, path: str = "$") -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path

    def to_payload(self) -> dict:
        return {"code": self.code, "message": self.message, "path": self.path}


# ---- 结构错误码 -----------------------------------------------------------
E_TYPE = "E_TYPE"            # 字段类型错误
E_MISSING = "E_MISSING"      # 必填字段缺失
E_UNKNOWN = "E_UNKNOWN"      # 出现未声明字段
E_EMPTY = "E_EMPTY"          # 集合/字符串为空但不允许
E_DUPLICATE = "E_DUPLICATE"  # 唯一键重复
E_REF = "E_REF"              # 引用了不存在的 ID / 原子类别
E_RANGE = "E_RANGE"          # 取值范围错误（如候选数 > 3）
# ---- 业务错误码 -----------------------------------------------------------
E_INFEASIBLE = "E_INFEASIBLE"          # plan 无解
E_PLAN_INVALID = "E_PLAN_INVALID"      # audit 既定方案违反约束
E_PLAN_EXPIRED = "E_PLAN_EXPIRED"      # 计划已过期
E_PLAN_NOT_FOUND = "E_PLAN_NOT_FOUND"  # update 找不到已登记计划
E_DB_STATE = "E_DB_STATE"              # 持久化状态与输入快照漂移
