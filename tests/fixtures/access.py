"""A catalog with groups and expansion ceilings for access-control tests (#277)."""

from __future__ import annotations

from pathlib import Path

import yaml

KNOWLEDGE = "group:research:knowledge"
STEWARD = "permission-agent"


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def agent(name: str, group: str | None = None) -> dict:
    metadata = {"name": name, "version": "0.1.0"}
    if group:
        metadata["group"] = group
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": metadata,
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/solo@0.1.0"},
        },
    }


def group(name: str, spec: dict) -> dict:
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Group",
        "metadata": {"name": name, "version": "0.1.0"},
        "spec": spec,
    }


def access_workspace(tmp_path: Path) -> Path:
    write(
        tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {
                "engine": "jinja2",
                "templates": {"default": {"file": "t.j2"}, "system": {"file": "t.j2"}},
            },
        },
    )
    write(tmp_path / "agents" / "worker" / "manifest.yaml", agent("worker", "research"))
    write(tmp_path / "agents" / "peer" / "manifest.yaml", agent("peer", "research"))
    write(tmp_path / "agents" / "loner" / "manifest.yaml", agent("loner"))
    write(tmp_path / "agents" / STEWARD / "manifest.yaml", agent(STEWARD, "research"))
    write(
        tmp_path / "groups" / "research.yaml",
        group(
            "research",
            {
                "access": {
                    "ceiling": {
                        "max_ttl_s": 600,
                        "memory": [{"space": KNOWLEDGE, "mode": "ro"}],
                        "egress": ["api.search.example.com"],
                        "a2a": ["loner"],
                    }
                }
            },
        ),
    )
    write(
        tmp_path / "groups" / "global.yaml",
        group(
            "global", {"access": {"ceiling": {"max_ttl_s": 60, "egress": ["status.example.com"]}}}
        ),
    )
    (tmp_path / "graphs").mkdir()
    return tmp_path
