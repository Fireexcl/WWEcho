"""声骸评分独立窗口；计算逻辑复用 echo_score。"""

from __future__ import annotations

import json
import math
import sys
from concurrent.futures import CancelledError
from copy import deepcopy
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRunnable, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QCloseEvent, QFont, QResizeEvent
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLayout, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QTabWidget, QToolButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from src.echo_costs import DEFAULT_COSTS, activation_defaults
from src.echo_score import DEFAULT_STATS, DEFAULT_WEIGHTS, EchoScorer, load_json


STYLE = """
QWidget { font-size: 14px; color: #243247; }
QMainWindow, QScrollArea, QWidget#canvas { background: #f4f6fa; }
QScrollArea { border: none; }
QGroupBox { background: white; border: 1px solid #e0e6ef; border-radius: 14px;
            margin-top: 20px; padding: 22px 18px 16px; font-size: 16px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 20px; padding: 0 8px; color: #334766; }
QFrame#collapsibleSection { background: white; border: 1px solid #e0e6ef; border-radius: 14px; }
QGroupBox#sectionContent { border: none; margin-top: 0; padding: 0 18px 16px; }
QPushButton#sectionToggle { background: transparent; border: none; border-radius: 10px;
    text-align: left; padding: 14px 20px; font-size: 16px; font-weight: 600; color: #334766; }
QPushButton#sectionToggle:hover { background: #edf3ff; }
QPushButton#sectionToggle:focus { border: 1px solid #9fb9ed; }
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


class CollapsibleSection(QFrame):
    """仅切换内容可见性，保留控件、输入与后台任务生命周期。"""

    def __init__(self, content: QGroupBox) -> None:
        super().__init__()
        self.setObjectName("collapsibleSection")
        self.section_title = content.title()
        self.content = content
        content.setTitle("")
        content.setObjectName("sectionContent")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        self.toggle_button = QPushButton()
        self.toggle_button.setObjectName("sectionToggle")
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(True)
        self.toggle_button.setAccessibleName(self.section_title)
        self.toggle_button.toggled.connect(self._set_expanded)
        layout.addWidget(self.toggle_button)
        layout.addWidget(content)
        self._set_expanded(True)

    def _set_expanded(self, expanded: bool) -> None:
        self.toggle_button.setText(f"{'▼' if expanded else '▶'}  {self.section_title}")
        self.toggle_button.setToolTip("点击折叠此区域" if expanded else "点击展开此区域")
        self.content.setVisible(expanded)
        self.updateGeometry()


class InfoButton(QToolButton):
    """快速悬停说明；点击固定，再次点击或按 Escape 收起。"""

    def __init__(self) -> None:
        super().__init__()
        self.setCheckable(True)
        self.popup = QFrame(self, Qt.WindowType.ToolTip)
        self.popup.setStyleSheet(
            "QFrame { background: #f6f8fc; border: 1px solid #9aa9bd; border-radius: 6px; }"
            "QLabel { color: #243247; border: none; font-size: 12px; }")
        layout = QVBoxLayout(self.popup)
        layout.setContentsMargins(12, 10, 12, 10)
        self.help_label = QLabel()
        self.help_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.help_label)
        self.hover_timer = QTimer(self)
        self.hover_timer.setSingleShot(True)
        self.hover_timer.setInterval(150)
        self.hover_timer.timeout.connect(self._show_help)
        self.toggled.connect(self._set_pinned)

    def _show_help(self) -> None:
        self.help_label.setText(self.toolTip())
        self.popup.adjustSize()
        position = self.mapToGlobal(QPoint(0, self.height() + 4))
        screen = self.screen().availableGeometry()
        position.setX(max(screen.left(), min(position.x(), screen.right() - self.popup.width() + 1)))
        if position.y() + self.popup.height() > screen.bottom() + 1:
            position.setY(self.mapToGlobal(QPoint(0, 0)).y() - self.popup.height() - 4)
        self.popup.move(position)
        self.popup.show()

    def _set_pinned(self, pinned: bool) -> None:
        self.hover_timer.stop()
        if pinned:
            self._show_help()
        else:
            self.popup.hide()

    def event(self, event: QEvent) -> bool:
        # 保留 toolTip 文本供辅助功能读取，但不再触发系统的慢速、限时提示。
        if event.type() == QEvent.Type.ToolTip:
            return True
        if event.type() == QEvent.Type.Enter and not self.isChecked():
            self.hover_timer.start()
        elif event.type() == QEvent.Type.Leave:
            self.hover_timer.stop()
            if not self.isChecked():
                self.popup.hide()
        elif event.type() == QEvent.Type.Hide:
            self.hover_timer.stop()
            self.setChecked(False)
            self.popup.hide()
        elif event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape:
            self.hover_timer.stop()
            self.setChecked(False)
            self.popup.hide()
            return True
        return super().event(event)


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
                self.cache.clear()
                self.cache.update(key=key, planner=planner)
            echo_key = json.dumps(data["echo"], sort_keys=True, ensure_ascii=False)
            if self.cache.get("echo_key") != echo_key:
                single = self.cache["planner"].analyze(data["echo"], cancelled=self.cancelled)
                if self.cancelled.is_set():
                    raise CancelledError()
                self.cache.update(echo_key=echo_key, single=single)
            single = self.cache["single"]
            from src.echo_reconstruction_budget import BudgetResourceError

            try:
                comparison = self.cache["planner"].compare_dynamic_budget(
                    data["echo"], data["budget"], analysis=single, cancelled=self.cancelled)
            except BudgetResourceError as exc:
                comparison = {"budget": data["budget"], "rows": [], "recommendation": None,
                              "first_plans": [{**plan, "dynamic_budget": None} for plan in single["plans"]],
                              "error": str(exc), "method": "动态预算资源超限（单次分析不受影响）"}
            result = {**single, "dynamic": comparison}
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
        try:
            cost_config = load_json(DEFAULT_COSTS)
            self.cost_defaults = activation_defaults(cost_config)
            if any(value > 1_000_000 or abs(round(value, 4) - value) > 1e-8
                   for value in self.cost_defaults.values()):
                raise ValueError("GUI 成本默认值支持 0～1000000，最多四位小数")
            self.cost_defaults_note = str(cost_config.get("default_weights_note", ""))
        except (OSError, ValueError, TypeError) as exc:
            # 成本配置不可用时仍可评分；具体策略错误由后台计算单独报告。
            self.cost_defaults = activation_defaults({})
            self.cost_defaults_note = f"成本默认配置不可用，暂用兼容默认值：{exc}"
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
        self._score_reached = False
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
        self.setWindowTitle("WWEcho: 声骸评分 · 开孔推荐 · 频整计算")
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
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        title = QLabel("声骸词条综合工作台")
        title.setObjectName("title")
        layout.addWidget(title)
        subtitle = QLabel("支持导入/手动权重 · 实时评分 · 开孔达标概率计算 · 频整器洗练推荐")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)
        self.sections: list[CollapsibleSection] = []
        for panel in (self._weights_panel(), self._stats_panel(),
                      self._results_panel(), self._reconstruction_panel()):
            section = CollapsibleSection(panel)
            self.sections.append(section)
            layout.addWidget(section)
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
    def _metric_cards(layout: QLayout, definitions: list[tuple[str, str, str]],
                      labels: dict[str, QLabel]) -> None:
        """用明确口径的标题搭配大数字；保持纯文本，不插入外部 HTML。"""
        for index, (key, title, hint) in enumerate(definitions):
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
            if isinstance(layout, QGridLayout):
                layout.addWidget(card, index // 2, index % 2)
                layout.setColumnStretch(index % 2, 1)
            else:
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
        hint = QLabel("权重为每单位词条数值的系数；不同的权重系统得到的评分不同；请根据队伍、实战需求进行调整。")
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
        group = QGroupBox("02  声骸词条与档位选择")
        layout = QVBoxLayout(group)
        hint = QLabel("直接点击词条名进行勾选，再次点击可取消勾选。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
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
        self.minor_stat_label.setContentsMargins(8, 0, 0, 0)
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
        return group

    def _results_panel(self) -> QGroupBox:
        group = QGroupBox("03  目标与计算结果")
        layout = QVBoxLayout(group)
        hint = QLabel("可自由设置目标分数，系统将据此进行开孔推荐。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("目标分数"))
        self.target = self._number(30)
        self.target.valueChanged.connect(self.calculate)
        toolbar.addWidget(self.target)
        self.theoretical_max_label = QLabel("对齐最高分：—")
        self.theoretical_max_label.setObjectName("muted")
        self.theoretical_max_label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self.theoretical_max_label)
        self.theoretical_max_help = InfoButton()
        self.theoretical_max_help.setText("?")
        self.theoretical_max_help.setAccessibleName("未对齐分数说明")
        self.theoretical_max_help.setToolTip(
            "未对齐分数 = 原始权重和档位数值下声骸的原始分数\n"
            "未对齐最高分 = 理想中全顶分最高档词条的原始分数\n"
            "对齐分数 = 未对齐分数折算到满分50分时的分数\n"
            "对齐最高分 = 满分50分")
        self.theoretical_max_help.setAutoRaise(True)
        self.theoretical_max_help.setFixedSize(20, 20)
        self.theoretical_max_help.setStyleSheet(
            "QToolButton { border: 1px solid #9aa9bd; border-radius: 10px; "
            "padding: 0; color: #65758b; font-size: 12px; font-weight: 600; }"
            "QToolButton:hover { background: #edf3ff; border-color: #2563eb; "
            "color: #2563eb; }"
            "QToolButton:focus { border: 1px solid #2563eb; }")
        toolbar.addWidget(self.theoretical_max_help)
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
        self.details.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details.setMinimumHeight(0)
        layout.addWidget(self.details)
        self.cost_divider = self._divider()
        layout.addWidget(self.cost_divider)
        self.cost_parameters_label = QLabel("成本估算相关参数")
        self.cost_parameters_label.setToolTip(
            "用户提供成本：每次开孔 10 个打孔器；经验按 500 为最小单位投入，余量保留。\n"
            "强化和开孔不会失败；只有主动弃置回收才返还，经验默认 70%、打孔器默认 30%。\n"
            "经验向下取整到 500，打孔器按个向下取整；不含货币及胚子获取成本。\n"
            + self.cost_defaults_note)
        layout.addWidget(self.cost_parameters_label)
        parameters = QGridLayout()
        self.cost_parameters_layout = parameters
        self.strategy_inputs: dict[str, QDoubleSpinBox] = {}
        for index, (name, text, default, maximum) in enumerate([
            ("recovery", "经验回收比例", self.cost_defaults["recovery"], 1),
            ("tuning_recovery", "打孔器回收比例", self.cost_defaults["tuning_recovery"], 1),
            ("tuning_weight", "打孔器权重", self.cost_defaults["tuning_weight"], 1_000_000),
            ("experience_weight", "每1000经验权重", self.cost_defaults["experience_weight"], 1_000_000),
            ("embryo_weight", "胚子权重", self.cost_defaults["embryo_weight"], 1_000_000),
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
        self.strategy_inputs["recovery"].setToolTip(
            "弃置回收时返还经验，实际有损耗略小于70%。\n"
            "成本模型假设未达标且停止投入时选择回收。\n"
            "按累计实际投入经验计算后向下取整到 500，不影响打孔器回收比例。")
        self.strategy_inputs["tuning_recovery"].setToolTip(
            "打孔器回收，默认 30%：每开孔消耗 10 个，主动回收返还 3 个。\n"
            "按累计已用打孔器计算后向下取整到整数个，不影响经验回收比例。")
        self.strategy_inputs["experience_weight"].setToolTip(
            "每 1000 点经验的成本权重，缺少密音筒可提高。\n"
            + self.cost_defaults_note)
        layout.addLayout(parameters)
        self.strategy_status = QLabel("策略等待计算")
        self.strategy_status.setObjectName("status")
        self.strategy_status.setWordWrap(True)
        self.strategy_status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.strategy_status)
        self.zero_strategy = QLabel("—")
        self.current_strategy = QLabel("—")
        strategy_results = QHBoxLayout()
        for title, label in [("从零激活 · 成功率、成本估算", self.zero_strategy),
                             ("当前词条 · 是否继续", self.current_strategy)]:
            panel = QGroupBox(title)
            panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
            panel_layout = QVBoxLayout(panel)
            metrics = QGridLayout() if label is self.zero_strategy else QHBoxLayout()
            definitions = ([
                ("zero_probability", "单个胚子成功率", "从新胚子开始，按材料权重选择继续或弃置；达标后必须开满五条。此概率表示单个胚子最终成功的近似概率。"),
                ("zero_embryos", "消耗胚子", "反复尝试直到成功所需胚子的平均数量；不是保证数量。"),
                ("zero_tuning", "消耗打孔器", "反复尝试直到成功所需打孔器的平均数量，已计入弃置回收；不是保证数量。"),
                ("zero_gold_tubes", "消耗金密音筒", "金密音筒按每个 5000 点经验折算；显示反复尝试直到成功所需的平均数量。"),
            ] if label is self.zero_strategy else [
                ("current_probability", "后续成功率", "按当前策略处理此胚子，最终成功的近似概率。"),
                ("current_cost", "后续平均成本", "按建议处理当前胚子直到开满五条并达标或弃置的平均成本；提前达标仍计入开满所需经验和打孔器，不含换新胚子的费用。"),
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
            "推荐规则：按当前材料权重，比较继续培养的累计成本，与弃置回收后新胚子做到成功的累计成本，选择较低的一项。"
            "目标是降低反复尝试到一个达标成品的平均成本，成功率和材料消耗可能随权重改变。\n"
            "弃置时经验默认返还 70%，由于向下取整到500实际比例略低，打孔器默认返还 30%。\n"
            # "成本和材料数量都是平均值，并非保证花费；材料已扣除弃置回收，负成本表示返还多于后续投入。"
            # "成功达标时累计成本包含反复换胚子直到开满五条并达标的全部投入；此胚子后续平均成本只算当前这一件。"
            # "两个动作的比较成本及从当前到成品的平均累计成本包含后续换胚子，不用当前件成本除以当前件成功率。"
            # "经验按原始点数显示，成本按每 1000 点经验权重折算；修改成本权重或回收比例会重新优化推荐，未计货币与主词条刷取成本。"
            # "最省成本仅针对当前分数网格与成本模型，不代表精确游戏最优；当前精度未确认正成功机会时不提供有限累计成本。"
            # "分数不是伤害提升百分比。"
            )
        self.result_hint.setObjectName("muted")
        self.result_hint.setWordWrap(True)
        layout.addWidget(self.result_hint)
        return group

    def _reconstruction_panel(self) -> QGroupBox:
        group = QGroupBox("04  频整器消耗计算")
        layout = QVBoxLayout(group)
        hint = QLabel("开满五条副词条后可用。点击下方已开词条，或点击列表中列举的锁定组合。"
                    #   "正提升指严格超过当前原始评分。"
                      )
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
        toolbar.addWidget(QLabel("设置预算"))
        self.reconstruction_budget = QSpinBox()
        self.reconstruction_budget.setRange(0, 6)
        self.reconstruction_budget.setValue(3)
        self.reconstruction_budget.setSuffix(" 枚")
        self.reconstruction_budget.setToolTip(
            "动态换锁支持 0～6 枚，默认 3 枚。预算越大首次计算越久；计算在后台运行，可继续编辑。")
        self.reconstruction_budget.valueChanged.connect(self._reconstruction_budget_changed)
        toolbar.addWidget(self.reconstruction_budget)
        toolbar.addStretch()
        self.clear_reconstruction_locks_button = QPushButton("清空锁定")
        self.clear_reconstruction_locks_button.setObjectName("clearSelection")
        self.clear_reconstruction_locks_button.clicked.connect(self.clear_reconstruction_locks)
        toolbar.addWidget(self.clear_reconstruction_locks_button)
        layout.addLayout(toolbar)
        self.reconstruction_divider = self._divider()
        layout.addWidget(self.reconstruction_divider)
        self.reconstruction_status = QLabel("请先在区域 2 开满五条副词条")
        self.reconstruction_status.setObjectName("status")
        self.reconstruction_selected = QLabel("勾选 1～4 个词条，查看当前锁法。")
        self.reconstruction_selected.setObjectName("notice")
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
            ("gain_on_success", "当前锁法 · 成功时平均 Δ 分",
             "仅在单次重构严格超过当前原始评分时的平均提分，按结果概率加权；无正提升机会时显示“—”。"),
            ("delta", "当前锁法 · 单次期望 Δ 分", "包含降分风险的期望分数变化，不是保证升分。"),
            ("cost", "当前锁法 · 单次消耗", "锁1/2/3/4条分别消耗1/1/2/3枚频整器。"),
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
            "用法 / 枚", "首步锁定（后续重新选锁）", "首步消耗", "最终期望分数", "最终期望Δ分",
            "期望实际消耗", "期望次数", "平均升分 / 枚"])
        for column, description in {
            0: "动态择优按每次实际保留状态选择剩余花费；拆分行完整执行指定花费。不投入单独对照，不混入方案均值。",
            1: "每次洗出候选可保留或拒绝，拒绝后仍用原词条且照扣材料；后续在实际保留的词条上重新选锁。",
            3: "完整方案结束时评分的平均值，不是保证达到的分数。",
            4: "最终实际保留评分减去开始评分的期望；包含接受/拒绝候选的选择，不混入未执行洗练的零收益。",
            5: "方案实际用完预算，拒绝候选仍消耗本次频整器；不投入行的零消耗不参与方案平均。",
            6: "实际执行洗练的次数；拒绝候选也算一次，拆分行执行完整指定次数。",
        }.items():
            self.reconstruction_budget_table.horizontalHeaderItem(column).setToolTip(description)
        self.reconstruction_budget_table.cellClicked.connect(self._apply_reconstruction_budget_plan)
        tabs.addTab(self.reconstruction_plans_table, "单次 · 全部 30 种锁法（点击套用）")
        tabs.addTab(self.reconstruction_budget_table, "同预算 · 动态换锁与花费拆分（点击套用首步）")
        layout.addWidget(tabs)
        self.reconstruction_hint = QLabel(
            "单次："
            # "期望Δ分含降分风险；正收益 = E[max(Δ分, 0)]；"
            "成功时平均Δ分为正提升条件均值；升分期望消耗 = 单次消耗 / 单次升分概率。\n"
            "同预算："
            "动态方案根据实际保留状态选择最优后续锁法。"
            # "每次洗出候选可保留或拒绝；拒绝后沿用原词条，但本次频整器照扣。"
            # "每次在实际保留的状态上重新选锁，计算后续收益。"
            # "三枚方案完整执行 1+1+1、1+2、2+1 或 3；不是首次超过原始评分即停。"
            # "最终期望Δ分 = E[最终保留评分 − 开始评分]；不掺入放弃洗练的零收益分支。"
            # "不投入仅单独对照，不影响各执行方案的均值。\n"
            # "概率复用归一化官方公示档位，类型不放回；档位和跨次独立为模型约定。"
            # "离散动态规划无分数网格或随机模拟，期望以浮点累积；不含额外材料或货币。"
            # "实际重构后，请在区域 2 录入实际保留结果（拒绝则词条不变），预算扣除本次消耗，再查看下一步。"
            )
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

    def _invalidate_reconstruction(self, message: str, *, budget_only: bool = False) -> None:
        self._reconstruction_version += 1
        self._reconstruction_cancelled.set()
        self._reconstruction_timer.stop()
        self._reconstruction_pending = None
        self._reconstruction_key = None
        self.reconstruction_status.setText(message)
        self.reconstruction_status.setVisible(True)
        self.reconstruction_recommendation.setText("—")
        if budget_only and self._reconstruction_result is not None:
            self._reconstruction_result = {**self._reconstruction_result, "dynamic": None}
            self._display_reconstruction_selection(refresh_single=False)
        else:
            self._reconstruction_result = None
            self.reconstruction_selected.setText("勾选 1～4 个词条，查看当前锁法。")
            for label in self.reconstruction_metrics.values():
                self._set_metric(label)
            self.reconstruction_plans_table.setRowCount(0)
        self._reconstruction_budget_rows = []
        self.reconstruction_budget_table.setRowCount(0)
        self._sync_reconstruction_buttons()

    def _queue_reconstruction(self, *, budget_only: bool = False) -> None:
        self._sync_reconstruction_buttons()
        if len(self.selected_sub_stats) != 5:
            self._invalidate_reconstruction("请先在区域 2 开满五条副词条")
            return
        if self._score_result is None:
            self._invalidate_reconstruction("频整器输入无效")
            return
        data = {"stats": self.stats, "weights": self.current_weights(), "echo": self.current_echo(),
            "budget": self.reconstruction_budget.value()}
        key = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if key == self._reconstruction_key:
            return
        self._invalidate_reconstruction("频整器动态换锁等待后台计算",
                                        budget_only=budget_only)
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
        self.reconstruction_status.setText("频整器动态换锁后台计算中（预算越大越久）")
        self.reconstruction_status.setVisible(True)
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
                self.reconstruction_status.setVisible(True)
            elif result is not None:
                previous_result = self._reconstruction_result
                try:
                    self._display_reconstruction(result)
                except (KeyError, TypeError, ValueError) as exc:
                    self._reconstruction_result = previous_result
                    self._reconstruction_key = None
                    self._reconstruction_budget_rows = []
                    self.reconstruction_budget_table.setRowCount(0)
                    self.reconstruction_recommendation.setText("—")
                    self.reconstruction_status.setText(f"频整器结果格式有误：{exc}")
                    self.reconstruction_status.setVisible(True)
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
        comparison = result["dynamic"]
        if comparison["budget"] != self.reconstruction_budget.value():
            return
        # 先完成所有计算，再提交，防止错误响应留下旧输入的部分表格。
        refresh_single = (self._reconstruction_result is None
                          or self._reconstruction_result["plans"] is not result["plans"])
        plans = (sorted(result["plans"], key=lambda plan: (plan["locked_count"], -plan["expected_delta"]))
                 if refresh_single else self._reconstruction_rows)
        rows = []
        amount = self._reconstruction_amount
        if refresh_single:
            for plan in plans:
                rows.append([
                    str(plan["locked_count"]), self._locked_description(plan, "\n"), f"{plan['probability']:.4%}",
                    amount(plan["expected_delta"], signed=True), amount(plan["gain_on_success"]), str(plan["cost"]),
                    amount(plan["positive_gain_per_transducer"]),
                    amount(plan["expected_cost_to_improve"]) if plan["expected_cost_to_improve"] is not None
                    else "无法获得 / ∞"])
        budget_rows = []
        for plan in comparison["rows"]:
            value = plan["budget"]
            path = plan["spending_path"]
            label = "+".join(map(str, path)) if path else plan["label"]
            budget_rows.append([
                label, self._locked_description(plan, "\n") if plan["cost"] else "停手 · 保留当前结果",
                f"{plan['cost']} 枚", amount(value["expected_final_score"]),
                amount(value["expected_delta"], signed=True), amount(value["expected_cost"]),
                amount(value["expected_attempts"]), amount(value["net_gain_per_transducer"], signed=True)])
        tables = [(self.reconstruction_budget_table, budget_rows)]
        if refresh_single:
            tables.insert(0, (self.reconstruction_plans_table, rows))
        for widget, values in tables:
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
        if refresh_single:
            self._reconstruction_rows = plans
        self._reconstruction_budget_rows = comparison["rows"]
        self._reconstruction_result = result
        self.reconstruction_status.clear()
        self.reconstruction_status.setVisible(False)
        recommendation = comparison["recommendation"]
        if comparison.get("error"):
            text = f"同预算动态结果暂不可用：{comparison['error']}"
        elif recommendation is None:
            text = "预算为 0：不执行洗练，仅显示当前评分基线；不与执行方案混算。"
        else:
            value = recommendation["budget"]
            text = (f"同预算动态方案：实际使用 {comparison['budget']} 枚，首步锁 {recommendation['locked_count']} 条、"
                    f"消耗 {recommendation['cost']} 枚。每次可保留或拒绝候选，拒绝也扣材料；之后按实际保留状态换锁。\n"
                    f"最终期望分数 {amount(value['expected_final_score'])}，"
                    f"最终期望Δ分 {amount(value['expected_delta'], signed=True)}；"
                    f"期望实际消耗 {amount(value['expected_cost'])} 枚，"
                    f"期望重构 {amount(value['expected_attempts'])} 次。\n"
                    f"首步锁法：{self._locked_description(recommendation)}")
        self.reconstruction_recommendation.setText(text)
        self._display_reconstruction_selection(refresh_single=refresh_single)

    def _display_reconstruction_selection(self, *, refresh_single: bool = True) -> None:
        if refresh_single:
            self.reconstruction_plans_table.clearSelection()
            for label in self.reconstruction_metrics.values():
                self._set_metric(label)
        if not self.reconstruction_locks:
            self.reconstruction_selected.setText("尚未锁定：请勾选 1～4 个词条。")
            return
        if self._reconstruction_result is None:
            self.reconstruction_selected.setText("当前锁法等待有效的五词条计算结果。")
            return
        plan = next(plan for plan in self._reconstruction_result["plans"]
                    if set(plan["locked_ids"]) == self.reconstruction_locks)
        amount = self._reconstruction_amount
        if refresh_single:
            self._set_metric(self.reconstruction_metrics["probability"], f"{plan['probability']:.4%}", "accent")
            gain = plan["gain_on_success"]
            self._set_metric(self.reconstruction_metrics["gain_on_success"], amount(gain),
                     "positive" if gain is not None else "muted")
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
            f"升分期望消耗 {success_cost} 枚")
        if refresh_single:
            for row, item in enumerate(self._reconstruction_rows):
                if set(item["locked_ids"]) == self.reconstruction_locks:
                    self.reconstruction_plans_table.selectRow(row)
                    break

    def _reconstruction_budget_changed(self) -> None:
        if not self.updating:
            self._queue_reconstruction(budget_only=True)

    def _apply_reconstruction_plan(self, row: int, _column: int) -> None:
        if self._reconstruction_result is None:
            return
        self.reconstruction_locks = set(self._reconstruction_rows[row]["locked_ids"])
        self._sync_reconstruction_buttons()
        self._display_reconstruction_selection()

    def _apply_reconstruction_budget_plan(self, row: int, _column: int) -> None:
        if self._reconstruction_result is None or self._reconstruction_result["dynamic"] is None:
            return
        plan = self._reconstruction_budget_rows[row]
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
        self._score_reached = False
        self.progress.set_target_score(self.target.value())
        self.progress_target_label.setText(f"│ 目标 {self.target.value():g} 分")
        try:
            scorer = EchoScorer(self.stats, weights)
            self.theoretical_max_label.setText(
                f"理论最高分：{scorer.aligned_max_score:g} 分"
                f"（未对齐：{scorer.unaligned_max_score:g}）")
            result = scorer.score(self.current_echo())
            # 与策略使用相同的未圆整贡献判定达标，展示分数四舍五入不能改变成功边界。
            base = math.fsum([
                scorer.stat_score("main_stats", result["main_stat"]["stat_id"],
                                  result["main_stat"]["value"], result["cost"]),
                scorer.stat_score("minor_stats", result["minor_stat"]["stat_id"],
                                  result["minor_stat"]["value"], result["cost"]),
            ])
            precise_score = math.fsum([base] + [
                scorer.stat_score("sub_stats", item["stat_id"], item["value"])
                for item in result["sub_stats"]])
            self._score_reached = precise_score >= self.target.value()
        except ValueError as exc:
            self.theoretical_max_label.setText("理论最高分：—")
            self.score_label.setText("—")
            self.summary.setText(f"输入有误：{exc}")
            self.details.setRowCount(0)
            self._resize_details_table()
            self.progress.setValue(0)
            self._update_progress_status(unavailable="输入有误")
            self.strategy_status.setText("策略输入无效")
            self._invalidate_reconstruction("频整器输入无效")
            return
        self._score_result = result
        score, target = result["score"], self.target.value()
        self.score_label.setText(f"{score:g} 分")
        status = (f"已达目标 · 超出 {max(0, score - target):g} 分" if self._score_reached
                  else f"未达目标 · 还差 {max(0, target - score):g} 分")
        if not self._score_reached and score >= target:
            status = "未达目标 · 显示分数已四舍五入"
        if self._score_reached and result["activated_count"] < 5:
            status += f" · 仍需开满剩余 {5 - result['activated_count']} 条"
        self.summary.setText(f"{status}\n主词条 {result['main_score']:g} 分 + 副词条 {result['sub_score']:g} 分"
                             f" + 小词条 {result['minor_score']:g} 分"
                             f" · 已激活 {result['activated_count']}/5")
        self.progress.setValue(min(1000, int(score / self.progress.MAX_SCORE * 1000)))
        self._update_progress_status()
        entries = [("主词条", result["main_stat"]), ("小词条", result["minor_stat"])] + [("副词条", item) for item in result["sub_stats"]]
        self.details.setRowCount(len(entries))
        self._resize_details_table()
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

    def _resize_details_table(self) -> None:
        row_height = self.details.verticalHeader().defaultSectionSize()
        header_height = self.details.horizontalHeader().sizeHint().height()
        height = header_height + self.details.rowCount() * row_height + 2 * self.details.frameWidth()
        self.details.setFixedHeight(height)

    def _update_progress_status(self, current: dict[str, Any] | None = None,
                                *, unavailable: str = "推荐计算中") -> None:
        result = self._score_result
        if result is not None and self._score_reached:
            remaining = 5 - result["activated_count"]
            text = f"分数达标 · 继续开满剩余 {remaining} 条" if remaining else "达成目标"
            color = "#16a34a"
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
        self.strategy_status.setText("策略后台计算中")
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
        def probability_range(item: dict[str, Any]) -> str:
            return f"{item['full_success_lower']:.2%} ～ {item['full_success_upper']:.2%}"

        def amount(value: Any, decimals: int = 2) -> str:
            if value is None:
                return "无法获得 / ∞"
            text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
            return "0" if text == "-0" else text

        zero, current = result["zero"], result["current"]
        zero_text = (f"成功达标时累计成本：{amount(zero['expected_cost'])}"
                 f"\n单个胚子成功或弃置时平均成本：{amount(zero['attempt_cost'])}"
                 f"\n一次性开五条达标概率：{probability_range(zero)}（估算范围）")
        recommendation = "建议继续" if current["recommend_continue"] else "建议放弃"
        continue_cost = "—" if current["continue_cost"] is None else amount(current["continue_cost"])
        restart_cost = "—" if current["restart_cost"] is None else amount(current["restart_cost"])
        current_text = (f"{recommendation}（按材料权重比较累计成本）"
                f"\n原因：{current['reason']}"
                f"\n继续成本：{continue_cost}"
                f"\n弃置成本：{restart_cost}"
                f"\n开完剩余词条达标概率：{probability_range(current)}（估算范围）")
        status = "策略已更新 · 累计成本策略（概率和成本为近似计算）"
        self.zero_strategy.setText(zero_text)
        self.current_strategy.setText(current_text)
        self.strategy_status.setText(status)
        self._set_metric(self.strategy_metrics["zero_probability"], f"{zero['success_probability']:.2%}", "accent")
        for key, field in (("zero_embryos", "expected_embryos"),
                   ("zero_tuning", "expected_tuning")):
            value = zero[field]
            self._set_metric(self.strategy_metrics[key], "不可达 / ∞" if value is None else amount(value),
                     "muted" if value is None else "accent")
        experience = zero["expected_experience"]
        tubes = None if experience is None else experience / 5000
        self._set_metric(self.strategy_metrics["zero_gold_tubes"],
                         "不可达 / ∞" if tubes is None else amount(tubes, decimals=4),
                 "muted" if tubes is None else "accent")
        self._set_metric(self.strategy_metrics["current_probability"],
                 f"{current['success_probability']:.2%}", "accent")
        cost = current["future_cost"]
        self._set_metric(self.strategy_metrics["current_cost"], "不可达 / ∞" if cost is None else amount(cost),
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