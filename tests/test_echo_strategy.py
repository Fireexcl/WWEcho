import copy
import unittest

from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, EchoScorer, load_json
from src.echo_strategy import ActivationPlanner, DEFAULT_COSTS


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.stats = load_json(DEFAULT_STATS)
        self.weights = load_json(DEFAULT_WEIGHTS)
        self.weights["minor_stats"] = {cost: dict.fromkeys(pool, 0)
                          for cost, pool in self.weights["minor_stats"].items()}
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
        # 经验权重按每 1000 点计；填 1000 使测试仍按每点经验 1 计费。
        params = {"tuning_weight": 1, "experience_weight": 1000, "embryo_weight": 1, **kwargs}
        return ActivationPlanner(scorer, self.costs, target / scorer.unaligned_max_score * 50,
                                 bins=100, **params)

    def test_main_already_reaches_target(self):
        result = self.planner(20).analyze(self.echo)
        self.assertEqual(result["zero"]["expected_embryos"], 1)
        self.assertEqual(result["zero"]["expected_cost"], 1)
        self.assertFalse(result["current"]["recommend_continue"])

    def test_minor_base_score_can_reach_target_without_substats(self):
        self.weights["minor_stats"]["4"]["atk_flat"] = 1
        scorer = EchoScorer(self.stats, self.weights)
        base = scorer.stat_score("main_stats", "crit_rate", 22, 4) + scorer.stat_score("minor_stats", "atk_flat", 150, 4)
        planner = ActivationPlanner(scorer, self.costs, base - .01, bins=100)
        result = planner.analyze(self.echo)
        self.assertEqual(result["zero"]["success_probability"], 1)
        self.assertEqual(result["zero"]["expected_tuning"], 0)
        self.assertFalse(result["current"]["recommend_continue"])

    def test_unreachable_and_no_division_by_zero(self):
        result = self.planner(1000).analyze(self.echo)
        self.assertEqual(result["zero"]["success_probability"], 0)
        self.assertIsNone(result["zero"]["expected_embryos"])
        self.assertIsNone(result["zero"]["expected_cost"])
        self.assertFalse(result["current"]["recommend_continue"])

    def test_last_slot_official_probability_and_zero_stat_baseline(self):
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 6.3},
                                   {"name": "atk_pct", "value": 11.6},
                                   {"name": "hp_flat", "value": 320},
                                   {"name": "def_flat", "value": 40}]
        planner = self.planner(60)
        result = planner.analyze(self.echo)
        current = result["current"]
        expected = (6 / 99.9999) / 9
        self.assertAlmostEqual(current["full_success_lower"], expected)
        self.assertGreaterEqual(current["full_success_upper"], expected)
        self.assertAlmostEqual(current["baseline_success_lower"],
                       result["zero"]["full_success_lower"])
        self.assertEqual(current["recommend_continue"],
                 current["full_success_lower"] > 0 and
                 current["full_success_lower"] >= current["baseline_success_lower"])

    def test_zero_weight_types_still_drawn_and_cost_recovery(self):
        # 只有暴击率有效，所有档位同分；抽到即成功。
        for name in self.weights["sub_stats"]:
            self.weights["sub_stats"][name] = 0
        self.weights["sub_stats"]["crit_rate"] = 20 / 10.5
        self.stats["sub_stats"]["tiers"]["crit_rate"]["values"] = [10.5] * 8
        result = self.planner(30).analyze(self.echo)["zero"]
        # 0 词条基准为 5/13；首次抽到非暴击率后条件概率降至 4/12，策略停止。
        probability = 1 / 13
        # 达标保留 4500 经验和 10 素材；12/13 的未达标弃置返还 3000 经验和 3 素材。
        net_tuning = 10 - (1 - probability) * 3
        net_exp = 4500 - (1 - probability) * 3000
        self.assertAlmostEqual(result["success_probability"], probability)
        self.assertAlmostEqual(result["expected_embryos"], 1 / probability)
        self.assertAlmostEqual(result["expected_tuning"], net_tuning / probability)
        self.assertAlmostEqual(result["expected_experience"], net_exp / probability)
        self.assertAlmostEqual(result["expected_tuning"], 94)
        self.assertAlmostEqual(result["expected_experience"], 22500)
        self.assertAlmostEqual(result["attempt_cost"], 1 + net_tuning + net_exp)
        self.assertAlmostEqual(result["expected_cost"], result["attempt_cost"] / probability)
        weighted = self.planner(30, tuning_weight=None, experience_weight=None,
                                embryo_weight=None).analyze(self.echo)["zero"]
        self.assertAlmostEqual(weighted["expected_cost"], 13 + 94 + 22500 / 1000 * 1.6)
        self.assertAlmostEqual(weighted["expected_cost"], 143)
        self.assertEqual(weighted["success_probability"], result["success_probability"])

    def test_cost_weight_defaults_use_material_drop_midpoint(self):
        weights = self.costs["default_weights"]
        self.assertEqual(weights, {"tuning_weight": 1, "experience_weight": 1.6,
                                   "embryo_weight": 1})
        self.assertEqual(self.costs["experience_weight_unit"], 1000)
        self.assertAlmostEqual(12500 / 1000 * weights["experience_weight"],
                               20 * weights["tuning_weight"])
        planner = ActivationPlanner(EchoScorer(self.stats, self.weights), self.costs, 30, bins=100)
        self.assertEqual((planner.tw, planner.xw, planner.ew), (1, 1.6, 1))

    def test_configured_cost_defaults_and_explicit_zero_override(self):
        costs = {**self.costs, "default_weights": {
            "tuning_weight": 2, "experience_weight": 3.2, "embryo_weight": 4}}
        scorer = EchoScorer(self.stats, self.weights)
        planner = ActivationPlanner(scorer, costs, 30, bins=100)
        self.assertEqual((planner.tw, planner.xw, planner.ew), (2, 3.2, 4))
        planner = ActivationPlanner(scorer, costs, 30, tuning_weight=0,
                                    experience_weight=0, embryo_weight=0, bins=100)
        self.assertEqual((planner.tw, planner.xw, planner.ew), (0, 0, 0))

    def test_default_costs_match_user_provided_requirements(self):
        steps = self.costs["steps"]
        self.assertEqual(self.costs["status"], "user_provided")
        self.assertEqual(self.costs["recovery_ratio"], .7)
        self.assertEqual(self.costs["tuning_recovery_ratio"], .3)
        self.assertEqual(self.costs["experience_material_unit"], 500)
        self.assertEqual(self.costs["tuning_material_unit"], 1)
        self.assertEqual(self.costs["enhanced_echo_material_experience_ratio"], .7)
        self.assertEqual([step["unlock_level"] for step in steps], [5, 10, 15, 20, 25])
        self.assertEqual([step["experience"] for step in steps], [4400, 12100, 23100, 39500, 63500])
        self.assertEqual([step["tuning"] for step in steps], [10] * 5)

    def test_experience_units_preserve_overflow_between_unlocks(self):
        planner = self.planner(30)
        self.assertEqual(planner.cumulative_required_exp, [0, 4400, 16500, 39600, 79100, 142600])
        self.assertEqual(planner.cumulative_exp, [0, 4500, 16500, 40000, 79500, 143000])
        # +5 多出的 100 抵扣 +10 所需经验，第二阶段不是单独进位后的 12500。
        self.assertEqual(planner.experience, [4500, 12000, 23500, 39500, 63500])
        self.assertEqual(planner.cumulative_tuning, [0, 10, 20, 30, 40, 50])

    def test_recovery_rounds_cumulative_actual_spending_not_requirements(self):
        planner = self.planner(30)
        self.assertEqual(planner.recovered_experience, [0, 3000, 11500, 28000, 55500, 100000])
        self.assertEqual(planner.recovered_tuning, [0, 3, 6, 9, 12, 15])
        # 按 +15 实际投入 40000 回收，而不是需求 39600；也不是逐阶段各自取整。
        self.assertEqual(planner.recovered_experience[3], 28000)

    def test_recovery_rounding_uses_decimal_ratio_at_integer_boundaries(self):
        costs = copy.deepcopy(self.costs)
        costs["steps"][0]["experience"] = 50000
        planner = ActivationPlanner(EchoScorer(self.stats, self.weights), costs, 30,
                                    recovery=.58, tuning_recovery=.58, bins=100)
        # 二进制 .58 * 50 和 .58 * 50000 均可能略低于整数，不能错误向下取整。
        self.assertEqual(planner.recovered_tuning[5], 29)
        self.assertEqual(planner.recovered_experience[1], 29000)

    def test_each_abandoned_stage_refunds_existing_materials_once(self):
        failed_stats = [("hp_flat", 320), ("def_flat", 40), ("hp_pct", 6.4),
                        ("def_pct", 8.1), ("energy_regen", 6.8)]
        planner = self.planner(1000)
        for count, (experience, tuning) in enumerate(zip(
                [0, 3000, 11500, 28000, 55500, 100000], [0, 3, 6, 9, 12, 15])):
            with self.subTest(count=count):
                echo = {**self.echo, "sub_stats": [
                    {"name": name, "value": value} for name, value in failed_stats[:count]]}
                current = planner.analyze(echo)["current"]
                self.assertEqual(current["success_probability"], 0)
                self.assertFalse(current["recommend_continue"])
                self.assertAlmostEqual(current["future_cost"], -experience - tuning)

    def test_kept_success_does_not_refund_existing_materials(self):
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 10.5}]
        current = self.planner(30).analyze(self.echo)["current"]
        self.assertEqual(current["success_probability"], 1)
        self.assertFalse(current["recommend_continue"])
        self.assertEqual(current["future_cost"], 0)

    def test_existing_echo_continues_multiple_stages_with_one_terminal_refund(self):
        # 只有暴击率、暴伤有效且均为固定满档，达标要求两种都出；已有暴击率。
        for name in self.weights["sub_stats"]:
            self.weights["sub_stats"][name] = 0
        for name in ("crit_rate", "crit_dmg"):
            values = self.stats["sub_stats"]["tiers"][name]["values"]
            maximum = max(values)
            self.weights["sub_stats"][name] = 20 / maximum
            self.stats["sub_stats"]["tiers"][name]["values"] = [maximum] * len(values)
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 10.5}]
        current = self.planner(59, tuning_weight=2, experience_weight=3,
                               embryo_weight=9).analyze(self.echo)["current"]
        self.assertAlmostEqual(current["baseline_success_lower"], (5 / 13) * (4 / 12))
        self.assertTrue(current["recommend_continue"])
        # 依次开第 2/3/4 槽，前面没抽到暴伤才继续；第 4 槽仍失败后 1/9 低于门槛。
        # 三个达标位置各占 1/12，剩余 9/12 在 +20 弃置，返还累计 12 素材、55500 经验。
        self.assertAlmostEqual(current["success_probability"], 3 / 12)
        net_tuning = 10 + (11 / 12) * 10 + (10 / 12) * 10 - (9 / 12) * 12
        net_exp = 12000 + (11 / 12) * 23500 + (10 / 12) * 39500 - (9 / 12) * 55500
        self.assertAlmostEqual(current["future_cost"], 2 * net_tuning + 3 * net_exp / 1000)
        self.assertAlmostEqual(current["future_cost"], 111.5)

    def test_recovery_rates_are_independent_and_explicit_zero_is_valid(self):
        for experience_ratio, tuning_ratio, experience, tuning in [
                (0, .3, 0, 9), (1, 0, 40000, 0), (.7, 1, 28000, 30)]:
            with self.subTest(experience_ratio=experience_ratio, tuning_ratio=tuning_ratio):
                planner = self.planner(30, recovery=experience_ratio, tuning_recovery=tuning_ratio)
                self.assertEqual(planner.recovered_experience[3], experience)
                self.assertEqual(planner.recovered_tuning[3], tuning)

    def test_per_thousand_weight_scales_future_cost_without_scaling_exp_output(self):
        self.echo["sub_stats"] = [{"name": "hp_flat", "value": 320}]
        current = self.planner(1000, experience_weight=None).analyze(self.echo)["current"]
        self.assertAlmostEqual(current["future_cost"], -3000 / 1000 * 1.6 - 3)
        self.assertAlmostEqual(current["future_cost"], -7.8)

    def test_legacy_per_point_weight_is_converted_without_changing_cost(self):
        costs = copy.deepcopy(self.costs)
        del costs["experience_weight_unit"]
        costs["default_weights"]["experience_weight"] = .0016
        scorer = EchoScorer(self.stats, self.weights)
        legacy = ActivationPlanner(scorer, costs, 30, bins=100)
        current = ActivationPlanner(scorer, self.costs, 30, bins=100)
        self.assertEqual(legacy.xw, 1.6)
        self.assertEqual(legacy.analyze(self.echo), current.analyze(self.echo))

    def test_legacy_cost_config_can_use_continuous_material_quantities(self):
        costs = {"schema_version": 1, "embryo_cost": 1, "steps": [
            {"slot": slot, "tuning": 1, "experience": 1} for slot in range(1, 6)]}
        planner = ActivationPlanner(EchoScorer(self.stats, self.weights), costs, 30,
                                    recovery=.7, bins=100)
        self.assertEqual(planner.cumulative_exp, [0, 1, 2, 3, 4, 5])
        self.assertAlmostEqual(planner.recovered_experience[1], .7)
        self.assertAlmostEqual(planner.recovered_tuning[1], .3)
        # 缺单位的旧配置按每点经验 1 兼容，折算为每 1000 经验权重 1000。
        self.assertEqual((planner.tw, planner.xw, planner.ew), (1, 1000, 1))

    def test_recovery_and_cost_weights_do_not_change_recommendation(self):
        a = self.planner(40, recovery=0, tuning_recovery=0).analyze(self.echo)
        b = self.planner(40, recovery=1, tuning_recovery=1, tuning_weight=2).analyze(self.echo)
        self.assertEqual(a["zero"]["success_probability"], b["zero"]["success_probability"])
        self.assertEqual(a["current"]["recommend_continue"], b["current"]["recommend_continue"])

    def test_sub_one_point_target_gap(self):
        result = self.planner(20.1).analyze(self.echo)
        self.assertGreater(result["zero"]["full_success_lower"], .9)

    def test_bounds_and_policy_success(self):
        result = self.planner(60).analyze(self.echo)["zero"]
        self.assertLessEqual(result["full_success_lower"], result["full_success_upper"] + 1e-12)
        self.assertLessEqual(result["success_probability"], result["full_success_lower"] + 1e-12)

    def test_zero_stat_baseline_and_sunk_experience_recovery(self):
        self.echo["sub_stats"] = [{"name": "hp_flat", "value": 320},
                                   {"name": "def_flat", "value": 40},
                                   {"name": "hp_pct", "value": 6.4},
                                   {"name": "def_pct", "value": 8.1}]
        result = self.planner(30).analyze(self.echo)
        base = result["current"]
        self.assertLessEqual(result["zero"]["success_probability"], result["zero"]["full_success_lower"] + 1e-12)
        self.assertEqual(base["recommend_continue"],
                         base["full_success_lower"] > 0 and
                         base["full_success_lower"] >= base["baseline_success_lower"])
        stopped = self.planner(30).analyze({**self.echo, "sub_stats": [
            {"name": "hp_flat", "value": 320}, {"name": "def_flat", "value": 40},
            {"name": "hp_pct", "value": 6.4}, {"name": "def_pct", "value": 8.1},
        ]})["current"]
        self.assertEqual(stopped["success_probability"], 0)
        self.assertAlmostEqual(stopped["future_cost"], -55500 - 12)

    def test_final_slot_bounds_contain_exact_enumeration(self):
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 6.3},
                                   {"name": "atk_pct", "value": 7.9},
                                   {"name": "energy_regen", "value": 8.4},
                                   {"name": "hp_flat", "value": 320}]
        planner = self.planner(60)
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
        for options in ({"recovery": -1}, {"recovery": 1.01}, {"tuning_recovery": -1},
                {"tuning_recovery": 1.01}, {"tuning_recovery": True}, {"tuning_weight": True},
                        {"experience_weight": float("inf")}):
            with self.assertRaises(ValueError):
                self.planner(80, **options)
        costs = copy.deepcopy(self.costs)
        costs["steps"][0]["tuning"] = -1
        with self.assertRaises(ValueError):
            ActivationPlanner(EchoScorer(self.stats, self.weights), costs, 80)
        for name, value in [("experience_material_unit", -500), ("tuning_material_unit", True),
                            ("recovery_ratio", 1.1), ("tuning_recovery_ratio", -1),
                            ("tuning_recovery_ratio", 1.1), ("tuning_recovery_ratio", True),
                            ("experience_weight_unit", 0), ("experience_weight_unit", -1000),
                            ("experience_weight_unit", True), ("experience_weight_unit", "1000")]:
            costs = copy.deepcopy(self.costs)
            costs[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                ActivationPlanner(EchoScorer(self.stats, self.weights), costs, 80)
        for value in (None, [], {"experience_weight": -1}, {"experience_weight": True},
                      {"tuning_weight": float("inf")}, {"embryo_weight": "1"}):
            costs = {**self.costs, "default_weights": value}
            with self.subTest(default_weights=value), self.assertRaises(ValueError):
                ActivationPlanner(EchoScorer(self.stats, self.weights), costs, 80)


if __name__ == "__main__":
    unittest.main()