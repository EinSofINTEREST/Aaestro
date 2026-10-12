"""테스트용 fetch 스킬."""

from aaestro.core.skill import SkillContext, skill


@skill
async def fetch_page(ctx: SkillContext, url: str) -> str:
    """URL 의 본문 텍스트를 추출합니다."""
    return f"content of {url}"
