"""Validate model-proposed event partitions without losing visual evidence."""

from __future__ import annotations


def validated_groups(data, expected):
    groups = data.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("Hierarchy model returned no event groups")
    if any(not isinstance(group, dict) for group in groups):
        raise ValueError("Every event group must be an object")
    if any(
        not isinstance(group.get("children"), list)
        or not group["children"]
        or any(not isinstance(child, str) for child in group["children"])
        or not isinstance(group.get("description"), str)
        or not group["description"].strip()
        for group in groups
    ):
        raise ValueError("Every event group needs observation IDs and a description")
    flattened = [child for group in groups for child in group["children"]]
    if flattened != expected:
        raise ValueError("Hierarchy must preserve all observation IDs exactly once and in order")
    # Keep provenance under application control, even if a backend ignores the
    # JSON schema and adds its own fields such as a misleading grouping label.
    return [{"children": group["children"], "description": group["description"]} for group in groups]


def single_window_groups(batch):
    """A conservative fallback: reuse each source description, infer no event joins."""
    result = []
    for node in batch:
        description = node.get("payload", {}).get("description")
        if not isinstance(description, str) or not description.strip():
            description = node["text"]
        result.append(
            {"children": [node["id"]], "description": description, "grouping": "single_window_fallback"}
        )
    return result
