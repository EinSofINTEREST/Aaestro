"""The CLI must be able to reach a durable checkpointer.

`--checkpointer postgres` 는 **어떤 조합으로도** 동작하지 않았다 (#220):
CLI 가 URL 없이 `build_checkpointer` 를 불렀고, `OrchestratorConfig` 에 URL 필드가
없었다. `configs/prod.yaml` 의 `checkpointer: postgres` 선언도 도달 불가능했다.

01 은 "Run 은 마지막 checkpoint 에서 재개 가능 — 데이터 손실 없음" 을 규정하는데,
`memory` checkpointer 는 프로세스와 함께 사라진다. 즉 CLI 사용자에게는 그 목표가
성립하지 않았다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml
from langgraph.checkpoint.memory import MemorySaver

from aaestro.cli.main import checkpointer_for
from aaestro.core.errors import AaestroError, ErrorCode


def write_config(directory: Path, name: str, orchestrator: dict) -> None:
    (directory / f"{name}.yaml").write_text(
        yaml.safe_dump({"orchestrator": orchestrator}), encoding="utf-8"
    )


def args_for(config_dir: Path, *, environment: str, checkpointer: str | None = None):
    return argparse.Namespace(
        checkpointer=checkpointer, environment=environment, config_dir=str(config_dir)
    )


def test_the_configured_backend_is_used(tmp_path):
    """설정이 backend 를 쥔다 — 플래그를 주지 않으면 설정값이다."""
    write_config(tmp_path, "local", {"checkpointer": "memory"})

    built = checkpointer_for(args_for(tmp_path, environment="local"))

    assert isinstance(built, MemorySaver)


def test_the_configured_url_reaches_the_builder(tmp_path):
    """#220 — URL 을 넣을 자리가 없어 postgres 선언이 도달 불가능했다.

    실제 접속 없이도 **URL 이 전달되었는지**는 드러난다: URL 이 비면
    `build_checkpointer` 가 `CFG_001` 로 거절하기 때문이다.
    """
    write_config(
        tmp_path,
        "local",
        {"checkpointer": "postgres", "checkpointer_url": "postgresql://u:p@127.0.0.1:1/db"},
    )

    built = checkpointer_for(args_for(tmp_path, environment="local"))

    assert not isinstance(built, MemorySaver), "postgres 를 요청했는데 in-memory 가 나왔다"


def test_a_missing_url_is_still_refused(tmp_path):
    """URL 없는 외부 backend 를 조용히 in-memory 로 떨어뜨리면 재개가 사라진다."""
    write_config(tmp_path, "local", {"checkpointer": "postgres"})

    with pytest.raises(AaestroError) as excinfo:
        checkpointer_for(args_for(tmp_path, environment="local"))

    assert excinfo.value.code == ErrorCode.CFG_001


def test_the_flag_overrides_the_configured_backend(tmp_path):
    """플래그는 override 다 — 설정을 고치지 않고 한 번만 다르게 돌릴 수 있어야 한다."""
    write_config(
        tmp_path,
        "local",
        {"checkpointer": "postgres", "checkpointer_url": "postgresql://u:p@127.0.0.1:1/db"},
    )

    built = checkpointer_for(args_for(tmp_path, environment="local", checkpointer="memory"))

    assert isinstance(built, MemorySaver)


def test_the_url_can_come_from_the_environment(tmp_path, monkeypatch):
    """자격증명을 파일에 굽지 않는 경로 — 이것이 기본 사용법이다."""
    write_config(tmp_path, "local", {"checkpointer": "postgres"})
    monkeypatch.setenv("AAESTRO_ORCHESTRATOR__CHECKPOINTER_URL", "postgresql://u:p@127.0.0.1:1/db")

    built = checkpointer_for(args_for(tmp_path, environment="local"))

    assert not isinstance(built, MemorySaver)


def test_the_environment_falls_back_to_the_env_var(tmp_path, monkeypatch):
    """CLI 와 상주 프로세스가 같은 이름으로 같은 설정을 봐야 한다."""
    write_config(tmp_path, "chosen", {"checkpointer": "memory"})
    monkeypatch.setenv("AAESTRO_ENV", "chosen")

    built = checkpointer_for(args_for(tmp_path, environment=None))

    assert isinstance(built, MemorySaver)


def test_the_shipped_prod_config_is_reachable():
    """`configs/prod.yaml` 이 postgres 를 선언한다 — URL 만 주면 닿아야 한다."""
    root = Path(__file__).resolve().parents[3]
    args = argparse.Namespace(
        checkpointer=None, environment="prod", config_dir=str(root / "configs")
    )

    with pytest.raises(AaestroError) as excinfo:
        checkpointer_for(args)

    # URL 이 없다는 이유로 거절되어야 한다 — backend 자체가 무시되면 memory 가 나온다
    assert excinfo.value.code == ErrorCode.CFG_001
    assert "url" in excinfo.value.message
