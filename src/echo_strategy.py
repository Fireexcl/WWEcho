"""不放回词条分数网格 DP；按材料权重最小化达标成品的平均累计成本。"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

import numpy as np

from src.echo_costs import DEFAULT_COSTS as DEFAULT_COSTS, EXPERIENCE_WEIGHT_UNIT, activation_defaults
from src.echo_score import EchoScorer, finite_number


def nonnegative(value: Any, label: str) -> float:
    number = finite_number(value, label)
    if number < 0:
        raise ValueError(f"{label} 不能为负数")
    return number


def _round_material(amount: Fraction, unit: Fraction, *, up: bool = False) -> Fraction:
    """按材料单位取整；unit=0 保留连续数量，兼容旧成本配置。"""
    if not unit:
        return amount
    count = math.ceil(amount / unit) if up else math.floor(amount / unit)
    return count * unit


class ActivationPlanner:
    """下取整模型内做累计成本最优停止，上取整提供不开弃置的概率上界。

    重复尝试假定胚子主词条已筛选相同、抽样独立且成本固定。
    用 Dinkelbach 分式优化求最小 C/p，并在每个状态比较继续与弃置重开的成本。
    强化和开孔不会失败；未达标且停止投入时主动弃置回收，达标后必须升至 +25 并开满五条。
    采用最少量经验材料逐阶段升级、余量保留；参数未覆盖时读取配置中的成本权重。
    recovery 只控制经验返还，tuning_recovery 独立控制打孔器返还；experience_weight 按每千经验计。
    """

    MAX_OPTIMIZATION_ITERATIONS = 64
    OPTIMIZATION_TOLERANCE = 1e-10

    def __init__(self, scorer: EchoScorer, costs: dict[str, Any], target: float, *,
                 recovery: float | None = None, tuning_recovery: float | None = None,
                 tuning_weight: float | None = None, experience_weight: float | None = None,
                 embryo_weight: float | None = None, bins: int = 1000) -> None:
        self.scorer = scorer
        self.target = nonnegative(target, "目标分数")
        defaults = activation_defaults(costs)
        self.recovery = defaults["recovery"] if recovery is None else nonnegative(recovery, "经验回收比例")
        self.tuning_recovery = (defaults["tuning_recovery"] if tuning_recovery is None
                                else nonnegative(tuning_recovery, "打孔器回收比例"))
        if self.recovery > 1 or self.tuning_recovery > 1:
            raise ValueError("经验和打孔器的弃置回收比例必须分别在 0～1 之间")
        if type(bins) is not int or not 10 <= bins <= 10000:
            raise ValueError("bins 必须是 10～10000 的整数")
        self.bins = bins
        self.tw = defaults["tuning_weight"] if tuning_weight is None else nonnegative(tuning_weight, "打孔器权重")
        self.xw = (defaults["experience_weight"] if experience_weight is None
               else nonnegative(experience_weight, "每千经验权重"))
        self.ew = defaults["embryo_weight"] if embryo_weight is None else nonnegative(embryo_weight, "胚子权重")
        self.ids = list(scorer.tiers)
        self.limit = scorer.stats["sub_stats"]["max_count"]
        if costs.get("schema_version") != 1:
            raise ValueError("成本配置 schema_version 必须为 1")
        steps = costs.get("steps")
        if not isinstance(steps, list) or len(steps) != self.limit:
            raise ValueError("成本配置必须覆盖全部激活槽位")
        experience_unit = Fraction(str(nonnegative(
            costs.get("experience_material_unit", 0), "经验材料单位")))
        tuning_unit = Fraction(str(nonnegative(
            costs.get("tuning_material_unit", 0), "打孔器单位")))
        required_exp = [Fraction(0)]
        cumulative_tuning = [Fraction(0)]
        for slot, step in enumerate(steps, 1):
            if not isinstance(step, dict) or step.get("slot") != slot:
                raise ValueError("成本步骤必须按 slot 顺序排列")
            tuning = Fraction(str(nonnegative(step["tuning"], "打孔器消耗")))
            experience = Fraction(str(nonnegative(step["experience"], "经验需求")))
            if tuning_unit and tuning % tuning_unit:
                raise ValueError("每次消耗的打孔器数量必须是材料单位的整数倍")
            cumulative_tuning.append(cumulative_tuning[-1] + tuning)
            required_exp.append(required_exp[-1] + experience)
        self.embryo = nonnegative(costs["embryo_cost"], "胚子消耗")
        # 先按累计需求补足材料，再取相邻差；不能将每段需求单独进位，避免重复计费余量。
        invested_exp = [_round_material(value, experience_unit, up=True) for value in required_exp]
        self.cumulative_required_exp = [float(value) for value in required_exp]
        self.cumulative_exp = [float(value) for value in invested_exp]
        self.experience = [float(after - before) for before, after in zip(invested_exp, invested_exp[1:])]
        self.cumulative_tuning = [float(value) for value in cumulative_tuning]
        self.tuning = [float(after - before) for before, after in zip(cumulative_tuning, cumulative_tuning[1:])]
        self.remaining_tuning = [self.cumulative_tuning[-1] - value for value in self.cumulative_tuning]
        self.remaining_experience = [self.cumulative_exp[-1] - value for value in self.cumulative_exp]
        # 只在主动弃置回收终点返还一次；十进制有理数避免比例在整数边界误少返材料。
        experience_ratio = Fraction(str(self.recovery))
        tuning_ratio = Fraction(str(self.tuning_recovery))
        self.recovered_experience = [float(_round_material(value * experience_ratio, experience_unit))
                                     for value in invested_exp]
        self.recovered_tuning = [float(_round_material(value * tuning_ratio, tuning_unit))
                                 for value in cumulative_tuning]
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

    def _completion_cost(self, activated_count: int) -> float:
        """已达标也必须支付余下升级经验和打孔器；历史投入不重复计费。"""
        return (self.tw * self.remaining_tuning[activated_count]
                + self.xw * (self.remaining_experience[activated_count] / EXPERIENCE_WEIGHT_UNIT))

    def _material_cost(self, tuning: Any, experience: Any) -> Any:
        return self.tw * tuning + self.xw * (experience / EXPERIENCE_WEIGHT_UNIT)

    def _prepare(self, main: float) -> None:
        if main == self._main:
            return
        self._main = main
        self.gap = self.target - main
        self.resolution = (self.gap if self.gap > 0 else 1.0) / self.bins
        self.lower_cache: dict[int, Any] = {}
        self.upper_cache: dict[int, Any] = {}
        self.policy_cache: dict[int, Any] = {}
        self.decision_cache: dict[int, Any] = {}
        self._candidate_cost: float | None = None
        self._expected_cost: float | None = None
        self._policy_ready = False
        self._optimization_iterations = 0
        self._optimization_converged = False
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

    def _continuation(self, mask: int) -> Any:
        """开下一条，并按同一套最优后续动作处理；不重新收取已有胚子成本。"""
        n = bin(mask).count("1")
        probability = np.zeros(self.bins + 1)
        tuning = np.full(self.bins + 1, self.tuning[n])
        experience = np.full(self.bins + 1, self.experience[n])
        for j, distributions in enumerate(self.quantized):
            if mask & (1 << j):
                continue
            child_p, child_t, child_x = self._policy(mask | (1 << j))
            for indices, p in distributions[0]:
                factor = p / (len(self.ids) - n)
                probability += factor * child_p[indices]
                tuning += factor * child_t[indices]
                experience += factor * child_x[indices]
        return probability, tuning, experience

    def _policy(self, mask: int) -> Any:
        """固定候选累计成本 λ，向后求 min(C-λp)，三项统计来自同一动作。"""
        if mask in self.policy_cache:
            return self.policy_cache[mask]
        n = bin(mask).count("1")
        probability = self.success.copy()
        tuning = np.full(self.bins + 1, -self.recovered_tuning[n])
        experience = np.full(self.bins + 1, -self.recovered_experience[n])
        # 达标后不会降分，不必枚举无关的后续档位，但必须计入开满的全部剩余材料。
        tuning[-1] = self.remaining_tuning[n]
        experience[-1] = self.remaining_experience[n]
        if n == self.limit:
            return probability, tuning, experience
        next_p, next_t, next_x = self._continuation(mask)
        if self._candidate_cost is None:
            # 初始可行政策：不提前弃置，全部开满，给出有限 C/p 上界。
            active = self.index < self.bins
        else:
            continue_value = self._material_cost(next_t, next_x) - self._candidate_cost * next_p
            abandon_value = -self._material_cost(self.recovered_tuning[n], self.recovered_experience[n])
            # 同成本时选成功机会更高的一项，特别处理全部成本为零。
            active = ((continue_value < abandon_value)
                      | ((continue_value == abandon_value) & (next_p > 0)))
            if mask == 0:
                # 尚未观察的同类新胚不能靠免费弃置空转；可达时必须先开第一条。
                active |= next_p > 0
            active &= self.index < self.bins
        probability[active] = next_p[active]
        tuning[active] = next_t[active]
        experience[active] = next_x[active]
        result = np.clip(probability, 0, 1), tuning, experience
        self.policy_cache[mask] = result
        self.decision_cache[mask] = active
        return result

    def _root_attempt(self) -> tuple[float, float]:
        probability, tuning, experience = self._policy(0)
        chance = float(probability[0])
        cost = self.ew * self.embryo + float(self._material_cost(tuning[0], experience[0]))
        if not math.isfinite(cost) or not math.isfinite(chance) or chance <= 0:
            raise RuntimeError("累计成本优化未得到有限、正成功率的可行策略")
        if cost < 0:
            raise RuntimeError("累计成本优化出现无效的负单胚成本")
        return chance, cost

    def _optimize(self, full_probability: float) -> None:
        if self._policy_ready:
            return
        if full_probability <= 0:
            # 不将网格下界为零伪装成正概率，也不计算 0/0；实际不可达与精度不足由上界区分。
            self._candidate_cost = 0.0
            self._policy_ready = True
            return
        chance, cost = self._root_attempt()
        candidate = cost / chance
        if not math.isfinite(candidate):
            raise ValueError("达标累计成本超出有限数值范围")
        for iteration in range(1, self.MAX_OPTIMIZATION_ITERATIONS + 1):
            self.policy_cache.clear()
            self.decision_cache.clear()
            self._candidate_cost = candidate
            chance, cost = self._root_attempt()
            updated = cost / chance
            if not math.isfinite(updated):
                raise ValueError("达标累计成本超出有限数值范围")
            self._optimization_iterations = iteration
            tolerance = self.OPTIMIZATION_TOLERANCE * max(1.0, abs(candidate), abs(updated))
            if updated > candidate + tolerance:
                raise RuntimeError("累计成本优化未单调收敛，请检查成本配置或计算精度")
            if abs(updated - candidate) <= tolerance:
                self._expected_cost = updated
                self._policy_ready = True
                self._optimization_converged = True
                return
            candidate = updated
        # 不静默把未收敛政策称为最优；GUI 会独立显示错误而不影响评分。
        self.policy_cache.clear()
        self.decision_cache.clear()
        self._candidate_cost = None
        raise RuntimeError("累计成本策略在迭代上限内未收敛")

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
        count = len(raw)
        if reached:
            completion_reason = (f"分数已达标，必须升至 +25 并开满剩余 {self.limit - count} 条"
                                 if count < self.limit else "分数已达标且五条已开满，培养完成")
        if self.gap <= 0:
            # 即使仅主/小词条的满级投影已达标，也要从零投入到 +25、五条全开。
            attempt = self.ew * self.embryo + self._completion_cost(0)
            if not math.isfinite(attempt):
                raise ValueError("达标累计成本超出有限数值范围")
            zero = dict(full_success_lower=1., full_success_upper=1., success_probability=1.,
                        attempt_cost=attempt, expected_cost=attempt,
                        expected_embryos=1., expected_tuning=self.cumulative_tuning[-1],
                        expected_experience=self.cumulative_exp[-1])
            current = dict(full_success_lower=1., full_success_upper=1., success_probability=1.,
                           recommend_continue=count < self.limit,
                           reason=completion_reason, future_cost=self._completion_cost(count),
                           expected_total_cost=self._completion_cost(count),
                           continue_cost=self._completion_cost(count) if count < self.limit else None,
                           restart_cost=None)
            self._optimization_converged = True
        else:
            lower = self._full(0)
            upper = self._full(0, True)
            self._optimize(float(lower[0]))
            p, t, x = self._policy(0)
            chance = float(p[0])
            attempt = (self.ew * self.embryo + self.tw * float(t[0])
                       + self.xw * (float(x[0]) / EXPERIENCE_WEIGHT_UNIT))
            zero = dict(full_success_lower=float(lower[0]), full_success_upper=float(upper[0]),
                        success_probability=chance, attempt_cost=attempt,
                        expected_cost=attempt / chance if chance > 0 else None,
                        expected_embryos=1 / chance if chance > 0 else None,
                        expected_tuning=float(t[0]) / chance if chance > 0 else None,
                        expected_experience=float(x[0]) / chance if chance > 0 else None)
            full_lo, full_hi = float(self._full(mask)[lo]), float(self._full(mask, True)[hi])
            restart_cost = (self._expected_cost - self._material_cost(
                self.recovered_tuning[count], self.recovered_experience[count])
                if self._expected_cost is not None else None)
            continue_cost = None
            if reached:
                full_lo = full_hi = 1.0
                current_p = 1.0
                future_cost = self._completion_cost(count)
                total_cost = future_cost
                restart_cost = None  # 已达标后不允许弃置。
                continue_cost = future_cost if count < self.limit else None
                recommend = count < self.limit
                reason = completion_reason
            elif count == self.limit:
                full_lo = full_hi = current_p = 0.0
                future_cost = -float(self._material_cost(
                    self.recovered_tuning[count], self.recovered_experience[count]))
                total_cost = restart_cost
                recommend = False
                reason = "五条已开满但未达标，建议弃置回收后换新胚子"
            else:
                cp, ct, cx = self._policy(mask)
                if self._expected_cost is None:
                    current_p = 0.0
                    future_cost = -float(self._material_cost(
                        self.recovered_tuning[count], self.recovered_experience[count]))
                    total_cost = None
                    recommend = False
                    reason = ("新胚子也无法达标，当前没有有限的成功成本" if float(upper[0]) == 0
                              else "当前计算精度还不能确认新胚子的成功机会，暂不提供成本最优建议")
                else:
                    next_p, next_t, next_x = self._continuation(mask)
                    continue_cost = float(self._material_cost(next_t[lo], next_x[lo]))
                    continue_cost += (1 - float(next_p[lo])) * self._expected_cost
                    recommend = bool(self.decision_cache[mask][lo])
                    current_p = float(cp[lo])
                    future_cost = float(self._material_cost(ct[lo], cx[lo]))
                    total_cost = future_cost + (1 - current_p) * self._expected_cost
                    reason = ("继续开，再按结果决策的平均累计成本不高于弃置重开，建议继续"
                              if recommend else "弃置回收后换新胚子的平均累计成本较低，建议停止")
            current = dict(full_success_lower=full_lo, full_success_upper=full_hi,
                           success_probability=current_p,
                           recommend_continue=recommend, reason=reason,
                           future_cost=future_cost, expected_total_cost=total_cost,
                           continue_cost=continue_cost, restart_cost=restart_cost)
        return dict(zero=zero, current=current, score_resolution=self.resolution,
                    method="分数网格上下界 DP；Dinkelbach 累计成本最优停止；达标后必须开满",
                    optimization_converged=self._optimization_converged,
                    optimization_iterations=self._optimization_iterations)