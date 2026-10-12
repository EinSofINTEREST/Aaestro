"""Cross-module compatibility rules.

모듈 간 호환성 검증 — 배포 검증(cli)이 소비하는 규칙 모음.
04-module-system.md Compatibility Rules 를 구현한다.
"""

from __future__ import annotations

from collections.abc import Iterable

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import AgentManifest
from aaestro.modules.promptset import DEFAULT_TEMPLATE, LoadedPromptset
from aaestro.modules.skillset import LoadedSkillset


def check_skillset_env(manifest: AgentManifest, skillset: LoadedSkillset) -> None:
    """Verify skillset env requirements are allowlisted by the agent.

    스킬셋의 ``requires.env`` 가 에이전트 ``env_allowlist`` 의 부분집합인지
    확인합니다 (Compatibility Rules 2).

    Raises:
        AaestroError: MODULE/``MOD_002`` if any required key is not allowlisted.
    """
    allowed = set(manifest.spec.runtime.env_allowlist)
    missing = [index for index, key in enumerate(skillset.required_env) if key not in allowed]
    if missing:
        # 키 이름이 아니라 스킬셋 선언 안의 위치로 가리킨다 (#386) — manifest · 배포 검증의 env 키
        # 검사와 같다: 그 자리에 비밀 값을 적는 실수가 에러로 나가지 않게
        positions = ", ".join(f"requires.env[{index}]" for index in missing)
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_002,
            message=f"skillset requires env keys absent from env_allowlist: {positions}",
            agent=manifest.name,
            details={"skillset": skillset.ref, "missing_env_index": missing},
        )


def check_promptset_templates(
    manifest: AgentManifest,
    promptset: LoadedPromptset,
    node_ids: Iterable[str],
    *,
    accepts_direct: bool = True,
) -> None:
    """Verify the promptset covers every node the agent is wired to.

    에이전트가 배선된 node_id 집합을 프롬프트셋이 모두 덮는지, 그리고 direct
    요청을 받는다면 ``default`` 템플릿이 있는지 확인합니다
    (Compatibility Rules 3, 4).

    Args:
        manifest: The agent manifest.
        promptset: The loaded promptset.
        node_ids: Graph node ids this agent is bound to.
        accepts_direct: Whether the agent must serve direct requests.

    Raises:
        AaestroError: MODULE/``MOD_002`` if a template is missing.
    """
    declared = promptset.template_names
    missing = sorted(set(node_ids) - declared)
    if missing:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_002,
            message=f"promptset is missing templates for nodes: {missing}",
            agent=manifest.name,
            details={"promptset": promptset.ref, "missing_templates": missing},
        )

    if accepts_direct and not promptset.has_default:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_002,
            message=(f"promptset must declare a '{DEFAULT_TEMPLATE}' template for direct requests"),
            agent=manifest.name,
            details={"promptset": promptset.ref},
        )


def check_tool_namespaces(
    skillsets: Iterable[LoadedSkillset], mcp_tool_names: Iterable[str] = ()
) -> None:
    """Verify tool names do not collide across skillsets and MCP servers.

    스킬셋 tool 과 MCP tool 의 네임스페이스 충돌을 검사합니다
    (03 MCP Rules 3 — MCP 는 ``mcp__{server}__{tool}`` 로 격리된다).

    Raises:
        AaestroError: MODULE/``MOD_002`` on any duplicate tool name.
    """
    seen: dict[str, str] = {}
    for skillset in skillsets:
        for item in skillset.skills:
            owner = seen.get(item.name)
            if owner is not None:
                raise AaestroError(
                    category=ErrorCategory.MODULE,
                    code=ErrorCode.MOD_002,
                    message=f"tool name collision: '{item.name}'",
                    details={"tool": item.name, "owners": [owner, skillset.ref]},
                )
            seen[item.name] = skillset.ref

    for name in mcp_tool_names:
        owner = seen.get(name)
        if owner is not None:
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_002,
                message=f"tool name collision: '{name}'",
                details={"tool": name, "owners": [owner, "mcp"]},
            )
        seen[name] = "mcp"
