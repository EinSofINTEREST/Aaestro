"""Unit tests for manifest and group schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from malkuth.core.manifest import (
    RESERVED_GLOBAL_GROUP,
    AgentManifest,
    GroupManifest,
    GroupQuotas,
    McpServerSpec,
    McpSidecar,
    ModelConfig,
    ModelOverride,
    ParsedModuleRef,
    ResourceSpec,
    sidecar_host,
    sidecar_url,
)
from tests.fixtures.builders import make_manifest, manifest_dict


def test_minimal_manifest_is_valid():
    manifest = make_manifest()

    assert manifest.name == "test-agent"
    assert manifest.spec.model.name == "claude-sonnet-5"


def test_manifest_is_frozen():
    manifest = make_manifest()

    with pytest.raises(ValidationError):
        manifest.metadata.name = "other"  # type: ignore[misc]


def test_group_defaults_to_global_when_undeclared():
    manifest = make_manifest()

    assert manifest.metadata.group is None
    assert manifest.group == RESERVED_GLOBAL_GROUP


def test_declaring_reserved_global_group_is_rejected():
    """`group: global` 직접 선언 금지 — 01-architecture.md Group Rules 1."""
    with pytest.raises(ValidationError, match="reserved"):
        make_manifest(metadata={"name": "a", "version": "0.1.0", "group": "global"})


def test_explicit_group_membership_is_accepted():
    manifest = make_manifest(metadata={"name": "a", "version": "0.1.0", "group": "research"})

    assert manifest.group == "research"


@pytest.mark.parametrize("name", ["Test-Agent", "test_agent", "테스트", "-agent", "agent-"])
def test_invalid_agent_names_are_rejected(name):
    with pytest.raises(ValidationError):
        make_manifest(metadata={"name": name, "version": "0.1.0"})


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-rc1", "latest"])
def test_non_semver_versions_are_rejected(version):
    with pytest.raises(ValidationError):
        make_manifest(metadata={"name": "a", "version": version})


@pytest.mark.parametrize(
    "ref",
    [
        "promptsets/test@latest",
        "promptsets/test",
        "test@0.1.0",
        "unknown/test@0.1.0",
        "skillsets/test@0.1.0",  # promptset 필드에 skillset ref
    ],
)
def test_invalid_promptset_refs_are_rejected(ref):
    with pytest.raises(ValidationError):
        make_manifest(
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": ref},
            }
        )


def test_skillset_field_rejects_non_skillset_ref():
    with pytest.raises(ValidationError, match="skillset ref"):
        make_manifest(
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
                "skillsets": [{"ref": "promptsets/other@0.1.0"}],
            }
        )


def _spec_with(**extra):
    return {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": "promptsets/test@0.1.0"},
        **extra,
    }


def test_stdio_mcp_server_requires_command():
    with pytest.raises(ValidationError, match="requires 'command'"):
        make_manifest(spec=_spec_with(mcp={"servers": [{"name": "fs", "transport": "stdio"}]}))


def test_stdio_mcp_server_rejects_shell_command():
    """셸 문자열 실행 금지 — 03-protocol-integration.md MCP Rules 5."""
    with pytest.raises(ValidationError, match="installed executable"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "fs",
                            "transport": "stdio",
                            "command": ["sh", "-c", "mcp-server-filesystem /workspace"],
                        }
                    ]
                }
            )
        )


def test_stdio_mcp_server_accepts_executable():
    manifest = make_manifest(
        spec=_spec_with(
            mcp={
                "servers": [
                    {
                        "name": "filesystem",
                        "transport": "stdio",
                        "command": ["mcp-server-filesystem", "/workspace"],
                        "allowed_tools": ["read_file", "list_directory"],
                    }
                ]
            }
        )
    )

    server = manifest.spec.mcp.servers[0]
    assert server.allowed_tools == ("read_file", "list_directory")
    assert server.optional is False


def test_a_legacy_sse_server_is_rejected_with_the_replacement():
    """선언을 받아 두면 매니페스트는 통과하고 initialize 에서야 실패한다 — 연결 경로가 없다."""
    server = {"name": "legacy", "transport": "sse", "url": "https://mcp.example.com/sse"}

    with pytest.raises(ValidationError, match="use 'streamable-http'"):
        make_manifest(spec=_spec_with(mcp={"servers": [server]}))


def test_http_mcp_server_requires_exactly_one_of_sidecar_or_url():
    with pytest.raises(ValidationError, match="exactly one"):
        make_manifest(
            spec=_spec_with(mcp={"servers": [{"name": "browser", "transport": "streamable-http"}]})
        )


def test_a_remote_mcp_credential_must_not_reach_a_stdio_server():
    """프록시가 켜지면 원격 자격은 env 에서 빠진다 — 같은 이름의 stdio 서버는 뜨지 못한다."""
    from pydantic import ValidationError

    from malkuth.core.manifest import McpSpec

    remote = {
        "name": "corp",
        "transport": "streamable-http",
        "url": "https://mcp.example/mcp",
        "auth": {"type": "bearer", "token_env": "CORP_TOKEN"},
    }
    local = {"name": "fs", "transport": "stdio", "command": ["mcp-fs"], "env_allowlist": ["X"]}

    McpSpec(servers=(remote, local))
    with pytest.raises(ValidationError, match="CORP_TOKEN"):
        McpSpec(servers=(remote, {**local, "env_allowlist": ["X", "CORP_TOKEN"]}))


def test_http_mcp_server_rejects_both_sidecar_and_url():
    with pytest.raises(ValidationError, match="exactly one"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "browser",
                            "transport": "streamable-http",
                            "sidecar": {"image": "mcp/playwright:1.2.0"},
                            "url": "https://example.com/mcp",
                        }
                    ]
                }
            )
        )


def test_sidecar_image_must_be_pinned():
    with pytest.raises(ValidationError, match="pinned"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "browser",
                            "transport": "streamable-http",
                            "sidecar": {"image": "mcp/playwright:latest"},
                        }
                    ]
                }
            )
        )


def test_external_mcp_server_auth_env_must_be_allowlisted():
    with pytest.raises(ValidationError, match="env_allowlist"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "corp-search",
                            "transport": "streamable-http",
                            "url": "https://mcp.internal.example.com/search",
                            "auth": {"type": "bearer", "token_env": "CORP_SEARCH_TOKEN"},
                        }
                    ]
                }
            )
        )


def test_external_mcp_server_auth_env_allowlisted_is_valid():
    manifest = make_manifest(
        spec=_spec_with(
            mcp={
                "servers": [
                    {
                        "name": "corp-search",
                        "transport": "streamable-http",
                        "url": "https://mcp.internal.example.com/search",
                        "auth": {"type": "bearer", "token_env": "CORP_SEARCH_TOKEN"},
                    }
                ]
            },
            runtime={"env_allowlist": ["CORP_SEARCH_TOKEN"]},
        )
    )

    assert manifest.spec.mcp.servers[0].auth is not None


def test_duplicate_mcp_server_names_are_rejected():
    with pytest.raises(ValidationError, match="duplicate mcp server name"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {"name": "fs", "transport": "stdio", "command": ["a"]},
                        {"name": "fs", "transport": "stdio", "command": ["b"]},
                    ]
                }
            )
        )


def test_mcp_env_allowlist_must_be_subset_of_runtime_allowlist():
    with pytest.raises(ValidationError, match="absent from"):
        make_manifest(
            spec=_spec_with(
                mcp={
                    "servers": [
                        {
                            "name": "fs",
                            "transport": "stdio",
                            "command": ["mcp-server-filesystem"],
                            "env_allowlist": ["SECRET_KEY"],
                        }
                    ]
                }
            )
        )


def test_agent_image_must_be_pinned():
    with pytest.raises(ValidationError, match="pinned"):
        make_manifest(spec=_spec_with(runtime={"image": "malkuth/agent-base:latest"}))


def test_duplicate_memory_alias_is_rejected():
    with pytest.raises(ValidationError, match="duplicate memory space alias"):
        make_manifest(
            spec=_spec_with(
                memory={
                    "spaces": [
                        {"ref": "memorysets/a@0.1.0", "as": "mem"},
                        {"ref": "memorysets/b@0.1.0", "as": "mem"},
                    ]
                }
            )
        )


@pytest.mark.parametrize(
    "path", ["/var/run/docker.sock", "/etc/passwd", "/root/.ssh", "/proc/self"]
)
def test_sensitive_volume_mounts_are_rejected(path):
    with pytest.raises(ValidationError, match="sensitive host path"):
        make_manifest(spec=_spec_with(runtime={"volumes": [{"name": "v", "mount_path": path}]}))


@pytest.mark.parametrize("cpu", ["one", "1.0.0", "-1", ""])
def test_invalid_cpu_is_rejected(cpu):
    with pytest.raises(ValidationError):
        ResourceSpec(cpu=cpu)


@pytest.mark.parametrize("memory", ["1GB", "1024", "1.5Gi", ""])
def test_invalid_memory_is_rejected(memory):
    with pytest.raises(ValidationError):
        ResourceSpec(memory=memory)


def test_resource_conversions():
    spec = ResourceSpec(cpu="2.5", memory="2Gi")

    assert spec.cpu_cores == 2.5
    assert spec.memory_bytes == 2 * 1024**3


def test_wrong_kind_is_rejected():
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(manifest_dict(kind="Graph"))


def test_wrong_api_version_is_rejected():
    with pytest.raises(ValidationError):
        AgentManifest.model_validate(manifest_dict(apiVersion="malkuth/v2"))


def _group_dict(**overrides):
    base = {
        "apiVersion": "malkuth/v1",
        "kind": "Group",
        "metadata": {"name": "research", "version": "0.1.0"},
        "spec": {
            "quotas": {"cpu": "8.0", "memory": "16Gi", "max_agents": 10},
            "secrets": ["SEARCH_API_KEY"],
        },
    }
    base.update(overrides)
    return base


def test_group_manifest_is_valid():
    group = GroupManifest.model_validate(_group_dict())

    assert group.name == "research"
    assert group.is_global is False
    assert group.spec.quotas.cpu_cores == 8.0
    assert group.spec.quotas.memory_bytes == 16 * 1024**3


def test_global_group_is_recognized():
    group = GroupManifest.model_validate(
        _group_dict(metadata={"name": "global", "version": "0.1.0"})
    )

    assert group.is_global is True


def test_group_definition_must_not_declare_membership():
    with pytest.raises(ValidationError):
        GroupManifest.model_validate(
            _group_dict(metadata={"name": "research", "version": "0.1.0", "group": "other"})
        )


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("skillsets/web-search@0.2.0", ("skillsets", "web-search", "0.2.0")),
        ("agents/planner@1.0.0", ("agents", "planner", "1.0.0")),
        ("memorysets/agent-longterm@0.1.0", ("memorysets", "agent-longterm", "0.1.0")),
    ],
)
def test_parse_module_ref_valid(ref, expected):
    parsed = ParsedModuleRef.parse(ref)

    assert (parsed.type, parsed.name, parsed.version) == expected
    assert str(parsed) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "web-search",
        "skillsets/x@latest",
        "skillsets/@1.0.0",
        "skillsets/x@main",
        "unknown/x@1.0.0",
        "skillsets/X@1.0.0",
    ],
)
def test_parse_module_ref_invalid(ref):
    with pytest.raises(ValueError, match="invalid module ref"):
        ParsedModuleRef.parse(ref)


@pytest.mark.parametrize(
    "image",
    [
        "registry.local:5000/mcp/foo",  # 레지스트리 포트를 태그로 오인하면 통과해버린다
        "mcp/foo",
        "mcp/foo:latest",
        "registry.local:5000/mcp/foo:latest",
    ],
)
def test_unpinned_sidecar_images_are_rejected(image):
    with pytest.raises(ValidationError, match="pinned"):
        McpSidecar(image=image)


@pytest.mark.parametrize(
    "image",
    [
        "mcp/playwright:1.2.0",
        "registry.local:5000/mcp/foo:1.2.0",
        "mcp/foo@sha256:" + "a" * 64,  # digest 고정도 유효한 pinning
    ],
)
def test_pinned_sidecar_images_are_accepted(image):
    assert McpSidecar(image=image).image == image


def test_sidecar_url_is_derived_from_the_agent_and_server():
    server = McpServerSpec(
        name="browser",
        transport="streamable-http",
        sidecar={"image": "mcp/playwright:1.2.0", "port": 3000, "path": "/rpc"},
    )

    assert sidecar_url("researcher", server) == "http://malkuth-researcher--mcp-browser:3000/rpc"


def test_sidecar_url_defaults_to_port_8000_and_mcp_path():
    server = McpServerSpec(
        name="browser", transport="streamable-http", sidecar={"image": "mcp/playwright:1.2.0"}
    )

    assert sidecar_url("researcher", server) == "http://malkuth-researcher--mcp-browser:8000/mcp"


def test_sidecar_names_never_collide_with_replicas_or_other_agents():
    # 에이전트 이름에 하이픈 연속이 없으므로 `--` 가 두 이름을 가른다
    assert sidecar_host("foo", "bar-0") != "malkuth-foo-mcp-bar-0"
    assert sidecar_host("foo", "mcp-bar") != sidecar_host("foo-mcp", "bar")


def test_sidecar_url_refuses_a_non_sidecar_server():
    server = McpServerSpec(name="fs", transport="stdio", command=("mcp-server-filesystem",))

    with pytest.raises(ValueError, match="not a sidecar"):
        sidecar_url("researcher", server)


@pytest.mark.parametrize(
    "path", ["mcp", "/mcp?x=1", "/mcp#f", "//evil.example/mcp", "/a@b", "/a\\b", "/a b"]
)
def test_sidecar_path_cannot_rewrite_the_address(path):
    with pytest.raises(ValidationError, match="sidecar path"):
        McpSidecar(image="mcp/playwright:1.2.0", path=path)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_sidecar_port_must_be_a_real_port(port):
    with pytest.raises(ValidationError):
        McpSidecar(image="mcp/playwright:1.2.0", port=port)


def test_unpinned_agent_image_with_registry_port_is_rejected():
    with pytest.raises(ValidationError, match="pinned"):
        make_manifest(spec=_spec_with(runtime={"image": "registry.local:5000/malkuth/agent"}))


def test_digest_pinned_agent_image_is_accepted():
    image = "malkuth/agent-base@sha256:" + "b" * 64

    manifest = make_manifest(spec=_spec_with(runtime={"image": image}))

    assert manifest.spec.runtime.image == image


@pytest.mark.parametrize(
    "command",
    [
        ["/usr/bin/bash", "-c", "x"],
        ["/bin/zsh", "-c", "x"],
        ["/usr/bin/env", "mcp-server"],
        ["dash", "-c", "x"],
        ["fish", "-c", "x"],
    ],
)
def test_shell_commands_are_rejected_by_basename(command):
    """경로를 붙여도 셸은 셸이다 — basename 으로 판정해야 우회되지 않는다."""
    with pytest.raises(ValidationError, match="installed executable"):
        make_manifest(
            spec=_spec_with(
                mcp={"servers": [{"name": "fs", "transport": "stdio", "command": command}]}
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("cpu", "not-a-number"), ("cpu", "1.0.0"), ("memory", "1GB"), ("memory", "1024")],
)
def test_invalid_group_quota_is_rejected_at_schema_time(field, value):
    """quota 오타는 집계 시점이 아니라 배포 검증에서 잡혀야 한다."""
    with pytest.raises(ValidationError):
        GroupQuotas(**{field: value})


def test_valid_group_quota_is_accepted():
    quota = GroupQuotas(cpu="8.0", memory="16Gi", max_agents=10)

    assert quota.cpu_cores == 8.0
    assert quota.memory_bytes == 16 * 1024**3


def test_empty_group_quota_has_no_limits():
    quota = GroupQuotas()

    assert quota.cpu_cores is None
    assert quota.memory_bytes is None


def test_builder_merges_nested_overrides_recursively():
    """override 는 필요한 필드만 바꾸고 나머지 기본값을 보존해야 한다."""
    raw = manifest_dict(spec={"model": {"name": "claude-opus-5"}})

    assert raw["spec"]["model"]["name"] == "claude-opus-5"
    assert raw["spec"]["model"]["provider"] == "anthropic"  # 기존 기본값 유지
    assert raw["spec"]["promptset"]["ref"] == "promptsets/test@0.1.0"


def test_builder_replaces_non_mapping_values():
    raw = manifest_dict(spec={"skillsets": [{"ref": "skillsets/x@0.1.0"}]})

    assert raw["spec"]["skillsets"] == [{"ref": "skillsets/x@0.1.0"}]


# --- 이그레스 목적지 (#293) ------------------------------------------------------------


@pytest.mark.parametrize(
    "destination", ["api.example.com", "fake-provider:8000", "localhost", "a-b.c-d.example.com:443"]
)
def test_an_explicit_egress_destination_is_accepted(destination):
    from malkuth.core.manifest import RuntimeSpec

    assert RuntimeSpec(egress=(destination,)).egress == (destination,)


@pytest.mark.parametrize(
    "destination",
    [
        "*.example.com",
        "*",
        "https://api.example.com",
        "api.example.com/path",
        "API.example.com",
        "",
        "host:",
    ],
)
def test_a_vague_egress_destination_is_refused(destination):
    """와일드카드·스킴·경로를 받으면 선언이 "어디든" 이 된다."""
    from pydantic import ValidationError

    from malkuth.core.manifest import RuntimeSpec

    with pytest.raises(ValidationError):
        RuntimeSpec(egress=(destination,))


@pytest.mark.parametrize(
    "destination", ["api.example.com:0", "api.example.com:65536", "api.example.com:99999"]
)
def test_an_egress_port_outside_the_valid_range_is_refused(destination):
    """범위 밖 포트는 어떤 CONNECT 와도 맞지 않는다 — 선언이 조용히 쓸모없어진다 (#294 리뷰)."""
    from pydantic import ValidationError

    from malkuth.core.manifest import RuntimeSpec

    with pytest.raises(ValidationError):
        RuntimeSpec(egress=(destination,))


@pytest.mark.parametrize(
    "url",
    [
        "https://mcp.example:99999/mcp",
        "ftp://mcp.example/mcp",
        "mcp.example/mcp",
        "https://:443/mcp",
        "https://user:secret@mcp.example/mcp",
        "https://token@mcp.example/mcp",
    ],
)
def test_an_external_mcp_url_must_be_a_valid_http_url(url):
    """판정이 선언을 풀다 터지면 그 에이전트의 판정 전체가 멈춘다 (#294 리뷰)."""
    from pydantic import ValidationError

    from malkuth.core.manifest import McpServerSpec

    with pytest.raises(ValidationError):
        McpServerSpec(name="corp", transport="streamable-http", url=url)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("agents/planner@0.1.0", ("planner", "0.1.0")),
        ("agents/feed-watcher@1.2.3", ("feed-watcher", "1.2.3")),
        # 형식이 틀려도 던지지 않는다 — 배포 검증이 "없는 에이전트" 로 보고한다
        ("agents/planner", ("planner", "")),
    ],
)
def test_an_agent_ref_splits_into_name_and_version(ref, expected):
    """노드가 가리키는 에이전트를 찾는 규칙은 이 하나뿐이다 (#319)."""
    from malkuth.core.manifest import agent_name, agent_ref_parts

    assert agent_ref_parts(ref) == expected
    assert agent_name(ref) == expected[0]


