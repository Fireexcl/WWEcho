"""频整器：单次类型子集与折半尾分布，动态换锁预算入口及旧固定锁法 API。"""

from __future__ import annotations

import math
from bisect import bisect_right
from concurrent.futures import CancelledError
from dataclasses import dataclass
from fractions import Fraction
from itertools import combinations
from threading import Event
from typing import Any, Optional

from src.echo_score import ALIGNMENT_SCORE, EchoScorer, finite_number


@dataclass(frozen=True)
class ScoreDistribution:
    scores: tuple[int, ...]
    probabilities: tuple[float, ...]
    prefix_probability: tuple[float, ...]
    prefix_deficit: tuple[float, ...]
    tail_probability: tuple[float, ...]
    tail_excess: tuple[float, ...]


class ReconstructionPlanner:
    """单次对离散模型求和，不量化分数；概率以浮点累加。

    按十进制配置构造整数原始贡献，严格比较新旧副词条总贡献，避免平分
    被浮点或展示圆整误判为正提升。类型不放回；给定类型后档位独立。
    """

    def __init__(self, scorer: EchoScorer) -> None:
        self.scorer = scorer
        self.ids = list(scorer.tiers)
        self.lock_costs = {1: 1, 2: 1, 3: 2, 4: 3}
        rules = scorer.stats["reconstruction"]
        self.limit = rules["required_activated_count"]
        if (self.limit != 5 or scorer.stats["sub_stats"]["max_count"] != 5
                or scorer.stats["sub_stats"]["pool_size"] != len(self.ids)
                or scorer.stats["sub_stats"]["allow_duplicate_types"] is not False
                or rules["exclude_locked_types"] is not True
                or rules["allow_original_unlocked_type"] is not True):
            raise ValueError("重构需要五词条、不重复类型、仅排除锁定类型的规则")
        cases = rules["cases"]
        if not isinstance(cases, list) or len(cases) != 4:
            raise ValueError("重构配置必须覆盖锁 1～4 条")
        self.lock_counts = []
        for count, case in enumerate(cases, 1):
            if (case["locked_count"] != count or case["rerolled_count"] != self.limit - count
                    or case["candidate_count"] != len(self.ids) - count
                    or case["type_probability"] != {"numerator": 1,
                                                    "denominator": len(self.ids) - count}):
                raise ValueError("重构锁数、重构数、候选池及类型概率不一致")
            self.lock_counts.append(count)
        if set(self.lock_counts) != set(self.lock_costs):
            raise ValueError("重构锁数与频整器消耗映射不一致")

        products: dict[str, list[Fraction]] = {}
        self.probabilities: dict[str, list[float]] = {}
        rational_probabilities: dict[str, list[Fraction]] = {}
        for stat_id, tier in scorer.tiers.items():
            published = scorer.stats["probability_distributions"][
                tier["probability_distribution"]]["published_probability_pct"]
            if not isinstance(published, list) or len(published) != len(tier["values"]):
                raise ValueError("重构档位与概率数组必须一一对应")
            probabilities = [finite_number(value, "档位概率") for value in published]
            if not probabilities or any(value < 0 for value in probabilities):
                raise ValueError("档位概率必须非负且至少有一档")
            total = math.fsum(probabilities)
            if not math.isfinite(total) or total <= 0:
                raise ValueError("档位概率总和必须为有限正数")
            rational = [Fraction(str(value)) for value in probabilities]
            rational_total = sum(rational)
            rational_probabilities[stat_id] = [value / rational_total for value in rational]
            self.probabilities[stat_id] = [float(value) for value in rational_probabilities[stat_id]]
            weight = Fraction(str(scorer.weights["sub_stats"][stat_id]))
            products[stat_id] = [Fraction(str(value)) * weight for value in tier["values"]]

        # 公分母仅用于整数比较，不是分数网格；最大支持任意精度 Python 整数。
        denominator = math.lcm(*(value.denominator for values in products.values() for value in values))
        integers = {name: [int(value * denominator) for value in values]
                    for name, values in products.items()}
        divisor = math.gcd(*(value for values in integers.values() for value in values)) or 1
        self.scores = {name: [value // divisor for value in values]
                       for name, values in integers.items()}
        self.point_scale = (Fraction(divisor, denominator)
                            / Fraction(str(scorer.unaligned_max_score)) * Fraction(str(ALIGNMENT_SCORE))
                            if scorer.unaligned_max_score else Fraction(0))
        self.mean_scores = {name: sum(score * probability
                                     for score, probability in zip(values, rational_probabilities[name]))
                            for name, values in self.scores.items()}
        self._halves: dict[tuple[str, ...], ScoreDistribution] = {}
        self._adaptive: Any = None

    def _points(self, score: int | Fraction) -> float:
        return float(score * self.point_scale)

    def _half(self, stat_ids: tuple[str, ...]) -> ScoreDistribution:
        """每半至多两种类型（64 个档位组合），复用其尾概率和一阶矩。"""
        if stat_ids in self._halves:
            return self._halves[stat_ids]
        masses = {0: 1.0}
        for stat_id in stat_ids:
            combined: dict[int, float] = {}
            for previous, mass in masses.items():
                for value, probability in zip(self.scores[stat_id], self.probabilities[stat_id]):
                    if probability:
                        combined[previous + value] = combined.get(previous + value, 0.0) + mass * probability
            masses = combined
        scores = tuple(sorted(masses))
        probabilities = tuple(masses[score] for score in scores)
        prefix_p, prefix_d = [0.0], [0.0]
        for index, probability in enumerate(probabilities):
            gap = self._points(scores[index] - scores[index - 1]) if index else 0.0
            prefix_d.append(math.fsum([prefix_d[-1], gap * prefix_p[-1]]))
            prefix_p.append(math.fsum([prefix_p[-1], probability]))
        tail_p, tail_e = [0.0], [0.0]
        for index in reversed(range(len(scores))):
            probability = probabilities[index]
            gap = self._points(scores[index + 1] - scores[index]) if index + 1 < len(scores) else 0.0
            tail_e.append(math.fsum([tail_e[-1], gap * tail_p[-1]]))
            tail_p.append(math.fsum([tail_p[-1], probability]))
        result = ScoreDistribution(scores, probabilities, tuple(prefix_p), tuple(prefix_d),
                                   tuple(reversed(tail_p)), tuple(reversed(tail_e)))
        self._halves[stat_ids] = result
        return result

    def _positive(self, stat_ids: tuple[str, ...], threshold: int) -> tuple[float, float, float]:
        middle = len(stat_ids) // 2
        left, right = self._half(stat_ids[:middle]), self._half(stat_ids[middle:])
        probabilities, gains, losses = [], [], []
        for score, probability in zip(left.scores, left.probabilities):
            index = bisect_right(right.scores, threshold - score)
            tail_p = right.tail_probability[index]
            probabilities.append(probability * tail_p)
            if index < len(right.scores):
                # 先减整数再对齐，尾矩相对本地最小值累计，避免巨大总分相减吞掉微小增分。
                gap = self._points(score + right.scores[index] - threshold)
                gains.append(probability * math.fsum([right.tail_excess[index], gap * tail_p]))
            if index:
                gap = self._points(threshold - score - right.scores[index - 1])
                losses.append(probability * math.fsum([
                    right.prefix_deficit[index], gap * right.prefix_probability[index]]))
        return math.fsum(probabilities), math.fsum(gains), math.fsum(losses)

    def analyze(self, echo: dict[str, Any], *, cancelled: Optional[Event] = None) -> dict[str, Any]:
        result = self.scorer.score(echo)
        entries = result["sub_stats"]
        if len(entries) != self.limit:
            raise ValueError("频整器只能用于已开满五条副词条的声骸")
        current = {}
        for item in entries:
            name = item["stat_id"]
            tier_index = self.scorer.tiers[name]["values"].index(item["value"])
            current[name] = self.scores[name][tier_index]
        plans = []
        for count in self.lock_counts:
            for locked_entries in combinations(entries, count):
                locked = {item["stat_id"] for item in locked_entries}
                pool = [name for name in self.ids if name not in locked]
                rerolled = self.limit - count
                threshold = sum(value for name, value in current.items() if name not in locked)
                probabilities, positive_gains, negative_gains = [], [], []
                for types in combinations(pool, rerolled):
                    if cancelled is not None and cancelled.is_set():
                        raise CancelledError()
                    probability, positive_gain, negative_gain = self._positive(types, threshold)
                    probabilities.append(probability)
                    positive_gains.append(positive_gain)
                    negative_gains.append(negative_gain)
                subsets = math.comb(len(pool), rerolled)
                probability = min(1.0, max(0.0, math.fsum(probabilities) / subsets))
                positive_gain = math.fsum(positive_gains) / subsets
                negative_gain = math.fsum(negative_gains) / subsets
                delta = self._points(Fraction(rerolled, len(pool))
                                     * sum(self.mean_scores[name] for name in pool) - threshold)
                # 等分不计成功；若无正提升路径，一阶矩浮点消去不能制造正收益。
                if probability == 0:
                    positive_gain = 0.0
                    delta = min(0.0, delta)
                    negative_gain = -delta
                elif probability == 1:
                    negative_gain = 0.0
                cost = self.lock_costs[count]
                plans.append({
                    "locked_ids": [item["stat_id"] for item in locked_entries],
                    "locked_stats": list(locked_entries), "locked_count": count,
                    "rerolled_count": rerolled, "cost": cost,
                    "probability": probability, "expected_delta": delta,
                    "expected_positive_gain": positive_gain,
                    "expected_negative_gain": negative_gain,
                    "gain_on_success": positive_gain / probability if probability else None,
                    "expected_cost_to_improve": cost / probability if probability else None,
                    "positive_gain_per_transducer": positive_gain / cost,
                })
        current_score = math.fsum([
            self.scorer.stat_score("main_stats", result["main_stat"]["stat_id"],
                                   result["main_stat"]["value"], result["cost"]),
            self.scorer.stat_score("minor_stats", result["minor_stat"]["stat_id"],
                                   result["minor_stat"]["value"], result["cost"]),
            self._points(sum(current.values())),
        ])
        return {"plans": plans, "current_score": current_score,
                "method": "类型子集枚举 + 折半尾分布；十进制整数贡献严格比较，无分数网格"}

    def compare_dynamic_budget(self, echo: dict[str, Any], budget: int, *,
                               analysis: Optional[dict[str, Any]] = None,
                               cancelled: Optional[Event] = None) -> dict[str, Any]:
        """每次重构后按完整新状态与剩余预算重新选锁，最大化最终分数期望。"""
        if self._adaptive is None:
            from src.echo_reconstruction_budget import AdaptiveBudgetPlanner

            self._adaptive = AdaptiveBudgetPlanner(self)
        return self._adaptive.compare(echo, budget, analysis=analysis, cancelled=cancelled)


def evaluate_budget(plan: dict[str, Any], budget: int) -> dict[str, Any]:
    """固定原始锁法，独立重构，首次超过原始分即停；否则保留最后一次结果。

    不假定能够撤销失败结果，也不将历史最高分视为可以保留的装备。
    预算是最大可用枚数，成功提前停手，因此期望实际消耗可以低于预算。
    """
    if type(budget) is not int or budget < 0:
        raise ValueError("频整器预算必须为非负整数")
    cost = plan["cost"]
    if type(cost) is not int or cost <= 0:
        raise ValueError("单次频整器消耗必须为正整数")
    attempts = budget // cost
    probability = plan["probability"]
    positive = plan["expected_positive_gain"]
    negative = plan.get("expected_negative_gain", max(0.0, positive - plan["expected_delta"]))

    def terminal(n: int) -> tuple[float, float, float, float]:
        if n == 0:
            return 0.0, 0.0, 0.0, 0.0
        if probability == 0:
            return 0.0, plan["expected_delta"], 0.0, float(n)
        if probability == 1:
            return 1.0, positive, positive, 1.0
        if n == 1:
            return probability, plan["expected_delta"], positive, 1.0
        log_failure = n * math.log1p(-probability)
        success = -math.expm1(log_failure)
        failure = math.exp(log_failure)
        positive_terminal = success * (positive / probability)
        delta = math.fsum([positive_terminal, -failure * (negative / (1 - probability))])
        expected_attempts = min(float(n), max(1.0, success / probability))
        return success, delta, positive_terminal, expected_attempts

    success, delta, gain, expected_attempts = terminal(attempts)
    expected_cost = cost * expected_attempts
    # 直接计算边际，不能以两个已舍入/饱和的终值相减。
    if attempts == 0:
        marginal_delta, marginal_probability, marginal_cost = plan["expected_delta"], probability, float(cost)
    elif probability == 1:
        marginal_delta, marginal_probability, marginal_cost = 0.0, 0.0, 0.0
    else:
        log_q = math.log1p(-probability)
        surviving = math.exp(attempts * log_q)
        marginal_delta = math.exp((attempts - 1) * log_q) * math.fsum([
            (1 - probability) * positive, probability * negative])
        marginal_probability, marginal_cost = probability * surviving, cost * surviving
    return {
        "max_attempts": attempts, "max_cost": cost * attempts,
        "success_probability": success, "expected_delta": delta,
        "expected_positive_gain": gain, "expected_cost": expected_cost,
        "net_gain_per_transducer": delta / expected_cost if expected_cost else None,
        "next_marginal_delta": marginal_delta,
        "next_marginal_probability": marginal_probability,
        "next_marginal_cost": marginal_cost,
    }


def compare_budget(plans: list[dict[str, Any]], budget: int) -> dict[str, Any]:
    """旧固定锁法 API；界面已改用 compare_dynamic_budget，不可用于动态换锁。"""
    evaluated = [{**plan, "budget": evaluate_budget(plan, budget)} for plan in plans]

    def priority(item: dict[str, Any]) -> tuple[float, float, float]:
        value = item["budget"]
        return value["expected_delta"], value["success_probability"], -value["expected_cost"]

    best = [max((plan for plan in evaluated if plan["locked_count"] == count), key=priority)
            for count in sorted({plan["locked_count"] for plan in evaluated})]
    available = [plan for plan in best if plan["budget"]["max_attempts"] > 0]
    recommendation = max(available, key=priority) if available else None
    if recommendation is not None and recommendation["budget"]["expected_delta"] <= 0:
        recommendation = None
    return {"budget": budget, "plans": evaluated, "best_by_count": best,
            "recommendation": recommendation}