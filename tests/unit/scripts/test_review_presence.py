"""Review Presence 판정 — #514 의 "리뷰로 인정하는 것" (신뢰 계정으로 좁힘) 을 경우마다 고정한다.

fixture 는 실제 REST 응답 (PR #500 · #508 · #512, 2026-10-26) 에서 판정이 읽는 필드만 남긴 모양이다.
배선 테스트는 `_gh` 를 경로별 가짜 GitHub 으로 바꿔 `evaluate` · `main` 을 통째로 돌린다.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from scripts.ci import review_presence
from scripts.ci.review_presence import (
    CONTEXT,
    DESCRIPTION_LIMIT,
    WAITING,
    Reaction,
    Review,
    judge,
    to_reactions,
    to_reviews,
)

AUTHOR = "juhy0987"
CODEX = "chatgpt-codex-connector[bot]"
COPILOT = "copilot-pull-request-reviewer[bot]"
ACTIONS = "github-actions[bot]"
REPO = "o/r"
SHA = "a301ee2cb5b182ddd2054397350a6d60c5c802c3"

# PR #500 — Copilot 이 할당량 부족으로 본문만 남긴 리뷰
COPILOT_QUOTA_REVIEW: dict[str, Any] = {
    "id": 5642936749,
    "user": {"login": COPILOT, "type": "Bot"},
    "state": "COMMENTED",
    "author_association": "NONE",
    "body": "Copilot was unable to review this pull request because the user who requested "
    "the review has insufficient remaining quota.",
}
# PR #512 — Codex 리뷰와 그 리뷰에 달린 라인 코멘트
CODEX_REVIEW: dict[str, Any] = {
    "id": 5653051816,
    "user": {"login": CODEX, "type": "Bot"},
    "state": "COMMENTED",
    "author_association": "NONE",
    "body": "<!-- codex-review-receipt:v1 -->\n\n### Codex Review",
}
CODEX_LINE_COMMENT: dict[str, Any] = {
    "id": 4377988979,
    "pull_request_review_id": 5653051816,
    "user": {"login": CODEX, "type": "Bot"},
}
# PR #508 — Codex 가 지적 없이 끝낸 표시. 리액션의 user.type 은 User 로 온다
CODEX_THUMBS_UP: dict[str, Any] = {"content": "+1", "user": {"login": CODEX, "type": "User"}}
CODEX_EYES: dict[str, Any] = {"content": "eyes", "user": {"login": CODEX, "type": "User"}}


def review(
    login: str = "alice",
    *,
    state: str = "COMMENTED",
    body: str = "",
    lines: int = 0,
    bot: bool = False,
    association: str = "MEMBER",
) -> Review:
    return Review(login, bot, state, body, lines, association)


def raw_review(
    review_id: int, login: str, state: str, *, association: str = "MEMBER", body: str = ""
) -> dict[str, Any]:
    kind = "Bot" if login.endswith("[bot]") else "User"
    return {
        "id": review_id,
        "user": {"login": login, "type": kind},
        "state": state,
        "author_association": association,
        "body": body,
    }


# --- judge: 정의의 각 경우 -------------------------------------------------------------


def test_judge_pr_500_shape_copilot_quota_and_eyes_is_waiting() -> None:
    verdict = judge(AUTHOR, to_reviews([COPILOT_QUOTA_REVIEW], []), to_reactions([CODEX_EYES]))

    assert verdict.passed is False
    assert verdict.reason == WAITING


def test_judge_pr_512_shape_codex_line_comment_review_passes() -> None:
    reviews = to_reviews([COPILOT_QUOTA_REVIEW, CODEX_REVIEW], [CODEX_LINE_COMMENT])

    verdict = judge(AUTHOR, reviews, [])

    assert verdict.passed is True
    assert CODEX in verdict.reason


def test_judge_pr_508_shape_codex_thumbs_up_passes() -> None:
    assert judge(AUTHOR, [], to_reactions([CODEX_THUMBS_UP])).passed is True


def test_judge_no_reviews_is_waiting() -> None:
    assert judge(AUTHOR, [], []).passed is False


@pytest.mark.parametrize("state", ["APPROVED", "CHANGES_REQUESTED"])
@pytest.mark.parametrize("who", [review(), review(CODEX, bot=True, association="NONE")])
def test_judge_verdict_review_by_trusted_account_passes(state: str, who: Review) -> None:
    trusted = Review(who.login, who.is_bot, state, "", 0, who.association)

    assert judge(AUTHOR, [trusted], []).passed is True


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_judge_trusted_human_associations_pass(association: str) -> None:
    assert judge(AUTHOR, [review(state="APPROVED", association=association)], []).passed is True


@pytest.mark.parametrize("association", ["NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"])
def test_judge_outsider_review_is_waiting(association: str) -> None:
    outsider = review(state="APPROVED", body="lgtm", lines=2, association=association)

    assert judge(AUTHOR, [outsider], []).passed is False


@pytest.mark.parametrize("state", ["COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED"])
def test_judge_unlisted_bot_review_is_waiting(state: str) -> None:
    # PR 이 다른 워크플로로 github-actions[bot] 명의 리뷰를 만들어도 게이트가 열리지 않는다
    fabricated = review(ACTIONS, state=state, body="ok", lines=1, bot=True, association="NONE")

    assert judge(AUTHOR, [fabricated], []).passed is False


@pytest.mark.parametrize("state", ["COMMENTED", "APPROVED", "CHANGES_REQUESTED"])
def test_judge_line_comment_review_passes_in_any_state(state: str) -> None:
    bot_review = review(COPILOT, state=state, lines=1, bot=True, association="NONE")

    assert judge(AUTHOR, [bot_review], []).passed is True


def test_judge_human_comment_review_with_body_passes() -> None:
    assert judge(AUTHOR, [review(body="looks fine")], []).passed is True


@pytest.mark.parametrize("body", ["", "   \n"])
def test_judge_human_comment_review_without_body_is_waiting(body: str) -> None:
    assert judge(AUTHOR, [review(body=body)], []).passed is False


def test_judge_listed_bot_body_only_comment_review_is_waiting() -> None:
    body_only = review(COPILOT, body="summary", bot=True, association="NONE")

    assert judge(AUTHOR, [body_only], []).passed is False


def test_judge_listed_bot_with_user_type_is_still_not_a_human_comment() -> None:
    # Codex 리액션처럼 type 이 User 로 와도 allowlist 의 봇은 봇이다
    disguised = review(CODEX, body="summary", bot=False, association="MEMBER")

    assert judge(AUTHOR, [disguised], []).passed is False


@pytest.mark.parametrize(
    "own",
    [
        review(AUTHOR, state="APPROVED"),
        review(AUTHOR, lines=3),
        review(AUTHOR, body="self note"),
    ],
)
def test_judge_author_own_review_is_waiting(own: Review) -> None:
    assert judge(AUTHOR, [own], []).passed is False


@pytest.mark.parametrize("state", ["DISMISSED", "PENDING"])
def test_judge_dismissed_or_pending_review_is_waiting(state: str) -> None:
    assert judge(AUTHOR, [review(state=state, body="x", lines=2)], []).passed is False


def test_judge_thumbs_up_from_unlisted_account_is_waiting() -> None:
    reactions = [Reaction("alice", "+1"), Reaction(ACTIONS, "+1"), Reaction(AUTHOR, "+1")]

    assert judge(AUTHOR, [], reactions).passed is False


def test_judge_review_bots_list_is_configurable() -> None:
    assert judge(AUTHOR, [], [Reaction("x[bot]", "+1")], review_bots=["x[bot]"]).passed is True
    assert judge(AUTHOR, [], [Reaction(CODEX, "+1")], review_bots=["x[bot]"]).passed is False


def test_to_reviews_counts_line_comments_per_review() -> None:
    other = {**CODEX_LINE_COMMENT, "id": 1, "pull_request_review_id": 999}

    reviews = to_reviews([COPILOT_QUOTA_REVIEW, CODEX_REVIEW], [CODEX_LINE_COMMENT, other])

    assert [r.line_comments for r in reviews] == [0, 1]
    assert all(r.is_bot for r in reviews)


def test_to_reviews_reads_author_association() -> None:
    raw = raw_review(1, "alice", "APPROVED", association="COLLABORATOR")

    assert to_reviews([raw], [])[0].association == "COLLABORATOR"


def test_to_reviews_bot_suffix_counts_as_bot_even_if_type_says_user() -> None:
    raw = {**CODEX_REVIEW, "user": {"login": CODEX, "type": "User"}}

    assert to_reviews([raw], [])[0].is_bot is True


def test_to_reviews_deleted_account_is_not_a_human_comment() -> None:
    raw = {**COPILOT_QUOTA_REVIEW, "user": None, "body": "text", "author_association": "MEMBER"}

    assert judge(AUTHOR, to_reviews([raw], []), []).passed is False


# --- 배선: 가짜 GitHub 으로 evaluate · main 전체 -------------------------------------------


class FakeGitHub:
    """`gh api` 를 경로별로 흉내 낸다 — 응답은 실제 REST 모양이고, POST 는 기록 후 반영한다."""

    def __init__(self) -> None:
        self.prs: dict[int, dict[str, Any]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        self.posts: list[dict[str, str]] = []
        self.broken: set[int] = set()
        self.garbled: set[int] = set()
        self.headless: set[int] = set()

    def add_pr(
        self,
        number: int,
        *,
        author: str | None = AUTHOR,
        reviews: Sequence[dict[str, Any]] = (),
        comments: Sequence[dict[str, Any]] = (),
        reactions: Sequence[dict[str, Any]] = (),
        sha: str = SHA,
    ) -> None:
        self.prs[number] = {
            "pr": {
                "number": number,
                "user": None if author is None else {"login": author, "type": "User"},
                "head": {"sha": sha},
            },
            "reviews": list(reviews),
            "comments": list(comments),
            "reactions": list(reactions),
        }

    def __call__(self, args: Sequence[str]) -> str:
        args = list(args)
        if "POST" in args:
            return self._post(args)
        path = args[2] if args[1] == "--paginate" else args[1]
        parts = path.split("/")
        if path.endswith("pulls?state=open"):
            return self._lines([data["pr"] for data in self.prs.values()])
        if parts[3] == "commits":
            # combined status 의 `.statuses[]` — context 마다 최신 값 하나, 여러 context 가 섞인다
            latest: dict[str, dict[str, Any]] = {}
            for status in self.statuses.get(parts[4], []):
                latest[status["context"]] = status
            return self._lines(list(latest.values()))
        number = int(parts[4])
        if number in self.broken:
            raise subprocess.CalledProcessError(1, ["gh"], stderr="gh: Not Found (HTTP 404)")
        data = self.prs[number]
        if len(parts) == 5:
            if number in self.garbled:
                return "<html>bad gateway</html>"
            if number in self.headless:
                return json.dumps({k: v for k, v in data["pr"].items() if k != "head"})
            return json.dumps(data["pr"])
        return self._lines(data[parts[5]])

    def _post(self, args: list[str]) -> str:
        fields = dict(a.split("=", 1) for a in args if "=" in a)
        sha = args[args.index("POST") + 1].rsplit("/", 1)[1]
        self.posts.append({"sha": sha, **fields})
        self.statuses.setdefault(sha, []).append(fields)
        return "{}"

    @staticmethod
    def _lines(items: Sequence[dict[str, Any]]) -> str:
        return "".join(json.dumps(i) + "\n" for i in items)


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(review_presence, "_gh", fake)
    monkeypatch.delenv("REVIEW_BOTS", raising=False)
    return fake


def run(*args: str) -> int:
    return review_presence.main(["--repo", REPO, *args])


def test_evaluate_line_comment_review_passes(github: FakeGitHub) -> None:
    github.add_pr(512, reviews=[COPILOT_QUOTA_REVIEW, CODEX_REVIEW], comments=[CODEX_LINE_COMMENT])

    sha, verdict = review_presence.evaluate(REPO, 512, review_presence.DEFAULT_REVIEW_BOTS)

    assert sha == SHA
    assert verdict.passed is True


def test_evaluate_thumbs_up_reaction_passes(github: FakeGitHub) -> None:
    github.add_pr(508, reactions=[CODEX_THUMBS_UP])

    assert review_presence.evaluate(REPO, 508, [CODEX])[1].passed is True


@pytest.mark.parametrize(("state", "passed"), [("APPROVED", True), ("DISMISSED", False)])
def test_evaluate_reads_review_state(github: FakeGitHub, state: str, passed: bool) -> None:
    github.add_pr(1, reviews=[raw_review(1, "alice", state)], comments=[])

    assert review_presence.evaluate(REPO, 1, [CODEX])[1].passed is passed


def test_evaluate_dismissed_review_with_line_comments_is_waiting(github: FakeGitHub) -> None:
    comment = {**CODEX_LINE_COMMENT, "pull_request_review_id": 1}
    github.add_pr(1, reviews=[raw_review(1, "alice", "DISMISSED")], comments=[comment])

    assert review_presence.evaluate(REPO, 1, [CODEX])[1].passed is False


def test_evaluate_excludes_the_pr_author(github: FakeGitHub) -> None:
    github.add_pr(1, reviews=[raw_review(1, AUTHOR, "APPROVED", association="OWNER")])

    assert review_presence.evaluate(REPO, 1, [CODEX])[1].passed is False


def test_evaluate_deleted_pr_author_does_not_crash(github: FakeGitHub) -> None:
    github.add_pr(1, author=None, reviews=[raw_review(1, "alice", "APPROVED")])

    assert review_presence.evaluate(REPO, 1, [CODEX])[1].passed is True


def test_main_posts_status_when_none_exists(github: FakeGitHub) -> None:
    github.add_pr(512, reviews=[CODEX_REVIEW], comments=[CODEX_LINE_COMMENT])

    assert run("--pr", "512") == 0

    (post,) = github.posts
    assert post["sha"] == SHA
    assert post["state"] == "success"
    assert post["context"] == CONTEXT
    assert CODEX in post["description"]


def test_main_skips_write_when_status_already_matches(github: FakeGitHub) -> None:
    # sha · context 당 1000건 상한 — 같은 값을 schedule 마다 다시 쓰지 않는다
    github.add_pr(7)

    assert run("--pr", "7") == 0
    assert run("--pr", "7") == 0

    assert [p["state"] for p in github.posts] == ["pending"]


def test_main_overwrites_a_status_that_differs(github: FakeGitHub) -> None:
    # 위조된 success 는 판정 (pending) 과 다르므로 덮어쓴다
    github.add_pr(7)
    github.statuses[SHA] = [{"context": CONTEXT, "state": "success", "description": "forged"}]

    assert run("--pr", "7") == 0

    assert github.posts[-1]["state"] == "pending"
    assert github.posts[-1]["description"] == WAITING


def test_main_ignores_other_contexts_when_comparing(github: FakeGitHub) -> None:
    github.add_pr(7)
    github.statuses[SHA] = [{"context": "CodeRabbit", "state": "pending", "description": WAITING}]

    run("--pr", "7")

    assert len(github.posts) == 1


def test_main_finds_its_status_among_other_contexts(github: FakeGitHub) -> None:
    # 실제 combined status 처럼 다른 context 가 앞뒤에 있어도 자기 값을 찾아 같으면 쓰지 않는다
    github.add_pr(7)
    github.statuses[SHA] = [
        {"context": "CodeRabbit", "state": "success", "description": "Review rate limited"},
        {"context": CONTEXT, "state": "pending", "description": WAITING},
        {"context": "ci/other", "state": "failure", "description": "x"},
    ]

    run("--pr", "7")

    assert github.posts == []


def test_main_all_open_judges_every_open_pr(github: FakeGitHub) -> None:
    github.add_pr(1, sha="a" * 40)
    github.add_pr(2, sha="b" * 40, reactions=[CODEX_THUMBS_UP])

    assert run("--all-open") == 0

    assert {(p["sha"][0], p["state"]) for p in github.posts} == {("a", "pending"), ("b", "success")}


def test_main_review_bots_env_overrides_default(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    github.add_pr(1, sha="a" * 40, reviews=[raw_review(1, "custom[bot]", "APPROVED")])
    github.add_pr(2, sha="b" * 40, reactions=[CODEX_THUMBS_UP])
    monkeypatch.setenv("REVIEW_BOTS", "custom[bot], other[bot]")

    run("--pr", "1", "--pr", "2")

    assert [p["state"] for p in github.posts] == ["success", "pending"]


def test_main_description_fits_the_github_limit(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_bot = "b" * 200 + "[bot]"
    github.add_pr(1, reviews=[raw_review(1, long_bot, "APPROVED")])
    monkeypatch.setenv("REVIEW_BOTS", long_bot)

    run("--pr", "1")
    run("--pr", "1")

    assert len(github.posts[0]["description"]) == DESCRIPTION_LIMIT
    assert len(github.posts) == 1


def test_main_one_failure_skips_that_pr_and_exits_1(
    github: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    github.add_pr(1, sha="a" * 40)
    github.add_pr(2, sha="b" * 40)
    github.broken.add(1)

    assert run("--all-open") == 1

    assert [p["sha"][0] for p in github.posts] == ["b"]
    assert "#1 failed: gh: Not Found (HTTP 404)" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("kind", "error"), [("garbled", "JSONDecodeError"), ("headless", "KeyError")]
)
def test_main_malformed_response_skips_that_pr_and_exits_1(
    github: FakeGitHub, capsys: pytest.CaptureFixture[str], kind: str, error: str
) -> None:
    github.add_pr(1, sha="a" * 40)
    github.add_pr(2, sha="b" * 40)
    getattr(github, kind).add(1)

    assert run("--all-open") == 1

    assert [p["sha"][0] for p in github.posts] == ["b"]
    assert f"#1 failed: {error}" in capsys.readouterr().err


def test_main_dry_run_writes_nothing(github: FakeGitHub) -> None:
    github.add_pr(7)

    assert run("--pr", "7", "--dry-run") == 0

    assert github.posts == []


def test_workflow_review_bots_match_the_script_default() -> None:
    # 워크플로 env 가 기본값을 덮으므로 둘이 갈라지면 문서와 다른 목록으로 판정한다
    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/ci-review.yml"
    lines = workflow.read_text("utf-8").splitlines()
    line = next(line for line in lines if line.strip().startswith("REVIEW_BOTS:"))

    assert line.split(":", 1)[1].strip().split(",") == list(review_presence.DEFAULT_REVIEW_BOTS)
