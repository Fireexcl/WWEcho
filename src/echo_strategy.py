"""不放回词条模型的分数网格 DP；概率上下界与相对基准策略成本。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from src.echo_score import ROOT, EchoScorer, finite_number

DEFAULT_COSTS = ROOT / "config" / "activation-costs.json"


def nonnegative(value: Any, label: str) -> float:
    number = finite_number(value, label)
    if number < 0:
        raise ValueError(f"{label} 不能为负数")
    return number


class ActivationPlanner:
    """下取整增分保守决策，上取整增分提供完整激活概率上界。

    重复尝试假定胚子主词条已筛选相同、抽样独立且成本固定。
    推荐以相同主词条、Cost、权重和目标下的 0 词条达标概率下界为基准。
    """

    def __init__(self, scorer: EchoScorer, costs: dict[str, Any], target: float, *,
                 recovery: float = .66,
                 tuning_weight: float = 1, experience_weight: float = 1,
                 embryo_weight: float = 1, bins: int = 1000) -> None:
        self.scorer = scorer
        self.target = nonnegative(target, "目标分数")
        self.recovery = nonnegative(recovery, "回收系数")
        if self.recovery > 1:
            raise ValueError("回收系数必须在 0～1 之间")
        if type(bins) is not int or not 10 <= bins <= 10000:
            raise ValueError("bins 必须是 10～10000 的整数")
        self.bins = bins
        self.tw = nonnegative(tuning_weight, "调谐权重")
        self.xw = nonnegative(experience_weight, "经验权重")
        self.ew = nonnegative(embryo_weight, "胚子权重")
        self.ids = list(scorer.tiers)
        self.limit = scorer.stats["sub_stats"]["max_count"]
        if costs.get("schema_version") != 1:
            raise ValueError("成本配置 schema_version 必须为 1")
        steps = costs.get("steps")
        if not isinstance(steps, list) or len(steps) != self.limit:
            raise ValueError("成本配置必须覆盖全部激活槽位")
        self.tuning, self.experience = [], []
        for slot, step in enumerate(steps, 1):
            if not isinstance(step, dict) or step.get("slot") != slot:
                raise ValueError("成本步骤必须按 slot 顺序排列")
            self.tuning.append(nonnegative(step["tuning"], "调谐消耗"))
            self.experience.append(nonnegative(step["experience"], "经验消耗"))
        self.embryo = nonnegative(costs["embryo_cost"], "胚子消耗")
        self.cumulative_exp = [math.fsum(self.experience[:n]) for n in range(self.limit + 1)]
        self.outcomes = []
        for stat_id in self.ids:
            tier = scorer.tiers[stat_id]
            values = tier["values"]
            published = scorer.stats["probability_distributions"][tier["probability_distribution"]]["published_probability_pct"]
            if len(values) != len(published):
                raise ValueError("档位与概率数组必须一一对应")
            probabilities = [nonnegative(p, "档位概率") for p in published]
            total = math.fsum(probabilities)
            if total <= 0:
                raise ValueError("档位概率总和必须为正")
            self.outcomes.append([(scorer.stat_score("sub_stats", stat_id, value), p / total)
                                  for value, p in zip(values, probabilities)])
        self._main = None

    def _prepare(self, main: float) -> None:
        if main == self._main:
            return
        self._main = main
        self.gap = self.target - main
        self.resolution = (self.gap if self.gap > 0 else 1.0) / self.bins
        self.lower_cache: dict[int, Any] = {}
        self.upper_cache: dict[int, Any] = {}
        self.policy_cache: dict[int, Any] = {}
        self.baseline_probability = 0.0
        self.index = np.arange(self.bins + 1)
        self.success = (self.index == self.bins).astype(float)
        self.quantized = []
        for outcomes in self.outcomes:
            distributions = []
            for upper in (False, True):
                combined: dict[int, float] = {}
                for gain, probability in outcomes:
                    scaled = gain / self.resolution
                    if not math.isfinite(scaled):
                        raise ValueError("权重过大，策略增分超出有限数值范围")
                    # 不对临界点做容差修正，保留保守上下界。
                    amount = min(self.bins, math.ceil(scaled) if upper else math.floor(scaled))
                    combined[amount] = combined.get(amount, 0) + probability
                distributions.append([(np.minimum(self.index + gain, self.bins), p)
                                      for gain, p in combined.items()])
            self.quantized.append(distributions)

    def _full(self, mask: int, upper: bool = False) -> Any:
        cache = self.upper_cache if upper else self.lower_cache
        if mask in cache:
            return cache[mask]
        n = bin(mask).count("1")
        if n == self.limit:
            return self.success
        result = np.zeros(self.bins + 1)
        remaining = len(self.ids) - n
        for j, distributions in enumerate(self.quantized):
            if mask & (1 << j):
                continue
            child = self._full(mask | (1 << j), upper)
            for indices, probability in distributions[int(upper)]:
                result += (probability / remaining) * child[indices]
        result[-1] = 1
        cache[mask] = np.clip(result, 0, 1)
        return cache[mask]

    def _policy(self, mask: int) -> Any:
        if mask in self.policy_cache:
            return self.policy_cache[mask]
        n = bin(mask).count("1")
        probability = self.success.copy()
        tuning = np.zeros(self.bins + 1)
        experience = np.full(self.bins + 1, -self.recovery * self.cumulative_exp[n])
        experience[-1] = 0
        if n == self.limit:
            return probability, tuning, experience
        full = self._full(mask)
        active = (full >= self.baseline_probability) & (full > 0) & (self.index < self.bins)
        if np.any(active):
            next_p = np.zeros(self.bins + 1)
            next_t = np.full(self.bins + 1, self.tuning[n])
            next_x = np.full(self.bins + 1, self.experience[n])
            for j, distributions in enumerate(self.quantized):
                if mask & (1 << j):
                    continue
                child_p, child_t, child_x = self._policy(mask | (1 << j))
                for indices, p in distributions[0]:
                    factor = p / (len(self.ids) - n)
                    next_p += factor * child_p[indices]
                    next_t += factor * child_t[indices]
                    next_x += factor * child_x[indices]
            probability[active] = next_p[active]
            tuning[active] = next_t[active]
            experience[active] = next_x[active]
        result = np.clip(probability, 0, 1), tuning, experience
        self.policy_cache[mask] = result
        return result

    def analyze(self, echo: dict[str, Any]) -> dict[str, Any]:
        result = self.scorer.score(echo)
        main_id = result["main_stat"]["stat_id"]
        main = math.fsum([
            self.scorer.stat_score("main_stats", main_id, result["main_stat"]["value"], result["cost"]),
            self.scorer.stat_score("minor_stats", result["minor_stat"]["stat_id"],
                                   result["minor_stat"]["value"], result["cost"]),
        ])
        self._prepare(main)
        mask = 0
        low_score, high_score, raw = 0, 0, []
        for item in result["sub_stats"]:
            j = self.ids.index(item["stat_id"])
            mask |= 1 << j
            gain = self.scorer.stat_score("sub_stats", item["stat_id"], item["value"])
            raw.append(gain)
            low_score += math.floor(gain / self.resolution)
            high_score += math.ceil(gain / self.resolution)
        reached = math.fsum([main] + raw) >= self.target
        lo, hi = min(low_score, self.bins), min(high_score, self.bins)
        if self.gap <= 0:
            zero = dict(full_success_lower=1., full_success_upper=1., success_probability=1.,
                        attempt_cost=self.ew * self.embryo, expected_cost=self.ew * self.embryo,
                        expected_embryos=1., expected_tuning=0., expected_experience=0.)
            current = dict(full_success_lower=1., full_success_upper=1., success_probability=1.,
                           baseline_success_lower=1., recommend_continue=False,
                           reason="目标已达成，无需继续", future_cost=0.)
        else:
            lower = self._full(0)
            upper = self._full(0, True)
            baseline = float(lower[0])
            self.baseline_probability = baseline
            p, t, x = self._policy(0)
            chance = float(p[0])
            attempt = self.ew * self.embryo + self.tw * float(t[0]) + self.xw * float(x[0])
            zero = dict(full_success_lower=float(lower[0]), full_success_upper=float(upper[0]),
                        success_probability=chance, attempt_cost=attempt,
                        expected_cost=attempt / chance if chance > 0 else None,
                        expected_embryos=1 / chance if chance > 0 else None,
                        expected_tuning=float(t[0]) / chance if chance > 0 else None,
                        expected_experience=float(x[0]) / chance if chance > 0 else None)
            full_lo, full_hi = float(self._full(mask)[lo]), float(self._full(mask, True)[hi])
            cp, ct, cx = self._policy(mask)
            recommend = (not reached and len(raw) < self.limit
                         and full_lo > 0 and full_lo >= baseline)
            if reached:
                reason = "目标已达成，无需继续"
            elif len(raw) == self.limit:
                reason = "槽位已满，无法继续"
            elif full_hi == 0:
                reason = "剩余槽位无法达到目标，放弃"
            elif full_lo == 0:
                reason = "分数网格下界未确认正达标概率，保守停止"
            elif full_lo < baseline:
                reason = "当前保守达标概率低于 0 词条基准，放弃"
            else:
                reason = "当前保守达标概率不低于 0 词条基准，推荐继续并逐条重评"
            current = dict(full_success_lower=1. if reached else full_lo,
                           full_success_upper=1. if reached else full_hi,
                           success_probability=1. if reached else float(cp[lo]),
                           baseline_success_lower=baseline,
                           recommend_continue=recommend, reason=reason,
                           future_cost=0. if reached else self.tw * float(ct[lo]) + self.xw * float(cx[lo]))
        return dict(zero=zero, current=current, score_resolution=self.resolution,
                    method="分数网格上下界 DP；0 词条基准策略近似；失败仅回收经验")