"""用独立有理数状态穷举，交叉验证动态换锁有限预算。"""

import copy
import math
import unittest
from concurrent.futures import CancelledError
from fractions import Fraction
from functools import lru_cache
from itertools import combinations, product
from threading import Event
from unittest.mock import patch

from src.echo_reconstruction import ReconstructionPlanner
from src.echo_reconstruction_budget import AdaptiveBudgetPlanner, BudgetResourceError, MAX_DYNAMIC_BUDGET
from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, EchoScorer, load_json


def small_model():
    stats, weights = load_json(DEFAULT_STATS), load_json(DEFAULT_WEIGHTS)
    names = ("hp_flat", "atk_flat", "def_flat", "hp_pct", "atk_pct", "def_pct")
    for pools in (weights["main_stats"], weights["minor_stats"]):
        for pool in pools.values():
            for name in pool:
                pool[name] = 0
    weights["sub_stats"] = {name: 1 for name in names}
    stats["sub_stats"]["tiers"] = {
        name: {"values": [index + 1, index + 3], "probability_distribution": "test_dynamic"}
        for index, name in enumerate(names)}
    stats["sub_stats"]["pool_size"] = len(names)
    stats["probability_distributions"]["test_dynamic"] = {"published_probability_pct": [25, 75]}
    for case in stats["reconstruction"]["cases"]:
        case["candidate_count"] = len(names) - case["locked_count"]
        case["type_probability"]["denominator"] = case["candidate_count"]
    echo = {"cost": 4, "main_stat": {"name": "crit_rate", "value": 22},
            "sub_stats": [{"name": name, "value": stats["sub_stats"]["tiers"][name]["values"][0]}
                          for name in names[:5]]}
    return stats, weights, echo


class ExactOracle:
    """逐状态、逐动作、逐结果穷举，不访问实现的整数分数/条件表。"""

    costs = {1: 1, 2: 1, 3: 2, 4: 3}

    def __init__(self, stats, weights):
        self.names = tuple(stats["sub_stats"]["tiers"])
        self.values, self.probabilities = [], []
        for name in self.names:
            tier = stats["sub_stats"]["tiers"][name]
            self.values.append(tuple(Fraction(str(value)) * Fraction(str(weights["sub_stats"][name]))
                                     for value in tier["values"]))
            probabilities = [Fraction(str(value)) for value in
                             stats["probability_distributions"][tier["probability_distribution"]]
                             ["published_probability_pct"]]
            self.probabilities.append(tuple(value / sum(probabilities) for value in probabilities))
        self.scale = Fraction(50) / Fraction(str(EchoScorer(stats, weights).unaligned_max_score))
        self.actions = {state: tuple(locked for count in range(1, 5)
                                    for locked in combinations(state, count))
                        for types in combinations(range(len(self.names)), 5)
                        for tiers in product(range(2), repeat=5)
                        for state in [tuple(zip(types, tiers))]}

    def score(self, state):
        return sum((self.values[index][tier] for index, tier in state), Fraction(0))

    @lru_cache(None)
    def outcomes(self, locked):
        pool = tuple(index for index in range(len(self.names)) if index not in dict(locked))
        rerolled = 5 - len(locked)
        result = []
        for types in combinations(pool, rerolled):
            for tiers in product(range(2), repeat=rerolled):
                probability = math.prod(self.probabilities[index][tier] for index, tier in zip(types, tiers))
                probability /= math.comb(len(pool), rerolled)
                if probability:
                    result.append((tuple(sorted(locked + tuple(zip(types, tiers)))), probability))
        return tuple(result)

    @staticmethod
    def priority(value):
        return value[0], -value[1], -value[2]

    @lru_cache(None)
    def action(self, current, locked, tail):
        used = Fraction(self.costs[len(locked)])
        result = [Fraction(0), used, Fraction(1)]
        retained, _ = self.solve(current, tail)
        for state, probability in self.outcomes(locked):
            continuation, _ = self.solve(state, tail)
            if self.priority(retained) >= self.priority(continuation):
                continuation = retained
            for index in range(3):
                result[index] += probability * continuation[index]
        return tuple(result)

    @lru_cache(None)
    def solve(self, state, policy):
        if not policy:
            return (self.score(state), Fraction(0), Fraction(0)), ()
        best, first = None, ()
        if isinstance(policy, int):
            choices = tuple((cost, policy - cost) for cost in (1, 2, 3) if cost <= policy)
        else:
            choices = ((policy[0], policy[1:]),) if policy else ()
        for locked in self.actions[state]:
            cost = self.costs[len(locked)]
            for allowed, tail in choices:
                if cost == allowed:
                    candidate = self.action(state, locked, tail)
                    if best is None or self.priority(candidate) > self.priority(best):
                        best, first = candidate, locked
        return best, first


class DynamicBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stats, cls.weights, cls.echo = small_model()
        cls.single = ReconstructionPlanner(EchoScorer(cls.stats, cls.weights))
        cls.planner = AdaptiveBudgetPlanner(cls.single)
        cls.analysis = cls.single.analyze(cls.echo)
        cls.result = cls.planner.compare(cls.echo, 3, analysis=cls.analysis)
        cls.oracle = ExactOracle(cls.stats, cls.weights)
        cls.state = tuple((index, 0) for index in range(5))

    def assert_metrics(self, metrics, expected, start):
        final, cost, attempts = expected
        self.assertAlmostEqual(metrics["expected_delta"], float((final - start) * self.oracle.scale), places=11)
        self.assertAlmostEqual(metrics["expected_final_score"], float(final * self.oracle.scale), places=11)
        self.assertAlmostEqual(metrics["expected_cost"], float(cost), places=11)
        self.assertAlmostEqual(metrics["expected_attempts"], float(attempts), places=11)

    def test_free_and_ordered_paths_match_independent_rational_enumeration(self):
        self.assertEqual([row["spending_path"] for row in self.result["rows"]],
                         [[], None, [1, 1, 1], [1, 2], [2, 1], [3]])
        for row in self.result["rows"]:
            policy = 3 if row["spending_path"] is None else tuple(row["spending_path"])
            exact, _ = self.oracle.solve(self.state, policy)
            with self.subTest(path=policy):
                self.assert_metrics(row["budget"], exact, self.oracle.score(self.state))
                self.assertLessEqual(row["budget"]["expected_cost"], 3)
                self.assertLessEqual(row["budget"]["expected_attempts"], 3)
        best = self.result["rows"][1]["budget"]["expected_delta"]
        self.assertTrue(all(best + 1e-12 >= row["budget"]["expected_delta"]
                            for row in self.result["rows"]))

    def test_forced_first_lock_propagates_the_same_dynamic_policy(self):
        start = self.oracle.score(self.state)
        for plan in self.result["first_plans"]:
            locked = tuple((self.oracle.names.index(name), 0) for name in plan["locked_ids"])
            exact = self.oracle.action(self.state, locked, 3 - plan["cost"])
            self.assert_metrics(plan["dynamic_budget"], exact, start)

    def test_one_actual_wash_can_reject_candidate_but_always_pays_material(self):
        echo = copy.deepcopy(self.echo)
        for item in echo["sub_stats"]:
            item["value"] = self.stats["sub_stats"]["tiers"][item["name"]]["values"][1]
        analysis = self.single.analyze(echo)
        before = copy.deepcopy(analysis)
        result = self.planner.compare(echo, 1, analysis=analysis)
        for plan in result["first_plans"]:
            if plan["cost"] == 1:
                self.assertAlmostEqual(plan["dynamic_budget"]["expected_delta"],
                                       plan["expected_positive_gain"], places=12)
                self.assertEqual(plan["dynamic_budget"]["expected_cost"], 1)
                self.assertEqual(plan["dynamic_budget"]["expected_attempts"], 1)
            self.assertEqual(analysis, before)
            self.assertEqual(self.single.analyze(echo), before)
            self.assertTrue(any(plan["expected_delta"] < plan["expected_positive_gain"]
                        for plan in analysis["plans"]))

    def test_no_wash_is_separate_and_never_averaged_into_executed_paths(self):
        stats, weights, echo = small_model()
        weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 0)
        result = ReconstructionPlanner(EchoScorer(stats, weights)).compare_dynamic_budget(echo, 3)
        self.assertEqual(result["rows"][0]["budget"]["expected_cost"], 0)
        for row in result["rows"][1:]:
            self.assertEqual(row["budget"]["expected_cost"], 3)
            self.assertGreaterEqual(row["budget"]["expected_attempts"], 1)
            if row["spending_path"]:
                self.assertEqual(row["budget"]["expected_attempts"], len(row["spending_path"]))

    def test_observed_result_can_change_locks_and_continue_after_an_improvement(self):
        _, locked = self.oracle.solve(self.state, 3)
        witness = None
        for observed, probability in self.oracle.outcomes(locked):
            _, next_lock = self.oracle.solve(observed, 3 - self.oracle.costs[len(locked)])
            if probability and next_lock and next_lock != locked and self.oracle.score(observed) > self.oracle.score(self.state):
                witness = (observed, next_lock)
                break
        self.assertIsNotNone(witness)
        observed, expected_lock = witness
        echo = copy.deepcopy(self.echo)
        echo["sub_stats"] = [{"name": self.oracle.names[index],
                              "value": self.stats["sub_stats"]["tiers"][self.oracle.names[index]]["values"][tier]}
                             for index, tier in observed]
        remaining = 3 - self.oracle.costs[len(locked)]
        actual = self.planner.compare(echo, remaining)["recommendation"]
        self.assertIsNotNone(actual)
        actual_locked = tuple(sorted(self.oracle.names.index(name) for name in actual["locked_ids"]))
        self.assertEqual(actual_locked, tuple(index for index, _ in expected_lock))

    def test_spending_order_is_not_collapsed(self):
        by_path = {tuple(row["spending_path"]): row for row in self.result["rows"]
                   if row["spending_path"] is not None}
        self.assertNotAlmostEqual(by_path[(1, 2)]["budget"]["expected_delta"],
                                  by_path[(2, 1)]["budget"]["expected_delta"])

    def test_zero_budget_is_noop_but_executed_paths_do_not_stop_for_high_score(self):
        result = self.planner.compare(self.echo, 0, analysis=self.analysis)
        self.assertEqual(len(result["rows"]), 1)
        self.assertIsNone(result["recommendation"])
        self.assertTrue(all(plan["dynamic_budget"] is None for plan in result["first_plans"]))
        self.assertEqual(result["rows"][0]["budget"]["expected_delta"], 0)
        # 即使全局最高分也能实际洗、拒绝所有降分候选；收益零但材料和次数照计。
        high = copy.deepcopy(self.echo)
        names = self.oracle.names[1:]
        high["sub_stats"] = [{"name": name, "value": self.stats["sub_stats"]["tiers"][name]["values"][-1]}
                             for name in names]
        result = self.planner.compare(high, 1)
        self.assertIsNotNone(result["recommendation"])
        self.assertEqual(result["rows"][1]["budget"]["expected_cost"], 1)
        self.assertEqual(result["rows"][1]["budget"]["expected_attempts"], 1)
        self.assertTrue(all(plan["dynamic_budget"]["expected_delta"] == 0 for plan in result["first_plans"]
                            if plan["dynamic_budget"] is not None))

    def test_invalid_budget_cancellation_and_completed_cache_reuse(self):
        for invalid in (-1, 1.5, True, MAX_DYNAMIC_BUDGET + 1):
            with self.assertRaises(ValueError):
                self.planner.compare(self.echo, invalid)
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(CancelledError):
            self.planner.compare(self.echo, 3, cancelled=cancelled)
        class CompleteCache(dict):
            def __setitem__(self, key, value):
                raise AssertionError("complete policy cache was rebuilt")

        with patch.object(self.planner, "_table_cache", CompleteCache(self.planner._table_cache)):
            self.planner.compare(self.echo, 2, analysis=self.analysis)

    def test_zero_weight_and_zero_probability_tiers(self):
        stats, weights, echo = small_model()
        weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 0)
        single = ReconstructionPlanner(EchoScorer(stats, weights))
        planner = AdaptiveBudgetPlanner(single)
        result = planner.compare(echo, 3)
        self.assertIsNotNone(result["recommendation"])
        self.assertTrue(all(len(values) == 1 for values in planner._scores))
        self.assertTrue(all(row["budget"]["expected_cost"] == 3 for row in result["rows"][1:]))
        stats["probability_distributions"]["test_dynamic"]["published_probability_pct"] = [0, 100]
        weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 1)
        result = ReconstructionPlanner(EchoScorer(stats, weights)).compare_dynamic_budget(echo, 2)
        self.assertGreater(result["recommendation"]["budget"]["expected_delta"], 0)

    def test_partial_cancellation_does_not_publish_incomplete_condition_tables(self):
        planner = AdaptiveBudgetPlanner(self.single)
        cancelled = Event()
        original = planner._block

        def cancel_after_block(*args):
            value = original(*args)
            cancelled.set()
            return value

        with patch.object(planner, "_block", side_effect=cancel_after_block):
            with self.assertRaises(CancelledError):
                planner.compare(self.echo, 2, analysis=self.analysis, cancelled=cancelled)
        self.assertEqual(planner._table_cache, {})
        cancelled.clear()
        result = planner.compare(self.echo, 2, analysis=self.analysis, cancelled=cancelled)
        self.assertIsNotNone(result["recommendation"])
        self.assertTrue(all(not table.flags.writeable for tables in planner._table_cache.values()
                            for table in tables.values()))

    def test_local_integer_differences_keep_tiny_gains_and_large_integer_spans(self):
        stats, weights, _ = small_model()
        values = [1, 2**63 + 102]
        stats["sub_stats"]["tiers"]["hp_flat"]["values"] = values
        single = ReconstructionPlanner(EchoScorer(stats, weights))
        planner = AdaptiveBudgetPlanner(single)
        result = planner._differences((0,), 102)
        for value, actual in zip(values, result):
            self.assertAlmostEqual(float(actual), float((value - Fraction(102)) * single.point_scale))

    def test_rejected_candidate_preserves_a_zero_probability_old_tier(self):
        stats, weights, echo = small_model()
        stats["probability_distributions"]["test_dynamic"]["published_probability_pct"] = [100, 0]
        for item in echo["sub_stats"]:
            item["value"] = stats["sub_stats"]["tiers"][item["name"]]["values"][1]
        single = ReconstructionPlanner(EchoScorer(stats, weights))
        oracle = ExactOracle(stats, weights)
        state = tuple((index, 1) for index in range(5))
        result = single.compare_dynamic_budget(echo, 3)
        for row in result["rows"][1:]:
            policy = 3 if row["spending_path"] is None else tuple(row["spending_path"])
            exact, _ = oracle.solve(state, policy)
            self.assertAlmostEqual(row["budget"]["expected_delta"],
                                   float((exact[0] - oracle.score(state)) * oracle.scale), places=11)
            self.assertEqual(row["budget"]["expected_cost"], 3)

    def test_resource_limit_raises_instead_of_sampling_or_corrupting_single_analysis(self):
        single = self.single.analyze(self.echo)
        planner = AdaptiveBudgetPlanner(self.single)
        with patch("src.echo_reconstruction_budget.MAX_DYNAMIC_STATES", 1):
            with self.assertRaises(BudgetResourceError):
                planner.compare(self.echo, 3, analysis=single)
        self.assertEqual(single, self.analysis)
        self.assertEqual(planner._table_cache, {})

    def test_upper_budget_lists_all_ordered_paths_without_exceeding_limit(self):
        result = self.planner.compare(self.echo, MAX_DYNAMIC_BUDGET, analysis=self.analysis)
        self.assertEqual(len(result["rows"]), 26)  # 24 有序拆分 + 动态择优 + 不投入。
        best = result["recommendation"]["budget"]["expected_delta"]
        for row in result["rows"]:
            if row["spending_path"]:
                self.assertEqual(sum(row["spending_path"]), MAX_DYNAMIC_BUDGET)
            self.assertLessEqual(row["budget"]["expected_cost"], MAX_DYNAMIC_BUDGET + 1e-12)
            self.assertLessEqual(row["budget"]["expected_attempts"], MAX_DYNAMIC_BUDGET + 1e-12)
            self.assertGreaterEqual(best + 1e-12, row["budget"]["expected_delta"])


if __name__ == "__main__":
    unittest.main()