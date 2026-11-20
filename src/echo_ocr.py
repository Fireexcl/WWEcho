"""本地 OCR 适配与独立于界面的副词条解析。"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from src.echo_score import finite_number

if TYPE_CHECKING:
    from numpy import ndarray


OCR_MAX_STATS = 5
OCR_INSTALL_HINT = "源码版请按 README 安装可选 OCR 依赖；发行版请重新完整解压或安装。"
MODEL_FILES = {
    "det_model_path": "ch_PP-OCRv4_det_infer.onnx",
    "rec_model_path": "ch_PP-OCRv4_rec_infer.onnx",
    "cls_model_path": "ch_ppocr_mobile_v2.0_cls_infer.onnx",
}


class OcrError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class OcrToken:
    text: str
    box: tuple[float, float, float, float]  # 截图像素坐标，左／上／右／下
    confidence: float


@dataclass(frozen=True)
class SubStatEntry:
    stat_id: str
    value: float


@dataclass(frozen=True)
class ParsedEquipmentStat:
    stat_id: str | None
    value: float | None
    raw_text: str


@dataclass(frozen=True)
class ParsedSubStat:
    stat_id: str | None
    value: float | None
    raw_text: str
    issues: tuple[str, ...] = ()


class Completeness(Enum):
    NEEDS_REVIEW = "待人工核对"
    BLOCKED = "不能应用"


@dataclass(frozen=True)
class OcrResult:
    rows: tuple[ParsedSubStat, ...]
    raw_lines: tuple[str, ...]
    unmatched_text: tuple[str, ...]
    completeness: Completeness
    blocking_issues: tuple[str, ...]
    review_issues: tuple[str, ...]
    main_stat: ParsedEquipmentStat | None = None
    minor_stat: ParsedEquipmentStat | None = None
    cost: int | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class OcrSelection:
    cost: int
    main_stat: str
    minor_stat: str
    sub_stats: tuple[SubStatEntry, ...]


def validate_selection(entries: Sequence[SubStatEntry],
                       stats: dict[str, Any]) -> tuple[SubStatEntry, ...]:
    if not 1 <= len(entries) <= OCR_MAX_STATS:
        raise ValueError(f"OCR 仅可替换 1～{OCR_MAX_STATS} 条已开副词条。")
    tiers = stats["sub_stats"]["tiers"]
    seen = set()
    normalized = []
    for entry in entries:
        stat_id = entry.stat_id
        if stat_id not in tiers:
            raise ValueError("请选择配置中的副词条类型。")
        name = stats["stat_definitions"][stat_id]["name_zh"]
        if stat_id in seen:
            raise ValueError(f"副词条不能重复：{name}。")
        value = finite_number(entry.value, name)
        matched = next((tier for tier in tiers[stat_id]["values"] if value == tier), None)
        if matched is None:
            raise ValueError(f"{name}的数值不属于合法档位。")
        seen.add(stat_id)
        normalized.append(SubStatEntry(stat_id, matched))
    return tuple(normalized)


def _normalize(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).split())


def _group_lines(tokens: Sequence[OcrToken]) -> list[str]:
    lines: list[list[OcrToken]] = []
    for token in sorted((token for token in tokens if token.text.strip()),
                        key=lambda token: ((token.box[1] + token.box[3]) / 2, token.box[0])):
        _, top, _, bottom = token.box
        best = None
        best_distance = math.inf
        for line in lines:
            _, anchor_top, _, anchor_bottom = line[0].box
            distance = abs((top + bottom) - (anchor_top + anchor_bottom)) / 2
            overlap = min(bottom, anchor_bottom) - max(top, anchor_top)
            if (overlap >= min(bottom - top, anchor_bottom - anchor_top) / 2
                    and distance <= min(bottom - top, anchor_bottom - anchor_top) / 2
                    and distance < best_distance):
                best, best_distance = line, distance
        if best is None:
            lines.append([token])
        else:
            best.append(token)
    return [" ".join(token.text.strip() for token in sorted(line, key=lambda token: token.box[0]))
            for line in lines]


_NAME_VALUE = re.compile(r"([^0-9+:%.\-]*)(.*)")
_NUMBER = re.compile(r":?\+?([0-9]+(?:\.[0-9]+)?)(%)?")
_NAME_ALIASES = {"暴击": "crit_rate"}
_UNIT_DEPENDENT_NAMES = {
    "生命": {"flat": "hp_flat", "percentage_points": "hp_pct"},
    "攻击": {"flat": "atk_flat", "percentage_points": "atk_pct"},
    "防御": {"flat": "def_flat", "percentage_points": "def_pct"},
}
_NON_TARGET = ("主词条", "固定小词条", "洗炼结果", "洗练结果", "重构结果")


def _resolve_name(label: str, number: re.Match[str] | None,
                  definitions: dict[str, Any]) -> tuple[str | None, bool]:
    unit_names = _UNIT_DEPENDENT_NAMES.get(label)
    if unit_names is not None:
        if number is None:
            return None, True
        return unit_names["percentage_points" if number[2] else "flat"], True
    names = {_normalize(item["name_zh"]): stat_id for stat_id, item in definitions.items()}
    stat_id = names.get(label, _NAME_ALIASES.get(label))
    if stat_id is not None:
        return stat_id, True
    if len(label) >= 3:
        matches = {stat_id for name, stat_id in names.items()
                   if len(name) == len(label) + 1
                   and any(name[:index] + name[index + 1:] == label for index in range(len(name)))}
        if len(matches) == 1:
            return next(iter(matches)), True
    return None, False


def _has_additional_stat(text: str, definitions: dict[str, Any]) -> bool:
    for match in re.finditer(r"[0-9%]([^0-9+:%.\-]+)", text):
        _, known = _resolve_name(match[1], _NUMBER.match(text, match.end()), definitions)
        if known:
            return True
    return False


def _equipment_stat(raw: str, pools: dict[str, dict[str, float]],
                    definitions: dict[str, Any]) -> ParsedEquipmentStat:
    parts = _NAME_VALUE.fullmatch(_normalize(raw))
    if parts is None:
        return ParsedEquipmentStat(None, None, raw)
    label, number_text = parts.groups()
    number = _NUMBER.fullmatch(number_text)
    stat_id, _ = _resolve_name(label, number, definitions)
    if stat_id is None or not any(stat_id in pool for pool in pools.values()):
        return ParsedEquipmentStat(None, None, raw)
    if number is None:
        return ParsedEquipmentStat(stat_id, None, raw)
    value = float(Decimal(number[1]))
    expected_percent = definitions[stat_id]["unit"] == "percentage_points"
    if bool(number[2]) != expected_percent:
        return ParsedEquipmentStat(stat_id, None, raw)
    return ParsedEquipmentStat(stat_id, value, raw)


def _matching_upgrade_steps(value: float, stat_id: str, pool: dict[str, float],
                            definitions: dict[str, Any]) -> set[int]:
    # TODO: verify rounding and these inferred 0/5/10/15/20/25 values against in-game samples.
    maximum = Decimal(str(pool[stat_id]))
    quantum = Decimal("0.1") if definitions[stat_id]["unit"] == "percentage_points" else Decimal("1")
    observed = Decimal(str(value))
    return {
        step for step in range(6)
        if (maximum * Decimal(5 + 4 * step) / Decimal(25)).quantize(
            quantum, rounding=ROUND_HALF_UP) == observed
    }


def parse_sub_stats(tokens: Sequence[OcrToken], stats: dict[str, Any]) -> OcrResult:
    tiers = stats["sub_stats"]["tiers"]
    definitions = stats["stat_definitions"]
    main_pools = stats["main_stats"]["max_values_by_cost"]
    minor_pools = stats["minor_stats"]["max_values_by_cost"]
    out_of_scope = {_normalize(item["name_zh"]) for key, item in definitions.items()
                    if key not in tiers}
    rows = []
    unmatched = []
    blocking = []
    raw_lines = tuple(_group_lines(tokens))
    has_known_name = False
    main_stat = _equipment_stat(raw_lines[0], main_pools, definitions) if raw_lines else None
    minor_stat = _equipment_stat(raw_lines[1], minor_pools, definitions) if len(raw_lines) > 1 else None
    if main_stat is None or main_stat.stat_id is None:
        blocking.append("未识别到第一行主词条，请重新框选主词条、固定小词条和副词条。")
    if minor_stat is None or minor_stat.stat_id is None:
        blocking.append("未识别到第二行固定小词条，请重新框选主词条、固定小词条和副词条。")

    cost = None
    if (main_stat is not None and main_stat.stat_id is not None
            and minor_stat is not None and minor_stat.stat_id is not None):
        candidates = {
            key for key, pool in main_pools.items() if main_stat.stat_id in pool
        } & {
            key for key, pool in minor_pools.items() if minor_stat.stat_id in pool
        }
        name_candidates = candidates.copy()
        # Intermediate values can rule out a lower maximum, but must never be treated as max-level values.
        if main_stat.value is not None:
            candidates = {key for key in candidates
                          if main_stat.value <= main_pools[key][main_stat.stat_id]}
        if minor_stat.value is not None:
            candidates = {key for key in candidates
                          if minor_stat.value <= minor_pools[key][minor_stat.stat_id]}
        if len(candidates) > 1:
            progressed = set()
            for key in candidates:
                steps = set(range(6))
                if main_stat.value is not None:
                    steps &= _matching_upgrade_steps(
                        main_stat.value, main_stat.stat_id, main_pools[key], definitions)
                if minor_stat.value is not None:
                    steps &= _matching_upgrade_steps(
                        minor_stat.value, minor_stat.stat_id, minor_pools[key], definitions)
                if steps:
                    progressed.add(key)
            candidates = progressed
        if len(candidates) == 1:
            cost = int(next(iter(candidates)))
        elif not candidates:
            if name_candidates:
                blocking.append("主词条或固定小词条数值不符合 Cost 升级档位，请核对截图。")
            else:
                blocking.append("主词条与固定小词条组合不属于配置中的 Cost，请核对截图。")
        else:
            blocking.append("Cost 无法仅凭词条名称与当前过程值唯一判定，请补充可区分的截图。")

    for index, raw in enumerate(raw_lines):
        if _has_additional_stat(_normalize(raw), definitions):
            blocking.append("同一识别行含有多个词条，无法验证行数。请重新截图。")
        if index < 2:
            continue
        text = _normalize(raw)
        parts = _NAME_VALUE.fullmatch(text)
        if parts is None:
            unmatched.append(raw)
            continue
        label, number_text = parts.groups()
        if text.startswith(_NON_TARGET) or label in out_of_scope:
            if "检测到非副词条页面内容，请重新框选。" not in blocking:
                blocking.append("检测到非副词条页面内容，请重新框选。")
            unmatched.append(raw)
            continue
        number = _NUMBER.fullmatch(number_text)
        stat_id, known = _resolve_name(label, number, definitions)
        if not known:
            unmatched.append(raw)
            if number is not None or (label and ("+" in number_text or "%" in number_text)):
                rows.append(ParsedSubStat(None, None, raw, ("词条名未匹配，请核对原图。",)))
            continue
        if stat_id is None:
            has_known_name = True
            rows.append(ParsedSubStat(None, None, raw, ("固定值／百分比不明确，请手动选择。",)))
            continue
        if stat_id not in tiers:
            blocking.append("检测到非副词条页面内容，请重新框选。")
            unmatched.append(raw)
            continue
        has_known_name = True
        value = None
        if number is None:
            issues = ("数值缺失或格式无法识别，请核对档位。",)
        elif bool(number[2]) != (definitions[stat_id]["unit"] == "percentage_points"):
            issues = ("数值单位不匹配，请核对百分号与档位。",)
        else:
            numeric = Decimal(number[1])
            value = next((v for v in tiers[stat_id]["values"] if Decimal(str(v)) == numeric), None)
            issues = () if value is not None else ("不属于配置档位，请核对数值与小数点。",)
        rows.append(ParsedSubStat(stat_id, value, raw, issues))

    seen = set()
    for index, row in enumerate(rows):
        if row.stat_id in seen:
            rows[index] = ParsedSubStat(row.stat_id, row.value, row.raw_text,
                                        row.issues + ("词条类型重复，请核对。",))
        if row.stat_id is not None:
            seen.add(row.stat_id)
    if len(rows) > OCR_MAX_STATS:
        blocking.append(f"检测到 {len(rows)} 个副词条行；最多只能导入 {OCR_MAX_STATS} 条。")
    if not has_known_name:
        blocking.append("没有识别到可用的副词条名称；零条、非目标页面或不可识别画面均不能导入。")
    review = []
    if unmatched:
        review.append("存在未匹配文字，请核对原图与框选范围。")
    confidences = [token.confidence for token in tokens if token.text.strip()]
    return OcrResult(tuple(rows), raw_lines, tuple(unmatched),
                     Completeness.BLOCKED if blocking else Completeness.NEEDS_REVIEW,
                     tuple(dict.fromkeys(blocking)), tuple(review), main_stat, minor_stat, cost,
                     math.fsum(confidences) / len(confidences) if confidences else None)


class LocalOcrEngine:
    """一个串行队列内延迟初始化并复用 CPU 推理实例。"""

    def __init__(self) -> None:
        self._engine = None

    def _load(self):
        try:
            import rapidocr_onnxruntime
            from rapidocr_onnxruntime import RapidOCR
        except (ImportError, OSError) as exc:
            raise OcrError("OCR 依赖不可用", f"{exc}。{OCR_INSTALL_HINT}") from exc
        resource_dir = Path(rapidocr_onnxruntime.__file__).resolve().parent
        config_path = resource_dir / "config.yaml"
        models = {key: resource_dir / "models" / filename for key, filename in MODEL_FILES.items()}
        missing = [path.name for path in models.values() if not path.is_file()]
        if not config_path.is_file():
            missing.append(config_path.name)
        if missing:
            raise OcrError("OCR 模型资源缺失", f"缺少 {', '.join(missing)}。{OCR_INSTALL_HINT}")
        return RapidOCR(config_path=str(config_path), **{key: str(path) for key, path in models.items()},
                        intra_op_num_threads=2, inter_op_num_threads=1)

    def recognize(self, image_array: ndarray) -> list[OcrToken]:
        if self._engine is None:
            try:
                self._engine = self._load()
            except OcrError:
                raise
            except Exception as exc:
                raise OcrError("OCR 模型初始化失败",
                               f"{type(exc).__name__}：{exc}。{OCR_INSTALL_HINT}") from exc
        lines, _ = self._engine(image_array, use_det=True, use_cls=True, use_rec=True)
        if lines is None:
            return []
        result = []
        for points, text, confidence in lines:
            xs, ys = zip(*points)
            box = (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))
            score = float(confidence)
            if (not isinstance(text, str) or not all(math.isfinite(x) for x in (*box, score))
                    or box[0] >= box[2] or box[1] >= box[3] or not 0 <= score <= 1):
                raise OcrError("OCR 返回格式异常", "本地引擎返回了无效文字坐标或置信度。")
            result.append(OcrToken(text, box, score))
        return result
