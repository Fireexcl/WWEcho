"""同预算动态换锁：每次实际洗练后可接受候选或拒绝并沿用原词条。

方案按预算完整执行，不以未执行的零消耗分支稀释收益。接受/拒绝后均扣除
本次材料，再在实际保留的状态上决策。离散状态求和，不使用随机模拟或分数网格。
"""

from __future__ import annotations

import math
from concurrent.futures import CancelledError
from copy import deepcopy
from fractions import Fraction
from itertools import combinations
from threading import Event
from typing import TYPE_CHECKING, Any, Optional

import numpy as np

if TYPE_CHECKING:
    from src.echo_reconstruction import ReconstructionPlanner


MAX_DYNAMIC_BUDGET = 6
MAX_DYNAMIC_STATES = 1_000_000
MAX_DYNAMIC_CACHE_BYTES = 192 * 1024**2
_Policy = tuple
_Tables = dict[tuple[int, ...], np.ndarray]


class BudgetResourceError(ValueError):
    """拒绝截断状态或暗中抽样；超限只使动态预算不可用。"""


def _check_cancelled(cancelled: Optional[Event]) -> None:
    if cancelled is not None and cancelled.is_set():
        raise CancelledError()


def _paths(budget: int) -> list[tuple[int, ...]]:
    if not budget:
        return [()]
    return [(cost,) + tail for cost in (1, 2, 3) if cost <= budget
            for tail in _paths(budget - cost)]


