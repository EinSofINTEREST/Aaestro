"""테스트용 검색 스킬."""

from aaestro.core.skill import SkillContext, skill


@skill
async def search(ctx: SkillContext, query: str, max_results: int = 10) -> list[dict]:
    """웹 검색을 수행하고 상위 결과를 반환합니다.

    Args:
        query: 검색 질의
        max_results: 최대 결과 개수
    """
    return [{"title": query, "rank": i} for i in range(max_results)]
