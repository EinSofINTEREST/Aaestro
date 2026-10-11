"""Unit tests for configuration loading.

잘못된 설정으로는 기동하지 않는다 — 런타임 중에 발견되는 설정 오류는 이미 늦다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from malkuth.config import (
    ENV_PREFIX,
    MalkuthConfig,
    RuntimeConfig,
    env_overrides,
    load_config,
    merge,
)
from malkuth.core.errors import ErrorCategory, MalkuthError

REPO_CONFIGS = Path(__file__).resolve().parents[2] / "configs"


def write(tmp_path: Path, body: str, environment: str = "dev") -> Path:
    """설정 파일을 만든다."""
    (tmp_path / f"{environment}.yaml").write_text(body, encoding="utf-8")
    return tmp_path


# --- 배포되는 설정 파일 ---------------------------------------------------------


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
def test_shipped_configs_are_valid(environment):
    """저장소에 실린 설정이 스키마를 통과해야 한다."""
    config = load_config(environment, config_dir=REPO_CONFIGS, environ={})

    assert isinstance(config, MalkuthConfig)


def test_environments_differ_where_intended():
    """환경별 차이가 실제로 반영되는지 — 파일이 복사본으로 남지 않게."""
    dev = load_config("dev", config_dir=REPO_CONFIGS, environ={})
    prod = load_config("prod", config_dir=REPO_CONFIGS, environ={})

    assert dev.orchestrator.checkpointer == "memory"
    assert prod.orchestrator.checkpointer == "postgres"
    assert dev.observability.log_format == "pretty"
    assert prod.observability.log_format == "json"


# --- 기본값 -------------------------------------------------------------------


def test_empty_config_uses_defaults(tmp_path):
    """운영자는 바꾸고 싶은 것만 적는다."""
    config = load_config("dev", config_dir=write(tmp_path, ""), environ={})

    assert config.runtime.network == "malkuth-net"
    assert config.orchestrator.max_concurrent_runs == 10


def test_partial_section_keeps_sibling_defaults(tmp_path):
    """섹션 하나를 덮어써도 나머지 키가 사라지면 안 된다."""
    directory = write(tmp_path, "runtime:\n  network: custom-net\n")

    config = load_config("dev", config_dir=directory, environ={})

    assert config.runtime.network == "custom-net"
    assert config.runtime.agent_base_image == "malkuth/agent-base:0.1.0"


def test_registry_roots_cover_every_ref_type():
    """해석 루트가 빠진 ref 타입은 배포 검증에서 터진다."""
    roots = MalkuthConfig().registry.roots

    declared = set(roots.model_dump())
    assert {"skillsets", "promptsets", "memorysets", "agents", "graphs"} <= declared
    # groups 는 ref 타입이 아니라 선언 루트다 — agents 루트에서 유도하면 agents 를
    # 옮길 때 그룹 조회가 엉뚱한 곳을 본다 (#247 리뷰)
    assert "groups" in declared


# --- 실패 경로 ----------------------------------------------------------------


def test_missing_file_is_cfg_001(tmp_path):
    with pytest.raises(MalkuthError) as exc_info:
        load_config("absent", config_dir=tmp_path, environ={})

    assert exc_info.value.code == "CFG_001"
    assert exc_info.value.category is ErrorCategory.CONFIG


def test_unparseable_yaml_is_cfg_001(tmp_path):
    directory = write(tmp_path, "runtime: [unclosed\n")

    with pytest.raises(MalkuthError) as exc_info:
        load_config("dev", config_dir=directory, environ={})

    assert exc_info.value.code == "CFG_001"


def test_non_mapping_root_is_rejected(tmp_path):
    directory = write(tmp_path, "- just\n- a list\n")

    with pytest.raises(MalkuthError) as exc_info:
        load_config("dev", config_dir=directory, environ={})

    assert exc_info.value.code == "CFG_001"


def test_schema_violation_reports_the_field(tmp_path):
    """어느 필드가 왜 틀렸는지 알려주지 않으면 운영자가 고칠 수 없다."""
    directory = write(tmp_path, "orchestrator:\n  max_concurrent_runs: 0\n")

    with pytest.raises(MalkuthError) as exc_info:
        load_config("dev", config_dir=directory, environ={})

    fields = [e["field"] for e in exc_info.value.details["errors"]]
    assert "orchestrator.max_concurrent_runs" in fields


def test_unknown_checkpointer_is_rejected(tmp_path):
    directory = write(tmp_path, "orchestrator:\n  checkpointer: mystery\n")

    with pytest.raises(MalkuthError) as exc_info:
        load_config("dev", config_dir=directory, environ={})

    assert exc_info.value.code == "CFG_001"


# --- 스키마 규칙 --------------------------------------------------------------


def test_host_network_is_rejected():
    """호스트 네트워크 모드는 격리를 무력화한다."""
    with pytest.raises(ValueError, match="host"):
        RuntimeConfig(network="host")


def test_health_timeout_beyond_interval_is_rejected():
    """타임아웃이 주기보다 길면 확인이 밀려 상태 판정이 늦어진다."""
    with pytest.raises(ValueError, match="interval_s"):
        MalkuthConfig.model_validate(
            {"runtime": {"health_check": {"interval_s": 3, "timeout_s": 10}}}
        )


def test_inverted_idle_delays_are_rejected():
    """min 이 max 보다 크면 backoff 가 진행되지 않는다."""
    with pytest.raises(ValueError, match="idle_min_delay_s"):
        MalkuthConfig.model_validate(
            {
                "orchestrator": {
                    "service_defaults": {"idle_min_delay_s": 900, "idle_max_delay_s": 60}
                }
            }
        )


def test_inverted_port_range_is_rejected():
    with pytest.raises(ValueError, match="ascending"):
        MalkuthConfig.model_validate({"protocols": {"a2a": {"port_range": [9199, 9100]}}})


def test_privileged_port_range_is_rejected():
    """1024 미만은 root 권한이 필요하다 — non-root 컨테이너가 바인딩할 수 없다."""
    with pytest.raises(ValueError, match="privileged"):
        MalkuthConfig.model_validate({"protocols": {"a2a": {"port_range": [80, 443]}}})


def test_privileged_metrics_port_is_rejected():
    with pytest.raises(ValueError):
        MalkuthConfig.model_validate({"observability": {"metrics_port": 80}})


# --- 환경변수 오버라이드 --------------------------------------------------------


def test_env_override_replaces_a_value(tmp_path):
    """컨테이너 배포에서 파일을 다시 굽지 않고 조정할 수 있어야 한다."""
    directory = write(tmp_path, "runtime:\n  network: from-file\n")

    config = load_config(
        "dev", config_dir=directory, environ={f"{ENV_PREFIX}RUNTIME__NETWORK": "from-env"}
    )

    assert config.runtime.network == "from-env"


def test_env_override_coerces_numbers():
    """숫자가 문자열로 남으면 스키마 검증에서 터진다."""
    overrides = env_overrides({f"{ENV_PREFIX}ORCHESTRATOR__NODE_TIMEOUT_S": "42.5"})

    assert overrides == {"orchestrator": {"node_timeout_s": 42.5}}


def test_env_override_coerces_booleans():
    overrides = env_overrides({f"{ENV_PREFIX}RUNTIME__ENABLED": "true"})

    assert overrides == {"runtime": {"enabled": True}}


def test_env_override_handles_deep_nesting():
    overrides = env_overrides({f"{ENV_PREFIX}RUNTIME__HEALTH_CHECK__INTERVAL_S": "20"})

    assert overrides == {"runtime": {"health_check": {"interval_s": 20}}}


def test_nested_override_keeps_sibling_file_values(tmp_path):
    directory = write(
        tmp_path,
        "runtime:\n  health_check:\n    interval_s: 10\n    unhealthy_threshold: 7\n",
    )

    config = load_config(
        "dev",
        config_dir=directory,
        environ={f"{ENV_PREFIX}RUNTIME__HEALTH_CHECK__INTERVAL_S": "20"},
    )

    assert config.runtime.health_check.interval_s == 20
    assert config.runtime.health_check.unhealthy_threshold == 7


def test_unrelated_env_vars_are_ignored():
    assert env_overrides({"PATH": "/usr/bin", "HOME": "/root"}) == {}


def test_malformed_override_key_is_ignored():
    """MALKUTH_ 만 있거나 빈 구간이 있는 키는 무시한다."""
    assert env_overrides({ENV_PREFIX: "x", f"{ENV_PREFIX}RUNTIME__": "y"}) == {}


# --- 런타임 env 와의 분리 (#182) -------------------------------------------
# 컨테이너에 주입되는 MALKUTH_* 런타임 env 가 설정 키로 오인되면, 그 환경에서
# load_config 가 통째로 실패한다


@pytest.mark.parametrize(
    "name",
    ["AGENT_TOKEN", "A2A_PORT", "MANIFEST", "EXECUTOR", "MEMORY_TOKEN", "METRICS_PORT"],
)
def test_runtime_env_is_not_a_config_override(name):
    """컨테이너가 주입하는 env 는 설정 키가 아니다 — 구분자가 없다."""
    assert env_overrides({f"{ENV_PREFIX}{name}": "value"}) == {}


def test_config_loads_inside_a_container_environment(tmp_path):
    """compose 가 주입하는 조합 그대로 — 여기서 깨지면 컨테이너가 뜨지 않는다."""
    (tmp_path / "dev.yaml").write_text("memory:\n  backend: sqlite\n", encoding="utf-8")

    config = load_config(
        "dev",
        config_dir=tmp_path,
        environ={
            f"{ENV_PREFIX}AGENT_TOKEN": "e2e-token",
            f"{ENV_PREFIX}A2A_PORT": "19102",
            f"{ENV_PREFIX}MEMORY_URL": "http://memory:8090",
            f"{ENV_PREFIX}MEMORY_TOKEN": "opaque",
            f"{ENV_PREFIX}METRICS_PORT": "9090",
        },
    )

    assert config.memory.backend == "sqlite"


def test_a_runtime_env_does_not_shadow_its_section():
    """MALKUTH_MEMORY_TOKEN 이 memory 섹션 옆에 앉으면 설정이 오염된다."""
    overrides = env_overrides(
        {f"{ENV_PREFIX}MEMORY_TOKEN": "opaque", f"{ENV_PREFIX}MEMORY__BACKEND": "postgres"}
    )

    assert overrides == {"memory": {"backend": "postgres"}}


def test_override_can_introduce_a_new_section():
    overrides = env_overrides({f"{ENV_PREFIX}MEMORY__BACKEND": "postgres"})

    assert overrides == {"memory": {"backend": "postgres"}}


def test_invalid_override_still_fails_validation(tmp_path):
    """환경변수로도 스키마를 우회할 수 없다."""
    directory = write(tmp_path, "")

    with pytest.raises(MalkuthError) as exc_info:
        load_config(
            "dev",
            config_dir=directory,
            environ={f"{ENV_PREFIX}ORCHESTRATOR__CHECKPOINTER": "mystery"},
        )

    assert exc_info.value.code == "CFG_001"


@pytest.mark.parametrize(
    "environ",
    [
        {f"{ENV_PREFIX}RUNTIME": "x", f"{ENV_PREFIX}RUNTIME__NETWORK": "net"},
        {f"{ENV_PREFIX}RUNTIME__NETWORK": "net", f"{ENV_PREFIX}RUNTIME": "x"},
    ],
    ids=["parent-first", "child-first"],
)
def test_deep_key_wins_regardless_of_iteration_order(environ):
    """순회 순서에 좌우되면 같은 환경에서 결과가 달라진다."""
    assert env_overrides(environ) == {"runtime": {"network": "net"}}


def test_unknown_config_key_is_rejected(tmp_path):
    """오타를 조용히 무시하면 운영자는 설정이 반영된 줄 안다."""
    directory = write(tmp_path, "runtime:\n  netwrok: typo-net\n")

    with pytest.raises(MalkuthError) as exc_info:
        load_config("dev", config_dir=directory, environ={})

    assert exc_info.value.code == "CFG_001"
    assert any("netwrok" in e["field"] for e in exc_info.value.details["errors"])


def test_unknown_env_override_key_is_rejected(tmp_path):
    """환경변수 오타도 마찬가지로 잡혀야 한다."""
    directory = write(tmp_path, "")

    with pytest.raises(MalkuthError) as exc_info:
        load_config("dev", config_dir=directory, environ={f"{ENV_PREFIX}RUNTIME__NETWROK": "typo"})

    assert exc_info.value.code == "CFG_001"


# --- merge --------------------------------------------------------------------


def test_merge_is_deep():
    base = {"a": {"b": 1, "c": 2}}

    assert merge(base, {"a": {"b": 9}}) == {"a": {"b": 9, "c": 2}}


def test_merge_does_not_mutate_the_base():
    base = {"a": {"b": 1}}

    merge(base, {"a": {"b": 2}})

    assert base == {"a": {"b": 1}}


def test_merge_replaces_a_mapping_with_a_scalar():
    assert merge({"a": {"b": 1}}, {"a": 5}) == {"a": 5}


# --- 권한 레지스트리 설정 (#277) ----------------------------------------------------


@pytest.mark.parametrize("target", [":memory:", "file::memory:?cache=shared"])
def test_the_access_store_must_be_a_file(target):
    from malkuth.config import OrchestratorConfig

    with pytest.raises(ValueError, match="access_store must be a file path"):
        OrchestratorConfig(access_store=target)


def test_the_enforcer_token_must_differ_from_the_control_token():
    """같으면 강제 지점이 운영자 권한을 덤으로 갖는다."""
    from malkuth.config import OrchestratorConfig

    with pytest.raises(ValueError, match="must differ"):
        OrchestratorConfig(control_token="same-token", access_enforcer_token="same-token")

    assert (
        OrchestratorConfig(control_token="a", access_enforcer_token="b").access_enforcer_token
        == "b"
    )


@pytest.mark.parametrize(
    "url",
    [
        "control-plane:8700",
        "ftp://control-plane",
        "http://agent:secret@control-plane:8700",
        "http://control-plane:8700/v1?x=1",
        "http://",
    ],
)
def test_the_agent_facing_registry_url_must_be_a_plain_http_url(url):
    """자격을 URL 에 넣거나 경로를 붙이면 새거나 엉뚱한 곳으로 간다 (#291 리뷰)."""
    from malkuth.config import OrchestratorConfig

    with pytest.raises(ValidationError):
        OrchestratorConfig(access_agent_url=url)


@pytest.mark.parametrize("url", ["http://control-plane:8700", "https://cp.example.com/"])
def test_an_agent_facing_registry_url_is_accepted(url):
    from malkuth.config import OrchestratorConfig

    assert OrchestratorConfig(access_agent_url=url).access_agent_url == url


@pytest.mark.parametrize(
    "field",
    [
        {"connect_url": "https://egress:8080"},
        {"connect_url": "http://agent:secret@egress:8080"},
        {"providers_url": "http://egress:8081/anthropic"},
        {"providers_url": "egress:8081"},
    ],
)
def test_egress_proxy_urls_are_bare_http_addresses(field):
    """자격은 배포가 에이전트마다 붙인다 — 설정에 박힌 자격은 모든 에이전트가 나눠 갖는다."""
    from malkuth.config import EgressProxyConfig

    values = {"connect_url": "http://egress:8080", "providers_url": "http://egress:8081", **field}
    with pytest.raises(ValidationError):
        EgressProxyConfig(**values)


@pytest.mark.parametrize("url", ["http://egress", "http://egress:8080#frag"])
def test_egress_proxy_urls_need_a_port_and_nothing_extra(url):
    """포트가 없으면 80 으로 가서 8080·8081 창구에 닿지 않는다 (#294 리뷰)."""
    from malkuth.config import EgressProxyConfig

    with pytest.raises(ValidationError):
        EgressProxyConfig(connect_url=url, providers_url="http://egress:8081")


@pytest.mark.parametrize("container", ["-leading", "has space", "a/b", ""])
def test_the_egress_proxy_container_is_a_docker_name(container):
    """사이드카 네트워크에 붙일 컨테이너 (#304) — Docker 이름 규칙 밖이면 기동 전에 거절한다."""
    from malkuth.config import EgressProxyConfig

    with pytest.raises(ValidationError):
        EgressProxyConfig(
            connect_url="http://egress:8080",
            providers_url="http://egress:8081",
            container=container,
        )


def test_the_egress_proxy_container_is_optional():
    from malkuth.config import EgressProxyConfig

    base = {"connect_url": "http://egress:8080", "providers_url": "http://egress:8081"}
    assert EgressProxyConfig(**base).container is None
    assert EgressProxyConfig(**base, container="malkuth-egress-1").container == "malkuth-egress-1"


# --- 프록시가 종단하는 provider (#358) ---------------------------------------------


def test_the_egress_proxy_terminates_anthropic_by_default():
    from malkuth.config import EgressProxyConfig

    base = {"connect_url": "http://egress:8080", "providers_url": "http://egress:8081"}

    assert EgressProxyConfig(**base).providers == ("anthropic",)


@pytest.mark.parametrize("providers", [[], ["anthropic", "anthropic"]])
def test_egress_proxy_providers_must_be_non_empty_and_unique(providers):
    """빈 목록은 어떤 모델도 못 부르는 오설정이고, 중복은 대조 집합과 목록이 어긋난다."""
    from malkuth.config import EgressProxyConfig

    with pytest.raises(ValidationError, match="providers"):
        EgressProxyConfig(
            connect_url="http://egress:8080",
            providers_url="http://egress:8081",
            providers=providers,
        )


def test_egress_proxy_providers_declared_in_yaml_reach_the_loaded_config(tmp_path):
    """YAML 값이 load_config 를 통과해 읽혀야 한다 — 기본값만 읽히면 선언이 조용히 무시된다."""
    write(
        tmp_path,
        "runtime:\n"
        "  egress_proxy:\n"
        "    connect_url: http://egress:8080\n"
        "    providers_url: http://egress:8081\n"
        "    providers: [anthropic, openai-compat]\n",
    )

    config = load_config("dev", config_dir=tmp_path, environ={})

    assert config.runtime.egress_proxy.providers == ("anthropic", "openai-compat")
