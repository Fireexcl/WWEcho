"""频整器概率、严格升分边界与相同预算的边际收益。"""

import copy
import math
import unittest
from concurrent.futures import CancelledError
from fractions import Fraction
from itertools import combinations, product
from threading import Event

from src.echo_reconstruction import ReconstructionPlanner, compare_budget, evaluate_budget
from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, EchoScorer, load_json


class ReconstructionTests(unittest.TestCase):
    @staticmethod
    def lock_cost(locked_count):
        return {1: 1, 2: 1, 3: 2, 4: 3}[locked_count]

    def setUp(self):
        self.stats = load_json(DEFAULT_STATS)
        self.weights = load_json(DEFAULT_WEIGHTS)
        for pool in self.weights["main_stats"].values():
            for name in pool:
                pool[name] = 0
        for pool in self.weights["minor_stats"].values():
            for name in pool:
                pool[name] = 0
        self.weights["sub_stats"] = dict.fromkeys(self.weights["sub_stats"], 0)
        self.echo = {"cost": 4, "main_stat": {"name": "crit_rate", "value": 22},
                     "sub_stats": [{"name": name, "value": self.stats["sub_stats"]["tiers"][name]["values"][0]}
                                   for name in ["crit_rate", "crit_dmg", "hp_flat", "atk_flat", "def_flat"]]}

    def planner(self):
        return ReconstructionPlanner(EchoScorer(self.stats, self.weights))

    @staticmethod
    def plan(result, locked):
        return next(plan for plan in result["plans"] if set(plan["locked_ids"]) == set(locked))

    def small_model(self):
        names = ["hp_flat", "atk_flat", "def_flat", "hp_pct", "atk_pct", "def_pct"]
        self.stats["sub_stats"]["tiers"] = {
            name: {"values": [index + .1, index + .5], "probability_distribution": "test"}
            for index, name in enumerate(names, 1)}
        self.stats["sub_stats"]["pool_size"] = len(names)
        self.stats["probability_distributions"]["test"] = {"published_probability_pct": [25, 75]}
        for case in self.stats["reconstruction"]["cases"]:
            count = case["locked_count"]
            case["candidate_count"] = len(names) - count
            case["type_probability"]["denominator"] = len(names) - count
        self.weights["sub_stats"] = {name: (index + 1) * .25 for index, name in enumerate(names)}
        self.echo["sub_stats"] = [{"name": name, "value": self.stats["sub_stats"]["tiers"][name]["values"][0]}
                                  for name in names[:5]]

    def brute(self, locked):
        """独立有理数穷举全部档位，不调用实现的整数贡献或折半分布。"""
        names = self.stats["sub_stats"]["tiers"]
        pool = [name for name in names if name not in locked]
        count = 5 - len(locked)
        threshold = sum(Fraction(str(item["value"])) * Fraction(str(self.weights["sub_stats"][item["name"]]))
                        for item in self.echo["sub_stats"] if item["name"] not in locked)
        p, gain, delta, mass = Fraction(0), Fraction(0), Fraction(0), Fraction(0)
        for types in combinations(pool, count):
            outcomes = []
            for name in types:
                tier = names[name]
                published = self.stats["probability_distributions"][tier["probability_distribution"]]["published_probability_pct"]
                total = sum(Fraction(str(value)) for value in published)
                outcomes.append([(Fraction(str(value)) * Fraction(str(self.weights["sub_stats"][name])),
                                  Fraction(str(probability)) / total)
                                 for value, probability in zip(tier["values"], published)])
            for values in product(*outcomes):
                probability = math.prod(item[1] for item in values) / math.comb(len(pool), count)
                change = sum(item[0] for item in values) - threshold
                mass += probability
                delta += probability * change
                if change > 0:
                    p += probability
                    gain += probability * change
        scale = Fraction(50) / Fraction(str(EchoScorer(self.stats, self.weights).unaligned_max_score))
        self.assertEqual(mass, 1)
        return float(p), float(gain * scale), float(delta * scale)

    def test_all_thirty_lock_sets_match_independent_exact_enumeration(self):
        self.small_model()
        planner = self.planner()
        result = planner.analyze(self.echo)
        self.assertEqual(len(result["plans"]), 30)
        for count, expected in [(1, 5), (2, 10), (3, 10), (4, 5)]:
            self.assertEqual(sum(plan["locked_count"] == count for plan in result["plans"]), expected)
        for plan in result["plans"]:
            with self.subTest(locked=plan["locked_ids"]):
                p, gain, delta = self.brute(plan["locked_ids"])
                self.assertAlmostEqual(plan["probability"], p, places=13)
                self.assertAlmostEqual(plan["expected_positive_gain"], gain, places=12)
                self.assertAlmostEqual(plan["expected_delta"], delta, places=12)
                self.assertEqual(plan["cost"], self.lock_cost(plan["locked_count"]))
                self.assertEqual(plan["rerolled_count"] + plan["locked_count"], 5)
                self.assertAlmostEqual(plan["positive_gain_per_transducer"], gain / plan["cost"])
                if p:
                    self.assertAlmostEqual(plan["gain_on_success"], gain / p)
                    self.assertAlmostEqual(plan["expected_cost_to_improve"], plan["cost"] / p)
        self.assertLessEqual(max(len(key) for key in planner._halves), 2)

    def test_lock_four_reuses_original_unlocked_type_and_official_tier_probabilities(self):
        self.weights["sub_stats"]["crit_rate"] = 2
        result = self.planner().analyze(self.echo)
        plan = self.plan(result, ["crit_dmg", "hp_flat", "atk_flat", "def_flat"])
        # 原来未锁的暴击率可再次出现；只有更高的七档严格升分。
        expected = (99.9999 - 23.3333) / 99.9999 / 9
        self.assertAlmostEqual(plan["probability"], expected)
        self.assertGreater(plan["gain_on_success"], 0)
        self.assertLess(plan["expected_delta"], 0)

    def test_zero_weights_and_no_possible_improvement_have_no_fake_gain(self):
        for plan in self.planner().analyze(self.echo)["plans"]:
            self.assertEqual(plan["probability"], 0)
            self.assertEqual(plan["expected_delta"], 0)
            self.assertEqual(plan["expected_positive_gain"], 0)
            self.assertIsNone(plan["gain_on_success"])
            self.assertIsNone(plan["expected_cost_to_improve"])
        self.weights["sub_stats"]["crit_rate"] = 2
        self.echo["sub_stats"][0]["value"] = 10.5
        result = self.planner().analyze(self.echo)
        self.assertTrue(all(plan["probability"] == 0 for plan in result["plans"]))
        self.assertIsNone(compare_budget(result["plans"], 4)["recommendation"])

    def test_decimal_tie_is_not_strict_improvement(self):
        self.small_model()
        for name, tier in self.stats["sub_stats"]["tiers"].items():
            tier["values"] = [.1, .1] if name == "hp_flat" else [.3, .3]
            self.weights["sub_stats"][name] = 3 if name == "hp_flat" else 1
        for item in self.echo["sub_stats"]:
            item["value"] = .1 if item["name"] == "hp_flat" else .3
        self.assertTrue(all(plan["probability"] == 0 for plan in self.planner().analyze(self.echo)["plans"]))

    def test_changes_smaller_than_display_rounding_still_count(self):
        self.small_model()
        self.weights["main_stats"]["4"]["crit_rate"] = 1e6
        self.weights["sub_stats"] = dict.fromkeys(self.weights["sub_stats"], 0)
        self.weights["sub_stats"]["hp_flat"] = 1e-12
        result = self.planner().analyze(self.echo)
        plan = self.plan(result, ["atk_flat", "def_flat", "hp_pct", "atk_pct"])
        self.assertAlmostEqual(plan["probability"], .75 / 2)
        self.assertGreater(plan["expected_positive_gain"], 0)
        self.assertLess(plan["expected_positive_gain"], 1e-6)

    def test_tiny_positive_difference_survives_large_total_cancellation(self):
        self.weights["sub_stats"]["resonance_liberation_dmg_bonus"] = 1e6
        self.weights["sub_stats"]["atk_flat"] = 1e-12
        self.echo["sub_stats"] = [
            {"name": "resonance_liberation_dmg_bonus", "value": 11.6},
            {"name": "hp_flat", "value": 320}, {"name": "def_flat", "value": 40},
            {"name": "hp_pct", "value": 6.4}, {"name": "def_pct", "value": 8.1}]
        planner = self.planner()
        plan = self.plan(planner.analyze(self.echo), ["hp_flat", "def_flat", "hp_pct"])
        tier = self.stats["sub_stats"]["tiers"]["resonance_liberation_dmg_bonus"]
        published = self.stats["probability_distributions"][tier["probability_distribution"]]["published_probability_pct"]
        probability = Fraction(str(published[-1])) / sum(Fraction(str(p)) for p in published) / 45
        atk_tier = self.stats["sub_stats"]["tiers"]["atk_flat"]
        atk_published = self.stats["probability_distributions"][atk_tier["probability_distribution"]]["published_probability_pct"]
        mean_atk = sum(Fraction(str(value)) * Fraction(str(p)) for value, p in zip(atk_tier["values"], atk_published)) / sum(Fraction(str(p)) for p in atk_published)
        expected = float(probability * mean_atk * Fraction("1e-12") / Fraction(str(planner.scorer.unaligned_max_score)) * 50)
        self.assertAlmostEqual(plan["probability"], float(probability))
        self.assertGreater(plan["expected_positive_gain"], 0)
        self.assertAlmostEqual(plan["expected_positive_gain"] / expected, 1)
        self.assertGreater(plan["gain_on_success"], 0)

    def test_zero_probability_tiers_and_rounded_probability_normalization(self):
        self.small_model()
        self.stats["probability_distributions"]["test"]["published_probability_pct"] = [0, 99.9999]
        for plan in self.planner().analyze(self.echo)["plans"]:
            p, gain, delta = self.brute(plan["locked_ids"])
            self.assertAlmostEqual(plan["probability"], p)
            self.assertAlmostEqual(plan["expected_positive_gain"], gain)
            self.assertAlmostEqual(plan["expected_delta"], delta)

    def test_main_minor_scores_cancel_and_weights_are_scale_invariant(self):
        self.weights["sub_stats"]["crit_rate"] = 1
        before = self.planner().analyze(self.echo)
        self.weights["main_stats"]["4"]["crit_rate"] = 4
        self.weights["minor_stats"]["4"]["atk_flat"] = 2
        after = self.planner().analyze(self.echo)
        self.assertNotEqual(before["current_score"], after["current_score"])
        self.assertEqual([p["probability"] for p in before["plans"]], [p["probability"] for p in after["plans"]])
        for pool in [self.weights["sub_stats"], *self.weights["main_stats"].values(), *self.weights["minor_stats"].values()]:
            for name in pool:
                pool[name] *= 10
        scaled = self.planner().analyze(self.echo)
        for first, second in zip(after["plans"], scaled["plans"]):
            self.assertEqual(first["probability"], second["probability"])
            self.assertAlmostEqual(first["expected_delta"], second["expected_delta"])

    def test_inputs_probabilities_rules_and_cancellation(self):
        planner = self.planner()
        for entries in ([], self.echo["sub_stats"][:4]):
            with self.assertRaises(ValueError):
                planner.analyze({**self.echo, "sub_stats": entries})
        duplicate = copy.deepcopy(self.echo)
        duplicate["sub_stats"][-1] = duplicate["sub_stats"][0]
        with self.assertRaises(ValueError):
            planner.analyze(duplicate)
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(CancelledError):
            planner.analyze(self.echo, cancelled=cancelled)
        for probabilities in ([1], [-1] * 8, [0] * 8, [True] * 8, [float("nan")] * 8):
            stats = copy.deepcopy(self.stats)
            stats["probability_distributions"]["crit_eight_tiers"]["published_probability_pct"] = probabilities
            with self.assertRaises(ValueError):
                ReconstructionPlanner(EchoScorer(stats, self.weights))
        self.stats["reconstruction"]["cases"][0]["candidate_count"] = 13
        with self.assertRaises(ValueError):
            self.planner()


