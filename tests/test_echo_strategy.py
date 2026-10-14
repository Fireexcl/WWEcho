import copy
import unittest

from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, EchoScorer, load_json
from src.echo_strategy import ActivationPlanner, DEFAULT_COSTS


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.stats = load_json(DEFAULT_STATS)
        self.weights = load_json(DEFAULT_WEIGHTS)
        # 独立于用户默认预设，构造原始满档贡献 20 + 70 的固定测试场景。
        self.weights["sub_stats"] = dict.fromkeys(self.weights["sub_stats"], 0)
        for cost, pool in self.weights["main_stats"].items():
            self.weights["main_stats"][cost] = dict.fromkeys(pool, 0)
        self.weights["main_stats"]["4"]["crit_rate"] = 20 / 22
        for name, contribution in [("crit_rate", 20), ("crit_dmg", 20), ("atk_pct", 10),
                                   ("energy_regen", 10), ("basic_atk_dmg_bonus", 10), ("atk_flat", 5)]:
            self.weights["sub_stats"][name] = contribution / max(self.stats["sub_stats"]["tiers"][name]["values"])
        self.costs = load_json(DEFAULT_COSTS)
        self.echo = {"cost": 4, "main_stat": {"name": "crit_rate", "value": 22}, "sub_stats": []}

    def planner(self, target, **kwargs):
        scorer = EchoScorer(self.stats, self.weights)
        return ActivationPlanner(scorer, self.costs, target / scorer.unaligned_max_score * 50,
                                 bins=100, **kwargs)

    def test_main_already_reaches_target(self):
        result = self.planner(20).analyze(self.echo)
        self.assertEqual(result["zero"]["expected_embryos"], 1)
        self.assertEqual(result["zero"]["expected_cost"], 1)
        self.assertFalse(result["current"]["recommend_continue"])

    def test_unreachable_and_no_division_by_zero(self):
        result = self.planner(1000).analyze(self.echo)
        self.assertEqual(result["zero"]["success_probability"], 0)
        self.assertIsNone(result["zero"]["expected_embryos"])
        self.assertIsNone(result["zero"]["expected_cost"])
        self.assertFalse(result["current"]["recommend_continue"])

    def test_last_slot_official_probability_and_threshold(self):
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 6.3},
                                   {"name": "atk_pct", "value": 11.6},
                                   {"name": "hp_flat", "value": 320},
                                   {"name": "def_flat", "value": 40}]
        planner = self.planner(60, threshold=0)
        current = planner.analyze(self.echo)["current"]
        expected = (6 / 99.9999) / 9
        self.assertAlmostEqual(current["full_success_lower"], expected)
        self.assertGreaterEqual(current["full_success_upper"], expected)
        self.assertAlmostEqual(current["success_probability"], expected)
        self.assertTrue(current["recommend_continue"])
        self.assertFalse(self.planner(60).analyze(self.echo)["current"]["recommend_continue"])

    def test_zero_weight_types_still_drawn_and_cost_recovery(self):
        # 只有暴击率有效，所有档位同分；抽到即成功。
        for name in self.weights["sub_stats"]:
            self.weights["sub_stats"][name] = 0
        self.weights["sub_stats"]["crit_rate"] = 20 / 10.5
        self.stats["sub_stats"]["tiers"]["crit_rate"]["values"] = [10.5] * 8
        result = self.planner(30, threshold=0, recovery=.66).analyze(self.echo)["zero"]
        probability = 5 / 13
        draws = sum((13 - n) / 13 for n in range(5))
        failures_exp = 5 * (8 / 13)
        net_exp = draws - .66 * failures_exp
        self.assertAlmostEqual(result["success_probability"], probability)
        self.assertAlmostEqual(result["expected_embryos"], 1 / probability)
        self.assertAlmostEqual(result["expected_tuning"], draws / probability)
        self.assertAlmostEqual(result["expected_experience"], net_exp / probability)
        self.assertAlmostEqual(result["attempt_cost"], 1 + draws + net_exp)
        self.assertAlmostEqual(result["expected_cost"], result["attempt_cost"] / probability)

    def test_recovery_and_cost_weights_do_not_change_recommendation(self):
        a = self.planner(40, threshold=0, recovery=0).analyze(self.echo)
        b = self.planner(40, threshold=0, recovery=1, tuning_weight=2).analyze(self.echo)
        self.assertEqual(a["zero"]["success_probability"], b["zero"]["success_probability"])
        self.assertEqual(a["current"]["recommend_continue"], b["current"]["recommend_continue"])

    def test_sub_one_point_target_gap(self):
        result = self.planner(20.1, threshold=0).analyze(self.echo)
        self.assertGreater(result["zero"]["success_probability"], .9)

    def test_bounds_and_policy_success(self):
        result = self.planner(60).analyze(self.echo)["zero"]
        self.assertLessEqual(result["full_success_lower"], result["full_success_upper"] + 1e-12)
        self.assertLessEqual(result["success_probability"], result["full_success_lower"] + 1e-12)

    def test_threshold_equality_and_sunk_experience_recovery(self):
        self.echo["sub_stats"] = [{"name": "hp_flat", "value": 320},
                                   {"name": "def_flat", "value": 40},
                                   {"name": "hp_pct", "value": 6.4},
                                   {"name": "def_pct", "value": 8.1}]
        base = self.planner(30, threshold=0).analyze(self.echo)["current"]
        chance = base["full_success_lower"]
        self.assertTrue(self.planner(30, threshold=chance).analyze(self.echo)["current"]["recommend_continue"])
        stopped = self.planner(30, threshold=1).analyze(self.echo)["current"]
        self.assertEqual(stopped["success_probability"], 0)
        self.assertAlmostEqual(stopped["future_cost"], -.66 * 4)

    def test_final_slot_bounds_contain_exact_enumeration(self):
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 6.3},
                                   {"name": "atk_pct", "value": 7.9},
                                   {"name": "energy_regen", "value": 8.4},
                                   {"name": "hp_flat", "value": 320}]
        planner = self.planner(60, threshold=0)
        known = {item["name"] for item in self.echo["sub_stats"]}
        raw = planner.scorer.stat_score("main_stats", "crit_rate", 22) + sum(
                   planner.scorer.stat_score("sub_stats", item["name"], item["value"])
                       for item in self.echo["sub_stats"])
        exact = sum(p / 9 for name, outcomes in zip(planner.ids, planner.outcomes)
                    if name not in known for gain, p in outcomes if raw + gain >= planner.target)

        for name, outcomes in zip(planner.ids, planner.outcomes):
            for value, (gain, _) in zip(planner.scorer.tiers[name]["values"], outcomes):
                self.assertEqual(gain, planner.scorer.stat_score("sub_stats", name, value))
        result = planner.analyze(self.echo)["current"]
        self.assertLessEqual(result["full_success_lower"], exact + 1e-12)
        self.assertGreaterEqual(result["full_success_upper"], exact - 1e-12)

    def test_validation(self):
        for options in ({"threshold": 1.1}, {"recovery": -1}, {"tuning_weight": True},
                        {"experience_weight": float("inf")}):
            with self.assertRaises(ValueError):
                self.planner(80, **options)
        costs = copy.deepcopy(self.costs)
        costs["steps"][0]["tuning"] = -1
        with self.assertRaises(ValueError):
            ActivationPlanner(EchoScorer(self.stats, self.weights), costs, 80)


if __name__ == "__main__":
    unittest.main()