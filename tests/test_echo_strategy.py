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
        self.assertEqual(result["zero"]["expected_tuning"], 50)
        self.assertEqual(result["zero"]["expected_experience"], 143000)
        self.assertEqual(result["zero"]["attempt_cost"], 143051)
        self.assertEqual(result["zero"]["expected_cost"], 143051)
        self.assertTrue(result["current"]["recommend_continue"])
        self.assertEqual(result["current"]["future_cost"], 143050)
        self.assertIn("开满剩余 5 条", result["current"]["reason"])

    def test_base_reached_overflow_does_not_claim_a_finite_optimum(self):
        scorer = EchoScorer(self.stats, self.weights)
        for params in ({"tuning_weight": 1e308}, {"experience_weight": 1e308}):
            with self.subTest(params=params), self.assertRaisesRegex(ValueError, "有限数值"):
                ActivationPlanner(scorer, self.costs, 0, **params).analyze(self.echo)

    def test_minor_base_score_can_reach_target_without_substats(self):
        self.weights["minor_stats"]["4"]["atk_flat"] = 1
        scorer = EchoScorer(self.stats, self.weights)
        base = scorer.stat_score("main_stats", "crit_rate", 22, 4) + scorer.stat_score("minor_stats", "atk_flat", 150, 4)
        planner = ActivationPlanner(scorer, self.costs, base - .01, bins=100)
        result = planner.analyze(self.echo)
        self.assertEqual(result["zero"]["success_probability"], 1)
        self.assertEqual(result["zero"]["expected_tuning"], 50)
        self.assertEqual(result["zero"]["expected_experience"], 143000)
        self.assertAlmostEqual(result["zero"]["expected_cost"], 279.8)
        self.assertTrue(result["current"]["recommend_continue"])
        self.assertAlmostEqual(result["current"]["future_cost"], 278.8)

    def test_base_target_reached_at_every_count_still_finishes_all_slots(self):
        stat_ids = list(self.stats["sub_stats"]["tiers"])
        for count in range(6):
            echo = {**self.echo, "sub_stats": [
                {"name": name, "value": self.stats["sub_stats"]["tiers"][name]["values"][0]}
                for name in stat_ids[:count]]}
            planner = self.planner(20, experience_weight=None)
            result = planner.analyze(echo)
            current = result["current"]
            with self.subTest(count=count):
                self.assertEqual(current["success_probability"], 1)
                self.assertEqual(current["recommend_continue"], count < 5)
                self.assertAlmostEqual(current["future_cost"],
                                       (50 - count * 10) + (143000 - planner.cumulative_exp[count]) / 1000 * 1.6)
                self.assertAlmostEqual(result["zero"]["expected_cost"], 279.8)

    def test_zero_weights_and_zero_target_still_require_full_investment(self):
        for pool in self.weights["main_stats"].values():
            for name in pool:
                pool[name] = 0
        self.weights["sub_stats"] = dict.fromkeys(self.weights["sub_stats"], 0)
        scorer = EchoScorer(self.stats, self.weights)
        result = ActivationPlanner(scorer, self.costs, 0, bins=100).analyze(self.echo)
        self.assertEqual(result["zero"]["success_probability"], 1)
        self.assertAlmostEqual(result["zero"]["expected_cost"], 279.8)
        self.assertTrue(result["current"]["recommend_continue"])
        self.assertAlmostEqual(result["current"]["future_cost"], 278.8)

    def test_unreachable_and_no_division_by_zero(self):
        result = self.planner(1000).analyze(self.echo)
        self.assertEqual(result["zero"]["success_probability"], 0)
        self.assertIsNone(result["zero"]["expected_embryos"])
        self.assertIsNone(result["zero"]["expected_cost"])
        self.assertFalse(result["current"]["recommend_continue"])

    def test_zero_lower_positive_upper_does_not_claim_unreachability_or_optimality(self):
        scorer = EchoScorer(self.stats, self.weights)
        planner = ActivationPlanner(scorer, self.costs, 50)
        self.assertEqual(planner.bins, 1000)
        result = planner.analyze(self.echo)
        zero, current = result["zero"], result["current"]
        self.assertEqual(zero["full_success_lower"], 0)
        self.assertGreater(zero["full_success_upper"], 0)
        self.assertEqual(zero["success_probability"], 0)
        self.assertIsNone(zero["expected_cost"])
        self.assertIsNone(current["expected_total_cost"])
        self.assertIsNone(current["continue_cost"])
        self.assertIsNone(current["restart_cost"])
        self.assertIn("当前计算精度", current["reason"])
        self.assertFalse(result["optimization_converged"])

    def test_last_slot_official_probability_and_cumulative_cost_comparison(self):
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
        self.assertEqual(current["recommend_continue"],
                         current["continue_cost"] <= current["restart_cost"])
        self.assertAlmostEqual(current["expected_total_cost"],
                               min(current["continue_cost"], current["restart_cost"]))

    def test_zero_weight_types_still_drawn_and_cost_recovery(self):
        # 只有暴击率有效，所有档位同分；抽到即成功。
        for name in self.weights["sub_stats"]:
            self.weights["sub_stats"][name] = 0
        self.weights["sub_stats"]["crit_rate"] = 20 / 10.5
        self.stats["sub_stats"]["tiers"]["crit_rate"]["values"] = [10.5] * 8
        result = self.planner(30).analyze(self.echo)["zero"]
        # 在本组材料权重下，首条未中就回收的 C/p 最小；命中后必须开满。
        probability = 1 / 13
        # 首条达标也开满：成功支付 143000 经验和 50 素材；其余胚子首条弃置。
        net_tuning = probability * 50 + (1 - probability) * (10 - 3)
        net_exp = probability * 143000 + (1 - probability) * (4500 - 3000)
        self.assertAlmostEqual(result["success_probability"], probability)
        self.assertAlmostEqual(result["expected_embryos"], 1 / probability)
        self.assertAlmostEqual(result["expected_tuning"], net_tuning / probability)
        self.assertAlmostEqual(result["expected_experience"], net_exp / probability)
        self.assertAlmostEqual(result["expected_tuning"], 134)
        self.assertAlmostEqual(result["expected_experience"], 161000)
        self.assertAlmostEqual(result["attempt_cost"], 1 + net_tuning + net_exp)
        self.assertAlmostEqual(result["expected_cost"], result["attempt_cost"] / probability)
        weighted = self.planner(30, tuning_weight=None, experience_weight=None,
                                embryo_weight=None).analyze(self.echo)["zero"]
        self.assertAlmostEqual(weighted["expected_cost"], 13 + 134 + 161000 / 1000 * 1.6)
        self.assertAlmostEqual(weighted["expected_cost"], 404.6)
        self.assertEqual(weighted["success_probability"], result["success_probability"])

    def test_cost_weight_defaults_use_material_drop_estimate(self):
        weights = self.costs["default_weights"]
        self.assertEqual(weights["tuning_weight"], 1)
        self.assertEqual(weights["experience_weight"], 0.8)
        self.assertEqual(weights["embryo_weight"], 1)
        self.assertEqual(self.costs["experience_weight_unit"], 1000)
        planner = ActivationPlanner(EchoScorer(self.stats, self.weights), self.costs, 30, bins=100)
        self.assertEqual((planner.tw, planner.xw, planner.ew),
                         (1, weights["experience_weight"], 1))

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
        self.assertTrue(current["recommend_continue"])
        self.assertEqual(current["future_cost"], (143000 - 4500) + 40)

    def test_successful_grid_states_include_all_completion_materials(self):
        planner = self.planner(30)
        planner.analyze(self.echo)
        mask = 0
        for count in range(6):
            probability, tuning, experience = planner._policy(mask)
            with self.subTest(count=count):
                self.assertEqual(probability[-1], 1)
                self.assertEqual(tuning[-1], 50 - count * 10)
                self.assertEqual(experience[-1], 143000 - planner.cumulative_exp[count])
            mask |= 1 << count

    def test_exact_reached_score_overrides_conservative_grid_and_still_pays_tail(self):
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 6.3}]
        scorer = EchoScorer(self.stats, self.weights)
        base = scorer.stat_score("main_stats", "crit_rate", 22, 4)
        gain = scorer.stat_score("sub_stats", "crit_rate", 6.3)
        planner = ActivationPlanner(scorer, self.costs, base + gain, bins=10)
        current = planner.analyze(self.echo)["current"]
        self.assertEqual(current["full_success_lower"], 1)
        self.assertEqual(current["success_probability"], 1)
        self.assertTrue(current["recommend_continue"])
        self.assertAlmostEqual(current["future_cost"], 40 + (143000 - 4500) / 1000 * 1.6)

    def test_optimal_policy_matches_all_single_useful_type_stopping_rules(self):
        self.weights["sub_stats"] = dict.fromkeys(self.weights["sub_stats"], 0)
        self.weights["sub_stats"]["crit_rate"] = 20 / 10.5
        self.stats["sub_stats"]["tiers"]["crit_rate"]["values"] = [10.5] * 8
        for embryo, tuning, experience in [(1, 1, 1.6), (0, 10, 1.6),
                                           (0, 1, 16), (0, 0, 0), (100, 1, 0)]:
            with self.subTest(weights=(embryo, tuning, experience)):
                planner = self.planner(30, embryo_weight=embryo,
                                       tuning_weight=tuning, experience_weight=experience)
                candidates = []
                for count in range(1, 6):
                    probability = count / 13
                    single_tuning = (probability * 50 + (1 - probability)
                                     * (count * 10 - planner.recovered_tuning[count]))
                    single_exp = (probability * 143000 + (1 - probability)
                                  * (planner.cumulative_exp[count] - planner.recovered_experience[count]))
                    single_cost = embryo + tuning * single_tuning + experience * single_exp / 1000
                    candidates.append((single_cost / probability, -probability,
                                       single_tuning / probability, single_exp / probability, count))
                expected_cost, negative_p, expected_tuning, expected_exp, stop_count = min(candidates)
                result = planner.analyze(self.echo)
                zero = result["zero"]
                self.assertAlmostEqual(zero["success_probability"], -negative_p)
                self.assertAlmostEqual(zero["expected_cost"], expected_cost)
                self.assertAlmostEqual(zero["expected_tuning"], expected_tuning)
                self.assertAlmostEqual(zero["expected_experience"], expected_exp)
                self.assertAlmostEqual(zero["expected_embryos"], -1 / negative_p)
                current = planner.analyze({**self.echo, "sub_stats": [
                    {"name": "hp_flat", "value": 320}]})["current"]
                self.assertEqual(current["recommend_continue"], stop_count > 1)
                self.assertAlmostEqual(current["expected_total_cost"], current["future_cost"]
                                       + (1 - current["success_probability"]) * zero["expected_cost"])

    def test_material_weights_change_action_in_independently_solved_two_slot_model(self):
        stats, weights, costs = copy.deepcopy(self.stats), copy.deepcopy(self.weights), copy.deepcopy(self.costs)
        ids = ("crit_rate", "hp_flat", "def_flat")
        stats["sub_stats"]["tiers"] = {name: stats["sub_stats"]["tiers"][name] for name in ids}
        stats["sub_stats"].update(pool_size=3, max_count=2)
        stats["sub_stats"]["tiers"]["crit_rate"]["values"] = [10.5] * 8
        weights["sub_stats"] = {"crit_rate": 20 / 10.5, "hp_flat": 0, "def_flat": 0}
        costs["steps"] = [{"slot": 1, "tuning": 10, "experience": 500},
                          {"slot": 2, "tuning": 10, "experience": 9500}]
        scorer = EchoScorer(stats, weights)
        target = scorer.stat_score("main_stats", "crit_rate", 22, 4) + 1
        echo = {**self.echo, "sub_stats": [{"name": "hp_flat", "value": 320}]}
        for tuning, experience, expected_p, expected_cost, keep in [
                (1, 0, 2 / 3, 27, True), (0, 1, 1 / 3, 11, False),
                (1, 14, 2 / 3, 188, True), (0, 0, 2 / 3, 0, True)]:
            with self.subTest(weights=(tuning, experience)):
                planner = ActivationPlanner(scorer, costs, target, embryo_weight=0,
                                            tuning_weight=tuning, experience_weight=experience, bins=1000)
                result = planner.analyze(echo)
                self.assertAlmostEqual(result["zero"]["success_probability"], expected_p)
                self.assertAlmostEqual(result["zero"]["expected_cost"], expected_cost)
                current = result["current"]
                self.assertEqual(current["recommend_continue"], keep)
                self.assertAlmostEqual(current["expected_total_cost"],
                                       min(current["continue_cost"], current["restart_cost"]))

    def test_default_and_weighted_cost_optima_match_verified_thousand_bin_results(self):
        scorer = EchoScorer(load_json(DEFAULT_STATS), load_json(DEFAULT_WEIGHTS))
        for embryo, tuning, experience, probability, cost, embryos, tuners, exp in [
                (1, 1, 1.6, .03614128784982372, 954.3025182705469,
                 27.669185562928895, 381.3536237485227, 340799.81809943455),
                (0, 10, 1.6, .04133160244585571, 4259.280866593188,
                 24.19456156605584, 367.6761811375314, 364074.4095111718),
                (0, 1, 16, .025955048197779984, 5663.185461633643,
                 38.52815037675534, 439.90193639891265, 326455.2203271706)]:
            with self.subTest(weights=(embryo, tuning, experience)):
                planner = ActivationPlanner(scorer, self.costs, 30, embryo_weight=embryo,
                                            tuning_weight=tuning, experience_weight=experience)
                self.assertEqual(planner.bins, 1000)
                result = planner.analyze(self.echo)
                zero, current = result["zero"], result["current"]
                self.assertAlmostEqual(zero["success_probability"], probability)
                self.assertAlmostEqual(zero["expected_cost"], cost, places=6)
                self.assertAlmostEqual(zero["expected_embryos"], embryos)
                self.assertAlmostEqual(zero["expected_tuning"], tuners, places=6)
                self.assertAlmostEqual(zero["expected_experience"], exp, places=5)
                self.assertAlmostEqual(cost, embryo * embryos + tuning * tuners + experience * exp / 1000)
                self.assertAlmostEqual(current["expected_total_cost"], cost - embryo)
                self.assertTrue(result["optimization_converged"])
                previous_cache = planner.policy_cache
                self.assertEqual(planner.analyze(self.echo), result)
                self.assertIs(planner.policy_cache, previous_cache)

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

    def test_nonconverged_optimizer_does_not_claim_an_optimal_result(self):
        scorer = EchoScorer(load_json(DEFAULT_STATS), load_json(DEFAULT_WEIGHTS))
        planner = ActivationPlanner(scorer, self.costs, 30)
        planner.MAX_OPTIMIZATION_ITERATIONS = 1
        with self.assertRaisesRegex(RuntimeError, "收敛"):
            planner.analyze(self.echo)

    def test_sub_one_point_target_gap(self):
        result = self.planner(20.1).analyze(self.echo)
        self.assertGreater(result["zero"]["full_success_lower"], .9)

    def test_bounds_and_policy_success(self):
        result = self.planner(60).analyze(self.echo)["zero"]
        self.assertLessEqual(result["full_success_lower"], result["full_success_upper"] + 1e-12)
        self.assertLessEqual(result["success_probability"], result["full_success_lower"] + 1e-12)

    def test_unreachable_current_echo_can_restart_with_finite_total_cost(self):
        self.echo["sub_stats"] = [{"name": "hp_flat", "value": 320},
                                   {"name": "def_flat", "value": 40},
                                   {"name": "hp_pct", "value": 6.4},
                                   {"name": "def_pct", "value": 8.1}]
        result = self.planner(30).analyze(self.echo)
        base = result["current"]
        self.assertLessEqual(result["zero"]["success_probability"], result["zero"]["full_success_lower"] + 1e-12)
        self.assertAlmostEqual(base["expected_total_cost"],
                       min(base["continue_cost"], base["restart_cost"]))
        stopped = self.planner(30).analyze({**self.echo, "sub_stats": [
            {"name": "hp_flat", "value": 320}, {"name": "def_flat", "value": 40},
            {"name": "hp_pct", "value": 6.4}, {"name": "def_pct", "value": 8.1},
        ]})["current"]
        self.assertEqual(stopped["success_probability"], 0)
        self.assertAlmostEqual(stopped["future_cost"], -55500 - 12)
        self.assertAlmostEqual(stopped["expected_total_cost"],
                               result["zero"]["expected_cost"] - 55500 - 12)

    def test_full_echo_unreached_result_is_exact_even_when_grid_upper_reaches_target(self):
        self.echo["sub_stats"] = [
            {"name": name, "value": self.stats["sub_stats"]["tiers"][name]["values"][0]}
            for name in ("crit_rate", "crit_dmg", "atk_pct", "energy_regen", "hp_flat")]
        scorer = EchoScorer(self.stats, self.weights)
        base = scorer.stat_score("main_stats", "crit_rate", 22, 4)
        exact = base + sum(scorer.stat_score("sub_stats", item["name"], item["value"])
                           for item in self.echo["sub_stats"])
        current = ActivationPlanner(scorer, self.costs, exact + .001, bins=1000).analyze(self.echo)["current"]
        self.assertEqual(current["full_success_lower"], 0)
        self.assertEqual(current["full_success_upper"], 0)
        self.assertEqual(current["success_probability"], 0)
        self.assertIsNone(current["continue_cost"])

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