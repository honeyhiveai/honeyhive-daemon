"""YAML-driven Claude Code mapping helpers."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from importlib.resources import files
from typing import Any, Dict, Optional

import yaml


@lru_cache(maxsize=1)
def load_claude_code_mapping() -> Dict[str, Any]:
    """Load the Claude Code mapping config from package data."""
    mapping_path = files("honeyhive_daemon").joinpath("mappings/claude_code.yaml")
    return yaml.safe_load(mapping_path.read_text(encoding="utf-8"))


def resolve_payload_path(payload: Dict[str, Any], path: str) -> Optional[Any]:
    """Resolve a dotted payload path."""
    current: Any = payload
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def resolve_event_mapping(
    mapping_node: Any, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Resolve a possibly nested discriminator-driven mapping node."""
    if isinstance(mapping_node, str) and mapping_node.startswith("$ref:"):
        template_name = mapping_node.removeprefix("$ref:")
        templates = load_claude_code_mapping().get("tool_templates", {})
        template = templates.get(template_name)
        if template is None:
            return None
        return resolve_event_mapping(deepcopy(template), payload)

    if not isinstance(mapping_node, dict):
        return None

    if "$ref" in mapping_node:
        template_name = str(mapping_node["$ref"]).removeprefix("$ref:")
        templates = load_claude_code_mapping().get("tool_templates", {})
        template = templates.get(template_name)
        if template is None:
            return None
        return resolve_event_mapping(deepcopy(template), payload)

    if "discriminator" not in mapping_node:
        return mapping_node

    discriminator_value = resolve_payload_path(payload, mapping_node["discriminator"])
    resolved: Optional[Dict[str, Any]] = None
    if discriminator_value is not None:
        resolved = mapping_node.get("mappings", {}).get(str(discriminator_value))
    if resolved is None:
        resolved = mapping_node.get("default")
    if resolved is None:
        return None
    return resolve_event_mapping(resolved, payload)
