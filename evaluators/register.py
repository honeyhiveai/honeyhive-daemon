#!/usr/bin/env python3
"""Register coding agent evaluators in HoneyHive.

Usage:
    python -m evaluators.register [--dry-run] [--list]

Environment variables:
    HH_API_KEY   - HoneyHive API key (required)
    HH_API_URL   - HoneyHive base URL (default: https://api.honeyhive.ai)
    HH_PROJECT   - HoneyHive project name (required)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict

from .definitions import EVALUATORS


def _api_url() -> str:
    return os.environ.get("HH_API_URL", "https://api.honeyhive.ai").rstrip("/")


def _api_key() -> str:
    key = os.environ.get("HH_API_KEY", "")
    if not key:
        print("ERROR: HH_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)
    return key


def _request(method: str, path: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"{_api_url()}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def list_existing_metrics() -> list[Dict[str, Any]]:
    """Fetch all existing metrics from HoneyHive."""
    result = _request("GET", "/v1/metrics")
    if isinstance(result, list):
        return result
    return result.get("metrics", result.get("data", []))


def create_metric(evaluator: Dict[str, Any]) -> Dict[str, Any]:
    """Create a single metric/evaluator in HoneyHive."""
    return _request("POST", "/v1/metrics", evaluator)


def update_metric(evaluator: Dict[str, Any]) -> Dict[str, Any]:
    """Update an existing metric/evaluator in HoneyHive."""
    return _request("PUT", "/v1/metrics", evaluator)


def register_all(*, dry_run: bool = False) -> None:
    """Register all evaluators, creating or updating as needed."""
    existing = list_existing_metrics()
    existing_by_name = {}
    for m in existing:
        if isinstance(m, dict):
            existing_by_name[m.get("name", "")] = m

    for evaluator in EVALUATORS:
        name = evaluator["name"]
        existing_metric = existing_by_name.get(name)

        if dry_run:
            action = "UPDATE" if existing_metric else "CREATE"
            print(f"  [DRY RUN] {action}: {name} (type={evaluator['type']})")
            continue

        if existing_metric:
            payload = dict(evaluator)
            payload["id"] = existing_metric.get("id") or existing_metric.get("metric_id")
            try:
                result = update_metric(payload)
                print(f"  UPDATED: {name} -> {result}")
            except Exception as exc:
                print(f"  FAILED to update {name}: {exc}", file=sys.stderr)
        else:
            try:
                result = create_metric(evaluator)
                metric_id = result.get("metric_id", "?")
                print(f"  CREATED: {name} -> metric_id={metric_id}")
            except urllib.request.HTTPError as exc:
                body = exc.read().decode()
                print(f"  FAILED to create {name}: {exc} - {body}", file=sys.stderr)
            except Exception as exc:
                print(f"  FAILED to create {name}: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Register coding agent evaluators in HoneyHive")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be created/updated without making changes")
    parser.add_argument("--list", action="store_true", help="List existing evaluators and exit")
    args = parser.parse_args()

    if args.list:
        metrics = list_existing_metrics()
        print(f"Found {len(metrics)} existing metrics:")
        for m in metrics:
            if isinstance(m, dict):
                print(f"  {m.get('name', '?')} (type={m.get('type', '?')}, id={m.get('id', m.get('metric_id', '?'))})")
        return

    print(f"Registering {len(EVALUATORS)} evaluators against {_api_url()}...")
    register_all(dry_run=args.dry_run)
    print("Done.")


if __name__ == "__main__":
    main()
