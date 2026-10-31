"""区域 4 的真实 Qt 交互、预算展示、独立后台及过期结果回归。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QThread
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QHBoxLayout, QFrame

from src import echo_reconstruction, echo_strategy
from src.echo_gui import EchoWindow
from src.echo_reconstruction import ReconstructionPlanner
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
                        "success_probability": 1,
                        "recommend_continue": False, "reason": "test", "future_cost": 0,
                        "continue_cost": None, "restart_cost": None, "expected_total_cost": 0},
            "score_resolution": .1, "method": "test",
        }


class ControlledReconstructionPlanner:
    @classmethod
    def reset(cls):
        cls.instances, cls.calls, cls.budget_calls = [], [], []
        cls.active, cls.max_active = 0, 0
        cls.gate = threading.Event()
        cls.gate.set()
        cls.budget_gate = threading.Event()
        cls.budget_gate.set()
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

    def compare_dynamic_budget(self, echo, budget, *, analysis=None, cancelled=None):
        cls = type(self)
        cls.budget_calls.append((self, budget, threading.get_ident()))
        if not cls.budget_gate.wait(10):
            raise RuntimeError("dynamic budget test timeout")
        # 即使计算不响应取消，也必须由窗口版本检查拒绝旧预算的结果。
        return self.real.compare_dynamic_budget(echo, budget, analysis=analysis)


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
        ControlledReconstructionPlanner.budget_gate.set()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.reconstruction_patch.stop()
        self.activation_patch.stop()

    def wait_until(self, predicate, timeout=15):
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
        self.wait_until(lambda: self.window._reconstruction_result is not None
                        and self.window._reconstruction_result["dynamic"] is not None
                        and self.window._reconstruction_result["dynamic"]["budget"]
                        == self.window.reconstruction_budget.value())

    def test_clear_locks_button_is_right_aligned_and_matches_clear_selection_style(self):
        button = self.window.clear_reconstruction_locks_button
        reference = self.window.clear_sub_stats_button
        self.assertEqual(button.objectName(), reference.objectName())
        layout = button.parentWidget().layout()
        toolbar = layout.itemAt(2).layout()
        self.assertEqual(toolbar.count(), 4)
        self.assertIs(toolbar.itemAt(1).widget(), self.window.reconstruction_budget)
        self.assertIsNotNone(toolbar.itemAt(2).spacerItem())
        self.assertIs(toolbar.itemAt(3).widget(), button)
        self.window.show()
        self.app.processEvents()
        self.assertEqual(button.geometry().right(), toolbar.contentsRect().right())
        self.assertEqual(button.palette().button().color(), reference.palette().button().color())
        self.assertEqual(button.palette().buttonText().color(), reference.palette().buttonText().color())

    def test_reconstruction_divider_replaces_success_status_and_prompts_return(self):
        status = self.window.reconstruction_status
        layout = status.parentWidget().layout()
        divider = layout.itemAt(3).widget()
        self.assertIsInstance(divider, QFrame)
        self.assertIs(divider, self.window.reconstruction_divider)
        self.assertEqual(divider.objectName(), "divider")
        self.assertEqual(divider.frameShape(), QFrame.Shape.HLine)
        self.assertEqual(divider.height(), 1)
        self.assertFalse(status.isHidden())
        self.assertIn("开满五条", status.text())
        self.fill_five()
        self.wait_for_result()
        self.assertEqual(status.text(), "")
        self.assertTrue(status.isHidden())
        self.assertFalse(divider.isHidden())
        ControlledReconstructionPlanner.budget_gate.clear()
        self.window.reconstruction_budget.setValue(2)
        self.assertFalse(status.isHidden())
        self.assertIn("等待后台计算", status.text())
        self.wait_until(lambda: len(ControlledReconstructionPlanner.budget_calls) == 2)
        self.assertFalse(status.isHidden())
        self.assertIn("后台计算中", status.text())
        ControlledReconstructionPlanner.budget_gate.set()
        self.wait_for_result()
        self.assertEqual(status.text(), "")
        self.assertTrue(status.isHidden())
        self.window.clear_sub_stats_button.click()
        self.assertFalse(status.isHidden())
        self.assertIn("开满五条", status.text())
        self.assertFalse(divider.isHidden())

    def test_five_buttons_require_full_echo_and_limit_locks_to_four(self):
        buttons = self.window.reconstruction_lock_buttons
        self.assertEqual(len(buttons), 5)
        grid = self.window.reconstruction_buttons_layout
        for column, button in enumerate(buttons):
            self.assertIs(grid.itemAtPosition(0, column).widget(), button)
            self.assertEqual(grid.columnStretch(column), 1)
        self.assertEqual(grid.rowCount(), 1)
        self.assertEqual(grid.columnCount(), 5)
        self.assertTrue(all(button.isCheckable() and not button.isEnabled() for button in buttons))
        self.assertEqual(self.window.reconstruction_budget.value(), 3)
        self.assertEqual(self.window.reconstruction_budget.maximum(), 6)
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

    def test_single_and_budget_summaries_share_notice_style(self):
        selected = self.window.reconstruction_selected
        recommendation = self.window.reconstruction_recommendation
        for label in (selected, recommendation):
            label.ensurePolished()
            self.assertEqual(label.objectName(), "notice")
        self.assertEqual(selected.font(), recommendation.font())
        self.assertEqual(selected.palette().windowText().color(),
                         recommendation.palette().windowText().color())

    def test_all_thirty_rows_and_selected_probability_match_real_api(self):
        self.fill_five()
        self.wait_for_result()
        result = self.window._reconstruction_result
        table = self.window.reconstruction_plans_table
        self.assertEqual(table.rowCount(), 30)
        self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 6)
        for row, plan in enumerate(self.window._reconstruction_rows):
            self.assertEqual(table.item(row, 0).text(), str(plan["locked_count"]))
            self.assertEqual(table.item(row, 2).text(), f"{plan['probability']:.4%}")
            self.assertEqual(table.item(row, 3).text(), f"{plan['expected_delta']:+.4g}")
            self.assertEqual(table.item(row, 4).text(),
                             "—" if plan["gain_on_success"] is None else f"{plan['gain_on_success']:.4g}")
            self.assertEqual(table.item(row, 5).text(), str(plan["cost"]))
            self.assertEqual(table.item(row, 6).text(), f"{plan['positive_gain_per_transducer']:.4g}")
        table.cellClicked.emit(8, 1)
        selected = self.window._reconstruction_rows[8]
        self.assertEqual(self.window.reconstruction_locks, set(selected["locked_ids"]))
        self.assertIn(f"单次正提升概率 {selected['probability']:.4%}", self.window.reconstruction_selected.text())
        self.assertEqual(len(self.window.reconstruction_selected.text().splitlines()), 3)
        self.assertNotIn("预算", self.window.reconstruction_selected.text())
        self.assertNotIn("最终期望", self.window.reconstruction_selected.text())
        self.assertEqual(self.window.reconstruction_metrics["gain_on_success"].text(), table.item(8, 4).text())
        self.assertIs(self.window._reconstruction_result, result)
        self.assertEqual(len(table.selectionModel().selectedRows()), 1)
        self.assertEqual(table.selectionModel().selectedRows()[0].row(), 8)

    def test_metric_cards_use_four_equal_columns_in_expected_order(self):
        metrics = self.window.reconstruction_metrics
        cards = self.window.reconstruction_status.parentWidget().layout().itemAt(7).layout()
        self.assertIsInstance(cards, QHBoxLayout)
        self.assertEqual(cards.count(), 4)
        for column, name in enumerate(("probability", "gain_on_success", "delta", "cost")):
            self.assertIs(cards.itemAt(column).widget(), metrics[name].parentWidget())
            self.assertEqual(cards.stretch(column), 1)
        caption = metrics["gain_on_success"].parentWidget().layout().itemAt(0).widget()
        self.assertEqual(caption.text(), "当前锁法 · 成功时平均 Δ 分")
        for section in self.window.sections[:-1]:
            section.toggle_button.click()
        self.window.resize(760, 940)
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.window.centralWidget().horizontalScrollBar().maximum(), 0)

    def test_metric_cards_follow_selected_plan_and_clear_stale_results(self):
        self.fill_five()
        self.wait_for_result()
        row, selected = next((row, plan) for row, plan in enumerate(self.window._reconstruction_rows)
                             if plan["gain_on_success"] is not None)
        self.window.reconstruction_plans_table.cellClicked.emit(row, 1)
        metrics = self.window.reconstruction_metrics
        self.assertEqual(metrics["probability"].text(), f"{selected['probability']:.4%}")
        self.assertEqual(metrics["gain_on_success"].text(), f"{selected['gain_on_success']:.4g}")
        self.assertEqual(metrics["gain_on_success"].property("tone"), "positive")
        self.assertEqual(metrics["delta"].text(), f"{selected['expected_delta']:+.4g}")
        self.assertEqual(metrics["cost"].text(), f"{selected['cost']} 枚")
        self.assertEqual(metrics["delta"].property("tone"),
                         "positive" if selected["expected_delta"] > 0 else
                         "negative" if selected["expected_delta"] < 0 else "muted")
        self.window.clear_reconstruction_locks()
        self.assertTrue(all(label.text() == "—" for label in metrics.values()))
        self.assertEqual(metrics["gain_on_success"].property("tone"), "muted")
        self.window.reconstruction_lock_buttons[0].click()
        self.assertNotEqual(metrics["cost"].text(), "—")
        self.window.clear_sub_stats()
        self.assertTrue(all(label.text() == "—" for label in metrics.values()))
        self.assertEqual(metrics["gain_on_success"].property("tone"), "muted")

    def test_budget_changes_reuse_single_analysis_and_display_dynamic_terminal_values(self):
        self.fill_five()
        self.wait_for_result()
        result = self.window._reconstruction_result
        self.window.reconstruction_budget.setValue(2)
        self.assertIs(self.window._reconstruction_result["plans"], result["plans"])
        self.assertIsNone(self.window._reconstruction_result["dynamic"])
        self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 0)
        self.wait_for_result()
        comparison = self.window._reconstruction_result["dynamic"]
        self.assertEqual(comparison["budget"], 2)
        table = self.window.reconstruction_budget_table
        for row, plan in enumerate(comparison["rows"]):
            value = plan["budget"]
            self.assertEqual(table.item(row, 2).text(), f"{plan['cost']} 枚")
            self.assertEqual(table.item(row, 3).text(), f"{value['expected_final_score']:.4g}")
            self.assertEqual(table.item(row, 4).text(), f"{value['expected_delta']:+.4g}")
            self.assertEqual(table.item(row, 5).text(), f"{value['expected_cost']:.4g}")
            self.assertEqual(table.item(row, 6).text(), f"{value['expected_attempts']:.4g}")
        self.assertEqual([table.item(row, 0).text() for row in range(table.rowCount())],
                         ["不投入", "动态择优", "1+1", "2"])
        table.cellClicked.emit(1, 1)
        self.assertEqual(self.window.reconstruction_locks, set(comparison["rows"][1]["locked_ids"]))
        table.cellClicked.emit(0, 1)
        self.assertEqual(self.window.reconstruction_locks, set())
        self.window.reconstruction_lock_buttons[0].click()
        summary = self.window.reconstruction_selected.text()
        self.window.reconstruction_budget.setValue(0)
        self.assertEqual(self.window.reconstruction_selected.text(), summary)
        self.wait_for_result()
        self.assertIn("不执行洗练", self.window.reconstruction_recommendation.text())
        self.assertEqual(self.window.reconstruction_selected.text(), summary)
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 1)
        self.assertIs(self.window._reconstruction_result["plans"], result["plans"])
        self.assertEqual([call[1] for call in ControlledReconstructionPlanner.budget_calls], [3, 2, 0])

    def test_budget_updates_only_budget_tab_and_keeps_single_selection(self):
        self.fill_five()
        self.wait_for_result()
        self.window.show()
        self.app.processEvents()
        table = self.window.reconstruction_plans_table
        table.cellClicked.emit(8, 1)
        table.setCurrentCell(8, 1)
        scrollbar = table.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum() // 2)
        self.assertGreater(scrollbar.value(), 0)
        cells = [[table.item(row, column) for column in range(table.columnCount())]
                 for row in range(table.rowCount())]
        rows = self.window._reconstruction_rows
        single = self.window._reconstruction_result["plans"]
        locks = self.window.reconstruction_locks.copy()
        metrics = {name: label.text() for name, label in self.window.reconstruction_metrics.items()}
        summary = self.window.reconstruction_selected.text()
        current = (table.currentRow(), table.currentColumn())
        scroll = scrollbar.value()
        data_changes = QSignalSpy(table.model().dataChanged)
        selection_changes = QSignalSpy(table.selectionModel().selectionChanged)
        ControlledReconstructionPlanner.budget_gate.clear()
        self.window.reconstruction_budget.setValue(2)
        self.assertEqual(table.rowCount(), 30)
        self.assertIs(self.window._reconstruction_rows, rows)
        self.assertIs(self.window._reconstruction_result["plans"], single)
        self.assertIsNone(self.window._reconstruction_result["dynamic"])
        self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 0)
        self.wait_until(lambda: len(ControlledReconstructionPlanner.budget_calls) == 2)
        for phase in ("calculating", "finished"):
            with self.subTest(phase=phase):
                self.assertIs(self.window._reconstruction_rows, rows)
                for row, items in enumerate(cells):
                    for column, item in enumerate(items):
                        self.assertIs(table.item(row, column), item)
                self.assertEqual(self.window.reconstruction_locks, locks)
                self.assertEqual({name: label.text() for name, label
                                  in self.window.reconstruction_metrics.items()}, metrics)
                self.assertEqual(self.window.reconstruction_selected.text(), summary)
                self.assertEqual((table.currentRow(), table.currentColumn()), current)
                self.assertEqual([index.row() for index in table.selectionModel().selectedRows()], [8])
                self.assertEqual(scrollbar.value(), scroll)
                self.assertEqual(self.window.reconstruction_tabs.currentIndex(), 0)
                self.assertEqual(data_changes.count(), 0)
                self.assertEqual(selection_changes.count(), 0)
            if phase == "calculating":
                ControlledReconstructionPlanner.budget_gate.set()
                self.wait_for_result()
        self.assertEqual(self.window._reconstruction_result["dynamic"]["budget"], 2)
        self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 4)
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 1)

    def test_budget_errors_keep_single_plans_usable_and_can_retry(self):
        self.fill_five()
        self.wait_for_result()
        table = self.window.reconstruction_plans_table
        single = self.window._reconstruction_result["plans"]
        cell = table.item(8, 2)
        table.cellClicked.emit(8, 1)
        summary = self.window.reconstruction_selected.text()
        failures = [
            (2, ValueError("controlled budget failure"), "controlled budget failure"),
            (1, {"budget": 1}, "频整器结果格式有误"),
        ]
        for budget, response, message in failures:
            with self.subTest(budget=budget):
                with patch.object(ControlledReconstructionPlanner, "compare_dynamic_budget") as compare:
                    if isinstance(response, Exception):
                        compare.side_effect = response
                    else:
                        compare.return_value = response
                    self.window.reconstruction_budget.setValue(budget)
                    self.wait_until(lambda: message in self.window.reconstruction_status.text())
                self.assertFalse(self.window.reconstruction_status.isHidden())
                self.assertIs(self.window._reconstruction_result["plans"], single)
                self.assertIsNone(self.window._reconstruction_result["dynamic"])
                self.assertEqual(table.rowCount(), 30)
                self.assertIs(table.item(8, 2), cell)
                self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 0)
                table.cellClicked.emit(8, 1)
                self.assertEqual(self.window.reconstruction_locks,
                                 set(self.window._reconstruction_rows[8]["locked_ids"]))
                self.assertIn("单次正提升概率", self.window.reconstruction_selected.text())
                self.assertEqual(self.window.reconstruction_selected.text(), summary)
                self.assertTrue(all(label.text() != "—"
                                    for label in self.window.reconstruction_metrics.values()))
        self.window._reconstruction_budget_changed()
        self.wait_for_result()
        self.assertEqual(self.window._reconstruction_result["dynamic"]["budget"], 1)
        self.assertIs(table.item(8, 2), cell)
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 1)

    def test_input_change_during_budget_update_invalidates_single_analysis(self):
        self.fill_five()
        self.wait_for_result()
        single = self.window._reconstruction_result["plans"]
        ControlledReconstructionPlanner.budget_gate.clear()
        self.window.reconstruction_budget.setValue(2)
        self.wait_until(lambda: len(ControlledReconstructionPlanner.budget_calls) == 2)
        self.window.sub_tier_inputs["crit_rate"].setCurrentIndex(0)
        self.assertIsNone(self.window._reconstruction_result)
        self.assertEqual(self.window.reconstruction_plans_table.rowCount(), 0)
        self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 0)
        self.assertTrue(all(label.text() == "—" for label in self.window.reconstruction_metrics.values()))
        ControlledReconstructionPlanner.budget_gate.set()
        self.wait_for_result()
        self.assertIsNot(self.window._reconstruction_result["plans"], single)
        self.assertEqual(self.window._reconstruction_result["dynamic"]["budget"], 2)
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 2)
        self.assertEqual(ControlledReconstructionPlanner.calls[-1][1], self.window.current_echo())

    def test_three_budget_paths_and_scope_are_explicit(self):
        self.fill_five()
        self.wait_for_result()
        table = self.window.reconstruction_budget_table
        self.assertEqual([table.item(row, 0).text() for row in range(table.rowCount())],
                         ["不投入", "动态择优", "1+1+1", "1+2", "2+1", "3"])
        self.assertIn("动态换锁", self.window.reconstruction_tabs.tabText(1))
        self.assertIn("每次洗出候选可保留或拒绝", self.window.reconstruction_hint.text())
        self.assertIn("不是首次超过原始评分即停", self.window.reconstruction_hint.text())
        self.assertIn("不混入未执行洗练", table.horizontalHeaderItem(4).toolTip())
        self.assertIn("拒绝后仍用原词条且照扣材料", table.horizontalHeaderItem(1).toolTip())
        for row, attempts in ((2, "3"), (3, "2"), (4, "2"), (5, "1")):
            self.assertEqual(table.item(row, 5).text(), "3")
            self.assertEqual(table.item(row, 6).text(), attempts)
        self.assertIn("动态", self.window.reconstruction_recommendation.text())
        self.assertNotIn("下次边际", "".join(table.horizontalHeaderItem(column).text()
                                             for column in range(table.columnCount())))

    def test_stale_budget_results_are_rejected_and_latest_budget_reuses_distribution(self):
        self.fill_five()
        self.wait_for_result()
        ControlledReconstructionPlanner.budget_gate.clear()
        self.window.reconstruction_budget.setValue(2)
        self.wait_until(lambda: len(ControlledReconstructionPlanner.budget_calls) == 2)
        renders = []
        display = self.window._display_reconstruction

        def record(result):
            renders.append(result["dynamic"]["budget"])
            display(result)

        self.window._display_reconstruction = record
        self.window.reconstruction_budget.setValue(1)
        self.wait_until(lambda: not self.window._reconstruction_timer.isActive())
        ControlledReconstructionPlanner.budget_gate.set()
        self.wait_for_result()
        self.assertEqual(renders, [1])
        self.assertEqual(self.window._reconstruction_result["dynamic"]["budget"], 1)
        self.assertEqual(len(ControlledReconstructionPlanner.calls), 1)
        self.assertEqual(len(ControlledReconstructionPlanner.instances), 1)
        self.assertTrue(all(call[2] != threading.get_ident() for call in ControlledReconstructionPlanner.budget_calls))

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
        comparison = self.window._reconstruction_result["dynamic"]
        self.assertTrue(all(row["budget"]["expected_cost"] == 3 for row in comparison["rows"][1:]))
        self.assertTrue(all(row["budget"]["expected_delta"] == 0 for row in comparison["rows"]))
        self.assertIn("实际使用 3 枚", self.window.reconstruction_recommendation.text())
        self.window.reconstruction_lock_buttons[0].click()
        self.assertIn("成功时平均Δ分 —", self.window.reconstruction_selected.text())
        gain = self.window.reconstruction_metrics["gain_on_success"]
        self.assertEqual(gain.text(), "—")
        self.assertEqual(gain.property("tone"), "muted")
        self.assertIn("升分期望消耗 无法获得 / ∞", self.window.reconstruction_selected.text())

    def test_dynamic_resource_error_keeps_single_analysis_unchanged_and_visible(self):
        from src.echo_reconstruction_budget import BudgetResourceError

        with patch.object(ControlledReconstructionPlanner, "compare_dynamic_budget",
                          side_effect=BudgetResourceError("controlled dynamic resource limit")):
            self.fill_five()
            self.wait_for_result()
            result = self.window._reconstruction_result
            self.assertEqual(self.window.reconstruction_plans_table.rowCount(), 30)
            self.assertEqual(self.window.reconstruction_budget_table.rowCount(), 0)
            self.assertIn("controlled dynamic resource limit", self.window.reconstruction_recommendation.text())
            before = ReconstructionPlanner(EchoScorer(self.window.stats, self.window.current_weights())).analyze(
                self.window.current_echo())
            self.assertEqual(result["plans"], before["plans"])
            self.window.reconstruction_lock_buttons[0].click()
            self.assertIn("单次正提升概率", self.window.reconstruction_selected.text())
            summary = self.window.reconstruction_selected.text()
            self.assertNotIn("动态", summary)
            cell = self.window.reconstruction_plans_table.item(0, 2)
            self.window.reconstruction_budget.setValue(2)
            self.wait_for_result()
            self.assertEqual(self.window.reconstruction_selected.text(), summary)
            self.assertIs(self.window.reconstruction_plans_table.item(0, 2), cell)
            self.assertIs(self.window._reconstruction_result["plans"], result["plans"])
            self.assertIn("controlled dynamic resource limit", self.window.reconstruction_recommendation.text())

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
        self.assertFalse(self.window.reconstruction_status.isHidden())
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
        planner = ReconstructionPlanner(EchoScorer(self.window.stats, self.window.current_weights()))
        expected = planner.analyze(self.window.current_echo())
        comparison = planner.compare_dynamic_budget(self.window.current_echo(), 3, analysis=expected)
        self.assertEqual(self.window._reconstruction_result, {**expected, "dynamic": comparison})
        self.window.reconstruction_lock_buttons[0].click()
        selected = next(plan for plan in expected["plans"] if plan["locked_ids"] == ["crit_rate"])
        budget = comparison["recommendation"]["budget"]
        self.assertIn(f"单次正提升概率 {selected['probability']:.4%}", self.window.reconstruction_selected.text())
        self.assertNotIn("期望实际消耗", self.window.reconstruction_selected.text())
        self.assertIn(f"期望实际消耗 {budget['expected_cost']:.4g} 枚", self.window.reconstruction_recommendation.text())


if __name__ == "__main__":
    unittest.main()