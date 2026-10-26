"""Access registry routes — three audiences, three credentials (#277).

토큰 하나로 묶으면 강제 지점이 운영자 권한을, 권한 에이전트가 판정 조회를 덤으로 갖는다. 각 라우트가
**자기 호출자의 자격만** 받는지 본다.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from aaestro.core.errors import ErrorCode
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from tests.fixtures.access import KNOWLEDGE, STEWARD, access_workspace

CONTROL = "control-token"
ENFORCER = "enforcer-token"


@pytest.fixture
def registry(tmp_path: Path) -> AccessRegistry:
    catalog = Catalog.under(access_workspace(tmp_path))
    return AccessRegistry(
        store=InMemoryAccessStore(), catalog=catalog, stewards=frozenset({STEWARD})
    )


@pytest.fixture
async def api(registry):
    app = create_app(
        InMemoryRunStore(),
        catalog=registry.catalog,
        token=CONTROL,
        access=registry,
        enforcer_token=ENFORCER,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        yield client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


REVOCATION = {"agent": "worker", "kind": "egress", "target": "api.x", "reason": "incident"}
GRANT = {
    "agent": "worker",
    "kind": "memory",
    "target": KNOWLEDGE,
    "mode": "ro",
    "ttl_s": 60,
    "reason": "needs facts",
    "requested_by": "worker",
}


# --- 운영자 -------------------------------------------------------------------


async def test_the_operator_revokes_and_sees_the_record(api):
    created = await api.post("/v1/access/revocations", json=REVOCATION, headers=bearer(CONTROL))
    view = await api.get("/v1/access/agents/worker", headers=bearer(CONTROL))

    assert created.status_code == 201
    assert created.json()["effect"] == "deny" and created.json()["decided_by"] == "operator"
    assert [r["rule_id"] for r in view.json()["rules"]] == [created.json()["rule_id"]]


@pytest.mark.parametrize("token", [None, ENFORCER, "wrong"])
async def test_operator_routes_refuse_other_credentials(api, token):
    headers = bearer(token) if token else {}

    assert (
        await api.post("/v1/access/revocations", json=REVOCATION, headers=headers)
    ).status_code == 401
    assert (await api.get("/v1/access/agents/worker", headers=headers)).status_code == 401


async def test_lifting_ends_a_rule_without_deleting_it(api):
    created = (
        await api.post("/v1/access/revocations", json=REVOCATION, headers=bearer(CONTROL))
    ).json()

    lifted = await api.delete(f"/v1/access/rules/{created['rule_id']}", headers=bearer(CONTROL))
    view = (await api.get("/v1/access/agents/worker", headers=bearer(CONTROL))).json()

    assert lifted.json()["lifted_at"] is not None
    assert view["rules"][0]["lifted_at"] is not None


async def test_lifting_an_unknown_rule_is_404(api):
    assert (
        await api.delete("/v1/access/rules/rule-nope", headers=bearer(CONTROL))
    ).status_code == 404


@pytest.mark.parametrize(
    "change",
    [{"kind": "files"}, {"target": "api x"}, {"target": ""}, {"reason": ""}, {"extra": 1}],
)
async def test_a_malformed_revocation_is_400(api, change):
    response = await api.post(
        "/v1/access/revocations", json={**REVOCATION, **change}, headers=bearer(CONTROL)
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.VAL_002


# --- 권한 에이전트 --------------------------------------------------------------


async def test_the_permission_agent_grants_with_its_own_identity(api, registry):
    steward = registry.issue_identity(STEWARD, "dep-1")

    response = await api.post("/v1/access/grants", json=GRANT, headers=bearer(steward))

    assert response.status_code == 201
    assert response.json()["decided_by"] == STEWARD


@pytest.mark.parametrize("who", ["control", "enforcer", "worker", "none"])
async def test_grants_refuse_anyone_but_a_permission_agent(api, registry, who):
    """운영자 토큰도 부여하지 못한다 — 넓히기는 권한 에이전트만 (01 Access Control 3)."""
    credentials = {
        "control": CONTROL,
        "enforcer": ENFORCER,
        "worker": registry.issue_identity("worker", "dep-1"),
        "none": None,
    }
    headers = bearer(credentials[who]) if credentials[who] else {}

    response = await api.post("/v1/access/grants", json=GRANT, headers=headers)

    if who in ("none", "enforcer"):
        # 에이전트 경로에 닿는 자격이 아니면 라우팅 전에 401 이다 — 라우트 표를 열거 못 하게 (#462)
        assert response.status_code == 401
    else:
        assert response.status_code == 403
        assert response.json()["error"]["code"] in {ErrorCode.ACC_001, ErrorCode.ACC_003}
    assert registry.rules("worker") == []


# --- 강제 지점 ------------------------------------------------------------------


async def test_an_enforcer_gets_a_decision_for_a_credential(api, registry):
    worker = registry.issue_identity("worker", "dep-1")
    steward = registry.issue_identity(STEWARD, "dep-1")
    await api.post("/v1/access/grants", json=GRANT, headers=bearer(steward))

    response = await api.post(
        "/v1/access/decisions",
        json={"credential": worker, "kind": "memory", "target": KNOWLEDGE, "mode": "ro"},
        headers=bearer(ENFORCER),
    )

    body = response.json()
    assert body | {"version": 0, "valid_until": 0} == {
        "agent": "worker",
        "decision": "allow",
        "decided_by": STEWARD,
        "version": 0,
        "valid_until": 0,
    }
    assert body["valid_until"] is not None, (
        "부여의 만료가 판정에 실리지 않으면 캐시가 만료를 넘긴다"
    )


@pytest.mark.parametrize(
    ("path", "body", "token"),
    [
        ("/v1/access/revocations", REVOCATION | {"mode": "rw"}, CONTROL),
        ("/v1/access/grants", GRANT | {"kind": "egress", "target": "api.x"}, "steward"),
        ("/v1/access/grants", {k: v for k, v in GRANT.items() if k != "mode"}, "steward"),
        (
            "/v1/access/decisions",
            {"credential": "c", "kind": "egress", "target": "api.x", "mode": "ro"},
            ENFORCER,
        ),
        (
            "/v1/access/decisions",
            {"credential": "c", "kind": "memory", "target": KNOWLEDGE},
            ENFORCER,
        ),
    ],
)
async def test_a_mode_that_does_not_fit_the_kind_is_rejected(api, registry, path, body, token):
    """``egress, rw`` 를 받아 주면 요청자가 뜻하지 않은 "자원 전체" 기록이 남는다."""
    if token == "steward":
        token = registry.issue_identity(STEWARD, "dep-1")

    response = await api.post(path, json=body, headers=bearer(token))

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "VAL_002"
    assert not registry.rules("worker")


async def test_an_enforcer_learns_who_a_credential_belongs_to(api, registry):
    worker = registry.issue_identity("worker", "dep-1")

    known = await api.post(
        "/v1/access/identities", json={"credential": worker}, headers=bearer(ENFORCER)
    )
    unknown = await api.post(
        "/v1/access/identities", json={"credential": "made-up"}, headers=bearer(ENFORCER)
    )
    as_operator = await api.post(
        "/v1/access/identities", json={"credential": worker}, headers=bearer(CONTROL)
    )

    assert known.json()["agent"] == "worker"
    assert unknown.status_code == 200 and unknown.json()["agent"] is None
    assert as_operator.status_code == 401, "운영자 토큰으로 강제 지점 라우트를 부를 수 없다"


async def test_an_unknown_credential_is_a_cacheable_deny(api):
    response = await api.post(
        "/v1/access/decisions",
        json={"credential": "made-up", "kind": "egress", "target": "api.x"},
        headers=bearer(ENFORCER),
    )

    assert response.status_code == 200
    assert response.json()["agent"] is None and response.json()["decision"] == "deny"


@pytest.mark.parametrize("token", [None, CONTROL, "wrong"])
async def test_enforcer_routes_refuse_other_credentials(api, registry, token):
    """운영자 토큰으로도 판정을 조회하지 못한다 — 강제 지점의 토큰은 따로다."""
    headers = bearer(token) if token else {}
    body = {"credential": "x", "kind": "egress", "target": "api.x"}

    assert (await api.post("/v1/access/decisions", json=body, headers=headers)).status_code == 401
    assert (await api.get("/v1/access/changes?wait_s=0", headers=headers)).status_code == 401


async def test_the_change_feed_moves_after_a_revocation(api):
    before = (await api.get("/v1/access/changes?wait_s=0", headers=bearer(ENFORCER))).json()[
        "version"
    ]

    await api.post("/v1/access/revocations", json=REVOCATION, headers=bearer(CONTROL))
    after = await api.get(f"/v1/access/changes?after={before}&wait_s=5", headers=bearer(ENFORCER))

    assert after.json()["version"] > before


async def test_the_change_feed_caps_the_wait(api):
    response = await api.get("/v1/access/changes?wait_s=3600", headers=bearer(ENFORCER))

    assert response.status_code in (400, 422)


async def test_without_a_registry_the_routes_do_not_exist():
    app = create_app(InMemoryRunStore(), token=CONTROL)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://cp"
    ) as client:
        # 운영자 토큰을 가진 쪽에게 코드 없는 404 — 토큰 없이는 라우팅 전에 401 이다 (#462)
        response = await client.post("/v1/access/grants", json=GRANT, headers=bearer(CONTROL))
        assert response.status_code == 404


# --- A2A 호출 표 (#281) ---------------------------------------------------------


async def test_an_agent_gets_a_ticket_and_the_callee_verifies_it_with_its_own_identity(
    api, registry
):
    caller = registry.issue_identity("worker", "dep-1")
    callee = registry.issue_identity("peer", "dep-1")

    issued = await api.post(
        "/v1/access/a2a/tickets", json={"callee": "peer"}, headers=bearer(caller)
    )
    verified = await api.post(
        "/v1/access/a2a/verify", json={"ticket": issued.json()["ticket"]}, headers=bearer(callee)
    )

    assert issued.status_code == 201, issued.text
    body = verified.json()
    assert body["agent"] == "worker"
    assert body["decision"] == "deny", "이 작업공간에는 연결 선언이 없다 — 표는 허가가 아니다"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/access/a2a/tickets", {"callee": "peer"}),
        ("/v1/access/a2a/verify", {"ticket": "anything"}),
    ],
)
@pytest.mark.parametrize("token", [CONTROL, ENFORCER, "made-up"])
async def test_ticket_routes_take_only_an_agent_identity(api, path, body, token):
    """운영자 토큰도 강제 지점 토큰도 에이전트 신원이 아니다."""
    response = await api.post(path, json=body, headers=bearer(token))

    if token in ("made-up", ENFORCER):
        # 에이전트 경로에 닿는 청중의 자격이 아니면 라우팅 전에 401 이다 (#462)
        assert response.status_code == 401
        return
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACC_001"


async def test_the_change_feed_takes_an_enforcer_token_or_a_live_agent_identity(api, registry):
    agent = registry.issue_identity("worker", "dep-1")
    url = "/v1/access/changes?wait_s=0"

    assert (await api.get(url, headers=bearer(ENFORCER))).status_code == 200
    assert (await api.get(url, headers=bearer(agent))).status_code == 200
    assert (await api.get(url, headers=bearer(CONTROL))).status_code == 401
    registry.revoke_deployment("dep-1")
    assert (await api.get(url, headers=bearer(agent))).status_code == 401


async def test_the_operator_view_shows_declarations_ceilings_and_recent_denials(registry, api):
    """권한 화면이 한 번에 보는 것 — 기본 권한, 확장 상한, 최근 거부 (#283)."""
    from aaestro.access.baselines import EgressBaseline
    from aaestro.access.model import ResourceKind

    registry.baselines = {ResourceKind.EGRESS: EgressBaseline(registry.catalog)}
    registry.decide("worker", ResourceKind.EGRESS, "evil.example.com")
    registry.decide("worker", ResourceKind.EGRESS, "api.anthropic.com")

    view = (await api.get("/v1/access/agents/worker", headers=bearer(CONTROL))).json()

    assert {"kind": "egress", "target": "api.anthropic.com", "mode": None} in view["declared"]
    assert {c["group"] for c in view["ceilings"]} == {"global", "research"}
    research = next(c for c in view["ceilings"] if c["group"] == "research")
    assert research["max_ttl_s"] == 600 and research["egress"] == ["api.search.example.com"]
    [denial] = view["denials"]
    assert (denial["kind"], denial["target"], denial["decided_by"]) == (
        "egress",
        "evil.example.com",
        "default",
    )
