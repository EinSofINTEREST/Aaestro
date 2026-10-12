"""Per-agent Control API tokens.

runtime 이 에이전트마다 발급하고, 컨테이너에 env 로 주입하며, 자신이
호출할 때 같은 값을 싣는다 — **발급처와 사용처가 하나**여야 토큰이 어긋나지
않는다 (02 API Rules 3).

토큰은 프로세스 메모리에만 존재한다. 파일이나 이미지에 남기지 않는다.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

AGENT_TOKEN_ENV = "AAESTRO_AGENT_TOKEN"  # noqa: S105 — 값이 아니라 키 이름이다
if TYPE_CHECKING:
    from collections.abc import Mapping

TOKEN_BYTES = 32
"""토큰 엔트로피 — 추측 공격을 실질적으로 배제하는 길이."""


def generate_token() -> str:
    """Mint one Control API token.

    Control API 토큰 하나를 발급합니다 — 예측 불가능한 난수여야 하므로
    ``secrets`` 를 씁니다.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


@dataclass
class TokenIssuer:
    """Issues and remembers per-agent tokens.

    에이전트별 토큰을 발급하고 기억한다. 같은 에이전트에 두 번 발급하면
    이미 기동한 컨테이너의 토큰과 어긋나므로, **재발급은 명시적으로**
    (``rotate``) 요청해야 한다.
    """

    _tokens: dict[str, str] = field(default_factory=dict, init=False)

    def issue(self, agent: str) -> str:
        """Return this agent's token, minting one on first use.

        에이전트의 토큰을 돌려줍니다 — 처음이면 새로 발급합니다.

        Args:
            agent: The agent name.

        Returns:
            The token to inject into the container and to present when calling it.
        """
        if agent not in self._tokens:
            self._tokens[agent] = generate_token()
        return self._tokens[agent]

    def rotate(self, agent: str) -> str:
        """Mint a fresh token, invalidating the previous one.

        새 토큰을 발급합니다 — 기존 컨테이너는 이 값을 모르므로 **재기동이
        전제**입니다. 그룹 이동이나 자격 갱신 시에만 씁니다.
        """
        self._tokens[agent] = generate_token()
        return self._tokens[agent]

    def remember(self, agent: str, token: str) -> None:
        """이미 기동한 컨테이너의 토큰을 **기억**한다 — 재발급이 아니다.

        재시작 뒤 붙는 경로(#243)에서 쓴다. agentd 는 env 로 받은 토큰을
        바꿀 수 없으므로, 새로 발급하면 그 컨테이너와 영원히 어긋난다.
        """
        self._tokens[agent] = token

    def known(self, agent: str) -> str | None:
        """이미 발급된 토큰 — 없으면 None (발급 부수효과 없음)."""
        return self._tokens.get(agent)

    def forget(self, agent: str) -> None:
        """에이전트가 정리되면 토큰도 버린다 — 죽은 토큰을 들고 있지 않는다."""
        self._tokens.pop(agent, None)

    def env_for(self, agent: str) -> dict[str, str]:
        """Build the token environment for a container.

        컨테이너에 주입할 토큰 env 를 만듭니다.

        이 매핑은 manifest 의 ``env_allowlist`` 와 무관합니다 — allowlist 는
        **운영자가 선언한 secret** 을 통제하는 장치이고, 이 토큰은 runtime 이
        자신과 에이전트 사이에만 쓰는 내부 자격입니다.
        """
        return {AGENT_TOKEN_ENV: self.issue(agent)}


def authenticated_env(
    issuer: TokenIssuer, agent: str, declared: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Merge the agent's token into its declared secret environment.

    선언된 secret 환경에 에이전트 토큰을 합칩니다.

    토큰이 **나중에** 병합되므로, 운영자가 실수로 같은 키를 secret 으로
    선언해도 runtime 이 발급한 값이 이깁니다 — 컨테이너가 runtime 이 모르는
    토큰으로 뜨면 모든 호출이 401 이 됩니다.

    Args:
        issuer: The token issuer that also serves the caller side.
        agent: The agent being started.
        declared: Secret values resolved from the scope chain.

    Returns:
        The container environment mapping.
    """
    return {**dict(declared or {}), **issuer.env_for(agent)}


__all__ = [
    "AGENT_TOKEN_ENV",
    "TOKEN_BYTES",
    "TokenIssuer",
    "authenticated_env",
    "generate_token",
]
