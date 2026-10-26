"""声骸评分独立窗口；计算逻辑复用 echo_score。"""

from __future__ import annotations

import json
import sys
from concurrent.futures import CancelledError
from copy import deepcopy
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QFont, QResizeEvent
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, EchoScorer, load_json


STYLE = """
QWidget { font-size: 14px; color: #243247; }
QMainWindow, QScrollArea, QWidget#canvas { background: #f4f6fa; }
QScrollArea { border: none; }
QGroupBox { background: white; border: 1px solid #e0e6ef; border-radius: 14px;
            margin-top: 20px; padding: 22px 18px 16px; font-size: 16px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 20px; padding: 0 8px; color: #334766; }
QLabel { background: transparent; }
QLabel#title { font-size: 30px; font-weight: 700; color: #172b4d; }
QLabel#muted, QLabel#metricCaption { color: #65758b; font-size: 12px; }
QLabel#score { font-size: 52px; font-weight: 700; color: #2563eb;
              background: #edf3ff; border-radius: 12px; padding: 12px 20px; }
QLabel#summary { font-size: 15px; padding: 8px 12px; }
QLabel#badge { background: #edf3ff; color: #2456a6; border-radius: 8px;
              padding: 6px 12px; font-weight: 600; }
QLabel#notice { background: #f0f5ff; border: 1px solid #dae6fc;
               border-radius: 10px; padding: 12px; color: #294c80; }
QLabel#status { font-size: 12px; color: #65758b; padding: 4px 0; }
QFrame#metricCard { background: #f6f8fc; border: 1px solid #e4eaf3; border-radius: 10px; }
QFrame#divider { background: #e0e6ef; border: none; max-height: 1px; }
QLabel#metricValue { font-size: 23px; font-weight: 700; color: #2456a6; }
QLabel#metricValue[tone="positive"] { color: #15803d; }
QLabel#metricValue[tone="negative"] { color: #b91c1c; }
QLabel#metricValue[tone="muted"] { color: #7c8a9e; }
QPushButton { background: #f4f7fc; border: 1px solid #d8e2f0;
              border-radius: 8px; padding: 8px 14px; font-weight: 500; }
QPushButton:hover { background: #e8effc; border-color: #9fb9ed; }
QPushButton:pressed { background: #dce7fa; }
QPushButton:checked { background: #2563eb; color: white; border-color: #2563eb; }
QPushButton#subStat { padding: 8px 6px; }
QPushButton#clearSelection { background: #fff3e6; color: #a65316; border-color: #f0cba5; }
QPushButton#clearSelection:hover { background: #ffe6cc; border-color: #dfac77; }
QPushButton#lockStat { padding: 8px 4px; }
QPushButton:disabled { background: #f1f4f8; color: #8c99ab; border-color: #e4e9f0; }
QPushButton#primary { background: #2563eb; color: white; border-color: #2563eb; }
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #2563eb; }
QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #ced9e7;
                                   border-radius: 7px; padding: 6px 8px; min-height: 22px;
                                   selection-background-color: #2563eb; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e8effc;
                            selection-color: #1d4ed8; padding: 4px; }
QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    background: #f1f4f8; color: #9aa7b8; border-color: #e4e9f0; }
QLabel:disabled { color: #9aa7b8; }
QTabWidget::pane { border: 1px solid #e1e7f0; border-radius: 8px; background: white; }
QTabBar::tab { background: #f0f3f9; color: #65758b; padding: 9px 12px;
               margin-right: 3px; border-top-left-radius: 7px; border-top-right-radius: 7px; }
QTabBar::tab:selected { background: #e8efff; color: #2456a6; font-weight: 600; }
QTabBar::tab:hover { color: #2563eb; }
QTableWidget { border: 1px solid #e1e7f0; background: white; alternate-background-color: #f7f9fc;
               gridline-color: #eef2f7; selection-background-color: #e4edff;
               selection-color: #1e40af; border-radius: 6px; }
QHeaderView::section { background: #f0f4fa; color: #52647d; border: none;
                       border-bottom: 1px solid #e1e7f0; padding: 9px 7px; font-weight: 600; }
QProgressBar { border: none; background: #e8eef8; border-radius: 5px; height: 10px; }
QProgressBar::chunk { background: #2563eb; border-radius: 5px; }
QScrollBar:vertical { background: #f0f3f8; width: 9px; margin: 0; }
QScrollBar::handle:vertical { background: #c5cfdf; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QStatusBar { background: #f4f6fa; color: #65758b; }
"""


class ManualWeightInput(QDoubleSpinBox):
    """保留数值校验与手动编辑，禁用按键和滚轮步进。"""

    def stepBy(self, steps: int) -> None:
        pass


class ScoreProgressBar(QProgressBar):
    """固定 0～50 分刻度；目标线独立于当前分数与推荐颜色。"""

    MAX_SCORE = 50

    def __init__(self) -> None:
        super().__init__()
        self.setRange(0, 1000)
        self.setTextVisible(False)
        self.setFixedHeight(18)
        self.target_score = 0.0
        self.target_marker = QWidget(self)
        self.target_marker.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.target_marker.setStyleSheet("background: #243247; border: none;")

    def set_target_score(self, target: float) -> None:
        self.target_score = target
        self.setToolTip(f"目标分数：{target:g} 分 · 深色竖线标记目标 · 刻度 0～50 分")
        self._position_marker()

    def _position_marker(self) -> None:
        ratio = min(1.0, max(0.0, self.target_score / self.MAX_SCORE))
        self.target_marker.setGeometry(round(max(0, self.width() - 2) * ratio),
                                       0, 2, self.height())
        self.target_marker.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_marker()


class StrategySignals(QObject):
    finished = Signal(int, object, str)


class StrategyTask(QRunnable):
    """只处理输入快照，不持有窗口；缓存仅由串行后台任务访问。"""

    def __init__(self, version: int, snapshot: dict[str, Any],
                 cache: dict[str, Any], cancelled: Event) -> None:
        super().__init__()
        self.version = version
        self.snapshot = snapshot
        self.cache = cache
        self.cancelled = cancelled
        self.signals = StrategySignals()

    def run(self) -> None:
        result, error = None, ""
        try:
            if self.cancelled.is_set():
                return
            # 延迟导入，策略模块/成本文件不可用时仍可正常评分。
            from src.echo_strategy import ActivationPlanner, DEFAULT_COSTS

            data = self.snapshot
            costs = load_json(DEFAULT_COSTS)
            key = json.dumps({name: data[name] for name in ("stats", "weights", "target", "params")},
                             sort_keys=True, ensure_ascii=False) + json.dumps(costs, sort_keys=True)
            if self.cache.get("key") != key:
                planner = ActivationPlanner(EchoScorer(data["stats"], data["weights"]),
                                            costs, data["target"], **data["params"])
                self.cache.update(key=key, planner=planner)
            if not self.cancelled.is_set():
                result = self.cache["planner"].analyze(data["echo"])
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self.signals.finished.emit(self.version, result, error)