class BudgetTests(unittest.TestCase):
    @staticmethod
    def plan(cost=1, p=.5, gain=1, delta=.5):
        return {"cost": cost, "probability": p, "expected_positive_gain": gain,
                "expected_delta": delta, "locked_count": cost}

    def test_two_one_lock_attempts_differ_from_one_two_lock_attempt(self):
        # 单次 +2（50%）或 -1（50%），不能免费保留失败前的原装备。
        twice = evaluate_budget(self.plan(), 2)
        once = evaluate_budget(self.plan(cost=2), 2)
        self.assertEqual(twice["success_probability"], .75)
        self.assertEqual(once["success_probability"], .5)
        self.assertEqual(twice["expected_delta"], 1.25)
        self.assertEqual(once["expected_delta"], .5)
        self.assertEqual(twice["expected_positive_gain"], 1.5)
        self.assertEqual(twice["expected_cost"], 1.5)
        self.assertEqual(once["expected_cost"], 2)
        self.assertEqual(twice["next_marginal_delta"], .375)
        self.assertEqual(twice["next_marginal_probability"], .125)
        self.assertEqual(twice["next_marginal_cost"], .25)
        self.assertNotEqual(twice["expected_delta"], 2 * once["expected_delta"])

    def test_marginal_probability_and_gain_diminish_after_initial_attempt(self):
        values = [evaluate_budget(self.plan(), budget) for budget in range(1, 5)]
        for earlier, later in zip(values, values[1:]):
            self.assertAlmostEqual(later["next_marginal_probability"], earlier["next_marginal_probability"] / 2)
            self.assertAlmostEqual(later["next_marginal_delta"], earlier["next_marginal_delta"] / 2)

    def test_budget_matches_independent_stopped_sequence_enumeration(self):
        outcomes = [-3, 0, 1, 4]
        plan = self.plan(p=.5, gain=1.25, delta=.5)
        for attempts in range(1, 5):
            probability = Fraction(1, len(outcomes) ** attempts)
            p, delta, cost, gain = (Fraction(0) for _ in range(4))
            for sequence in product(outcomes, repeat=attempts):
                used = next((index + 1 for index, value in enumerate(sequence) if value > 0), attempts)
                change = sequence[used - 1]
                p += probability * (change > 0)
                delta += probability * change
                cost += probability * used
                gain += probability * max(0, change)
            result = evaluate_budget(plan, attempts)
            self.assertAlmostEqual(result["success_probability"], float(p))
            self.assertAlmostEqual(result["expected_delta"], float(delta))
            self.assertAlmostEqual(result["expected_cost"], float(cost))
            self.assertAlmostEqual(result["expected_positive_gain"], float(gain))

    def test_fixed_locks_do_not_accumulate_scores_or_preserve_historical_best(self):
        # 总失败时最终仍为最后一次 -2，不是零损失，也不是 -2×次数。
        plan = self.plan(p=0, gain=0, delta=-2)
        value = evaluate_budget(plan, 3)
        self.assertEqual(value["expected_delta"], -2)
        self.assertEqual(value["expected_cost"], 3)
        self.assertEqual(value["next_marginal_delta"], 0)
        self.assertEqual(value["next_marginal_cost"], 1)
        value = evaluate_budget(self.plan(p=1, gain=2, delta=2), 4)
        self.assertEqual(value["expected_delta"], 2)
        self.assertEqual(value["expected_cost"], 1)
        self.assertEqual(value["next_marginal_delta"], 0)

    def test_budget_unaffordable_remainder_and_validation(self):
        value = evaluate_budget(self.plan(cost=3), 2)
        self.assertEqual(value["max_attempts"], 0)
        self.assertEqual(value["max_cost"], 0)
        self.assertEqual(value["expected_cost"], 0)
        self.assertEqual(value["expected_delta"], 0)
        self.assertIsNone(value["net_gain_per_transducer"])
        self.assertEqual(evaluate_budget(self.plan(cost=3), 8)["max_cost"], 6)
        for invalid in (-1, 1.5, True):
            with self.assertRaises(ValueError):
                evaluate_budget(self.plan(), invalid)
        for invalid in (0, -1, 1.5, True):
            with self.assertRaises(ValueError):
                evaluate_budget(self.plan(cost=invalid), 1)

    def test_tiny_probability_uses_stable_complement(self):
        value = evaluate_budget(self.plan(p=1e-20, gain=2e-20, delta=-1), 4)
        self.assertGreater(value["success_probability"], 0)
        self.assertAlmostEqual(value["success_probability"] / 1e-20, 4)
        self.assertEqual(value["expected_cost"], 4)
        self.assertAlmostEqual(value["next_marginal_delta"] / 1e-20, 3)
        self.assertAlmostEqual(value["next_marginal_probability"] / 1e-20, 1)
        saturated = evaluate_budget(self.plan(), 100)
        self.assertGreater(saturated["next_marginal_probability"], 0)
        self.assertGreater(saturated["next_marginal_cost"], 0)
        self.assertGreater(saturated["next_marginal_delta"], 0)

    def test_recommendation_optimizes_budget_net_gain_and_can_stop(self):
        plans = [self.plan(), self.plan(cost=2, p=.8, gain=.5, delta=.4),
                 self.plan(cost=3), self.plan(cost=4)]
        result = compare_budget(plans, 2)
        self.assertEqual(result["recommendation"]["locked_count"], 1)
        self.assertEqual(len(result["best_by_count"]), 4)
        self.assertEqual(result["best_by_count"][2]["budget"]["max_attempts"], 0)
        self.assertIsNone(compare_budget([self.plan(delta=-2)], 1)["recommendation"])


if __name__ == "__main__":
    unittest.main()