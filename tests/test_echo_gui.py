"""离屏创建真实 Qt 控件并验证交互，不要求桌面显示服务。"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy
import unittest

from PySide6.QtWidgets import QApplication
from src.echo_gui import EchoWindow


class EchoGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = EchoWindow()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def choose_example(self):
        self.window.slot_count.setValue(5)
        for index, stat_id in enumerate(["crit_rate", "crit_dmg", "atk_pct", "energy_regen", "basic_atk_dmg_bonus"]):
            _, stat, tier = self.window.sub_rows[index]
            stat.setCurrentIndex(stat.findData(stat_id))
            tier.setCurrentIndex(tier.count() - 1)
        self.window.main_stat.setCurrentIndex(self.window.main_stat.findData("crit_rate"))

    def test_example_live_score_and_target(self):
        self.choose_example()
        self.assertEqual(self.window.score_label.text(), "90 分")
        self.assertIn("已达目标", self.window.summary.text())
        self.window.target.setValue(100)
        self.assertIn("还差 10 分", self.window.summary.text())
        self.assertEqual(self.window.details.rowCount(), 6)
        self.window.target.setValue(0)
        self.assertEqual(self.window.progress.value(), 1000)

    def test_weight_edit_and_restore(self):
        self.choose_example()
        self.window.weight_inputs["sub_stats"]["crit_rate"].setValue(30)
        self.assertEqual(self.window.score_label.text(), "100 分")
        self.window.restore_weights()
        self.assertEqual(self.window.score_label.text(), "90 分")

    def test_cost_refreshes_main_and_disables_slots(self):
        self.choose_example()
        self.window.cost.setCurrentIndex(0)
        self.assertEqual(self.window.main_stat.count(), 3)
        self.assertNotEqual(self.window.main_stat.currentData(), "crit_rate")
        self.window.slot_count.setValue(1)
        self.assertFalse(self.window.sub_rows[1][1].isEnabled())
        self.assertEqual(len(self.window.current_echo()["sub_stats"]), 1)

    def test_all_sub_types_have_correct_tiers(self):
        _, stat, tier = self.window.sub_rows[0]
        for stat_id, data in self.window.stats["sub_stats"]["tiers"].items():
            stat.setCurrentIndex(stat.findData(stat_id))
            self.assertEqual([tier.itemData(i) for i in range(tier.count())], data["values"])

    def test_duplicate_clears_stale_result(self):
        self.choose_example()
        self.window.sub_rows[1][1].setCurrentIndex(self.window.sub_rows[1][1].findData("crit_rate"))
        self.assertEqual(self.window.score_label.text(), "—")
        self.assertIn("不能重复", self.window.summary.text())
        self.assertEqual(self.window.details.rowCount(), 0)

    def test_invalid_import_is_atomic_and_valid_roundtrips(self):
        original = self.window.current_weights()
        invalid = copy.deepcopy(original)
        invalid["main_stats"]["hp_pct"] = 99
        invalid["sub_stats"]["crit_rate"] = 2_000_000
        with self.assertRaises(ValueError):
            self.window.apply_weights(invalid)
        self.assertEqual(self.window.current_weights(), original)
        original["sub_stats"]["crit_rate"] = 12.3456
        self.window.apply_weights(original)
        self.assertEqual(self.window.current_weights(), original)


if __name__ == "__main__":
    unittest.main()