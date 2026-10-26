"""Review Presence — judge whether a PR has at least one review and record it as a commit status.

PR 에 리뷰로 인정할 것이 하나 이상 있는지 판정하고 head 커밋의 commit status 로 남긴다 (#514).
판정 (`judge`) 은 순수 함수이고, GitHub 읽기 · 쓰기는 `gh api` 글루가 한다.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

CONTEXT = "Review Presence"
# 리뷰로 인정하는 봇 — 이 목록 밖의 봇 (github-actions[bot] 포함) 은 리뷰 · 라인 코멘트 · 👍 모두
# 불인정이다. PR 이 다른 워크플로로 봇 명의 리뷰를 만들어 게이트를 여는 것을 막는다.
# 워크플로 env REVIEW_BOTS (쉼표 구분) 가 있으면 그것으로 덮어쓴다
DEFAULT_REVIEW_BOTS = (
    "chatgpt-codex-connector[bot]",
    "copilot-pull-request-reviewer[bot]",
    "coderabbitai[bot]",
)
# 사람은 저장소에 권한이 있는 계정만 — 공개 저장소라 외부인 리뷰 하나로 통과시키지 않는다
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
WAITING = "waiting for at least one review"
# commit status description 의 GitHub 상한
DESCRIPTION_LIMIT = 140
# 철회된 리뷰와 제출 전 리뷰는 리뷰가 아니다 — dismissed 이벤트가 판정을 되돌리는 근거
IGNORED_STATES = frozenset({"DISMISSED", "PENDING"})
VERDICT_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED"})


@dataclass(frozen=True)
class Review:
    """One submitted review, reduced to what the judgment reads."""

    login: str
    is_bot: bool
    state: str
    body: str
    line_comments: int
    association: str = "NONE"


@dataclass(frozen=True)
class Reaction:
    """One reaction on the PR body."""

    login: str
    content: str


@dataclass(frozen=True)
class Verdict:
    """Judgment result — `passed` maps to success, otherwise pending."""

    passed: bool
    reason: str

    @property
    def state(self) -> str:
        return "success" if self.passed else "pending"

    @property
    def description(self) -> str:
        return self.reason[:DESCRIPTION_LIMIT]


def is_bot(login: str, account_type: str) -> bool:
    """Bot by account type or by the `[bot]` login suffix."""
    return account_type == "Bot" or login.endswith("[bot]")


def _counts(review: Review, author: str, bots: set[str]) -> str | None:
    """Reason this review counts, or None."""
    if review.login == author or review.state in IGNORED_STATES:
        return None
    listed_bot = review.login in bots
    human = not review.is_bot and not listed_bot
    if not listed_bot and not (human and review.association in TRUSTED_ASSOCIATIONS):
        return None
    if review.state in VERDICT_STATES:
        return f"{review.state.lower()} review by {review.login}"
    if review.line_comments > 0:
        return f"review with line comments by {review.login}"
    if review.state == "COMMENTED" and human and review.body.strip():
        return f"comment review by {review.login}"
    return None


def judge(
    author: str,
    reviews: Iterable[Review],
    reactions: Iterable[Reaction],
    review_bots: Iterable[str] = DEFAULT_REVIEW_BOTS,
) -> Verdict:
    """Return whether a trusted account other than the author reviewed the PR (#514)."""
    bots = set(review_bots)
    for review in reviews:
        reason = _counts(review, author, bots)
        if reason is not None:
            return Verdict(True, reason)
    for reaction in reactions:
        if reaction.content == "+1" and reaction.login in bots and reaction.login != author:
            return Verdict(True, f"+1 reaction by {reaction.login}")
    return Verdict(False, WAITING)


def to_reviews(
    raw_reviews: Iterable[dict[str, Any]], raw_comments: Iterable[dict[str, Any]]
) -> list[Review]:
    """Convert REST review and review-comment payloads into `Review`s."""
    per_review = Counter(c.get("pull_request_review_id") for c in raw_comments)
    reviews = []
    for raw in raw_reviews:
        # 탈퇴한 계정은 user 가 null 로 온다 — 누구인지 모르니 작성자도 사람도 아닌 것으로 본다
        user = raw.get("user") or {"login": "ghost", "type": "Bot"}
        reviews.append(
            Review(
                login=user["login"],
                is_bot=is_bot(user["login"], user.get("type", "")),
                state=raw["state"],
                body=raw.get("body") or "",
                line_comments=per_review[raw["id"]],
                association=raw.get("author_association") or "NONE",
            )
        )
    return reviews


def to_reactions(raw_reactions: Iterable[dict[str, Any]]) -> list[Reaction]:
    """Convert REST reaction payloads into `Reaction`s."""
    return [Reaction((r.get("user") or {}).get("login", ""), r["content"]) for r in raw_reactions]


def _gh(args: Sequence[str]) -> str:
    return subprocess.run(  # noqa: S603 — 고정된 gh 명령, 셸 없음
        ["gh", *args],  # noqa: S607 — runner 의 PATH 에 있는 gh
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _list(path: str, jq: str = ".[]") -> list[dict[str, Any]]:
    # --paginate 는 페이지마다 응답을 내므로 원소를 한 줄씩 받아 합친다
    out = _gh(["api", "--paginate", path, "--jq", jq])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def evaluate(repo: str, number: int, review_bots: Sequence[str]) -> tuple[str, Verdict]:
    """Fetch one PR and return (head sha, verdict)."""
    pr = json.loads(_gh(["api", f"repos/{repo}/pulls/{number}"]))
    reviews = to_reviews(
        _list(f"repos/{repo}/pulls/{number}/reviews"),
        _list(f"repos/{repo}/pulls/{number}/comments"),
    )
    reactions = to_reactions(_list(f"repos/{repo}/issues/{number}/reactions"))
    # 작성자 계정이 탈퇴했으면 user 가 null — 작성자 본인 제외 규칙이 걸릴 대상이 없다
    author = (pr.get("user") or {}).get("login", "")
    return pr["head"]["sha"], judge(author, reviews, reactions, review_bots)


def current_status(repo: str, sha: str) -> tuple[str, str] | None:
    """Latest (state, description) of the `Review Presence` status on `sha`, or None."""
    # combined status 는 context 마다 최신 값 하나만 싣는다. 거르기는 jq 가 아니라 여기서 한다 —
    # 테스트가 실제 응답 모양 (여러 context) 으로 이 비교를 검사할 수 있게
    statuses = _list(f"repos/{repo}/commits/{sha}/status", ".statuses[]")
    found = [s for s in statuses if s.get("context") == CONTEXT]
    return (found[0]["state"], found[0].get("description") or "") if found else None


def post_status(repo: str, sha: str, verdict: Verdict) -> bool:
    """Write the verdict on `sha` unless the latest status already says the same.

    sha · context 당 status 는 1000건이 상한이라 (넘으면 422) 같은 값을 반복해 쓰지 않는다.
    한 번 쓴 위조는 판정과 state · description 이 다르므로 다음 판정이 덮어쓴다. 쓰기 권한
    토큰으로 반복해 쓰는 위조는 이길 수 없고, 그 루프가 1000건을 채우면 이 쓰기가 실패한다.
    같은지는 (state, description) 로만 본다 — 누가 썼는지 (출처) 는 보지 않는다.
    Returns whether a status was written.
    """
    if current_status(repo, sha) == (verdict.state, verdict.description):
        return False
    _gh(
        [
            "api",
            "--method",
            "POST",
            f"repos/{repo}/statuses/{sha}",
            "-f",
            f"state={verdict.state}",
            "-f",
            f"context={CONTEXT}",
            "-f",
            f"description={verdict.description}",
        ]
    )
    return True


def _judge_one(repo: str, number: int, review_bots: Sequence[str], *, dry_run: bool) -> bool:
    """Judge and post one PR. Returns False on an API failure (already logged)."""
    # 판정 직후 곧바로 현재 status 를 읽고 쓴다 — 다른 PR 처리를 사이에 끼우지 않아, 판정과 쓰기
    # 사이에 들어온 철회를 오래된 판정이 덮는 창을 이 PR 의 API 호출 몇 번으로 줄인다
    try:
        sha, verdict = evaluate(repo, number, review_bots)
        if dry_run:
            action = "dry-run"
        else:
            action = "posted" if post_status(repo, sha, verdict) else "unchanged"
    except subprocess.CalledProcessError as err:
        print(f"#{number} failed: {err.stderr.strip() or err}", file=sys.stderr)
        return False
    # 응답 모양이 어긋난 PR 하나 (JSON 오류 · 빠진 키) 가 뒤의 판정을 막지 않게 — 실패는 종료 코드 1
    except Exception as err:  # noqa: BLE001
        print(f"#{number} failed: {type(err).__name__}: {err}", file=sys.stderr)
        return False
    print(f"#{number} {sha[:12]} {verdict.state} ({action}): {verdict.reason}")
    return True


def main(argv: Sequence[str] | None = None) -> int:
    """CLI — judge the given PRs (or every open PR) and post statuses unless --dry-run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--pr", type=int, action="append", help="PR number (repeatable)")
    parser.add_argument("--all-open", action="store_true", help="judge every open PR")
    parser.add_argument("--dry-run", action="store_true", help="print verdicts, write nothing")
    args = parser.parse_args(argv)
    if not args.pr and not args.all_open:
        parser.error("give --pr or --all-open")

    bots_env = os.environ.get("REVIEW_BOTS", "")
    review_bots = [b.strip() for b in bots_env.split(",") if b.strip()] or DEFAULT_REVIEW_BOTS
    numbers = list(args.pr or [])
    if args.all_open:
        numbers += [p["number"] for p in _list(f"repos/{args.repo}/pulls?state=open")]

    # PR 하나의 실패가 뒤의 판정을 막지 않게 끝까지 돌고, 실패가 있었으면 1 로 끝낸다
    results = [_judge_one(args.repo, n, review_bots, dry_run=args.dry_run) for n in numbers]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
