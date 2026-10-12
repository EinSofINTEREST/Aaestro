"""AgentCard generation.

Card 는 manifest 와 **실제 로드된 skillset** 에서 생성한다 — 수동 작성 금지
(03 AgentCard). 손으로 쓴 card 는 실제 능력과 어긋나고, peer 는 그 어긋남을
호출 실패로만 발견하게 된다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.core.manifest import AgentManifest
    from aaestro.core.skill import SkillSpec


class SkillCard(BaseModel):
    """One advertised skill.

    Card 에 실리는 skill 하나 — 실제 바인딩된 tool 에서 파생된다.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""


class AgentCard(BaseModel):
    """The A2A AgentCard for one agent.

    A2A AgentCard. Control API ``/v1/card`` 와 well-known 경로가 동일한 내용을
    제공한다.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    description: str = ""
    capabilities: dict[str, bool] = Field(default_factory=dict)
    skills: tuple[SkillCard, ...] = ()

    def skill_names(self) -> tuple[str, ...]:
        """광고 중인 skill 이름."""
        return tuple(s.name for s in self.skills)


def build_card(
    manifest: AgentManifest,
    tools: Sequence[SkillSpec | Any] | None = None,
) -> AgentCard:
    """Derive the AgentCard from a manifest and its loaded tools.

    manifest 와 실제 로드된 tool 로부터 card 를 생성합니다.

    Args:
        manifest: The validated agent manifest.
        tools: The tools actually bound at startup. Names without a
            description are advertised with an empty one.

    Returns:
        The generated card.
    """
    skills = tuple(
        SkillCard(
            name=getattr(tool, "name", str(tool)),
            description=getattr(tool, "description", ""),
        )
        for tool in (tools or ())
    )
    return AgentCard(
        name=manifest.name,
        version=manifest.metadata.version,
        description=manifest.metadata.description or "",
        capabilities={
            "streaming": manifest.spec.a2a.capabilities.streaming,
            "push_notifications": manifest.spec.a2a.capabilities.push_notifications,
        },
        skills=skills,
    )


__all__ = ["AgentCard", "SkillCard", "build_card"]
