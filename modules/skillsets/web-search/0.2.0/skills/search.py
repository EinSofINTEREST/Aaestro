"""Web search skill.

웹 검색 스킬. 실제 provider 호출은 SkillContext 가 주입하는 자원으로 수행한다 —
모듈 레벨에서 클라이언트를 만들면 import 시점에 부수효과가 생긴다.
"""

from __future__ import annotations

from typing import Any

from aaestro.core.skill import SkillContext, skill


@skill
async def search(ctx: SkillContext, query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """웹 검색을 수행하고 상위 결과를 반환합니다.

    Args:
        query: 검색 질의
        max_results: 최대 결과 개수
    """
    api_key = ctx.secrets.get("SEARCH_API_KEY")
    if not api_key:
        # 자격증명 없이 조용히 빈 결과를 주면 모델이 "검색 결과가 없다" 고 오해한다
        raise RuntimeError("SEARCH_API_KEY is not available")

    ctx.log.info("web search requested", tool="search", max_results=max_results)
    raise NotImplementedError("search provider binding is supplied by the deployment")