# --- 모델 회복력 선언 (#358) ---------------------------------------------------

PRIMARY = {"provider": "anthropic", "name": "claude-sonnet-5"}
HAIKU = {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]}


def test_a_model_without_resilience_keys_keeps_its_old_shape():
    model = make_manifest().spec.model

    assert (model.fallbacks, model.retry, model.circuit) == ((), None, None)
    assert model.providers() == frozenset({"anthropic"})


def test_a_full_resilience_declaration_loads():
    model = ModelConfig.model_validate(
        {
            **PRIMARY,
            "fallbacks": [HAIKU],
            "retry": {"rate_limit": {"start_s": 10, "attempts": 5}},
            "circuit": {"max_failures": 5, "reset_s": 60},
        }
    )

    assert model.fallbacks[0].on == ("rate_limit",)
    assert model.retry is not None and model.retry.rate_limit is not None
    assert model.circuit is not None and model.circuit.reset_s == 60


@pytest.mark.parametrize(
    "extra",
    [
        {"fallbacks": [{**PRIMARY, "on": ["rate_limit"]}]},  # 주 모델과 같다
        {"fallbacks": [HAIKU, HAIKU]},  # fallback 중복
        {"fallbacks": [{**HAIKU, "on": []}]},  # 빈 on
        {"fallbacks": [{**HAIKU, "on": ["rate_limit", "rate_limit"]}]},
        {"fallbacks": [{**HAIKU, "on": ["timeout"]}]},  # 모르는 trigger
        {"retry": {"network": {"start_s": 5, "attempts": 3, "max_s": 1}}},
        {"retry": {"network": {"start_s": 1, "attempts": 0}}},
        {"circuit": {"max_failures": 0, "reset_s": 60}},
        {"fallback": [HAIKU]},  # 오타 — 조용히 무시되면 백업이 없는 채로 돈다
    ],
)
def test_an_invalid_resilience_declaration_is_rejected(extra):
    with pytest.raises(ValidationError):
        ModelConfig.model_validate({**PRIMARY, **extra})