class AdaptiveBudgetPlanner:
    """V(s)=S(s)+G(s)，支付后比较 V_tail(旧状态) 与 V_tail(候选)。

    后缀表保存完整离散状态的 G 与期望次数。所有可执行政策用完剩余预算，
    因此消耗是确定的预算，不包含“放弃洗练”的样本。相同最终价值选少次数，
    接受/拒绝仍平分时保留旧状态。单次分析的输出、算法与指标不作修改。
    """

    def __init__(self, single_planner: ReconstructionPlanner) -> None:
        self.single_planner = single_planner
        self.ids = tuple(single_planner.ids)
        self._indices = {name: index for index, name in enumerate(self.ids)}
        self._scale = Fraction(single_planner.point_scale)
        self._float_scale = self._fraction_number(self._scale)
        self._costs = dict(single_planner.lock_costs)
        self._scores = []
        self._probabilities = []
        self._score_indices = []
        for name in self.ids:
            grouped: dict[int, list[float]] = {}
            for score, probability in zip(single_planner.scores[name], single_planner.probabilities[name]):
                grouped.setdefault(score, []).append(probability)
            scores = tuple(sorted(grouped))
            self._scores.append(scores)
            self._score_indices.append({score: index for index, score in enumerate(scores)})
            # 合法零概率档位仍可作为输入/保留状态；不以锁定档位概率作除数。
            self._probabilities.append(np.array([math.fsum(grouped[value]) for value in scores],
                                                 dtype=np.longdouble))
        self._types = tuple(combinations(range(len(self.ids)), 5))
        self._state_count = sum(math.prod(len(self._scores[index]) for index in types) for types in self._types)
        self._contexts: dict[tuple[int, ...], tuple[tuple[int, ...], ...]] = {}
        self._table_cache: dict[_Policy, _Tables] = {}
        self._free_cache: dict[int, _Policy] = {0: ()}
        self._path_cache: dict[tuple[int, ...], _Policy] = {(): ()}

    @staticmethod
    def _fraction_number(value: Fraction) -> np.longdouble:
        # 先相除再转 double 会丢掉长双精度；极端超长整数则使用有界比值转换。
        limit = min(12000, np.finfo(np.longdouble).maxexp - 16)
        if max(value.numerator.bit_length(), value.denominator.bit_length()) < limit:
            return np.longdouble(str(value.numerator)) / np.longdouble(str(value.denominator))
        return np.longdouble(float(value))

    def _free(self, budget: int) -> _Policy:
        if budget not in self._free_cache:
            self._free_cache[budget] = tuple((cost, self._free(budget - cost))
                                             for cost in (1, 2, 3) if cost <= budget)
        return self._free_cache[budget]

    def _path(self, path: tuple[int, ...]) -> _Policy:
        if path not in self._path_cache:
            self._path_cache[path] = ((path[0], self._path(path[1:])),)
        return self._path_cache[path]

    @classmethod
    def _attempt_limits(cls, policy: _Policy) -> tuple[int, int]:
        if not policy:
            return 0, 0
        limits = [cls._attempt_limits(suffix) for _, suffix in policy]
        return 1 + min(value[0] for value in limits), 1 + max(value[1] for value in limits)

    def _completions(self, locked: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
        if locked not in self._contexts:
            pool = tuple(index for index in range(len(self.ids)) if index not in locked)
            self._contexts[locked] = tuple(tuple(sorted(locked + rest))
                                            for rest in combinations(pool, 5 - len(locked)))
        return self._contexts[locked]

    def _differences(self, types: tuple[int, ...], anchor: int) -> np.ndarray:
        """先减整数局部基准，锁定的巨大贡献在候选与原状态两侧抵消。"""
        values = [self._scores[index] for index in types]
        shape = tuple(len(scores) for scores in values)
        baseline = sum(scores[0] for scores in values) - anchor
        span = sum(scores[-1] - scores[0] for scores in values)
        if not self._scale:
            return np.zeros(shape, dtype=np.longdouble)
        if abs(baseline) + span < 2**62 and self._float_scale > 0:
            integers = np.full(shape, baseline, dtype=np.int64)
            for axis, scores in enumerate(values):
                axis_shape = [1] * len(shape)
                axis_shape[axis] = len(scores)
                integers += np.array([value - scores[0] for value in scores]).reshape(axis_shape)
            return integers.astype(np.longdouble) * self._float_scale
        return np.fromiter((self._fraction_number((sum(values[axis][tier]
                             for axis, tier in enumerate(tiers)) - anchor) * self._scale)
                            for tiers in np.ndindex(shape)), dtype=np.longdouble).reshape(shape)

    def _block(self, types: tuple[int, ...], locked: tuple[int, ...], child: Optional[_Tables],
               anchor: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, tuple[int, ...]]:
        kept = tuple(types.index(index) for index in locked)
        other = tuple(axis for axis in range(5) if axis not in kept)
        permutation = kept + other
        rows = math.prod(len(self._scores[index]) for index in locked)
        unlocked = tuple(types[axis] for axis in other)
        delta = self._differences(unlocked, anchor).reshape(1, -1)
        if child is None:
            gains = np.zeros((rows, delta.size), dtype=np.longdouble)
            attempts = np.zeros_like(gains)
        else:
            gains = child[types][0].transpose(permutation).reshape(rows, -1)
            attempts = child[types][1].transpose(permutation).reshape(rows, -1)
        probability: Any = np.array(1, dtype=np.longdouble)
        for index in unlocked:
            probability = np.multiply.outer(probability, self._probabilities[index])
        return gains + delta, gains, attempts, probability.reshape(-1), permutation

    @staticmethod
    def _rank(values: np.ndarray, attempts: np.ndarray, weights: np.ndarray) -> tuple:
        order = np.lexsort((-attempts, values), axis=-1)
        ranked = np.take_along_axis(values, order, axis=-1)
        counts = np.take_along_axis(attempts, order, axis=-1)
        probability = weights[order] / np.sum(weights, dtype=np.longdouble)
        tail_p = np.cumsum(probability[..., ::-1], axis=-1)[..., ::-1]
        tail_n = np.cumsum((probability * counts)[..., ::-1], axis=-1)[..., ::-1]
        excess = np.zeros_like(ranked)
        if ranked.shape[-1] > 1:
            increments = np.diff(ranked, axis=-1) * tail_p[..., 1:]
            excess[..., :-1] = np.cumsum(increments[..., ::-1], axis=-1)[..., ::-1]
        return order, ranked, counts, probability, tail_p, tail_n, excess

    def _tables(self, policy: _Policy, cancelled: Optional[Event]) -> Optional[_Tables]:
        _check_cancelled(cancelled)
        if not policy:
            return None
        if policy in self._table_cache:
            return self._table_cache[policy]
        if self._state_count > MAX_DYNAMIC_STATES:
            raise BudgetResourceError(f"动态保留/拒绝模型有 {self._state_count:,} 个状态，超过 "
                                      f"{MAX_DYNAMIC_STATES:,} 个上限；本次不提供动态推荐，单次分析不变")
        # 多种首步花费的自由政策是对应单边政策逐状态择优，复用同一后缀。
        if len(policy) > 1:
            branches = [self._tables((edge,), cancelled) for edge in policy]
        else:
            branches = []
            self._tables(policy[0][1], cancelled)
        allocated = sum(value.nbytes for tables in self._table_cache.values() for value in tables.values())
        needed = self._state_count * 2 * np.dtype(np.longdouble).itemsize
        if allocated + needed > MAX_DYNAMIC_CACHE_BYTES:
            raise BudgetResourceError("动态保留/拒绝缓存超过 192 MiB；请降低比较预算，单次分析不受影响")
        tables = {types: np.full((2,) + tuple(len(self._scores[index]) for index in types),
                    np.inf, dtype=np.longdouble)
                  for types in self._types}
        for value in tables.values():
            value[0].fill(-np.inf)
        if branches:
            for branch in branches:
                assert branch is not None
                for types, best in tables.items():
                    _check_cancelled(cancelled)
                    candidate = branch[types]
                    better = (candidate[0] > best[0]) | ((candidate[0] == best[0]) & (candidate[1] < best[1]))
                    np.copyto(best[0], candidate[0], where=better)
                    np.copyto(best[1], candidate[1], where=better)
        else:
            cost, suffix = policy[0]
            child = self._tables(suffix, cancelled)
            for count, action_cost in self._costs.items():
                if action_cost != cost:
                    continue
                for locked in combinations(range(len(self.ids)), count):
                    _check_cancelled(cancelled)
                    contexts = self._completions(locked)
                    anchor = min(sum(self._scores[index][0] for index in types if index not in locked)
                                 for types in contexts)
                    chunks = [self._block(types, locked, child, anchor) for types in contexts]
                    values = np.concatenate([chunk[0] for chunk in chunks], axis=1)
                    gains = np.concatenate([chunk[1] for chunk in chunks], axis=1)
                    attempts = np.concatenate([chunk[2] for chunk in chunks], axis=1)
                    weights = np.concatenate([chunk[3] for chunk in chunks])
                    order, _, ranked_n, probability, _, tail_n, excess = self._rank(values, attempts, weights)
                    candidate_g = np.take_along_axis(gains, order, axis=1) + excess
                    candidate_n = 1 + ranked_n * np.cumsum(probability, axis=1)
                    candidate_n[:, :-1] += tail_n[:, 1:]
                    unsorted_g, unsorted_n = np.empty_like(gains), np.empty_like(gains)
                    np.put_along_axis(unsorted_g, order, candidate_g, axis=1)
                    np.put_along_axis(unsorted_n, order, candidate_n, axis=1)
                    offset = 0
                    for types, chunk in zip(contexts, chunks):
                        length = chunk[0].shape[1]
                        shape = tuple(len(self._scores[types[axis]]) for axis in chunk[4])
                        inverse = tuple(np.argsort(chunk[4]))
                        gain = unsorted_g[:, offset:offset + length].reshape(shape).transpose(inverse)
                        used = unsorted_n[:, offset:offset + length].reshape(shape).transpose(inverse)
                        best = tables[types]
                        better = (gain > best[0]) | ((gain == best[0]) & (used < best[1]))
                        np.copyto(best[0], gain, where=better)
                        np.copyto(best[1], used, where=better)
                        offset += length
        _check_cancelled(cancelled)
        minimum, maximum = self._attempt_limits(policy)
        for value in tables.values():
            # 次数传播的概率累积仅允许机器尾差，不可越过政策的确定性边界。
            np.clip(value[1], minimum, maximum, out=value[1])
            value.flags.writeable = False
        self._table_cache[policy] = tables
        return tables

    def _action(self, current: dict[int, int], plan: dict[str, Any], suffix: _Policy,
                cancelled: Optional[Event]) -> tuple[np.longdouble, float]:
        child = self._tables(suffix, cancelled)
        if child is None:
            # 同预算末步允许拒绝；只读取单次已有正收益，不修改其原始均值指标。
            return np.longdouble(plan["expected_positive_gain"]), 1.0
        types = tuple(sorted(current))
        tiers = tuple(self._score_indices[index][current[index]] for index in types)
        old_g, old_n = child[types][(slice(None),) + tiers]
        locked = tuple(sorted(self._indices[name] for name in plan["locked_ids"]))
        locked_shape = tuple(len(self._scores[index]) for index in locked)
        locked_tiers = tuple(self._score_indices[index][current[index]] for index in locked)
        row = np.ravel_multi_index(locked_tiers, locked_shape)
        anchor = sum(value for index, value in current.items() if index not in locked)
        chunks = []
        for candidate_types in self._completions(locked):
            _check_cancelled(cancelled)
            chunk = self._block(candidate_types, locked, child, anchor)
            chunks.append((chunk[0][row], chunk[2][row], chunk[3]))
        values = np.concatenate([chunk[0] for chunk in chunks])
        attempts = np.concatenate([chunk[1] for chunk in chunks])
        weights = np.concatenate([chunk[2] for chunk in chunks])
        _, values, counts, probability, tail_p, tail_n, excess = self._rank(values, attempts, weights)
        left, right = np.searchsorted(values, old_g, side="left"), np.searchsorted(values, old_g, side="right")
        split = left + np.searchsorted(-counts[left:right], -old_n, side="right")
        if split == len(values):
            return old_g, float(1 + old_n)
        gain = old_g + excess[split] + (values[split] - old_g) * tail_p[split]
        used = 1 + old_n * np.sum(probability[:split], dtype=np.longdouble) + tail_n[split]
        return gain, float(used)

    @staticmethod
    def _metrics(gain: np.longdouble, attempts: float, current_score: float,
                 budget: int, maximum: int) -> dict[str, Any]:
        delta = float(gain)
        return {"max_cost": budget, "max_attempts": maximum, "expected_delta": delta,
                "expected_final_score": math.fsum((current_score, delta)),
            "expected_cost": float(budget), "expected_attempts": min(float(maximum), max(0.0, attempts)),
                "net_gain_per_transducer": delta / budget if budget else None}

    def compare(self, echo: dict[str, Any], budget: int, *, analysis: Optional[dict[str, Any]] = None,
                cancelled: Optional[Event] = None) -> dict[str, Any]:
        """比较实际执行完整预算的方案；只有独立“不投入”行是不执行基线。"""
        if type(budget) is not int or not 0 <= budget <= MAX_DYNAMIC_BUDGET:
            raise ValueError(f"动态频整器预算必须为 0～{MAX_DYNAMIC_BUDGET} 的整数")
        _check_cancelled(cancelled)
        if analysis is None:
            analysis = self.single_planner.analyze(echo, cancelled=cancelled)
        entries = self.single_planner.scorer.score(echo)["sub_stats"]
        if len(entries) != 5:
            raise ValueError("动态频整器只能用于已开满五条副词条的声骸")
        current = {}
        for item in entries:
            name = item["stat_id"]
            tier = self.single_planner.scorer.tiers[name]["values"].index(item["value"])
            current[self._indices[name]] = self.single_planner.scores[name][tier]
        score, plans = analysis["current_score"], analysis["plans"]
        action_cache: dict[tuple, tuple] = {}

        def action(plan: dict[str, Any], suffix: _Policy) -> tuple:
            key = (tuple(plan["locked_ids"]), suffix)
            if key not in action_cache:
                action_cache[key] = self._action(current, plan, suffix, cancelled)
            return action_cache[key]

        def row(label: str, path: Optional[tuple[int, ...]], policy: _Policy) -> dict[str, Any]:
            best, first = (np.longdouble(0), 0.0), None
            for cost, suffix in policy:
                for plan in plans:
                    if plan["cost"] == cost:
                        candidate = action(plan, suffix)
                        if first is None or (candidate[0], -candidate[1]) > (best[0], -best[1]):
                            best, first = candidate, plan
            if first and path is not None:
                best = best[0], float(len(path))
            return {"label": label, "spending_path": list(path) if path is not None else None,
                    "locked_ids": deepcopy(first["locked_ids"]) if first else [],
                    "locked_stats": deepcopy(first["locked_stats"]) if first else [],
                    "locked_count": first["locked_count"] if first else 0, "cost": first["cost"] if first else 0,
                    "budget": self._metrics(best[0], best[1], score, budget if first else 0,
                          (len(path) if path is not None else 1 + budget - first["cost"]) if first else 0)}

        rows = [row("不投入", (), ())]
        if budget:
            rows.append(row("动态择优", None, self._free(budget)))
            rows.extend(row("+".join(map(str, path)), path, self._path(path)) for path in _paths(budget))
        first_plans = []
        for plan in plans:
            _check_cancelled(cancelled)
            copied = deepcopy(plan)
            if plan["cost"] <= budget:
                gain, attempts = action(plan, self._free(budget - plan["cost"]))
                copied["dynamic_budget"] = self._metrics(gain, attempts, score, budget, 1 + budget - plan["cost"])
            else:
                copied["dynamic_budget"] = None
            first_plans.append(copied)
        return {"budget": budget, "current_score": score, "rows": rows, "first_plans": first_plans,
                "recommendation": rows[1] if budget else None,
                "method": "动态换锁 DP · 候选可保留/拒绝 · 实际用完预算，不混入未洗分支"}