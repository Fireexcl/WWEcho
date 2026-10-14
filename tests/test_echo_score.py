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
        main = max(value * weights["main_stats"][name]
                   for pool in self.stats["main_stats"]["max_values_by_cost"].values()
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
            return entry["value"] * weights[category][name] / maximum * 50 if maximum else 0
        main_score = contribution("main_stats", echo["main_stat"])
        sub_score = math.fsum(contribution("sub_stats", item) for item in echo["sub_stats"])
        return round(main_score + sub_score, 6), round(main_score, 6), round(sub_score, 6)

    def test_full_echo(self):
        result = self.scorer.score(self.echo)
        expected = self.expected_scores(self.echo)
        self.assertEqual((result["score"], result["main_score"], result["sub_score"]), expected)
        self.assertEqual(self.scorer.unaligned_max_score, self.raw_maximum())

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
        self.weights["main_stats"]["crit_rate"] = 0
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
            (value * self.weights["main_stats"][name], cost, name, value)
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
        for category in ("main_stats", "sub_stats"):
            zero[category] = dict.fromkeys(zero[category], 0)
            scaled[category] = {name: value * 3 for name, value in scaled[category].items()}
        scorer = EchoScorer(self.stats, zero)
        self.assertEqual(scorer.unaligned_max_score, 0)
        self.assertEqual(scorer.aligned_max_score, 0)
        self.assertEqual(scorer.score(self.echo)["score"], 0)
        self.assertEqual(EchoScorer(self.stats, scaled).score(self.echo)["score"],
                         self.scorer.score(self.echo)["score"])

    def test_maximum_ranks_value_times_weight_not_weight(self):
        weights = copy.deepcopy(self.weights)
        for category in ("main_stats", "sub_stats"):
            weights[category] = dict.fromkeys(weights[category], 0)
        weights["main_stats"].update(crit_rate=2, crit_dmg=1.5)
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