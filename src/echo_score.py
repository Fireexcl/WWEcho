"""五星满级声骸评分核心及 JSON 命令行入口（无第三方依赖）。"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATS = ROOT / "config" / "echo-stats.json"
DEFAULT_WEIGHTS = ROOT / "config" / "score-weights.json"
ALIGNMENT_SCORE = 50.0


def load_json(path: Path) -> dict[str, Any]:
    """读取 JSON 对象；不依赖调用者当前工作目录。"""
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} 的顶层必须是 JSON 对象")
    return data


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值，百分比使用百分点，例如 10.5")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} 必须是有限数值") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数值")
    return number


class EchoScorer:
    """数值乘以单位权重，以全局理论最高原始分对齐到 50 分。"""

    def __init__(self, stats: dict[str, Any], weights: dict[str, Any]) -> None:
        if stats.get("schema_version") != 1 or weights.get("schema_version") != 1:
            raise ValueError("属性及权重配置的 schema_version 必须为 1")
        self.stats = stats
        self.definitions = stats["stat_definitions"]
        self.main_by_cost = stats["main_stats"]["max_values_by_cost"]
        # 小词条仅预留权重，不参与当前评分和全局最高分。
        minor_pools = stats["minor_stats"]["max_values_by_cost"]
        minor_values = weights.get("minor_stats", {
            cost: dict.fromkeys(pool, 0) for cost, pool in minor_pools.items()})
        if not isinstance(minor_values, dict) or set(minor_values) != set(minor_pools):
            raise ValueError("小词条权重必须包含 Cost 1、3、4")
        self.minor_weights_by_cost: dict[str, dict[str, float]] = {}
        for cost, pool in minor_pools.items():
            values = minor_values[cost]
            if not isinstance(values, dict) or set(values) != set(pool):
                raise ValueError(f"小词条权重 Cost {cost} 必须仅包含对应的固定小词条")
            self.minor_weights_by_cost[cost] = {}
            for stat_id, value in values.items():
                weight = finite_number(value, f"minor_stats.{cost}.{stat_id} 权重")
                if weight < 0:
                    raise ValueError("权重不能为负数；无效属性请设为 0")
                self.minor_weights_by_cost[cost][stat_id] = weight
        self.tiers = stats["sub_stats"]["tiers"]
        self.names = {definition["name_zh"]: stat_id
                      for stat_id, definition in self.definitions.items()}
        self.weights: dict[str, Any] = {}
        main_values = weights.get("main_stats")
        by_cost = isinstance(main_values, dict) and set(main_values) == set(self.main_by_cost)
        for category, expected in (
            ("main_stats", set().union(*(set(pool) for pool in self.main_by_cost.values()))),
            ("sub_stats", set(self.tiers)),
        ):
            values = weights.get(category)
            if category == "main_stats" and by_cost:
                self.weights[category] = {}
                for cost, pool in self.main_by_cost.items():
                    cost_values = values[cost]
                    if not isinstance(cost_values, dict) or set(cost_values) != set(pool):
                        raise ValueError(f"主词条权重 Cost {cost} 必须完整且仅包含该 Cost 的候选词条")
                    self.weights[category][cost] = {}
                    for stat_id, value in cost_values.items():
                        weight = finite_number(value, f"main_stats.{cost}.{stat_id} 权重")
                        if weight < 0:
                            raise ValueError("权重不能为负数；无效属性请设为 0")
                        self.weights[category][cost][stat_id] = weight
                continue
            if not isinstance(values, dict) or set(values) != expected:
                raise ValueError(f"权重配置 {category} 必须完整且仅包含该分类的词条标识")
            self.weights[category] = {}
            for stat_id, value in values.items():
                weight = finite_number(value, f"{category}.{stat_id} 权重")
                if weight < 0:
                    raise ValueError("权重不能为负数；无效属性请设为 0")
                self.weights[category][stat_id] = weight

        self.main_weights_by_cost = (self.weights["main_stats"] if by_cost else {
            cost: {stat_id: self.weights["main_stats"][stat_id] for stat_id in pool}
            for cost, pool in self.main_by_cost.items()})
        main_max = max(value * self.main_weights_by_cost[cost][stat_id]
                       for cost, pool in self.main_by_cost.items()
                       for stat_id, value in pool.items())
        sub_maxima = sorted((max(tier["values"]) * self.weights["sub_stats"][stat_id]
                             for stat_id, tier in self.tiers.items()), reverse=True)
        try:
            self.unaligned_max_score = math.fsum(
                [main_max] + sub_maxima[:self.stats["sub_stats"]["max_count"]])
        except OverflowError as exc:
            raise ValueError("权重过大，评分超出有限数值范围") from exc
        if not math.isfinite(self.unaligned_max_score):
            raise ValueError("权重过大，评分超出有限数值范围")
        self.aligned_max_score = ALIGNMENT_SCORE if self.unaligned_max_score > 0 else 0.0

    def stat_score(self, category: str, stat_id: str, value: float,
                   cost: Optional[int] = None) -> float:
        """返回未圆整贡献；评分明细和策略计算共用同一公式。"""
        if self.unaligned_max_score == 0:
            return 0.0
        weight = self.stat_weight(category, stat_id, value, cost)
        return (value * weight / self.unaligned_max_score
                * ALIGNMENT_SCORE)

    def stat_weight(self, category: str, stat_id: str, value: float,
                    cost: Optional[int] = None) -> float:
        if category != "main_stats":
            return self.weights[category][stat_id]
        candidates = [key for key, pool in self.main_by_cost.items()
                      if (cost is None or key == str(cost)) and stat_id in pool
                      and self._matches(value, pool[stat_id])]
        if not candidates:
            raise ValueError("主词条类型或数值不符合指定 Cost 的满级值")
        weights = {self.main_weights_by_cost[key][stat_id] for key in candidates}
        if len(weights) != 1:
            raise ValueError("主词条匹配多个 Cost 且权重不同，请提供 cost")
        return weights.pop()

    @classmethod
    def from_files(cls, stats_path: Path = DEFAULT_STATS,
                   weights_path: Path = DEFAULT_WEIGHTS) -> EchoScorer:
        return cls(load_json(stats_path), load_json(weights_path))

    def _parse_stat(self, entry: Any) -> tuple[str, float]:
        if not isinstance(entry, dict) or set(entry) != {"name", "value"}:
            raise ValueError("每个词条必须且只能包含 name 和 value")
        name = entry["name"]
        if not isinstance(name, str):
            raise ValueError("词条名必须是字符串")
        name = name.strip()
        stat_id = self.names.get(name, name)
        if stat_id not in self.definitions:
            raise ValueError(f"未知词条：{name}")
        return stat_id, finite_number(entry["value"], name)

    @staticmethod
    def _matches(value: float, reference: float) -> bool:
        return math.isclose(value, reference, rel_tol=0, abs_tol=1e-8)

    def _contribution(self, category: str, stat_id: str, value: float,
                      reference: float, cost: Optional[int] = None) -> dict[str, Any]:
        weight = self.stat_weight(category, stat_id, value, cost)
        ratio = value / reference
        return {
            "stat_id": stat_id,
            "name": self.definitions[stat_id]["name_zh"],
            "value": value,
            "reference_value": reference,
            "weight": weight,
            "ratio": round(ratio, 8),
            "score": round(self.stat_score(category, stat_id, value, cost), 6),
        }

    def score(self, echo: dict[str, Any]) -> dict[str, Any]:
        """输入 main_stat、sub_stats，可选 cost；返回总分及贡献明细。

        百分比值使用百分点（22 表示 22%）。支持 0～5 条副词条。主词条必须是五星 +25
        满级值，副词条必须匹配配置中的实际档位。未激活槽位不计分。
        """
        if not isinstance(echo, dict):
            raise ValueError("声骸输入必须是 JSON 对象")
        if set(echo) - {"main_stat", "sub_stats", "cost"}:
            raise ValueError("声骸仅支持 main_stat、sub_stats 和可选 cost 字段")
        if "main_stat" not in echo or "sub_stats" not in echo:
            raise ValueError("输入必须包含 main_stat 和 sub_stats")
        cost = echo.get("cost")
        if "cost" in echo and (type(cost) is not int or str(cost) not in self.main_by_cost):
            raise ValueError("cost 必须是整数 1、3 或 4")
        main_id, main_value = self._parse_stat(echo["main_stat"])
        pools = (list(self.main_by_cost.values()) if cost is None
                 else [self.main_by_cost[str(cost)]])
        references = [pool[main_id] for pool in pools if main_id in pool
                      and self._matches(main_value, pool[main_id])]
        if not references:
            raise ValueError("主词条类型或数值不符合指定 Cost（或任一 Cost）的五星 +25 满级值")
        main = self._contribution("main_stats", main_id, references[0], references[0], cost)
        entries = echo["sub_stats"]
        max_count = self.stats["sub_stats"]["max_count"]
        if not isinstance(entries, list) or not 0 <= len(entries) <= max_count:
            raise ValueError(f"已激活副词条必须为 0～{max_count} 条")
        seen: set[str] = set()
        subs = []
        raw_sub_scores = []
        for entry in entries:
            stat_id, value = self._parse_stat(entry)
            if stat_id not in self.tiers:
                raise ValueError(f"{stat_id} 不能作为副词条")
            if stat_id in seen:
                raise ValueError(f"副词条类型不能重复：{stat_id}")
            seen.add(stat_id)
            values = self.tiers[stat_id]["values"]
            matched = next((tier for tier in values if self._matches(value, tier)), None)
            if matched is None:
                raise ValueError(f"{stat_id} 数值不属于合法档位：{values}")
            reference = max(values)
            subs.append(self._contribution("sub_stats", stat_id, matched, reference))
            raw_sub_scores.append(self.stat_score("sub_stats", stat_id, matched))
        main_score = self.stat_score("main_stats", main_id, references[0], cost)
        try:
            sub_score = math.fsum(raw_sub_scores)
            total = math.fsum([main_score, sub_score])
        except OverflowError as exc:
            raise ValueError("权重过大，评分超出有限数值范围") from exc
        if not math.isfinite(total):
            raise ValueError("权重过大，评分超出有限数值范围")
        return {
            "score": round(total, 6),
            "main_score": round(main_score, 6),
            "sub_score": round(sub_score, 6),
            "activated_count": len(subs),
            "main_stat": main,
            "sub_stats": subs,
        }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="五星 +25 声骸：主词条 + 0～5 副词条评分")
    parser.add_argument("input", nargs="?", type=Path, help="输入 JSON 文件；省略时从标准输入读取")
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS, help="用户权重 JSON")
    parser.add_argument("--stats", type=Path, default=DEFAULT_STATS, help="属性数据 JSON")
    parser.add_argument("--details", action="store_true", help="输出 JSON 贡献明细（默认仅输出总分）")
    args = parser.parse_args(argv)
    try:
        echo = load_json(args.input) if args.input else json.load(sys.stdin)
        result = EchoScorer.from_files(args.stats, args.weights).score(echo)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.details else result["score"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        print(f"评分失败：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())