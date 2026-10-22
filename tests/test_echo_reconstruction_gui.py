"""区域 4 的真实 Qt 交互、预算展示、独立后台及过期结果回归。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from src import echo_reconstruction, echo_strategy
from src.echo_gui import EchoWindow
from src.echo_reconstruction import ReconstructionPlanner, compare_budget, evaluate_budget
from src.echo_score import EchoScorer


class FastActivationPlanner:
    @classmethod
    def reset(cls):
        cls.gate = threading.Event()
        cls.gate.set()
        cls.calls = []

    def __init__(self, scorer, costs, target, **params):
        self.scorer = scorer

    def analyze(self, echo):
        self.calls.append(copy.deepcopy(echo))
        if not self.gate.wait(5):
            raise RuntimeError("activation test timeout")
        return {
            "zero": {"full_success_lower": 1, "full_success_upper": 1,
                     "success_probability": 1, "attempt_cost": 1, "expected_cost": 1,
                     "expected_embryos": 1, "expected_tuning": 0, "expected_experience": 0},
            "current": {"full_success_lower": 1, "full_success_upper": 1,
                        "success_probability": 1, "baseline_success_lower": 1,
                        "recommend_continue": False, "reason": "test", "future_cost": 0},
            "score_resolution": .1, "method": "test",
        }


class ControlledReconstructionPlanner:
    @classmethod
    def reset(cls):
        cls.instances, cls.calls = [], []
        cls.active, cls.max_active = 0, 0
        cls.gate = threading.Event()
        cls.gate.set()
        cls.error = None

    def __init__(self, scorer):
        self.scorer = scorer
        self.real = ReconstructionPlanner(scorer)
        self.thread = threading.get_ident()
        self.instances.append(self)

    def analyze(self, echo, *, cancelled=None):
        cls = type(self)
        cls.calls.append((self, copy.deepcopy(echo), threading.get_ident()))
        cls.active += 1
        cls.max_active = max(cls.max_active, cls.active)
        try:
            if not cls.gate.wait(5):
                raise RuntimeError("reconstruction test timeout")
            if cls.error:
                raise cls.error
            # 故意不遵从取消，证明即使任务返回旧成功结果也会被版本检查拒绝。
            return self.real.analyze(echo)
        finally:
            cls.active -= 1


class ReconstructionGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        FastActivationPlanner.reset()
        ControlledReconstructionPlanner.reset()
        self.activation_patch = patch.object(echo_strategy, "ActivationPlanner", FastActivationPlanner)
        self.reconstruction_patch = patch.object(echo_reconstruction, "ReconstructionPlanner",
                                                ControlledReconstructionPlanner)
        self.activation_patch.start()
        self.reconstruction_patch.start()
        self.window = EchoWindow()

    def tearDown(self):
        FastActivationPlanner.gate.set()
        ControlledReconstructionPlanner.gate.set()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.reconstruction_patch.stop()
        self.activation_patch.stop()

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        release_gil = threading.Event()
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            release_gil.wait(.005)
        self.assertTrue(predicate(), self.window.reconstruction_status.text())

    def fill_five(self):
        self.window.updating = True
        try:
            for name in ["crit_rate", "crit_dmg", "atk_pct", "hp_flat", "def_flat"]:
                if not self.window.sub_stat_buttons[name].isChecked():
                    self.window.sub_stat_buttons[name].click()
                tier = self.window.sub_tier_inputs[name]
                tier.setCurrentIndex(tier.count() - 1)
        finally:
            self.window.updating = False
        self.window.calculate()

    def wait_for_result(self):
        self.wait_until(lambda: self.window._reconstruction_result is not None)

    def test_five_buttons_require_full_echo_and_limit_locks_to_four(self):
        buttons = self.window.reconstruction_lock_buttons
        self.assertEqual(len(buttons), 5)
        self.assertTrue(all(button.isCheckable() and not button.isEnabled() for button in buttons))
        self.assertEqual(self.window.reconstruction_budget.value(), 4)
        self.assertIn("开满五条", self.window.reconstruction_status.text())
        self.fill_five()
        self.assertTrue(all(button.isEnabled() for button in buttons))
        self.wait_for_result()
        version = self.window._strategy_version
        result = self.window._reconstruction_result
        for index, name in enumerate(self.window.selected_sub_stats):
            self.assertIn(self.window.stats["stat_definitions"][name]["name_zh"], buttons[index].text())
            self.assertIn(f"{self.window.sub_tier_inputs[name].currentIndex() + 1}档", buttons[index].text())
            buttons[index].click()
        self.assertEqual(len(self.window.reconstruction_locks), 4)
        self.assertFalse(buttons[4].isChecked())
        self.assertIn("最多锁定 4", self.window.statusBar().currentMessage())
        self.assertIn("当前锁 4 条 / 洗 1 条", self.window.reconstruction_selected.text())
        self.assertEqual(self.window._strategy_version, version)
        self.assertIs(self.window._reconstruction_result, result)
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 1)
        self.window.clear_reconstruction_locks_button.click()
        self.assertEqual(self.window.reconstruction_locks, set())
        self.assertTrue(all(not button.isChecked() for button in buttons))
        self.assertIn("尚未锁定", self.window.reconstruction_selected.text())

    def test_all_thirty_rows_and_selected_probability_match_real_api(self):
        self.fill_five()
        self.wait_for_result()
        result = self.window._reconstruction_result
        table = self.window.reconstruction_plans_table
        self.assertEqual(table.rowCount(), 30)
        self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 4)
        for row, plan in enumerate(self.window._reconstruction_rows):
            self.assertEqual(table.item(row, 0).text(), str(plan["locked_count"]))
            self.assertEqual(table.item(row, 2).text(), f"{plan['probability']:.4%}")
            self.assertEqual(table.item(row, 3).text(), f"{plan['expected_delta']:+.4g}")
            self.assertEqual(table.item(row, 5).text(), str(plan["cost"]))
            self.assertEqual(table.item(row, 6).text(), f"{plan['positive_gain_per_transducer']:.4g}")
        table.cellClicked.emit(8, 1)
        selected = self.window._reconstruction_rows[8]
        self.assertEqual(self.window.reconstruction_locks, set(selected["locked_ids"]))
        self.assertIn(f"单次正提升概率 {selected['probability']:.4%}", self.window.reconstruction_selected.text())
        self.assertIs(self.window._reconstruction_result, result)
        self.assertEqual(len(table.selectionModel().selectedRows()), 1)
        self.assertEqual(table.selectionModel().selectedRows()[0].row(), 8)

    def test_budget_changes_reuse_cache_and_show_equal_budget_and_marginal_values(self):
        self.fill_five()
        self.wait_for_result()
        result = self.window._reconstruction_result
        self.window.reconstruction_budget.setValue(2)
        comparison = compare_budget(result["plans"], 2)
        table = self.window.reconstruction_budget_table
        for row, plan in enumerate(comparison["best_by_count"]):
            value = plan["budget"]
            self.assertEqual(table.item(row, 2).text(), f"{value['max_attempts']} 次 / {value['max_cost']} 枚")
            if row < 2:
                self.assertEqual(table.item(row, 3).text(), f"{value['success_probability']:.4%}")
                self.assertEqual(table.item(row, 4).text(), f"{value['expected_delta']:+.4g}")
                self.assertEqual(table.item(row, 7).text(), f"{value['next_marginal_delta']:+.4g}")
                self.assertEqual(table.item(row, 8).text(), f"{value['next_marginal_cost']:.4g}")
            else:
                self.assertEqual(table.item(row, 1).text(), "预算不足")
        self.assertEqual(table.item(0, 2).text(), "2 次 / 2 枚")
        self.assertEqual(table.item(1, 2).text(), "1 次 / 2 枚")
        table.cellClicked.emit(0, 1)
        before = set(self.window.reconstruction_locks)
        table.cellClicked.emit(3, 1)
        self.assertEqual(self.window.reconstruction_locks, before)
        self.window.reconstruction_budget.setValue(0)
        self.assertIn("暂不投入", self.window.reconstruction_recommendation.text())
        self.assertIn("最多 0 次 / 0 枚", self.window.reconstruction_selected.text())
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 1)
        self.assertIs(self.window._reconstruction_result, result)

    def test_tier_and_reselection_track_stat_identity_and_clear_incomplete_results(self):
        self.fill_five()
        self.wait_for_result()
        self.window.reconstruction_lock_buttons[1].click()
        self.window.reconstruction_lock_buttons[2].click()
        self.window.sub_tier_inputs["crit_dmg"].setCurrentIndex(0)
        self.assertIn("1档", self.window.reconstruction_lock_buttons[1].text())
        self.assertIsNone(self.window._reconstruction_result)
        self.assertEqual(self.window.reconstruction_plans_table.rowCount(), 0)
        self.wait_for_result()
        self.assertEqual(len(ControlledReconstructionPlanner.instances), 1)
        self.window.sub_stat_buttons["crit_rate"].click()
        self.assertEqual(self.window.reconstruction_locks, {"crit_dmg", "atk_pct"})
        self.assertTrue(all(not button.isEnabled() for button in self.window.reconstruction_lock_buttons))
        self.assertIsNone(self.window._reconstruction_result)
        self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 0)
        self.assertEqual(self.window.reconstruction_recommendation.text(), "—")
        self.window.updating = True
        self.window.sub_stat_buttons["energy_regen"].click()
        self.window.updating = False
        self.window.calculate()
        self.wait_for_result()
        self.assertTrue(self.window.reconstruction_lock_buttons[0].isChecked())
        self.assertTrue(self.window.reconstruction_lock_buttons[1].isChecked())
        self.assertIn("暴击伤害", self.window.reconstruction_lock_buttons[0].text())
        self.window.sub_stat_buttons["crit_dmg"].click()
        self.assertEqual(self.window.reconstruction_locks, {"atk_pct"})
        self.window.clear_sub_stats()
        self.assertEqual(self.window.reconstruction_locks, set())
        self.assertTrue(all(not button.isChecked() for button in self.window.reconstruction_lock_buttons))

    def test_target_and_activation_parameters_do_not_recompute_reconstruction(self):
        self.fill_five()
        self.wait_for_result()
        result = self.window._reconstruction_result
        text = self.window.reconstruction_status.text()
        self.window.target.setValue(87)
        self.window.strategy_inputs["recovery"].setValue(.8)
        self.assertIs(self.window._reconstruction_result, result)
        self.assertEqual(self.window.reconstruction_status.text(), text)
        self.assertIsNone(self.window._reconstruction_pending)
        self.assertEqual(len(ControlledReconstructionPlanner.instances), 1)
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(3)
        self.assertIsNone(self.window._reconstruction_result)
        self.wait_for_result()
        self.assertEqual(len(ControlledReconstructionPlanner.instances), 2)
        self.assertNotEqual(self.window._reconstruction_result, result)

    def test_no_improvement_displays_undefined_gain_and_infinite_success_cost(self):
        self.fill_five()
        weights = self.window.current_weights()
        for pool in [weights["sub_stats"], *weights["main_stats"].values(), *weights["minor_stats"].values()]:
            for name in pool:
                pool[name] = 0
        self.window.apply_weights(weights)
        self.wait_for_result()
        table = self.window.reconstruction_plans_table
        for row in range(table.rowCount()):
            self.assertEqual(table.item(row, 2).text(), "0.0000%")
            self.assertEqual(table.item(row, 4).text(), "—")
            self.assertEqual(table.item(row, 7).text(), "无法获得 / ∞")
        self.assertIn("暂不投入", self.window.reconstruction_recommendation.text())
        self.window.reconstruction_lock_buttons[0].click()
        self.assertIn("成功时平均Δ分 —", self.window.reconstruction_selected.text())
        self.assertIn("升分期望消耗 无法获得 / ∞", self.window.reconstruction_selected.text())

    def test_separate_worker_runs_while_activation_is_blocked(self):
        FastActivationPlanner.gate.clear()
        self.wait_until(lambda: len(FastActivationPlanner.calls) == 1)
        self.fill_five()
        self.wait_for_result()
        self.assertIsNotNone(self.window._strategy_task)
        self.assertEqual(len(FastActivationPlanner.calls), 1)
        self.assertEqual(self.window.reconstruction_plans_table.rowCount(), 30)

    def test_debounce_single_task_and_stale_results_rejected_on_main_thread(self):
        ControlledReconstructionPlanner.gate.clear()
        self.fill_five()
        renders = []
        display = self.window._display_reconstruction

        def record(result):
            renders.append((QThread.currentThread(), result))
            display(result)

        self.window._display_reconstruction = record
        self.wait_until(lambda: len(ControlledReconstructionPlanner.calls) == 1)
        for index in (0, 1, 2):
            self.window.sub_tier_inputs["crit_rate"].setCurrentIndex(index)
        self.wait_until(lambda: not self.window._reconstruction_timer.isActive())
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 1)
        self.assertEqual(self.window.reconstruction_plans_table.rowCount(), 0)
        expected_score = EchoScorer(self.window.stats, self.window.current_weights()).score(self.window.current_echo())["score"]
        self.assertEqual(self.window.score_label.text(), f"{expected_score:g} 分")
        ControlledReconstructionPlanner.gate.set()
        self.wait_for_result()
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 2)
        self.assertEqual(len(renders), 1)
        self.assertIs(renders[0][0], self.app.thread())
        self.assertEqual(ControlledReconstructionPlanner.max_active, 1)
        self.assertEqual(ControlledReconstructionPlanner.calls[-1][1], self.window.current_echo())
        self.assertTrue(all(call[2] != threading.get_ident() for call in ControlledReconstructionPlanner.calls))
        self.assertTrue(all(item.thread != threading.get_ident() for item in ControlledReconstructionPlanner.instances))

    def test_failure_does_not_affect_scoring_and_can_retry(self):
        ControlledReconstructionPlanner.error = ValueError("controlled reconstruction failure")
        self.fill_five()
        self.wait_until(lambda: "controlled reconstruction failure" in self.window.reconstruction_status.text())
        score = self.window.score_label.text()
        self.assertNotEqual(score, "—")
        self.assertEqual(self.window.reconstruction_plans_table.rowCount(), 0)
        ControlledReconstructionPlanner.error = None
        self.window.calculate()
        self.wait_for_result()
        self.assertEqual(self.window.score_label.text(), score)

    def test_invalid_input_rejects_stale_error_and_recovers(self):
        ControlledReconstructionPlanner.gate.clear()
        self.fill_five()
        self.wait_until(lambda: len(ControlledReconstructionPlanner.calls) == 1)
        tier = self.window.sub_tier_inputs["crit_rate"]
        tier.addItem("invalid", -1)
        tier.setCurrentIndex(tier.count() - 1)
        ControlledReconstructionPlanner.error = ValueError("stale error")
        ControlledReconstructionPlanner.gate.set()
        self.wait_until(lambda: self.window._reconstruction_task is None)
        self.assertEqual(self.window.reconstruction_status.text(), "频整器输入无效")
        self.assertIsNone(self.window._reconstruction_result)
        self.assertTrue(all(not button.isEnabled() for button in self.window.reconstruction_lock_buttons))
        self.assertIn("输入有误", self.window.summary.text())
        ControlledReconstructionPlanner.error = None
        tier.setCurrentIndex(0)
        self.wait_for_result()

    def test_close_cleans_up_inflight_and_pending_reconstruction(self):
        ControlledReconstructionPlanner.gate.clear()
        self.fill_five()
        self.wait_until(lambda: len(ControlledReconstructionPlanner.calls) == 1)
        self.window.sub_tier_inputs["crit_rate"].setCurrentIndex(0)
        release = threading.Timer(.05, ControlledReconstructionPlanner.gate.set)
        release.start()
        try:
            self.window.close()
        finally:
            release.join()
        self.app.processEvents()
        self.assertEqual(self.window._reconstruction_pool.activeThreadCount(), 0)
        self.assertIsNone(self.window._reconstruction_task)
        self.assertIsNone(self.window._reconstruction_pending)
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 1)
        self.assertNotIn("频整器已更新", self.window.reconstruction_status.text())

    def test_close_before_debounce_does_not_launch_reconstruction(self):
        self.fill_five()
        self.window.close()
        self.assertIsNone(self.window._reconstruction_pending)
        self.assertEqual(ControlledReconstructionPlanner.calls, [])

    def test_real_reconstruction_worker_matches_api_without_planner_patch(self):
        self.reconstruction_patch.stop()
        self.fill_five()
        self.wait_for_result()
        expected = ReconstructionPlanner(EchoScorer(self.window.stats, self.window.current_weights())).analyze(self.window.current_echo())
        self.assertEqual(self.window._reconstruction_result, expected)
        self.window.reconstruction_lock_buttons[0].click()
        selected = next(plan for plan in expected["plans"] if plan["locked_ids"] == ["crit_rate"])
        budget = evaluate_budget(selected, 4)
        self.assertIn(f"单次正提升概率 {selected['probability']:.4%}", self.window.reconstruction_selected.text())
        self.assertIn(f"期望实际消耗 {budget['expected_cost']:.4g} 枚", self.window.reconstruction_selected.text())


if __name__ == "__main__":
    unittest.main()