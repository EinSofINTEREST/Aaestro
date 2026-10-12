"""Scoped secret resolution.

리소스 스코프 해석 — ``env_allowlist`` 의 각 키를 **local > group > global** 순으로
해석한다. 가까운 스코프가 우선하며(shadowing 허용), 그룹 스코프 키는 그 그룹의
``secrets`` 목록에 선언된 것만 멤버에게 제공된다.

이 모듈은 값을 다루므로, 어떤 예외/로그에도 secret 값을 싣지 않는다 —
노출되는 것은 키 이름과 출처 스코프뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import RESERVED_GLOBAL_GROUP

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.core.manifest import AgentManifest, GroupManifest


class SecretScope(StrEnum):
    """Secret 이 해석된 출처 스코프.

    해석 결과 추적(감사/디버깅)에 쓰인다 — 값이 아니라 출처만 남긴다.
    """

    LOCAL = "local"
    GROUP = "group"
    GLOBAL = "global"


@dataclass(frozen=True)
class ResolvedSecret:
    """A resolved secret key and where it came from.

    해석된 secret. ``value`` 는 주입에만 쓰이며 로그/에러에 실리지 않는다.
    """

    key: str
    # repr 에 값이 실리면 객체를 로깅하는 것만으로 secret 이 새어나간다
    value: str = field(repr=False)
    scope: SecretScope = SecretScope.LOCAL
    group: str | None = None

    def describe(self) -> dict[str, str]:
        """Render an audit-safe description (never the value).

        감사 로그용 표현 — 값은 포함하지 않는다.
        """
        described = {"key": self.key, "scope": str(self.scope)}
        if self.group is not None:
            described["group"] = self.group
        return described


class ScopedSecrets:
    """Resolves declared secret keys across local / group / global scopes.

    선언된 secret 키를 스코프 체인으로 해석한다.
    """

    def __init__(
        self,
        *,
        local: Mapping[str, str] | None = None,
        group: Mapping[str, str] | None = None,
        global_: Mapping[str, str] | None = None,
        group_name: str | None = None,
        group_declared: frozenset[str] | None = None,
        allowlist: frozenset[str] | None = None,
    ) -> None:
        self._local = dict(local or {})
        self._group = dict(group or {})
        self._global = dict(global_ or {})
        self._group_name = group_name
        # 그룹이 제공하기로 선언한 키 — 미선언 키는 멤버에게도 보이지 않는다
        self._group_declared = group_declared if group_declared is not None else frozenset()
        # 에이전트가 선언한 env_allowlist — None 이면 제한 없음 (테스트/저수준 사용)
        self._allowlist = allowlist

    @classmethod
    def for_agent(
        cls,
        manifest: AgentManifest,
        *,
        groups: Mapping[str, GroupManifest] | None = None,
        local: Mapping[str, str] | None = None,
        group_values: Mapping[str, str] | None = None,
        global_values: Mapping[str, str] | None = None,
    ) -> ScopedSecrets:
        """Build a resolver for an agent's group membership.

        에이전트의 소속에 맞는 해석기를 만듭니다.

        Args:
            manifest: The agent manifest declaring membership and allowlist.
            groups: Known group definitions keyed by name.
            local: Agent-scoped secret values.
            group_values: Group-scoped secret values.
            global_values: Global-scoped secret values.

        Returns:
            A resolver bound to this agent's scope chain.
        """
        group_name = manifest.metadata.group
        declared: frozenset[str] = frozenset()

        if group_name is not None and groups is not None:
            definition = groups.get(group_name)
            if definition is not None:
                declared = frozenset(definition.spec.secrets)

        return cls(
            local=local,
            group=group_values,
            global_=global_values,
            group_name=group_name,
            group_declared=declared,
            allowlist=frozenset(manifest.spec.runtime.env_allowlist),
        )

    def resolve(self, key: str) -> ResolvedSecret:
        """Resolve a single key through the scope chain.

        키를 local > group > global 순으로 해석합니다.

        Args:
            key: The environment key to resolve.

        Returns:
            The resolved secret with its originating scope.

        Raises:
            AaestroError: CONFIG/``CFG_002`` if the key resolves in no scope.
        """
        # 선언되지 않은 키는 어떤 스코프에서도 해석하지 않는다 —
        # 그러지 않으면 에이전트가 allowlist 밖의 secret 을 읽을 수 있다
        if self._allowlist is not None and key not in self._allowlist:
            raise AaestroError(
                category=ErrorCategory.CONFIG,
                code=ErrorCode.CFG_002,
                message=f"secret key is not in the agent env_allowlist: {key}",
                details={"key": key, "group": self._group_name or RESERVED_GLOBAL_GROUP},
            )

        if key in self._local:
            return ResolvedSecret(key, self._local[key], SecretScope.LOCAL)

        # 그룹 값은 그 그룹이 제공하기로 선언한 키에 한한다 —
        # 비멤버는 물론, 멤버라도 미선언 키는 그룹 값으로 해석되지 않는다
        if self._group_name is not None and key in self._group_declared and key in self._group:
            return ResolvedSecret(key, self._group[key], SecretScope.GROUP, group=self._group_name)

        if key in self._global:
            return ResolvedSecret(key, self._global[key], SecretScope.GLOBAL)

        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_002,
            message=f"secret key cannot be resolved in any scope: {key}",
            details={
                "key": key,
                "group": self._group_name or RESERVED_GLOBAL_GROUP,
            },
        )

    def resolve_all(self, keys: tuple[str, ...]) -> dict[str, ResolvedSecret]:
        """Resolve every declared key, failing on the first unresolvable one.

        선언된 키를 전부 해석합니다 — 배포 검증에서 이 결과로 기동 가부를 판정합니다.

        Args:
            keys: The agent's ``env_allowlist``.

        Returns:
            Mapping of key to its resolution.

        Raises:
            AaestroError: CONFIG/``CFG_002`` on the first key that resolves nowhere.
        """
        return {key: self.resolve(key) for key in keys}

    def env_for(self, keys: tuple[str, ...]) -> dict[str, str]:
        """Build the container environment mapping for declared keys.

        컨테이너에 주입할 env 매핑을 만듭니다 — allowlist 에 있는 키만 포함됩니다.
        """
        return {key: resolved.value for key, resolved in self.resolve_all(keys).items()}

    def get(self, key: str) -> str | None:
        """Look up a key, returning ``None`` when it resolves nowhere.

        ``SecretsProvider`` 계약 구현 — 에이전트 코드가 쓰는 조회 경로입니다.
        """
        try:
            return self.resolve(key).value
        except AaestroError:
            return None
