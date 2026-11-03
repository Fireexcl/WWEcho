"""GUI 权重方案库：校验、独立命名、最近选择及原子保存。"""

from __future__ import annotations

import json
import os
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QLockFile

from src.echo_score import EchoScorer


DEFAULT_PRESET_NAME = "默认-爱弥斯-企鹅bot"
WEIGHTS_DESCRIPTION = "GUI 用户手动权重"


def normalize_weights(stats: dict[str, Any], weights: dict[str, Any]) -> dict[str, Any]:
    """统一旧预设及元数据，并在改变任何控件或文件前校验 GUI 数值范围。"""
    if not isinstance(weights, dict):
        raise ValueError("权重必须是 JSON 对象")
    scorer = EchoScorer(stats, weights)
    result = {
        "schema_version": 1,
        "description": WEIGHTS_DESCRIPTION,
        "main_stats": deepcopy(scorer.main_weights_by_cost),
        "minor_stats": deepcopy(scorer.minor_weights_by_cost),
        "sub_stats": deepcopy(scorer.weights["sub_stats"]),
    }
    for category in (result["main_stats"], result["minor_stats"]):
        for pool in category.values():
            for name, value in pool.items():
                pool[name] = _gui_value(value)
    for name, value in result["sub_stats"].items():
        result["sub_stats"][name] = _gui_value(value)
    return result


def _gui_value(value: float) -> float:
    if value > 1_000_000 or abs(round(value, 4) - value) > 1e-8:
        raise ValueError("GUI 权重支持 0～1000000，最多四位小数；未更改任何权重")
    return round(value, 4)


def _preset_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("方案名称不能为空")
    name = value.strip()
    if len(name) > 80 or any(ord(char) < 32 for char in name):
        raise ValueError("方案名称最多 80 个字符，不能包含换行或控制字符")
    return name


