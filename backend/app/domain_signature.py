"""解析签名与版本号的纯函数工具。

单独成模块，供示例数据、DOM 解析服务和来源索引共同使用，避免三处各写一份摘要算法。
"""
from __future__ import annotations

import hashlib
from typing import Any


def text(value: Any) -> str:
    return str(value or "").strip()


def split_fields(schema: Any) -> list[str]:
    result: list[str] = []
    for item in text(schema).replace("，", ",").split(","):
        field = item.strip()
        if field and field not in result:
            result.append(field)
    return result


def split_selectors(selector_rule: Any) -> list[str]:
    return [item.strip() for item in text(selector_rule).replace("，", ",").split(",")
            if item.strip()]


def normalize_version(value: Any) -> str:
    """把 v1 / 1 / config:3@v1 这类写法统一成 v1；空值保持空。"""
    raw = text(value)
    if not raw:
        return ""
    if "@" in raw:
        raw = raw.rsplit("@", 1)[1].strip()
    if raw.isdigit():
        return f"v{raw}"
    return raw


def next_version(history: list[dict[str, Any]] | None) -> str:
    highest = 0
    for item in history or []:
        raw = normalize_version(item.get("配置版本"))
        if raw.startswith("v") and raw[1:].isdigit():
            highest = max(highest, int(raw[1:]))
    return f"v{highest + 1}"


def compute_parse_signature(uri: Any, engine: Any, selector: Any, schema: Any) -> str:
    """解析签名：目标页面、引擎、选择器规则、输出 Schema 的稳定摘要。

    任一项为空都无法形成可复验签名，返回空串，由调用方标记“无法补签名”。
    """
    parts = [text(uri), text(engine), text(selector), text(schema)]
    if not all(parts):
        return ""
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def compute_field_signature(signature: Any, field: Any) -> str:
    """字段签名：把输出字段绑定到解析签名，供重放时核对字段出处。"""
    if not text(signature) or not text(field):
        return ""
    return hashlib.sha256(f"{text(signature)}|{text(field)}".encode()).hexdigest()[:12]


def source_version_of(config_id: Any, version: Any) -> str:
    if not text(config_id):
        return ""
    return f"config:{text(config_id)}@{normalize_version(version) or '未版本化'}"