@pytest.mark.parametrize("model_cls", [ModelConfig, ModelOverride])
@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_max_tokens_is_rejected(model_cls, value):
    with pytest.raises(ValidationError):
        model_cls.model_validate({**PRIMARY, "max_tokens": value})


@pytest.mark.parametrize("model_cls", [ModelConfig, ModelOverride])
def test_a_positive_max_tokens_is_accepted(model_cls):
    assert model_cls.model_validate({**PRIMARY, "max_tokens": 1}).max_tokens == 1


def test_overrides_apply_nearest_last():
    """manifest → 노드 → 배포 — 가까운 쪽이 이긴다."""
    base = ModelConfig.model_validate({**PRIMARY, "max_tokens": 100, "fallbacks": [HAIKU]})
    node = ModelOverride.model_validate({"name": "claude-opus-5", "max_tokens": 200})
    deploy = ModelOverride.model_validate({"max_tokens": 300})

    resolved = base.overridden(node, None, deploy)

    assert (resolved.provider, resolved.name, resolved.max_tokens) == (
        "anthropic",
        "claude-opus-5",
        300,
    )
    assert resolved.fallbacks == base.fallbacks  # 명시하지 않은 키는 아래 계층을 지킨다


def test_an_explicit_empty_override_clears_the_lower_layer():
    base = ModelConfig.model_validate(
        {**PRIMARY, "fallbacks": [HAIKU], "circuit": {"max_failures": 5, "reset_s": 60}}
    )

    resolved = base.overridden(ModelOverride.model_validate({"fallbacks": [], "circuit": None}))

    assert (resolved.fallbacks, resolved.circuit) == ((), None)


