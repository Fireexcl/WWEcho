import copy
import io
import json
import math
import unittest
from contextlib import redirect_stderr, redirect_stdout

from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, ROOT, EchoScorer, load_json, main


class EchoScorerTests(unittest.TestCase):
    def setUp(self):
        self.stats = load_json(DEFAULT_STATS)
        self.weights = load_json(DEFAULT_WEIGHTS)
        self.scorer = EchoScorer(self.stats, self.weights)
        self.echo = load_json(ROOT / "examples" / "echo.json")

    def raw_maximum(self, weights=None):
        weights = self.weights if weights is None else weights
        main = max(value * weights["main_stats"][cost][name] + sum(
               minor_value * weights.get("minor_stats", {}).get(cost, {}).get(minor_id, 0)
               for minor_id, minor_value in self.stats["minor_stats"]["max_values_by_cost"][cost].items())
               for cost, pool in self.stats["main_stats"]["max_values_by_cost"].items()
                   for name, value in pool.items())
        subs = sorted((max(tier["values"]) * weights["sub_stats"][name]
                       for name, tier in self.stats["sub_stats"]["tiers"].items()), reverse=True)
        return math.fsum([main] + subs[:5])

    def expected_scores(self, echo, weights=None):
        weights = self.weights if weights is None else weights
        maximum = self.raw_maximum(weights)
        names = {data["name_zh"]: name for name, data in self.stats["stat_definitions"].items()}
        def contribution(category, entry):
            name = names.get(entry["name"], entry["name"])
            if category == "main_stats":
                cost = str(echo["cost"]) if "cost" in echo else next(
                    cost for cost, pool in self.stats["main_stats"]["max_values_by_cost"].items()
                    if pool.get(name) == entry["value"])
                weight = weights[category][cost][name]
            else:
                weight = weights[category][name]
            return entry["value"] * weight / maximum * 50 if maximum else 0
        main_score = contribution("main_stats", echo["main_stat"])
        main_name = names.get(echo["main_stat"]["name"], echo["main_stat"]["name"])
        cost = str(echo["cost"]) if "cost" in echo else next(
            key for key, pool in self.scorer.main_by_cost.items() if pool.get(main_name) == echo["main_stat"]["value"])
        minor_score = sum(value * weights.get("minor_stats", {}).get(cost, {}).get(name, 0)
                          for name, value in self.stats["minor_stats"]["max_values_by_cost"][cost].items())
        minor_score = minor_score / maximum * 50 if maximum else 0
        sub_score = math.fsum(contribution("sub_stats", item) for item in echo["sub_stats"])
        return round(main_score + minor_score + sub_score, 6), round(main_score, 6), round(sub_score, 6)

    def test_full_echo(self):
        result = self.scorer.score(self.echo)
        expected = self.expected_scores(self.echo)
        self.assertEqual((result["score"], result["main_score"], result["sub_score"]), expected)
        self.assertEqual(self.scorer.unaligned_max_score, self.raw_maximum())

    def test_default_config_has_separate_cost_weights(self):
        self.assertEqual(set(self.weights["main_stats"]), {"1", "3", "4"})
        for cost, pool in self.stats["main_stats"]["max_values_by_cost"].items():
            self.assertEqual(set(self.weights["main_stats"][cost]), set(pool))
        changed = copy.deepcopy(self.weights)
        changed["main_stats"]["1"]["atk_pct"] = 7
        self.assertEqual(changed["main_stats"]["3"]["atk_pct"], self.weights["main_stats"]["3"]["atk_pct"])
        self.assertEqual(changed["main_stats"]["4"]["atk_pct"], self.weights["main_stats"]["4"]["atk_pct"])
        scorer = EchoScorer(self.stats, changed)
        echo = {"cost": 1, "main_stat": {"name": "atk_pct", "value": 18}, "sub_stats": []}
        self.assertEqual(scorer.score(echo)["main_stat"]["weight"], 7)

    def test_one_to_five_slots_accumulate(self):
        previous = -1
        for count in range(6):
            with self.subTest(count=count):
                echo = dict(self.echo, sub_stats=self.echo["sub_stats"][:count])
            score = self.scorer.score(echo)["score"]
            self.assertEqual(score, self.expected_scores(echo)[0])
            self.assertGreaterEqual(score, previous)
            previous = score

    def test_reference_ratio_and_flat_units(self):
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 6.3},
                                  {"name": "atk_flat", "value": 30}]
        result = self.scorer.score(self.echo)
        self.assertEqual(result["score"], self.expected_scores(self.echo)[0])
        self.assertEqual(result["sub_stats"][1]["score"],
                 round(30 * self.weights["sub_stats"]["atk_flat"] / self.raw_maximum() * 50, 6))

    def test_zero_and_custom_weights(self):
        self.weights["main_stats"]["4"]["crit_rate"] = 0
        self.weights["sub_stats"]["crit_rate"] = 30
        self.echo["sub_stats"] = self.echo["sub_stats"][:1]
        self.assertEqual(EchoScorer(self.stats, self.weights).score(self.echo)["score"],
                 self.expected_scores(self.echo)[0])

    def test_every_main_candidate(self):
        for cost, pool in self.stats["main_stats"]["max_values_by_cost"].items():
            for name, value in pool.items():
                with self.subTest(cost=cost, name=name):
                    echo = dict(self.echo, cost=int(cost), main_stat={"name": name, "value": value})
                    self.assertEqual(self.scorer.score(echo)["main_score"], self.expected_scores(echo)[1])

    def test_every_sub_tier(self):
        for name, data in self.stats["sub_stats"]["tiers"].items():
            for value in data["values"]:
                with self.subTest(name=name, value=value):
                    echo = dict(self.echo, sub_stats=[{"name": name, "value": value}])
                    self.assertEqual(self.scorer.score(echo)["score"], self.expected_scores(echo)[0])

    def test_best_echo_is_aligned_to_fifty(self):
        _, cost, name, value = max(
            (value * self.weights["main_stats"][cost][name] + sum(
                minor_value * self.weights["minor_stats"][cost][minor_id]
                for minor_id, minor_value in self.scorer.minor_by_cost[cost].items()), cost, name, value)
            for cost, pool in self.scorer.main_by_cost.items() for name, value in pool.items())
        best_subs = sorted(self.scorer.tiers,
                           key=lambda name: max(self.scorer.tiers[name]["values"]) *
                           self.weights["sub_stats"][name], reverse=True)[:5]
        echo = {"cost": int(cost), "main_stat": {"name": name, "value": value},
                "sub_stats": [{"name": name, "value": max(self.scorer.tiers[name]["values"])}
                              for name in best_subs]}
        self.assertEqual(self.scorer.score(echo)["score"], 50)

    def test_zero_weights_and_uniform_rescaling(self):
        zero = copy.deepcopy(self.weights)
        scaled = copy.deepcopy(self.weights)
        zero["sub_stats"] = dict.fromkeys(zero["sub_stats"], 0)
        scaled["sub_stats"] = {name: value * 3 for name, value in scaled["sub_stats"].items()}
        for cost, pool in self.weights["main_stats"].items():
            zero["main_stats"][cost] = dict.fromkeys(pool, 0)
            scaled["main_stats"][cost] = {name: value * 3 for name, value in pool.items()}
        for cost, pool in self.weights["minor_stats"].items():
            zero["minor_stats"][cost] = dict.fromkeys(pool, 0)
            scaled["minor_stats"][cost] = {name: value * 3 for name, value in pool.items()}
        scorer = EchoScorer(self.stats, zero)
        self.assertEqual(scorer.unaligned_max_score, 0)
        self.assertEqual(scorer.aligned_max_score, 0)
        self.assertEqual(scorer.score(self.echo)["score"], 0)
        self.assertEqual(EchoScorer(self.stats, scaled).score(self.echo)["score"],
                         self.scorer.score(self.echo)["score"])

    def test_maximum_ranks_value_times_weight_not_weight(self):
        weights = copy.deepcopy(self.weights)
        weights.pop("minor_stats")
        weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 0)
        for cost, pool in weights["main_stats"].items():
            weights["main_stats"][cost] = dict.fromkeys(pool, 0)
        weights["main_stats"]["4"].update(crit_rate=2, crit_dmg=1.5)
        weights["sub_stats"].update(hp_flat=1, crit_rate=2, crit_dmg=1,
                                   atk_pct=1, energy_regen=1, atk_flat=.5)
        scorer = EchoScorer(self.stats, weights)
        self.assertAlmostEqual(scorer.unaligned_max_score, 66 + 580 + 21 + 21 + 12.4 + 30)
        result = scorer.score(self.echo)
        for category, items in [("main_stats", [result["main_stat"]]), ("sub_stats", result["sub_stats"])]:
            for item in items:
                self.assertEqual(item["score"], round(item["value"] * item["weight"] /
                                                     scorer.unaligned_max_score * 50, 6))

    def test_invalid_inputs(self):
        invalid = [
            {}, [],
            dict(self.echo, sub_stats=None),
            dict(self.echo, sub_stats=self.echo["sub_stats"] + [{"name": "固定攻击", "value": 30}]),
            dict(self.echo, sub_stats=[{"name": "crit_rate", "value": 10.5},
                                      {"name": "暴击率", "value": 6.3}]),
            dict(self.echo, main_stat={"name": "暴击率", "value": 21}),
            dict(self.echo, main_stat={"name": "固定攻击", "value": 150}),
            dict(self.echo, cost=3),
            dict(self.echo, cost=True),
            dict(self.echo, cost="4"),
            dict(self.echo, cost=None),
            dict(self.echo, level=20),
        ]
        for name, value in [("未知", 10), ("healing_bonus", 26), ("crit_rate", 7),
                            ("crit_rate", 0.105), ("crit_rate", -1), ("crit_rate", True),
                            ("crit_rate", "10.5%"), ("crit_rate", float("nan")),
                            ("crit_rate", float("inf")), (None, 10.5)]:
            invalid.append(dict(self.echo, sub_stats=[{"name": name, "value": value}]))
        for echo in invalid:
            with self.subTest(echo=echo), self.assertRaises(ValueError):
                self.scorer.score(echo)

    def test_invalid_weights(self):
        for value in [-1, True, "20", float("nan"), float("inf")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                weights = copy.deepcopy(self.weights)
                weights["sub_stats"]["crit_rate"] = value
                EchoScorer(self.stats, weights)
        for action in ["missing", "extra"]:
            weights = copy.deepcopy(self.weights)
            if action == "missing":
                del weights["sub_stats"]["crit_rate"]
            else:
                weights["sub_stats"]["typo"] = 20
            with self.assertRaises(ValueError):
                EchoScorer(self.stats, weights)

    def test_minor_scores_and_same_cost_maximum(self):
        weights = copy.deepcopy(self.weights)
        weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 0)
        weights["main_stats"] = {cost: dict.fromkeys(pool, 0) for cost, pool in self.scorer.main_by_cost.items()}
        weights["minor_stats"] = {"1": {"hp_flat": .1}, "3": {"atk_flat": .5}, "4": {"atk_flat": 1}}
        weights["main_stats"]["4"]["crit_rate"] = 5
        scorer = EchoScorer(self.stats, weights)
        # Cost1基础228，Cost4基础110+150=260；不能误算110+228。
        self.assertEqual(scorer.unaligned_max_score, 260)
        for cost, name, value, minor_value, weight in [(1, "atk_pct", 18, 2280, .1),
                                                       (3, "atk_pct", 30, 100, .5),
                                                       (4, "crit_rate", 22, 150, 1)]:
            echo = {"cost": cost, "main_stat": {"name": name, "value": value}, "sub_stats": []}
            result = scorer.score(echo)
            self.assertEqual(result["minor_stat"]["value"], minor_value)
            self.assertEqual(result["minor_score"], round(minor_value * weight / 260 * 50, 6))
            self.assertAlmostEqual(result["score"], result["main_score"] + result["minor_score"], places=5)
        self.assertEqual(result["score"], 50)

    def test_minor_weight_validation_and_legacy_default(self):
        self.assertEqual(set(self.weights["minor_stats"]), {"1", "3", "4"})
        for cost, name in [("4", "atk_flat"), ("3", "atk_flat"), ("1", "hp_flat")]:
            self.assertEqual(set(self.weights["minor_stats"][cost]), {name})
            for value in (-1, True, "1", float("inf")):
                invalid = copy.deepcopy(self.weights)
                invalid["minor_stats"][cost][name] = value
                with self.assertRaises(ValueError):
                    EchoScorer(self.stats, invalid)
        legacy = copy.deepcopy(self.weights)
        del legacy["minor_stats"]
        scorer = EchoScorer(self.stats, legacy)
        self.assertTrue(all(value == 0 for pool in scorer.minor_weights_by_cost.values() for value in pool.values()))
        self.assertEqual(scorer.score(self.echo)["minor_score"], 0)
        invalid = copy.deepcopy(self.weights)
        invalid["minor_stats"]["4"]["hp_flat"] = 1
        with self.assertRaises(ValueError):
            EchoScorer(self.stats, invalid)

    def test_cost_specific_weights_and_global_maximum(self):
        weights = copy.deepcopy(self.weights)
        weights.pop("minor_stats")
        weights["main_stats"] = copy.deepcopy(self.scorer.main_weights_by_cost)
        for pool in weights["main_stats"].values():
            for name in pool:
                pool[name] = 0
        weights["sub_stats"] = dict.fromkeys(weights["sub_stats"], 0)
        weights["main_stats"]["1"]["atk_pct"] = 1
        weights["main_stats"]["3"]["atk_pct"] = 2
        weights["main_stats"]["4"]["atk_pct"] = 3
        scorer = EchoScorer(self.stats, weights)
        self.assertEqual(scorer.unaligned_max_score, 99)
        for cost, value, expected in [(1, 18, 18 / 99 * 50), (3, 30, 60 / 99 * 50), (4, 33, 50)]:
            echo = {"cost": cost, "main_stat": {"name": "atk_pct", "value": value}, "sub_stats": []}
            self.assertEqual(scorer.score(echo)["score"], round(expected, 6))
            echo.pop("cost")
            self.assertEqual(scorer.score(echo)["score"], round(expected, 6))

    def test_invalid_cost_specific_weights(self):
        weights = copy.deepcopy(self.weights)
        weights["main_stats"] = copy.deepcopy(self.scorer.main_weights_by_cost)
        for value in (-1, True, float("inf")):
            invalid = copy.deepcopy(weights)
            invalid["main_stats"]["1"]["atk_pct"] = value
            with self.assertRaises(ValueError):
                EchoScorer(self.stats, invalid)
        for action in ("missing_cost", "missing_stat", "extra_stat"):
            invalid = copy.deepcopy(weights)
            if action == "missing_cost":
                del invalid["main_stats"]["3"]
            elif action == "missing_stat":
                del invalid["main_stats"]["1"]["atk_pct"]
            else:
                invalid["main_stats"]["1"]["crit_rate"] = 1
            with self.assertRaises(ValueError):
                EchoScorer(self.stats, invalid)

    def test_large_weights_do_not_overflow_intermediate_product(self):
        self.weights["sub_stats"]["crit_rate"] = 1e300
        self.echo["sub_stats"] = self.echo["sub_stats"][:1]
        scorer = EchoScorer(self.stats, self.weights)
        self.assertEqual(scorer.score(self.echo)["score"], 50)

    def test_total_overflow_is_validation_error(self):
        self.weights["sub_stats"]["crit_rate"] = 1e308
        self.weights["sub_stats"]["crit_dmg"] = 1e308
        with self.assertRaises(ValueError):
            EchoScorer(self.stats, self.weights).score(self.echo)

    def test_cli_score_and_details(self):
        for details in [False, True]:
            output = io.StringIO()
            args = [str(ROOT / "examples" / "echo.json")] + (["--details"] if details else [])
            with redirect_stdout(output):
                self.assertEqual(main(args), 0)
            expected = self.expected_scores(self.echo)[0]
            self.assertEqual(json.loads(output.getvalue())["score"] if details
                             else float(output.getvalue()), expected)

    def test_cli_missing_file(self):
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main([str(ROOT / "examples" / "missing.json")]), 2)
        self.assertIn("评分失败", error.getvalue())


if __name__ == "__main__":
    unittest.main()