def atomic_write_json(path: Path, document: dict[str, Any]) -> bytes:
    """在目标目录写临时文件，完整落盘后替换，失败不截断原文件。"""
    payload = (json.dumps(document, ensure_ascii=False, indent=4, allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return payload


class WeightPresetStore:
    """仅在完整文件成功落盘后提交内存状态，写入失败不会丢失已有方案。"""

    def __init__(self, path: Path, stats: dict[str, Any], defaults: dict[str, Any],
                 *, legacy_path: Path | None = None) -> None:
        self.path = Path(path)
        self.stats = stats
        self._revision: bytes | None = None
        self.initial_save_error = ""
        self.migrated_from: Path | None = None
        if self.path.exists():
            contents = self.path.read_bytes()
            self._document = self._validate_document(json.loads(contents))
            self._revision = contents
        else:
            if legacy_path is not None and legacy_path.is_file():
                # 旧库仅作为只读迁移源，目标写入失败也不回退到用户目录。
                self._document = self._validate_document(json.loads(legacy_path.read_bytes()))
                self.migrated_from = legacy_path
            else:
                preset_id = uuid4().hex
                self._document = {
                    "schema_version": 1,
                    "selected_id": preset_id,
                    "presets": [{"id": preset_id, "name": DEFAULT_PRESET_NAME,
                                 "weights": normalize_weights(stats, defaults)}],
                }
            try:
                self._commit(self._document)
            except OSError as exc:
                # 默认权重可用时仍允许评分；用户可重试保存或导出。
                self.initial_save_error = str(exc)

    def _validate_document(self, document: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(document, dict) or document.get("schema_version") != 1:
            raise ValueError("权重方案库的 schema_version 必须为 1")
        presets = document.get("presets")
        if not isinstance(presets, list) or not presets:
            raise ValueError("权重方案库至少需要一个方案")
        ids: set[str] = set()
        names: set[str] = set()
        result = []
        for preset in presets:
            if not isinstance(preset, dict):
                raise ValueError("权重方案必须是 JSON 对象")
            preset_id = preset.get("id")
            if not isinstance(preset_id, str) or not preset_id or preset_id in ids:
                raise ValueError("权重方案 ID 不能为空或重复")
            name = _preset_name(preset.get("name"))
            if name.casefold() in names:
                raise ValueError("权重方案名称不能重复")
            ids.add(preset_id)
            names.add(name.casefold())
            result.append({"id": preset_id, "name": name,
                           "weights": normalize_weights(self.stats, preset["weights"])})
        if document.get("selected_id") not in ids:
            raise ValueError("上次选择的权重方案不存在")
        return {"schema_version": 1, "selected_id": document["selected_id"], "presets": result}

    @property
    def presets(self) -> list[dict[str, Any]]:
        return deepcopy(self._document["presets"])

    @property
    def selected_id(self) -> str:
        return self._document["selected_id"]

    @property
    def persisted(self) -> bool:
        return self._revision is not None

    @property
    def current(self) -> dict[str, Any]:
        return next(preset for preset in self.presets if preset["id"] == self.selected_id)

    def _unique_name(self, name: str, *, excluding: str = "") -> str:
        name = _preset_name(name)
        if any(preset["id"] != excluding and preset["name"].casefold() == name.casefold()
               for preset in self._document["presets"]):
            raise ValueError("已存在同名权重方案，请换一个名称")
        return name

    def _commit(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = QLockFile(str(self.path) + ".lock")
        if not lock.tryLock(0):
            raise OSError("权重方案库正由其他窗口保存，请稍后重试")
        try:
            try:
                contents = self.path.read_bytes()
            except FileNotFoundError:
                contents = None
            if contents != self._revision:
                raise ValueError("方案库已被其他窗口修改，未覆盖任何数据；请先导出当前权重，再重启读取最新方案")
            self._revision = atomic_write_json(self.path, document)
        finally:
            lock.unlock()
        self._document = document
        self.initial_save_error = ""

    def select(self, preset_id: str) -> None:
        if not any(preset["id"] == preset_id for preset in self._document["presets"]):
            raise ValueError("权重方案不存在")
        if preset_id != self.selected_id:
            self._commit({**deepcopy(self._document), "selected_id": preset_id})

    def save_current(self, weights: dict[str, Any]) -> None:
        normalized = normalize_weights(self.stats, weights)
        document = deepcopy(self._document)
        for preset in document["presets"]:
            if preset["id"] == self.selected_id:
                preset["weights"] = normalized
        self._commit(document)

    def export_document(self, *, current_weights: dict[str, Any] | None = None) -> dict[str, Any]:
        """导出完整方案库快照；可包含当前草稿，但不改变本地保存状态。"""
        document = deepcopy(self._document)
        if current_weights is not None:
            weights = normalize_weights(self.stats, current_weights)
            for preset in document["presets"]:
                if preset["id"] == self.selected_id:
                    preset["weights"] = weights
        return document

    def export_presets(self, path: Path, *, current_weights: dict[str, Any] | None = None) -> bool:
        """导出全部方案；若目标就是本地库，使用相同的锁及版本检查提交。"""
        document = self.export_document(current_weights=current_weights)
        if path.resolve() == self.path.resolve():
            self._commit(document)
            return True
        atomic_write_json(path, document)
        return False

    def import_presets(self, incoming: dict[str, Any]) -> dict[str, int]:
        """按 ID 追加外部方案，相同 ID 保留本地/首次出现项，不改变当前选择。"""
        if not isinstance(incoming, dict) or incoming.get("schema_version") != 1:
            raise ValueError("请导入 schema_version 为 1 的完整权重方案库")
        presets = incoming.get("presets")
        if not isinstance(presets, list):
            raise ValueError("权重方案库必须包含 presets 数组；旧单套权重请通过导入预设添加")
        document = deepcopy(self._document)
        ids = {preset["id"] for preset in document["presets"]}
        names = {preset["name"].casefold() for preset in document["presets"]}
        summary = {"added": 0, "skipped": 0, "renamed": 0}
        for preset in presets:
            if not isinstance(preset, dict):
                raise ValueError("权重方案必须是 JSON 对象；未导入任何方案")
            preset_id = preset.get("id")
            if not isinstance(preset_id, str) or not preset_id:
                raise ValueError("权重方案 ID 必须是非空字符串；未导入任何方案")
            if preset_id in ids:
                summary["skipped"] += 1
                continue
            original_name = _preset_name(preset.get("name"))
            weights = normalize_weights(self.stats, preset.get("weights"))
            name, number = original_name, 1
            while name.casefold() in names:
                suffix = f"（导入 {number}）"
                name = original_name[:80 - len(suffix)] + suffix
                number += 1
            if name != original_name:
                summary["renamed"] += 1
            document["presets"].append({"id": preset_id, "name": name, "weights": weights})
            ids.add(preset_id)
            names.add(name.casefold())
            summary["added"] += 1
        # 只读导入全重复方案时不改写文件；所有新方案验证通过后才一次性提交。
        if summary["added"]:
            self._commit(document)
        return summary

    def add(self, name: str, weights: dict[str, Any]) -> None:
        name = self._unique_name(name)
        normalized = normalize_weights(self.stats, weights)
        document = deepcopy(self._document)
        preset_id = uuid4().hex
        document["presets"].append({"id": preset_id, "name": name, "weights": normalized})
        document["selected_id"] = preset_id
        self._commit(document)

    def rename_current(self, name: str) -> None:
        name = self._unique_name(name, excluding=self.selected_id)
        document = deepcopy(self._document)
        for preset in document["presets"]:
            if preset["id"] == self.selected_id:
                preset["name"] = name
        self._commit(document)

    def delete_current(self) -> None:
        if len(self._document["presets"]) == 1:
            raise ValueError("至少保留一个权重方案，请先另存为新方案")
        document = deepcopy(self._document)
        index = next(index for index, preset in enumerate(document["presets"])
                     if preset["id"] == self.selected_id)
        document["presets"].pop(index)
        document["selected_id"] = document["presets"][min(index, len(document["presets"]) - 1)]["id"]
        self._commit(document)