def test_null_fallbacks_override_clears_the_lower_layer():
    base = ModelConfig.model_validate({**PRIMARY, "fallbacks": [HAIKU]})

    assert base.overridden(ModelOverride.model_validate({"fallbacks": None})).fallbacks == ()


def test_an_override_that_collides_with_a_fallback_is_rejected_after_resolution():
    base = ModelConfig.model_validate({**PRIMARY, "fallbacks": [HAIKU]})

    with pytest.raises(ValidationError):
        base.overridden(ModelOverride.model_validate({"name": "claude-haiku-4-5"}))


@pytest.mark.parametrize(
    "override",
    [{"provider": "anthropic"}, {"provider": None, "name": "x"}, {"name": None}, {"temp": 1}],
)
def test_an_invalid_override_is_rejected(override):
    with pytest.raises(ValidationError):
        ModelOverride.model_validate(override)


def test_merged_lets_the_later_override_win_only_where_it_speaks():
    node = ModelOverride.model_validate({"name": "claude-opus-5", "max_tokens": 200})
    deploy = ModelOverride.model_validate({"max_tokens": 300})

    merged = node.merged(deploy)

    assert (merged.name, merged.max_tokens) == ("claude-opus-5", 300)
    assert merged.model_fields_set == {"name", "max_tokens"}
