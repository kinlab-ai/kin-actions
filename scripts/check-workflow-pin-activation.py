#!/usr/bin/env python3
"""Validate the no-bypass immutable version-tag freeze for pin rollout."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import sys
from pathlib import Path


class ActivationError(RuntimeError):
    """The live tag ruleset cannot freeze one rollout version."""


EXPECTED_RULESET_ID = 19932834
EXPECTED_SOURCE = "kinlab-ai/kin-actions"
EXPECTED_RULE_TYPES = Counter(("update", "deletion", "non_fast_forward"))


def validate_tag_freeze(ruleset: dict) -> dict:
    if ruleset.get("id") != EXPECTED_RULESET_ID:
        raise ActivationError("wrong version-tag freeze ruleset id")
    if (
        ruleset.get("source") != EXPECTED_SOURCE
        or ruleset.get("source_type") != "Repository"
    ):
        raise ActivationError("version-tag freeze must belong to the exact repository")
    if ruleset.get("name") != "Freeze version release tags":
        raise ActivationError("wrong version-tag freeze ruleset")
    if ruleset.get("target") != "tag" or ruleset.get("enforcement") != "active":
        raise ActivationError("version-tag freeze must actively target tags")
    if "bypass_actors" not in ruleset:
        raise ActivationError(
            "bypass actor visibility is absent; an external ruleset-write audit is required"
        )
    bypass = ruleset["bypass_actors"]
    if bypass != []:
        raise ActivationError("version-tag freeze must have zero bypass actors")
    conditions = ruleset.get("conditions")
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if ref_name != {"exclude": [], "include": ["refs/tags/v*.*.*"]}:
        raise ActivationError("version-tag freeze ref condition must remain exact")
    rules = ruleset.get("rules")
    if not isinstance(rules, list):
        raise ActivationError("version-tag freeze rules are missing")
    types = [rule.get("type") for rule in rules if isinstance(rule, dict)]
    if len(types) != len(rules) or Counter(types) != EXPECTED_RULE_TYPES:
        raise ActivationError(
            "version-tag freeze must block update, deletion, and non-fast-forward"
        )
    return {"status": "ready", "ruleset_id": EXPECTED_RULESET_ID}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ruleset", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.ruleset.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ActivationError("ruleset response must be an object")
        result = validate_tag_freeze(data)
    except (OSError, json.JSONDecodeError, ActivationError) as exc:
        print(f"workflow-pin activation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
