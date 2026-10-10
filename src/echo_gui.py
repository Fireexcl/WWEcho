"""声骸评分独立窗口；计算逻辑复用 echo_score。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, EchoScorer, load_json


STYLE = """
QWidget { font-size: 13px; color: #243247; }
QMainWindow, QScrollArea, QWidget#canvas { background: #f2f5fa; }
QGroupBox { background: white; border: 1px solid #dde4ef; border-radius: 10px;
            margin-top: 16px; padding: 16px 12px 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 6px; }
QLabel#title { font-size: 25px; font-weight: 700; }
QLabel#muted { color: #63758d; }
QLabel#score { font-size: 38px; font-weight: 700; color: #2563eb; }
QPushButton { background: #edf2fb; border: 1px solid #d8e2f2;
              border-radius: 6px; padding: 7px 15px; }
QPushButton:hover { background: #dce8fc; }
QPushButton#primary { background: #2563eb; color: white; border: none; }
QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #ced9e7;
                                   border-radius: 5px; padding: 5px; }
QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    background: #f1f4f8; color: #9aa7b8; border-color: #e4e9f0; }
QLabel:disabled { color: #9aa7b8; }
QTableWidget { border: 1px solid #e1e7f0; background: white; gridline-color: #eef2f7; }
QHeaderView::section { background: #f1f5fb; border: none; padding: 7px; }
QProgressBar { border: none; background: #e8eef8; border-radius: 5px; height: 10px; }
QProgressBar::chunk { background: #2563eb; border-radius: 5px; }
"""


class EchoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.stats = load_json(DEFAULT_STATS)
        self.defaults = load_json(DEFAULT_WEIGHTS)
        EchoScorer(self.stats, self.defaults)
        self.updating = True
        self.weight_inputs: dict[str, dict[str, QDoubleSpinBox]] = {}
        self.sub_rows: list[tuple[QLabel, QComboBox, QComboBox]] = []
        self.setWindowTitle("WWEcho · 声骸评分")
        self.resize(980, 940)
        self.setMinimumSize(760, 680)
        self.setStyleSheet(STYLE)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        canvas = QWidget()
        canvas.setObjectName("canvas")
        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        title = QLabel("声骸评分工作台")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("五星 +25 · 手动权重 · 实时评分（不计固定小词条）")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)
        layout.addWidget(self._weights_panel())
        layout.addWidget(self._stats_panel())
        layout.addWidget(self._results_panel())
        scroll.setWidget(canvas)
        self.setCentralWidget(scroll)
        self._refresh_main()
        self._set_slot_count()
        self.updating = False
        self.calculate()

    def _number(self, value: float = 0) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0, 1_000_000)
        box.setDecimals(4)
        box.setSingleStep(1)
        box.setValue(value)
        box.setKeyboardTracking(False)
        return box

    def _weights_panel(self) -> QGroupBox:
        group = QGroupBox("01  权重配置")
        layout = QVBoxLayout(group)
        toolbar = QHBoxLayout()
        hint = QLabel("权重 = 一个满档词条的分值；0 表示不计分。当前默认值仅为占位。")
        hint.setWordWrap(True)
        toolbar.addWidget(hint, 1)
        for text, callback in [("导入", self.import_weights), ("导出", self.export_weights),
                               ("恢复默认", self.restore_weights)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        tabs = QTabWidget()
        for category, title in [("main_stats", "主词条权重"), ("sub_stats", "副词条权重")]:
            values = self.defaults[category]
            table = QTableWidget(len(values), 2)
            table.setHorizontalHeaderLabels(["词条", "参考满档分值"])
            table.verticalHeader().hide()
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setMinimumHeight(170)
            table.setMaximumHeight(200)
            self.weight_inputs[category] = {}
            for row, (stat_id, value) in enumerate(values.items()):
                table.setItem(row, 0, QTableWidgetItem(self.stats["stat_definitions"][stat_id]["name_zh"]))
                box = self._number(value)
                box.valueChanged.connect(self.calculate)
                table.setCellWidget(row, 1, box)
                table.setRowHeight(row, 38)
                self.weight_inputs[category][stat_id] = box
            tabs.addTab(table, title)
        layout.addWidget(tabs)
        return group

    def _stats_panel(self) -> QGroupBox:
        group = QGroupBox("02  声骸词条与档位")
        grid = QGridLayout(group)
        self.cost = QComboBox()
        for cost in (1, 3, 4):
            self.cost.addItem(f"{cost} Cost", cost)
        self.cost.setCurrentIndex(2)
        self.main_stat = QComboBox()
        self.main_value = QLabel()
        self.slot_count = QSpinBox()
        self.slot_count.setRange(1, 5)
        self.slot_count.setValue(1)
        grid.addWidget(QLabel("Cost"), 0, 0)
        grid.addWidget(self.cost, 0, 1)
        grid.addWidget(QLabel("已激活副词条"), 0, 2)
        grid.addWidget(self.slot_count, 0, 3)
        grid.addWidget(QLabel("满级主词条"), 1, 0)
        grid.addWidget(self.main_stat, 1, 1)
        grid.addWidget(self.main_value, 1, 2, 1, 2)
        for index in range(5):
            label = QLabel(f"副词条 {index + 1}")
            stat = QComboBox()
            for stat_id in self.stats["sub_stats"]["tiers"]:
                stat.addItem(self.stats["stat_definitions"][stat_id]["name_zh"], stat_id)
            stat.setCurrentIndex(index)
            tier = QComboBox()
            self.sub_rows.append((label, stat, tier))
            self._refresh_tiers(index)
            stat.currentIndexChanged.connect(lambda _value, i=index: self._sub_changed(i))
            tier.currentIndexChanged.connect(self.calculate)
            grid.addWidget(label, index + 2, 0)
            grid.addWidget(stat, index + 2, 1)
            grid.addWidget(tier, index + 2, 2, 1, 2)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(3, 1)
        self.cost.currentIndexChanged.connect(self._refresh_main)
        self.main_stat.currentIndexChanged.connect(self._main_changed)
        self.slot_count.valueChanged.connect(self._set_slot_count)
        hint = QLabel("副词条只能选择实际档位；重复类型会提示错误。未激活行不参与评分。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        grid.addWidget(hint, 7, 0, 1, 4)
        return group

    def _results_panel(self) -> QGroupBox:
        group = QGroupBox("03  目标与计算结果")
        layout = QVBoxLayout(group)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("目标分数"))
        self.target = self._number(80)
        self.target.valueChanged.connect(self.calculate)
        toolbar.addWidget(self.target)
        toolbar.addStretch()
        button = QPushButton("计算评分")
        button.setObjectName("primary")
        button.clicked.connect(self.calculate)
        toolbar.addWidget(button)
        layout.addLayout(toolbar)
        row = QHBoxLayout()
        self.score_label = QLabel("—")
        self.score_label.setObjectName("score")
        row.addWidget(self.score_label)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        row.addWidget(self.summary, 1)
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.details = QTableWidget(0, 5)
        self.details.setHorizontalHeaderLabels(["分类", "词条", "数值", "权重", "贡献分"])
        self.details.verticalHeader().hide()
        self.details.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.details.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.details.setMinimumHeight(170)
        self.details.setMaximumHeight(210)
        layout.addWidget(self.details)
        hint = QLabel("目标只用于当前得分比较；不计算后续调谐概率或材料成本。分数不是伤害提升百分比。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return group

    def _format_value(self, stat_id: str, value: float) -> str:
        suffix = "%" if self.stats["stat_definitions"][stat_id]["unit"] == "percentage_points" else ""
        return f"{value:g}{suffix}"

    def _refresh_main(self) -> None:
        previous = self.main_stat.currentData()
        self.main_stat.blockSignals(True)
        self.main_stat.clear()
        pool = self.stats["main_stats"]["max_values_by_cost"][str(self.cost.currentData())]
        for stat_id in pool:
            self.main_stat.addItem(self.stats["stat_definitions"][stat_id]["name_zh"], stat_id)
        index = self.main_stat.findData(previous)
        self.main_stat.setCurrentIndex(max(0, index))
        self.main_stat.blockSignals(False)
        self._main_changed()

    def _main_changed(self) -> None:
        stat_id = self.main_stat.currentData()
        value = self.stats["main_stats"]["max_values_by_cost"][str(self.cost.currentData())][stat_id]
        self.main_value.setText(f"+25 满级值：{self._format_value(stat_id, value)}")
        self.calculate()

    def _refresh_tiers(self, index: int) -> None:
        _, stat, tier = self.sub_rows[index]
        stat_id = stat.currentData()
        tier.blockSignals(True)
        tier.clear()
        for rank, value in enumerate(self.stats["sub_stats"]["tiers"][stat_id]["values"], 1):
            tier.addItem(f"第 {rank} 档 · {self._format_value(stat_id, value)}", value)
        tier.blockSignals(False)

    def _sub_changed(self, index: int) -> None:
        self._refresh_tiers(index)
        self.calculate()

    def _set_slot_count(self) -> None:
        for index, row in enumerate(self.sub_rows):
            for widget in row:
                widget.setEnabled(index < self.slot_count.value())
        self.calculate()

    def current_weights(self) -> dict[str, Any]:
        return {"schema_version": 1, "description": "GUI 用户手动权重",
                **{category: {stat_id: box.value() for stat_id, box in inputs.items()}
                   for category, inputs in self.weight_inputs.items()}}

    def current_echo(self) -> dict[str, Any]:
        cost = self.cost.currentData()
        main = self.main_stat.currentData()
        return {"cost": cost,
                "main_stat": {"name": main, "value": self.stats["main_stats"]["max_values_by_cost"][str(cost)][main]},
                "sub_stats": [{"name": stat.currentData(), "value": tier.currentData()}
                              for _, stat, tier in self.sub_rows[:self.slot_count.value()]]}

    def calculate(self) -> None:
        if self.updating:
            return
        try:
            result = EchoScorer(self.stats, self.current_weights()).score(self.current_echo())
        except ValueError as exc:
            self.score_label.setText("—")
            self.summary.setText(f"输入有误：{exc}")
            self.details.setRowCount(0)
            self.progress.setValue(0)
            return
        score, target = result["score"], self.target.value()
        self.score_label.setText(f"{score:g} 分")
        status = f"已达目标 · 超出 {score - target:g} 分" if score >= target else f"未达目标 · 还差 {target - score:g} 分"
        self.summary.setText(f"{status}\n主词条 {result['main_score']:g} 分 + 副词条 {result['sub_score']:g} 分"
                             f" · 已激活 {result['activated_count']}/5")
        self.progress.setValue(1000 if target == 0 else min(1000, int(score / target * 1000)))
        entries = [("主词条", result["main_stat"])] + [("副词条", item) for item in result["sub_stats"]]
        self.details.setRowCount(len(entries))
        for row, (category, item) in enumerate(entries):
            values = [category, item["name"], self._format_value(item["stat_id"], item["value"]),
                      f"{item['weight']:g}", f"{item['score']:g}"]
            for col, value in enumerate(values):
                self.details.setItem(row, col, QTableWidgetItem(value))

    def apply_weights(self, weights: dict[str, Any]) -> None:
        EchoScorer(self.stats, weights)
        for category, inputs in self.weight_inputs.items():
            for stat_id, box in inputs.items():
                value = weights[category][stat_id]
                if value > box.maximum() or abs(round(value, box.decimals()) - value) > 1e-8:
                    raise ValueError("GUI 权重支持 0～1000000，最多四位小数；未导入任何权重")
        self.updating = True
        try:
            for category, inputs in self.weight_inputs.items():
                for stat_id, box in inputs.items():
                    box.setValue(weights[category][stat_id])
        finally:
            self.updating = False
        self.calculate()

    def restore_weights(self) -> None:
        self.apply_weights(self.defaults)
        self.statusBar().showMessage("已恢复占位默认权重（未覆盖任何文件）", 5000)

    def import_weights(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "导入权重预设", "", "JSON (*.json)")
        if not name:
            return
        try:
            self.apply_weights(load_json(Path(name)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self.statusBar().showMessage("已导入权重；修改立即生效", 5000)

    def export_weights(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "导出权重预设", "my-weights.json", "JSON (*.json)")
        if not name:
            return
        try:
            Path(name).write_text(json.dumps(self.current_weights(), ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.statusBar().showMessage("权重已导出；可再次导入或供命令行评分使用", 5000)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("WWEcho")
    app.setStyle("Fusion")
    try:
        window = EchoWindow()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        QMessageBox.critical(None, "启动失败", f"配置无法读取：{exc}")
        return 2
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())