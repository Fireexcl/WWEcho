"""轻量成本配置默认值；GUI 读取时不导入 NumPy 或策略计算模块。"""

from __future__ import annotations

from typing import Any

from src.echo_score import ROOT, finite_number


DEFAULT_COSTS = ROOT / "config" / "activation-costs.json"
EXPERIENCE_WEIGHT_UNIT = 1000


def activation_defaults(costs: dict[str, Any]) -> dict[str, float]:
    """返回独立回收比例和每千经验权重；旧每点经验配置自动换算。"""
    weights = costs.get("default_weights", {})
    if not isinstance(weights, dict):
        raise ValueError("成本配置 default_weights 必须是 JSON 对象")
    experience_unit = finite_number(costs.get("experience_weight_unit", 1), "经验权重单位")
    if experience_unit <= 0:
        raise ValueError("经验权重单位必须大于 0")
    values = {"recovery": costs.get("recovery_ratio", .7),
              "tuning_recovery": costs.get("tuning_recovery_ratio", .3),
              **{name: weights.get(name, 1) for name in (
                  "tuning_weight", "experience_weight", "embryo_weight")}}
    defaults = {}
    for name, value in values.items():
        number = finite_number(value, f"默认成本参数 {name}")
        if number < 0:
            raise ValueError(f"默认成本参数 {name} 不能为负数")
        defaults[name] = number
    if defaults["recovery"] > 1 or defaults["tuning_recovery"] > 1:
        raise ValueError("经验和开孔素材的默认弃置回收比例必须分别在 0～1 之间")
    defaults["experience_weight"] *= EXPERIENCE_WEIGHT_UNIT / experience_unit
    finite_number(defaults["experience_weight"], "每千经验权重")
    return defaults