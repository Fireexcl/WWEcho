import copy
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, ROOT, EchoScorer, load_json, main


class EchoScorerTests(unittest.TestCase):
    def setUp(self):
        self.stats = load_json(DEFAULT_STATS)
        self.weights = load_json(DEFAULT_WEIGHTS)
        self.scorer = EchoScorer(self.stats, self.weights)
        self.echo = load_json(ROOT / "examples" / "echo.json")

    def test_full_echo(self):
        result = self.scorer.score(self.echo)
        self.assertEqual(result["score"], 90)
        self.assertEqual(result["main_score"], 20)
        self.assertEqual(result["sub_score"], 70)

    def test_one_to_five_slots_accumulate(self):
        for count, expected in enumerate([40, 60, 70, 80, 90], 1):
            with self.subTest(count=count):
                echo = dict(self.echo, sub_stats=self.echo["sub_stats"][:count])
                self.assertEqual(self.scorer.score(echo)["score"], expected)

    def test_reference_ratio_and_flat_units(self):
        self.echo["sub_stats"] = [{"name": "crit_rate", "value": 6.3},
                                  {"name": "atk_flat", "value": 30}]
        self.assertEqual(self.scorer.score(self.echo)["score"], 34.5)

    def test_zero_and_custom_weights(self):
        self.weights["main_stats"]["crit_rate"] = 0
        self.weights["sub_stats"]["crit_rate"] = 30
        self.echo["sub_stats"] = self.echo["sub_stats"][:1]
        self.assertEqual(EchoScorer(self.stats, self.weights).score(self.echo)["score"], 30)

    def test_every_main_candidate(self):
        for cost, pool in self.stats["main_stats"]["max_values_by_cost"].items():
            for name, value in pool.items():
                with self.subTest(cost=cost, name=name):
                    echo = dict(self.echo, cost=int(cost), main_stat={"name": name, "value": value})
                    self.assertEqual(self.scorer.score(echo)["main_score"], 20)

    def test_every_sub_tier(self):
        for name, data in self.stats["sub_stats"]["tiers"].items():
            for value in data["values"]:
                with self.subTest(name=name, value=value):
                    echo = dict(self.echo, sub_stats=[{"name": name, "value": value}])
                    expected = 20 + self.weights["sub_stats"][name] * value / max(data["values"])
                    self.assertAlmostEqual(self.scorer.score(echo)["score"], expected, places=5)

    def test_invalid_inputs(self):
        invalid = [
            {}, [],
            dict(self.echo, sub_stats=[]),
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
        self.weights["sub_stats"]["crit_rate"] = 1e308
        self.echo["sub_stats"] = self.echo["sub_stats"][:1]
        scorer = EchoScorer(self.stats, self.weights)
        self.assertEqual(scorer.score(self.echo)["score"], 1e308)

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
            self.assertIn('"score": 90.0' if details else "90.0", output.getvalue())

    def test_cli_missing_file(self):
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main([str(ROOT / "examples" / "missing.json")]), 2)
        self.assertIn("评分失败", error.getvalue())


if __name__ == "__main__":
    unittest.main()