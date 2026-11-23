"""封口参数的定义、规范化与比较。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

#: 参与核对的三个封口参数（API 中的字段名）。
PARAM_KEYS: tuple[str, ...] = (
    "seal_temperature_c",
    "seal_pressure_kpa",
    "dwell_time_s",
)

PARAM_LABELS: dict[str, str] = {
    "seal_temperature_c": "封口温度(°C)",
    "seal_pressure_kpa": "封口压力(kPa)",
    "dwell_time_s": "停留时间(s)",
}

#: 参数在数据库中的列名（可带前缀）。
COLUMNS: dict[str, str] = {
    "seal_temperature_c": "temperature_c",
    "seal_pressure_kpa": "pressure_kpa",
    "dwell_time_s": "dwell_time_s",
}

PARAM_DIGITS = 3


def canonicalize(values: Mapping[str, Any]) -> dict[str, float]:
    """把参数统一四舍五入到 3 位小数，得到可直接比较的规范值。"""
    result: dict[str, float] = {}
    for key in PARAM_KEYS:
        if key not in values:
            raise ValueError(f"缺少参数 {key}")
        value = float(values[key])
        if not math.isfinite(value):
            raise ValueError(f"参数 {key} 必须是有限数值")
        result[key] = round(value, PARAM_DIGITS)
    return result


def differing_keys(left: Mapping[str, float], right: Mapping[str, float]) -> list[str]:
    """按固定顺序返回取值不同的参数名。"""
    return [key for key in PARAM_KEYS if left[key] != right[key]]


def params_from_row(row: Any, prefix: str = "") -> dict[str, float]:
    """从 sqlite3.Row 中取出参数，支持 ``plan_`` 之类的前缀。"""
    return {key: row[f"{prefix}{column}"] for key, column in COLUMNS.items()}


def columns_from_params(values: Mapping[str, float]) -> dict[str, float]:
    return {column: values[key] for key, column in COLUMNS.items()}


def params_to_json(values: Mapping[str, float] | None) -> dict[str, float] | None:
    if values is None:
        return None
    return {key: round(float(values[key]), PARAM_DIGITS) for key in PARAM_KEYS}