class ReconstructionTask(QRunnable):
    """频整器独立后台队列，不等待激活策略；支持输入变化时取消。"""

    def __init__(self, version: int, snapshot: dict[str, Any],
                 cache: dict[str, Any], cancelled: Event) -> None:
        super().__init__()
        self.version, self.snapshot = version, snapshot
        self.cache, self.cancelled = cache, cancelled
        self.signals = StrategySignals()

    def run(self) -> None:
        result, error = None, ""
        try:
            if self.cancelled.is_set():
                return
            from src.echo_reconstruction import ReconstructionPlanner

            data = self.snapshot
            key = json.dumps({name: data[name] for name in ("stats", "weights")},
                             sort_keys=True, ensure_ascii=False)
            if self.cache.get("key") != key:
                planner = ReconstructionPlanner(EchoScorer(data["stats"], data["weights"]))
                self.cache.update(key=key, planner=planner)
            result = self.cache["planner"].analyze(data["echo"], cancelled=self.cancelled)
        except CancelledError:
            pass
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self.signals.finished.emit(self.version, result, error)


class EchoWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.stats = load_json(DEFAULT_STATS)
        self.defaults = load_json(DEFAULT_WEIGHTS)
        default_scorer = EchoScorer(self.stats, self.defaults)
        self.defaults["main_stats"] = deepcopy(default_scorer.main_weights_by_cost)
        self.defaults["minor_stats"] = deepcopy(default_scorer.minor_weights_by_cost)
        self.updating = True
        self._closing = False
        self._strategy_version = 0
        self._strategy_pending = None
        self._strategy_task = None
        self._strategy_cache: dict[str, Any] = {}
        self._score_result: dict[str, Any] | None = None
        self._strategy_cancelled = Event()
        self._strategy_pool = QThreadPool(self)
        self._strategy_pool.setMaxThreadCount(1)
        self._strategy_timer = QTimer(self)
        self._strategy_timer.setSingleShot(True)
        self._strategy_timer.setInterval(180)
        self._strategy_timer.timeout.connect(self._start_strategy)
        self._reconstruction_version = 0
        self._reconstruction_pending = None
        self._reconstruction_task = None
        self._reconstruction_cache: dict[str, Any] = {}
        self._reconstruction_key: str | None = None
        self._reconstruction_result: dict[str, Any] | None = None
        self._reconstruction_cancelled = Event()
        self._reconstruction_pool = QThreadPool(self)
        self._reconstruction_pool.setMaxThreadCount(1)
        self._reconstruction_timer = QTimer(self)
        self._reconstruction_timer.setSingleShot(True)
        self._reconstruction_timer.setInterval(180)
        self._reconstruction_timer.timeout.connect(self._start_reconstruction)
        self.reconstruction_locks: set[str] = set()
        self.weight_inputs: dict[str, dict[str, QDoubleSpinBox]] = {}
        self.sub_stat_buttons: dict[str, QPushButton] = {}
        self.selected_sub_stats: list[str] = []
        self.sub_tier_inputs: dict[str, QComboBox] = {}
        self.strategy_metrics: dict[str, QLabel] = {}
        self.reconstruction_metrics: dict[str, QLabel] = {}
        font = QFont()
        font.setFamilies(["PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", "Segoe UI", "Arial"])
        self.setFont(font)
        self.setWindowTitle("WWEcho · 声骸评分")
        self.resize(980, 940)
        self.setMinimumSize(760, 680)
        self.setStyleSheet(STYLE)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        canvas = QWidget()
        canvas.setObjectName("canvas")
        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)
        title = QLabel("声骸评分工作台")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("五星 +25 · 手动权重 · 主词条、小词条与副词条实时评分")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)
        layout.addWidget(self._weights_panel())
        layout.addWidget(self._stats_panel())
        layout.addWidget(self._results_panel())
        layout.addWidget(self._reconstruction_panel())
        scroll.setWidget(canvas)
        self.setCentralWidget(scroll)
        self._refresh_main()
        self.updating = False
        self.calculate()

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setObjectName("divider")
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        return line

    @staticmethod
    def _metric_cards(layout: QHBoxLayout, definitions: list[tuple[str, str, str]],
                      labels: dict[str, QLabel]) -> None:
        """用明确口径的标题搭配大数字；保持纯文本，不插入外部 HTML。"""
        for key, title, hint in definitions:
            card = QFrame()
            card.setObjectName("metricCard")
            card.setToolTip(hint)
            content = QVBoxLayout(card)
            content.setContentsMargins(12, 10, 12, 10)
            content.setSpacing(4)
            caption = QLabel(title)
            caption.setObjectName("metricCaption")
            caption.setWordWrap(True)
            value = QLabel("—")
            value.setObjectName("metricValue")
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            content.addWidget(caption)
            content.addWidget(value)
            labels[key] = value
            layout.addWidget(card, 1)

    @staticmethod
    def _set_metric(label: QLabel, text: str = "—", tone: str = "muted") -> None:
        label.setText(text)
        label.setProperty("tone", tone)
        label.style().unpolish(label)
        label.style().polish(label)

    @staticmethod
    def _style_table(table: QTableWidget) -> None:
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setDefaultSectionSize(36)

    @staticmethod
    def _emphasize_item(item: QTableWidgetItem, *, tone: str = "", bold: bool = False) -> None:
        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if tone:
            item.setForeground(QColor({"positive": "#15803d", "negative": "#b91c1c",
                                      "accent": "#2456a6"}[tone]))
        if bold:
            font = item.font()
            font.setBold(True)
            item.setFont(font)

    def _number(self, value: float = 0, *, manual_only: bool = False) -> QDoubleSpinBox:
        box = ManualWeightInput() if manual_only else QDoubleSpinBox()
        if manual_only:
            box.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
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
        hint = QLabel("权重 = 每单位词条数值的系数；全局理论最高分对齐为 50 分，0 表示不计分。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        toolbar.addWidget(hint, 1)
        for text, callback in [("导入", self.import_weights), ("导出", self.export_weights),
                               ("恢复默认", self.restore_weights)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.weight_source_label = QLabel("默认-爱弥斯-企鹅bot")
        self.weight_source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.weight_source_label.setWordWrap(True)
        layout.addWidget(self.weight_source_label)
        tabs = QTabWidget()
        self.weight_tabs = tabs
        groups = [("sub_stats", "副词条权重", self.defaults["sub_stats"])] + [
            (f"main_stats_{cost}", f"Cost {cost} 主词条权重", self.defaults["main_stats"][cost])
            for cost in ("1", "3", "4")]
        groups.append(("minor_stats", "小词条权重", {
            cost: next(iter(self.defaults["minor_stats"][cost].values()))
            for cost in ("4", "3", "1")}))
        for category, title, values in groups:
            pairs_per_row = 2
            row_count = (len(values) + pairs_per_row - 1) // pairs_per_row
            table = QTableWidget(row_count, pairs_per_row * 2)
            self._style_table(table)
            table.horizontalHeader().hide()
            table.verticalHeader().hide()
            table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setFixedHeight(row_count * 44 + 12)
            self.weight_inputs[category] = {}
            for index, (stat_id, value) in enumerate(values.items()):
                row, pair = divmod(index, pairs_per_row)
                column = pair * 2
                if category == "minor_stats":
                    minor_id = next(iter(self.stats["minor_stats"]["max_values_by_cost"][stat_id]))
                    label = f"Cost {stat_id} · {self.stats['stat_definitions'][minor_id]['name_zh']}"
                else:
                    label = self.stats["stat_definitions"][stat_id]["name_zh"]
                table.setItem(row, column, QTableWidgetItem(label))
                box = self._number(value, manual_only=True)
                box.valueChanged.connect(self._weight_changed)
                table.setCellWidget(row, column + 1, box)
                table.setRowHeight(row, 44)
                self.weight_inputs[category][stat_id] = box
                if category == "minor_stats":
                    box.setToolTip("按当前 Cost 的固定满级数值计分，不占副词条槽位。")
            tabs.addTab(table, title)
        layout.addWidget(tabs)
        return group

    def _weight_changed(self) -> None:
        if self.updating:
            return
        self.weight_source_label.setText("自定义权重")
        self.calculate()

    def _stats_panel(self) -> QGroupBox:
        group = QGroupBox("02  声骸词条与档位")
        layout = QVBoxLayout(group)
        header = QGridLayout()
        self.cost = QComboBox()
        for cost in (1, 3, 4):
            self.cost.addItem(f"{cost} Cost", cost)
        self.cost.setCurrentIndex(2)
        self.main_stat = QComboBox()
        self.main_value = QLabel()
        self.main_value.setObjectName("muted")
        self.selected_count = QLabel("0 / 5")
        self.selected_count.setObjectName("badge")
        header.addWidget(QLabel("Cost"), 0, 0)
        header.addWidget(self.cost, 0, 1)
        header.addWidget(QLabel("已选副词条"), 0, 2)
        selection_toolbar = QHBoxLayout()
        selection_toolbar.addWidget(self.selected_count)
        self.clear_sub_stats_button = QPushButton("清空勾选")
        self.clear_sub_stats_button.setObjectName("clearSelection")
        self.clear_sub_stats_button.clicked.connect(self.clear_sub_stats)
        selection_toolbar.addWidget(self.clear_sub_stats_button)
        header.addLayout(selection_toolbar, 0, 3)
        header.addWidget(QLabel("满级主词条"), 1, 0)
        header.addWidget(self.main_stat, 1, 1)
        header.addWidget(self.main_value, 1, 2, 1, 2)
        self.minor_stat_label = QLabel()
        self.minor_value = QLabel()
        self.minor_value.setObjectName("muted")
        header.addWidget(QLabel("固定小词条"), 2, 0)
        header.addWidget(self.minor_stat_label, 2, 1)
        header.addWidget(self.minor_value, 2, 2, 1, 2)
        layout.addLayout(header)
        self.stats_divider = self._divider()
        layout.addWidget(self.stats_divider)

        options = QGridLayout()
        self.sub_options_layout = options
        options.setHorizontalSpacing(8)
        options.setVerticalSpacing(8)
        for index, stat_id in enumerate(self.stats["sub_stats"]["tiers"]):
            option = QWidget()
            option_layout = QHBoxLayout(option)
            option_layout.setContentsMargins(4, 2, 4, 2)
            option_layout.setSpacing(6)
            button = QPushButton(self.stats["stat_definitions"][stat_id]["name_zh"])
            button.setObjectName("subStat")
            button.setCheckable(True)
            button.setMinimumHeight(34)
            button.setMinimumWidth(88)
            button.toggled.connect(
                lambda checked, selected_id=stat_id: self._toggle_sub_stat(selected_id, checked)
            )
            tier = QComboBox()
            tier.setMinimumContentsLength(5)
            tier.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            for rank, value in enumerate(self.stats["sub_stats"]["tiers"][stat_id]["values"], 1):
                tier.addItem(f"{rank}档 · {self._format_value(stat_id, value)}", value)
            tier.setEnabled(False)
            tier.currentIndexChanged.connect(
                lambda _index, selected_id=stat_id: self._sub_tier_changed(selected_id)
            )
            option_layout.addWidget(button, 1)
            option_layout.addWidget(tier, 1)
            self.sub_tier_inputs[stat_id] = tier
            self.sub_stat_buttons[stat_id] = button
            options.addWidget(option, index // 3, index % 3)
        for column in range(3):
            options.setColumnStretch(column, 1)
        layout.addLayout(options)
        self.cost.currentIndexChanged.connect(self._refresh_main)
        self.main_stat.currentIndexChanged.connect(self._main_changed)
        hint = QLabel()
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        hint.setText("直接点击词条名选择，最多 5 项；每项档位可单独设置。再次点击可取消选择。")
        layout.addWidget(hint)
        return group

    def _results_panel(self) -> QGroupBox:
        group = QGroupBox("03  目标与计算结果")
        layout = QVBoxLayout(group)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("目标分数"))
        self.target = self._number(30)
        self.target.valueChanged.connect(self.calculate)
        toolbar.addWidget(self.target)
        self.theoretical_max_label = QLabel("理论最高分：—")
        self.theoretical_max_label.setObjectName("muted")
        self.theoretical_max_label.setWordWrap(True)
        self.theoretical_max_label.setToolTip(
            "未对齐最高分 = 所有 Cost 中最大的主词条与固定小词条原始贡献之和，"
            "加上贡献最高的 5 种不同满档副词条；对齐后最高 50 分。"
            "主词条与小词条按同一 Cost 配对，不受当前选择限制；全零权重时为 0 分。")
        toolbar.addWidget(self.theoretical_max_label, 1)
        toolbar.addStretch()
        button = QPushButton("计算评分")
        button.setObjectName("primary")
        button.clicked.connect(self.calculate)
        toolbar.addWidget(button)
        layout.addLayout(toolbar)
        row = QHBoxLayout()
        self.score_label = QLabel("—")
        self.score_label.setObjectName("score")
        self.score_label.setMinimumWidth(220)
        row.addWidget(self.score_label)
        self.summary = QLabel()
        self.summary.setObjectName("summary")
        self.summary.setWordWrap(True)
        row.addWidget(self.summary, 1)
        layout.addLayout(row)
        self.progress_status = QLabel("推荐计算中")
        layout.addWidget(self.progress_status)
        self.progress = ScoreProgressBar()
        layout.addWidget(self.progress)
        scale = QHBoxLayout()
        scale.addWidget(QLabel("0 分"))
        scale.addStretch()
        self.progress_target_label = QLabel()
        self.progress_target_label.setObjectName("muted")
        scale.addWidget(self.progress_target_label)
        scale.addStretch()
        scale.addWidget(QLabel("50 分"))
        layout.addLayout(scale)
        self.details = QTableWidget(0, 5)
        self._style_table(self.details)
        self.details.setHorizontalHeaderLabels(["分类", "词条", "数值", "权重", "贡献分"])
        self.details.verticalHeader().hide()
        self.details.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.details.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.details.setMinimumHeight(170)
        self.details.setMaximumHeight(210)
        layout.addWidget(self.details)
        self.cost_divider = self._divider()
        layout.addWidget(self.cost_divider)
        self.cost_parameters_label = QLabel("成本估算相关参数")
        layout.addWidget(self.cost_parameters_label)
        parameters = QGridLayout()
        self.cost_parameters_layout = parameters
        self.strategy_inputs: dict[str, QDoubleSpinBox] = {}
        for index, (name, text, default, maximum) in enumerate([
            ("recovery", "失败经验回收系数", .66, 1),
            ("tuning_weight", "调谐权重", 1, 1_000_000),
            ("experience_weight", "经验权重", 1, 1_000_000),
            ("embryo_weight", "胚子权重", 1, 1_000_000),
        ]):
            box = self._number(default, manual_only=True)
            box.setMaximum(maximum)
            box.valueChanged.connect(self.calculate)
            self.strategy_inputs[name] = box
            caption = QLabel(text)
            caption.setObjectName("muted")
            caption.setWordWrap(True)
            parameters.addWidget(caption, 0, index)
            parameters.addWidget(box, 1, index)
            parameters.setColumnStretch(index, 1)
        layout.addLayout(parameters)
        self.strategy_status = QLabel("策略等待计算")
        self.strategy_status.setObjectName("status")
        self.strategy_status.setWordWrap(True)
        self.strategy_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.strategy_status)
        self.zero_strategy = QLabel("—")
        self.current_strategy = QLabel("—")
        strategy_results = QHBoxLayout()
        for title, label in [("从零激活 · 反复尝试至成功", self.zero_strategy),
                             ("当前词条 · 是否继续", self.current_strategy)]:
            panel = QGroupBox(title)
            panel_layout = QVBoxLayout(panel)
            metrics = QHBoxLayout()
            definitions = ([
                ("zero_probability", "策略成功率 · 近似", "从零开始，按基准策略单次尝试成功的近似概率，不是完整激活概率。"),
                ("zero_cost", "成功期望总净成本", "反复尝试直到成功的总加权净成本；材料成本仍为占位。"),
            ] if label is self.zero_strategy else [
                ("current_probability", "完整激活概率下界", "当前声骸继续开满后的达标概率下界，不是相对基准策略成功率。"),
                ("future_cost", "后续加权净成本", "当前词条按基准策略继续的后续加权净成本，不代表成本最优。"),
            ])
            self._metric_cards(metrics, definitions, self.strategy_metrics)
            panel_layout.addLayout(metrics)
            label.setObjectName("muted")
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            panel_layout.addWidget(label)
            strategy_results.addWidget(panel, 1)
        layout.addLayout(strategy_results)
        self.result_hint = QLabel(
            "分数网格近似：当前完整激活概率下界与 0 词条基准比较；达到或超过基准且下界大于 0 才推荐继续。"
            "相对基准策略成功率（近似）与完整激活概率不是同一指标，推荐不是成本最优。"
            "材料成本为占位；回收仅作用于失败经验，调谐和胚子无回收。"
            "从零成本包含反复尝试至成功的总加权净成本；胚子期望为 1/p。"
            "分数不是伤害提升百分比。")
        self.result_hint.setObjectName("muted")
        self.result_hint.setWordWrap(True)
        layout.addWidget(self.result_hint)
        return group

    def _reconstruction_panel(self) -> QGroupBox:
        group = QGroupBox("04  频整器消耗计算")
        layout = QVBoxLayout(group)
        hint = QLabel("开满五条副词条后可用；点击下方词条锁定属性和档位，锁 1～4 条各消耗 1～4 枚频整器，"
                      "其余属性及档位全部随机。正提升指严格超过当前原始评分，与区域 3 的目标无关。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QGridLayout()
        self.reconstruction_buttons_layout = buttons
        self.reconstruction_lock_buttons: list[QPushButton] = []
        for index in range(5):
            button = QPushButton(f"词条 {index + 1}\n待开满")
            button.setObjectName("lockStat")
            button.setCheckable(True)
            button.setEnabled(False)
            button.setMinimumHeight(54)
            button.toggled.connect(
                lambda checked, position=index: self._toggle_reconstruction_lock(position, checked))
            self.reconstruction_lock_buttons.append(button)
            buttons.addWidget(button, 0, index)
        for column in range(5):
            buttons.setColumnStretch(column, 1)
        layout.addLayout(buttons)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("比较预算"))
        self.reconstruction_budget = QSpinBox()
        self.reconstruction_budget.setRange(0, 10000)
        self.reconstruction_budget.setValue(4)
        self.reconstruction_budget.setSuffix(" 枚")
        self.reconstruction_budget.valueChanged.connect(self._reconstruction_budget_changed)
        toolbar.addWidget(self.reconstruction_budget)
        self.clear_reconstruction_locks_button = QPushButton("清空锁定")
        self.clear_reconstruction_locks_button.clicked.connect(self.clear_reconstruction_locks)
        toolbar.addWidget(self.clear_reconstruction_locks_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.reconstruction_status = QLabel("请先在区域 2 开满五条副词条")
        self.reconstruction_status.setObjectName("status")
        self.reconstruction_selected = QLabel("勾选 1～4 个词条，查看当前锁法。")
        self.reconstruction_recommendation = QLabel("—")
        self.reconstruction_recommendation.setObjectName("notice")
        for label in (self.reconstruction_status, self.reconstruction_selected,
                      self.reconstruction_recommendation):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            layout.addWidget(label)
        metrics = QHBoxLayout()
        self._metric_cards(metrics, [
            ("probability", "当前锁法 · 单次升分概率", "严格超过当前原始评分的概率，与目标分数无关。"),
            ("delta", "当前锁法 · 单次期望 Δ 分", "包含降分风险的期望分数变化，不是保证升分。"),
            ("cost", "当前锁法 · 单次消耗", "每次消耗的频整器枚数等于锁定的副词条数。"),
        ], self.reconstruction_metrics)
        layout.addLayout(metrics)
        tabs = QTabWidget()
        self.reconstruction_tabs = tabs

        def table(headers: list[str]) -> QTableWidget:
            widget = QTableWidget(0, len(headers))
            self._style_table(widget)
            widget.setHorizontalHeaderLabels(headers)
            widget.verticalHeader().hide()
            widget.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            widget.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            widget.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
            widget.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            widget.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            widget.horizontalHeader().setMinimumSectionSize(80)
            widget.setMinimumHeight(270)
            widget.setMaximumHeight(330)
            return widget

        self.reconstruction_plans_table = table([
            "锁数", "锁定词条与档位", "正提升概率", "期望Δ分", "成功时平均Δ分",
            "消耗 / 枚", "正收益 / 枚", "升分期望消耗"])
        self.reconstruction_plans_table.cellClicked.connect(self._apply_reconstruction_plan)
        self.reconstruction_budget_table = table([
            "锁数", "锁定词条与档位", "最多次数 / 消耗", "预算内升分概率", "终态期望Δ分",
            "期望消耗", "净升分 / 枚", "下次边际Δ分", "下次边际消耗"])
        self.reconstruction_budget_table.cellClicked.connect(self._apply_reconstruction_budget_plan)
        tabs.addTab(self.reconstruction_plans_table, "单次 · 全部 30 种锁法（点击套用）")
        tabs.addTab(self.reconstruction_budget_table, "同预算 · 锁 1～4 条择优与边际收益")
        layout.addWidget(tabs)
        self.reconstruction_hint = QLabel(
            "单次：期望Δ分含降分风险；正收益 = E[max(Δ分, 0)]，不是保底收益；"
            "成功时平均Δ分为正提升条件均值；升分期望消耗 = 锁数 / 单次升分概率。\n"
            "同预算：固定原始锁法，独立重复；首次超过原始评分即停，否则保留最后一次结果（不假设可撤销）。"
            "各锁数按终态期望Δ分择优；性价比 = 终态期望Δ分 / 期望实际消耗。"
            "下次边际为在当前最多次数上再加洗一次的无条件增量，并非单次收益简单相加。\n"
            "概率复用归一化官方公示档位，类型不放回；档位和跨次独立为模型约定。"
            "不含每次看结果后重选锁法的全局最优策略、额外材料或货币；分数不代表伤害提升百分比。")
        self.reconstruction_hint.setObjectName("muted")
        self.reconstruction_hint.setWordWrap(True)
        layout.addWidget(self.reconstruction_hint)
        return group

    def _sync_reconstruction_buttons(self) -> None:
        self.reconstruction_locks.intersection_update(self.selected_sub_stats)
        full = len(self.selected_sub_stats) == 5
        for index, button in enumerate(self.reconstruction_lock_buttons):
            button.blockSignals(True)
            if index < len(self.selected_sub_stats):
                name = self.selected_sub_stats[index]
                tier = self.sub_tier_inputs[name]
                text = (f"{self.stats['stat_definitions'][name]['name_zh']}\n"
                        f"{tier.currentIndex() + 1}档 · {self._format_value(name, tier.currentData())}")
                button.setText(text)
                button.setChecked(name in self.reconstruction_locks)
            else:
                button.setText(f"词条 {index + 1}\n待开满")
                button.setChecked(False)
            button.setEnabled(full and self._score_result is not None)
            button.blockSignals(False)

    def _toggle_reconstruction_lock(self, index: int, checked: bool) -> None:
        if index >= len(self.selected_sub_stats):
            return
        name = self.selected_sub_stats[index]
        if checked and name not in self.reconstruction_locks and len(self.reconstruction_locks) >= 4:
            button = self.reconstruction_lock_buttons[index]
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
            self.statusBar().showMessage("频整器最多锁定 4 个副词条，至少保留 1 条可重构", 4000)
            return
        if checked:
            self.reconstruction_locks.add(name)
        else:
            self.reconstruction_locks.discard(name)
        self._display_reconstruction_selection()

    def clear_reconstruction_locks(self) -> None:
        self.reconstruction_locks.clear()
        self._sync_reconstruction_buttons()
        self._display_reconstruction_selection()

    def _invalidate_reconstruction(self, message: str) -> None:
        self._reconstruction_version += 1
        self._reconstruction_cancelled.set()
        self._reconstruction_timer.stop()
        self._reconstruction_pending = None
        self._reconstruction_key = None
        self._reconstruction_result = None
        self.reconstruction_status.setText(message)
        self.reconstruction_selected.setText("勾选 1～4 个词条，查看当前锁法。")
        self.reconstruction_recommendation.setText("—")
        for label in self.reconstruction_metrics.values():
            self._set_metric(label)
        self.reconstruction_plans_table.setRowCount(0)
        self.reconstruction_budget_table.setRowCount(0)
        self._sync_reconstruction_buttons()

    def _queue_reconstruction(self) -> None:
        self._sync_reconstruction_buttons()
        if len(self.selected_sub_stats) != 5:
            self._invalidate_reconstruction("请先在区域 2 开满五条副词条")
            return
        data = {"stats": self.stats, "weights": self.current_weights(), "echo": self.current_echo()}
        key = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if key == self._reconstruction_key:
            return
        self._invalidate_reconstruction("频整器等待后台计算（评分仍可编辑）")
        self._reconstruction_key = key
        self._reconstruction_pending = (self._reconstruction_version, deepcopy(data))
        self._reconstruction_timer.start()

    @Slot()
    def _start_reconstruction(self) -> None:
        if self._closing or self._reconstruction_task is not None or self._reconstruction_pending is None:
            return
        version, snapshot = self._reconstruction_pending
        self._reconstruction_pending = None
        self._reconstruction_cancelled = Event()
        task = ReconstructionTask(version, snapshot, self._reconstruction_cache,
                                  self._reconstruction_cancelled)
        task.signals.finished.connect(self._reconstruction_finished, Qt.ConnectionType.QueuedConnection)
        self._reconstruction_task = task
        self.reconstruction_status.setText("频整器后台计算中（评分仍可编辑）")
        self._reconstruction_pool.start(task)

    @Slot(int, object, str)
    def _reconstruction_finished(self, version: int, result: Any, error: str) -> None:
        if self._closing:
            return
        self._reconstruction_task = None
        if version == self._reconstruction_version:
            if error:
                self._reconstruction_key = None
                self.reconstruction_status.setText(f"频整器计算失败（不影响评分）：{error}")
            elif result is not None:
                try:
                    self._display_reconstruction(result)
                except (KeyError, TypeError, ValueError) as exc:
                    self._reconstruction_result = None
                    self.reconstruction_status.setText(f"频整器结果格式有误：{exc}")
        if self._reconstruction_pending is not None and not self._reconstruction_timer.isActive():
            self._start_reconstruction()

    def _locked_description(self, plan: dict[str, Any], separator: str = "、") -> str:
        return separator.join(
            f"{item['name']} {self.stats['sub_stats']['tiers'][item['stat_id']]['values'].index(item['value']) + 1}档"
            f" · {self._format_value(item['stat_id'], item['value'])}"
            for item in plan["locked_stats"])

    @staticmethod
    def _reconstruction_amount(value: Any, *, signed: bool = False) -> str:
        if value is None:
            return "—"
        return f"{value:+.4g}" if signed else f"{value:.4g}"

    def _display_reconstruction(self, result: dict[str, Any]) -> None:
        from src.echo_reconstruction import compare_budget

        comparison = compare_budget(result["plans"], self.reconstruction_budget.value())
        # 先完成所有计算，再提交，防止错误响应留下旧输入的部分表格。
        plans = sorted(result["plans"], key=lambda plan: (plan["locked_count"], -plan["expected_delta"]))
        rows = []
        amount = self._reconstruction_amount
        for plan in plans:
            rows.append([
                str(plan["locked_count"]), self._locked_description(plan, "\n"), f"{plan['probability']:.4%}",
                amount(plan["expected_delta"], signed=True), amount(plan["gain_on_success"]), str(plan["cost"]),
                amount(plan["positive_gain_per_transducer"]),
                amount(plan["expected_cost_to_improve"]) if plan["expected_cost_to_improve"] is not None
                else "无法获得 / ∞"])
        budget_rows = []
        for plan in comparison["best_by_count"]:
            value = plan["budget"]
            affordable = value["max_attempts"] > 0
            budget_rows.append([
                str(plan["locked_count"]), self._locked_description(plan, "\n") if affordable else "预算不足",
                f"{value['max_attempts']} 次 / {value['max_cost']} 枚",
                f"{value['success_probability']:.4%}" if affordable else "—",
                amount(value["expected_delta"], signed=True) if affordable else "—",
                amount(value["expected_cost"]) if affordable else "—",
                amount(value["net_gain_per_transducer"], signed=True) if affordable else "—",
                amount(value["next_marginal_delta"], signed=True) if affordable else "—",
                amount(value["next_marginal_cost"]) if affordable else "—"])
        for widget, values in ((self.reconstruction_plans_table, rows),
                               (self.reconstruction_budget_table, budget_rows)):
            widget.setRowCount(len(values))
            for row, texts in enumerate(values):
                for column, text in enumerate(texts):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    if column != 1:
                        delta_column = 3 if widget is self.reconstruction_plans_table else 4
                        if column == delta_column and text != "—":
                            delta = float(text)
                            self._emphasize_item(item, tone="positive" if delta > 0 else
                                                 "negative" if delta < 0 else "", bold=True)
                        else:
                            self._emphasize_item(item, tone="accent" if column in (2, 3) else "",
                                                 bold=column == (2 if widget is self.reconstruction_plans_table else 3))
                    widget.setItem(row, column, item)
            widget.resizeRowsToContents()
        self._reconstruction_rows = plans
        self._reconstruction_budget_rows = comparison["best_by_count"]
        self._reconstruction_result = result
        self.reconstruction_status.setText(f"频整器已更新 · {len(plans)} 种锁法 · {result['method']}")
        recommendation = comparison["recommendation"]
        if recommendation is None:
            text = "同预算建议：暂不投入。可执行的固定锁法终态期望Δ分均不为正，或当前预算不足。"
        else:
            value = recommendation["budget"]
            text = (f"同预算建议（固定锁法内期望升分最高）：锁 {recommendation['locked_count']} 条，"
                    f"最多 {value['max_attempts']} 次 / {value['max_cost']} 枚；"
                    f"升分概率 {value['success_probability']:.4%}，终态期望Δ分 "
                    f"{amount(value['expected_delta'], signed=True)}，期望消耗 {amount(value['expected_cost'])} 枚。\n"
                    f"锁法：{self._locked_description(recommendation)}")
        self.reconstruction_recommendation.setText(text)
        self._display_reconstruction_selection()

    def _display_reconstruction_selection(self) -> None:
        from src.echo_reconstruction import evaluate_budget

        self.reconstruction_plans_table.clearSelection()
        for label in self.reconstruction_metrics.values():
            self._set_metric(label)
        if not self.reconstruction_locks:
            self.reconstruction_selected.setText("尚未锁定：请勾选 1～4 个词条；全部锁法与同预算比较见下表。")
            return
        if self._reconstruction_result is None:
            self.reconstruction_selected.setText("当前锁法等待有效的五词条计算结果。")
            return
        plan = next(plan for plan in self._reconstruction_result["plans"]
                    if set(plan["locked_ids"]) == self.reconstruction_locks)
        value = evaluate_budget(plan, self.reconstruction_budget.value())
        amount = self._reconstruction_amount
        self._set_metric(self.reconstruction_metrics["probability"], f"{plan['probability']:.4%}", "accent")
        delta = plan["expected_delta"]
        self._set_metric(self.reconstruction_metrics["delta"], amount(delta, signed=True),
                 "positive" if delta > 0 else "negative" if delta < 0 else "muted")
        self._set_metric(self.reconstruction_metrics["cost"], f"{plan['cost']} 枚", "accent")
        success_cost = (amount(plan["expected_cost_to_improve"])
                if plan["expected_cost_to_improve"] is not None else "无法获得 / ∞")
        self.reconstruction_selected.setText(
            f"当前锁 {plan['locked_count']} 条 / 洗 {plan['rerolled_count']} 条：{self._locked_description(plan)}\n"
            f"单次正提升概率 {plan['probability']:.4%} · 消耗 {plan['cost']} 枚 · "
            f"期望Δ分 {amount(plan['expected_delta'], signed=True)} · "
            f"成功时平均Δ分 {amount(plan['gain_on_success'])}\n"
            f"正收益/枚 {amount(plan['positive_gain_per_transducer'])} · "
            f"升分期望消耗 {success_cost} 枚\n"
            f"当前预算最多 {value['max_attempts']} 次 / {value['max_cost']} 枚 · "
            f"升分概率 {value['success_probability']:.4%} · 终态期望Δ分 "
            f"{amount(value['expected_delta'], signed=True)} · 期望实际消耗 {amount(value['expected_cost'])} 枚")
        for row, item in enumerate(self._reconstruction_rows):
            if set(item["locked_ids"]) == self.reconstruction_locks:
                self.reconstruction_plans_table.selectRow(row)
                break

    def _reconstruction_budget_changed(self) -> None:
        if self._reconstruction_result is not None:
            self._display_reconstruction(self._reconstruction_result)

    def _apply_reconstruction_plan(self, row: int, _column: int) -> None:
        if self._reconstruction_result is None:
            return
        self.reconstruction_locks = set(self._reconstruction_rows[row]["locked_ids"])
        self._sync_reconstruction_buttons()
        self._display_reconstruction_selection()

    def _apply_reconstruction_budget_plan(self, row: int, _column: int) -> None:
        if self._reconstruction_result is None:
            return
        plan = self._reconstruction_budget_rows[row]
        if not plan["budget"]["max_attempts"]:
            return
        self.reconstruction_locks = set(plan["locked_ids"])
        self._sync_reconstruction_buttons()
        self._display_reconstruction_selection()

    def _format_value(self, stat_id: str, value: float) -> str:
        suffix = "%" if self.stats["stat_definitions"][stat_id]["unit"] == "percentage_points" else ""
        return f"{value:g}{suffix}"

    def _refresh_main(self) -> None:
        self.main_stat.blockSignals(True)
        self.main_stat.clear()
        pool = self.stats["main_stats"]["max_values_by_cost"][str(self.cost.currentData())]
        for stat_id in pool:
            self.main_stat.addItem(self.stats["stat_definitions"][stat_id]["name_zh"], stat_id)
        default = "crit_rate" if self.cost.currentData() == 4 else "atk_pct"
        index = self.main_stat.findData(default)
        self.main_stat.setCurrentIndex(max(0, index))
        self.main_stat.blockSignals(False)
        self._main_changed()

    def _main_changed(self) -> None:
        stat_id = self.main_stat.currentData()
        value = self.stats["main_stats"]["max_values_by_cost"][str(self.cost.currentData())][stat_id]
        self.main_value.setText(f"+25 满级值：{self._format_value(stat_id, value)}")
        minor_id, minor_value = next(iter(
            self.stats["minor_stats"]["max_values_by_cost"][str(self.cost.currentData())].items()))
        self.minor_stat_label.setText(self.stats["stat_definitions"][minor_id]["name_zh"])
        self.minor_value.setText(f"+25 固定值：{self._format_value(minor_id, minor_value)}")
        self.calculate()

    def _toggle_sub_stat(self, stat_id: str, checked: bool) -> None:
        if checked:
            if len(self.selected_sub_stats) >= 5:
                button = self.sub_stat_buttons[stat_id]
                button.blockSignals(True)
                button.setChecked(False)
                button.blockSignals(False)
                self.statusBar().showMessage("最多选择 5 个副词条", 3000)
                return
            self.selected_sub_stats.append(stat_id)
            self.sub_tier_inputs[stat_id].setEnabled(True)
            self.selected_count.setText(f"{len(self.selected_sub_stats)} / 5")
            if not self.updating:
                QTimer.singleShot(0, self.sub_tier_inputs[stat_id].showPopup)
        else:
            if stat_id in self.selected_sub_stats:
                self.selected_sub_stats.remove(stat_id)
            self.sub_tier_inputs[stat_id].setEnabled(False)
            self.selected_count.setText(f"{len(self.selected_sub_stats)} / 5")
        self.calculate()

    def _sub_tier_changed(self, stat_id: str) -> None:
        if self.updating:
            return
        if stat_id in self.selected_sub_stats:
            self.calculate()

    def clear_sub_stats(self) -> None:
        for button in self.sub_stat_buttons.values():
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
        for tier in self.sub_tier_inputs.values():
            tier.setEnabled(False)
        self.selected_sub_stats.clear()
        self.selected_count.setText("0 / 5")
        self.calculate()

    def current_weights(self) -> dict[str, Any]:
        return {"schema_version": 1, "description": "GUI 用户手动权重",
                "sub_stats": {stat_id: box.value() for stat_id, box in self.weight_inputs["sub_stats"].items()},
                "main_stats": {cost: {stat_id: box.value() for stat_id, box in
                                      self.weight_inputs[f"main_stats_{cost}"].items()}
                           for cost in ("1", "3", "4")},
                  "minor_stats": {cost: {next(iter(self.stats["minor_stats"]["max_values_by_cost"][cost])): box.value()}
                            for cost, box in self.weight_inputs["minor_stats"].items()}}

    def current_echo(self) -> dict[str, Any]:
        cost = self.cost.currentData()
        main = self.main_stat.currentData()
        return {"cost": cost,
                "main_stat": {"name": main, "value": self.stats["main_stats"]["max_values_by_cost"][str(cost)][main]},
                "sub_stats": [{"name": stat_id, "value": self.sub_tier_inputs[stat_id].currentData()}
                              for stat_id in self.selected_sub_stats]}

    def calculate(self) -> None:
        if self.updating or self._closing:
            return
        weights = self.current_weights()
        # 所有输入变化（包括无效输入）立即使旧版本失效。
        self._strategy_version += 1
        self._strategy_pending = None
        self._strategy_timer.stop()
        self.zero_strategy.setText("—")
        self.current_strategy.setText("—")
        for label in self.strategy_metrics.values():
            self._set_metric(label)
        self._score_result = None
        self.progress.set_target_score(self.target.value())
        self.progress_target_label.setText(f"│ 目标 {self.target.value():g} 分")
        try:
            scorer = EchoScorer(self.stats, weights)
            self.theoretical_max_label.setText(
                f"理论最高分：{scorer.aligned_max_score:g} 分"
                f"（未对齐：{scorer.unaligned_max_score:g}）")
            result = scorer.score(self.current_echo())
        except ValueError as exc:
            self.theoretical_max_label.setText("理论最高分：—")
            self.score_label.setText("—")
            self.summary.setText(f"输入有误：{exc}")
            self.details.setRowCount(0)
            self.progress.setValue(0)
            self._update_progress_status(unavailable="输入有误")
            self.strategy_status.setText("策略输入无效")
            self._invalidate_reconstruction("频整器输入无效")
            return
        self._score_result = result
        score, target = result["score"], self.target.value()
        self.score_label.setText(f"{score:g} 分")
        status = f"已达目标 · 超出 {score - target:g} 分" if score >= target else f"未达目标 · 还差 {target - score:g} 分"
        self.summary.setText(f"{status}\n主词条 {result['main_score']:g} 分 + 副词条 {result['sub_score']:g} 分"
                             f" + 小词条 {result['minor_score']:g} 分"
                             f" · 已激活 {result['activated_count']}/5")
        self.progress.setValue(min(1000, int(score / self.progress.MAX_SCORE * 1000)))
        self._update_progress_status()
        entries = [("主词条", result["main_stat"]), ("小词条", result["minor_stat"])] + [("副词条", item) for item in result["sub_stats"]]
        self.details.setRowCount(len(entries))
        for row, (category, item) in enumerate(entries):
            values = [category, item["name"], self._format_value(item["stat_id"], item["value"]),
                      f"{item['weight']:g}", f"{item['score']:g}"]
            for col, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if col >= 2:
                    self._emphasize_item(cell, tone="accent" if col == 4 else "", bold=col == 4)
                self.details.setItem(row, col, cell)
        self._queue_reconstruction()
        params = {name: box.value() for name, box in self.strategy_inputs.items()}
        params["bins"] = 1000
        self._strategy_pending = (self._strategy_version, deepcopy({
            "stats": self.stats, "weights": self.current_weights(),
            "target": target, "params": params, "echo": self.current_echo(),
        }))
        self.strategy_status.setText("策略等待后台计算（评分已更新）")
        self._strategy_timer.start()

    def _update_progress_status(self, current: dict[str, Any] | None = None,
                                *, unavailable: str = "推荐计算中") -> None:
        result = self._score_result
        if result is not None and result["score"] >= self.target.value():
            text, color = "达成目标", "#16a34a"
        elif result is not None and result["activated_count"] == 5:
            text, color = "下次好运", "#dc2626"
        elif result is not None and current is not None:
            text, color = (("推荐继续", "#16a34a") if current["recommend_continue"]
                           else ("推荐放弃", "#dc2626"))
        else:
            text, color = unavailable, "#94a3b8"
        self.progress_status.setText(text)
        self.progress_status.setStyleSheet(
            f"color: {color}; font-size: 16px; font-weight: 700; padding: 5px 0;")
        self.progress.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}")

    @Slot()
    def _start_strategy(self) -> None:
        if self._closing or self._strategy_task is not None or self._strategy_pending is None:
            return
        version, snapshot = self._strategy_pending
        self._strategy_pending = None
        task = StrategyTask(version, snapshot, self._strategy_cache, self._strategy_cancelled)
        task.signals.finished.connect(self._strategy_finished, Qt.ConnectionType.QueuedConnection)
        self._strategy_task = task
        self.strategy_status.setText("策略后台计算中（评分仍可编辑）")
        self._strategy_pool.start(task)

    @Slot(int, object, str)
    def _strategy_finished(self, version: int, result: Any, error: str) -> None:
        if self._closing:
            return
        self._strategy_task = None
        if version == self._strategy_version:
            if error:
                self._update_progress_status(unavailable="推荐暂不可用")
                self.strategy_status.setText(f"策略计算失败（不影响评分）：{error}")
            elif result is not None:
                try:
                    self._display_strategy(result)
                except (KeyError, TypeError, ValueError) as exc:
                    self._update_progress_status(unavailable="推荐暂不可用")
                    self.strategy_status.setText(f"策略结果格式有误：{exc}")
        # 防抖已到期则消费最新快照；否则继续等定时器，不排队旧输入。
        if self._strategy_pending is not None and not self._strategy_timer.isActive():
            self._start_strategy()

    def _display_strategy(self, result: dict[str, Any]) -> None:
        def interval(item: dict[str, Any]) -> str:
                return (f"完整激活概率区间：{item['full_success_lower']:.2%} ～ "
                    f"{item['full_success_upper']:.2%}\n"
                    f"相对基准策略成功率（近似）：{item['success_probability']:.2%}")

        def amount(value: Any) -> str:
            return "无法获得 / ∞" if value is None else f"{value:,.4g}"

        zero, current = result["zero"], result["current"]
        zero_text = (interval(zero) + f"\n单次尝试加权净成本：{amount(zero['attempt_cost'])}"
                     f"\n成功期望总加权净成本：{amount(zero['expected_cost'])}"
                     f"\n期望胚子：{amount(zero['expected_embryos'])}"
                     f" · 调谐：{amount(zero['expected_tuning'])}"
                     f" · 净经验：{amount(zero['expected_experience'])}")
        recommendation = "建议继续" if current["recommend_continue"] else "建议停止"
        current_text = (interval(current)
                + f"\n0 词条达标概率下界基准：{current['baseline_success_lower']:.2%}"
                + f"\n{recommendation}（与同类新胚子比较，非成本最优）"
                        f"\n原因：{current['reason']}"
                        f"\n后续加权净成本：{amount(current['future_cost'])}")
        status = f"策略已更新 · 分数网格近似步长 {result['score_resolution']:g} · {result['method']}"
        self.zero_strategy.setText(zero_text)
        self.current_strategy.setText(current_text)
        self.strategy_status.setText(status)
        self._set_metric(self.strategy_metrics["zero_probability"], f"{zero['success_probability']:.2%}", "accent")
        cost = zero["expected_cost"]
        self._set_metric(self.strategy_metrics["zero_cost"], "不可达 / ∞" if cost is None else amount(cost),
                 "muted" if cost is None else "accent")
        self._set_metric(self.strategy_metrics["current_probability"], f"{current['full_success_lower']:.2%}", "accent")
        cost = current["future_cost"]
        self._set_metric(self.strategy_metrics["future_cost"], "不可达 / ∞" if cost is None else amount(cost),
                 "muted" if cost is None else "accent")
        self._update_progress_status(current)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._strategy_version += 1
        self._strategy_timer.stop()
        self._strategy_pending = None
        self._strategy_cancelled.set()
        if self._strategy_task is not None:
            self._strategy_task.signals.finished.disconnect(self._strategy_finished)
        # analyze 无中断 API，关闭时等待唯一在途任务退出，不强杀线程。
        # 控件销毁前回收线程池，排队结果仍由 _closing 拒绝。
        self._strategy_pool.waitForDone()
        self._strategy_task = None
        self._strategy_cache.clear()
        self._reconstruction_version += 1
        self._reconstruction_timer.stop()
        self._reconstruction_pending = None
        self._reconstruction_cancelled.set()
        if self._reconstruction_task is not None:
            self._reconstruction_task.signals.finished.disconnect(self._reconstruction_finished)
        self._reconstruction_pool.waitForDone()
        self._reconstruction_task = None
        self._reconstruction_cache.clear()
        super().closeEvent(event)

    def apply_weights(self, weights: dict[str, Any]) -> None:
        scorer = EchoScorer(self.stats, weights)
        values = {"sub_stats": scorer.weights["sub_stats"],
                  "minor_stats": {cost: next(iter(pool.values()))
                                  for cost, pool in scorer.minor_weights_by_cost.items()},
                  **{f"main_stats_{cost}": pool for cost, pool in scorer.main_weights_by_cost.items()}}
        for category, inputs in self.weight_inputs.items():
            for stat_id, box in inputs.items():
                value = values[category][stat_id]
                if value > box.maximum() or abs(round(value, box.decimals()) - value) > 1e-8:
                    raise ValueError("GUI 权重支持 0～1000000，最多四位小数；未导入任何权重")
        self.updating = True
        try:
            for category, inputs in self.weight_inputs.items():
                for stat_id, box in inputs.items():
                    box.setValue(values[category][stat_id])
        finally:
            self.updating = False
        self.weight_source_label.setText("自定义权重")
        self.calculate()

    def restore_weights(self) -> None:
        self.apply_weights(self.defaults)
        self.weight_source_label.setText("默认-爱弥斯-企鹅bot")
        self.statusBar().showMessage("已恢复默认权重（未覆盖任何文件）", 5000)

    def import_weights(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "导入权重预设", "", "JSON (*.json)")
        if not name:
            return
        try:
            self.apply_weights(load_json(Path(name)))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self.weight_source_label.setText(f"导入-{Path(name).name}")
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