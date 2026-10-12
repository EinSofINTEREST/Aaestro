"""Deploying a graph into real containers through the control plane (#243).

단위 테스트는 Docker 대역 위에서 돈다. 여기서는 **실제 Docker** 로 base 이미지에
선언을 실어 컨테이너를 세우고, health 로 Ready 를 판정하고, control plane 을
재시작해 다시 붙고, 해체한다 — 첫 실 배포에서 "첫 health 실패 직후 Ready" 가
드러났듯, 이 경계는 대역으로는 건너지 못한다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
import yaml

from tests.e2e.conftest import (
    AGENTS,
    GRAPH,
    REPO_ROOT,
    api,
    deployed_containers,
    plane_healthy,
    plane_url,
    start_plane,
    stop,
    until,
)
from tests.e2e.test_stack import docker, fetch, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]


def test_deploy_reattach_and_teardown(plane):
    # --- 배포: 선언을 실은 base 이미지 컨테이너가 health 를 지나 Ready 가 된다
    status, record = api("POST", "/v1/deployments", {"graph": GRAPH})
    assert status == 201, record
    assert record["status"] == "ready"
    assert sorted(a["name"] for a in record["agents"]) == sorted(AGENTS)
    assert all(a["image"] == "aaestro/agent-base:0.1.0" for a in record["agents"])

    containers = deployed_containers()
    assert sorted(containers) == [f"aaestro-{a}-0" for a in sorted(AGENTS)]
    # Ready 는 runtime 의 health 판정을 지난 뒤여야 한다 — 지금 바로 healthy 여야 맞다
    for agent in record["agents"]:
        health = fetch(f"http://127.0.0.1:{agent['control_port']}/v1/health")
        assert health["status"] in ("healthy", "degraded"), (agent["name"], health)

    # --- 배선: compose 가 손으로 적던 A2A env 를 runtime 이 그래프에서 만든다
    env = dict(
        line.split("=", 1)
        for line in docker(
            "inspect",
            "aaestro-researcher-0",
            "--format",
            "{{range .Config.Env}}{{println .}}{{end}}",
        ).splitlines()
        if "=" in line
    )
    assert env["AAESTRO_A2A_EDGES"] == "researcher>planner"
    planner_port = next(a["a2a_port"] for a in record["agents"] if a["name"] == "planner")
    assert env["AAESTRO_A2A_PEERS"] == f"planner=aaestro-planner-0:{planner_port}"
    assert env["ANTHROPIC_BASE_URL"] == "http://fake-provider:8000"
    assert "SEARCH_API_KEY" in env and "ANTHROPIC_API_KEY" in env
    mounts = docker(
        "inspect",
        "aaestro-researcher-0",
        "--format",
        "{{range .Mounts}}{{.Destination}}:{{.RW}} {{end}}",
    )
    assert "/app/declaration:false" in mounts and "/app/modules/promptsets:false" in mounts

    # --- UI (#245): 같은 프로세스가 화면을 서빙하고, 화면이 만드는 모양의 문서로 저작이 된다
    page = urllib.request.urlopen(f"{plane_url()}/ui/", timeout=10).read().decode()  # noqa: S310
    assert "<title>Aaestro</title>" in page
    draft = {
        "apiVersion": "aaestro/v1",
        "kind": "Graph",
        "metadata": {"name": "ui-e2e", "version": "0.1.0", "description": "made in the ui"},
        "spec": {
            "mode": "mission",
            "goal": "ui e2e",
            "state": {"schema": "aaestro.graphs.schemas:ResearchState"},
            "nodes": [
                {
                    "id": "planner",
                    "agent": "agents/planner@0.4.0",
                    # 템플릿의 필수 변수는 노드가 공급한다 — 없으면 검증이 거절한다 (#260)
                    "input_map": {"query": "state.query"},
                }
            ],
            "edges": [{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
            "connections": [],
        },
    }
    status, verdict = api("POST", "/v1/validate", {"graphs": [draft]})
    assert status == 200 and verdict["ok"], verdict
    try:
        status, saved = api("PUT", "/v1/graphs/ui-e2e", draft)
        assert status == 200, saved
        status, listed = api("GET", "/v1/graphs")
        assert "ui-e2e" in [g["name"] for g in listed["items"]]
    finally:
        status, _ = api("DELETE", "/v1/graphs/ui-e2e")  # 실패해도 저장소에 남기지 않는다
    assert status == 204

    # --- run 제출 (#244): 주소를 모르고도 배포에 run 을 내고, GET 으로 완주를 본다
    status, submitted = api(
        "POST", "/v1/runs", {"deployment_id": record["deployment_id"], "input": {"query": "e2e"}}
    )
    assert status == 202, submitted
    assert submitted["status"] == "running"

    def finished() -> dict | None:
        _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
        return current if current["status"] != "running" else None

    done = until(finished, what="run to finish")
    assert done["status"] == "completed", done
    assert done["state"].get("report"), done
    status, refused = api("POST", "/v1/runs", {"deployment_id": "dep-nope", "input": {}})
    assert status == 404, refused

    # --- 수정 보호: 배포 중인 에이전트는 덮어쓰지 못한다 (#242 in_use ↔ #243)
    manifest = yaml.safe_load((REPO_ROOT / "agents" / "planner" / "manifest.yaml").read_text())
    manifest["metadata"]["version"] = "99.0.0"
    status, body = api("PUT", "/v1/agents/planner", manifest)
    # #242 의 authoring 은 사용 중 선언을 검증 실패(VAL_002)로 거절한다
    assert status == 400 and "deployed" in body["error"]["message"], body

    # --- 재부착: control plane 을 죽였다 살려도 컨테이너와 기록이 이어진다
    stop(plane["process"])
    plane["process"] = start_plane(plane["config_dir"], plane["tokens_path"])
    until(plane_healthy, what="restarted control plane health")
    status, again = api("GET", f"/v1/deployments/{record['deployment_id']}")
    assert status == 200 and again["status"] == "ready", again
    assert [a["container_id"] for a in again["agents"]] == [
        a["container_id"] for a in record["agents"]
    ]

    # --- 감시/재시작: 죽은 컨테이너를 launcher 가 다시 세우고 기록이 그것을 따라간다
    #     (02 Lifecycle 6 — #213/#215 가 launcher 에 넣은 경로가 이 배포에서 실제로 돈다)
    old_writer = next(a for a in again["agents"] if a["name"] == "writer")
    docker("kill", "aaestro-writer-0")

    def replaced() -> dict | None:
        status, current = api("GET", f"/v1/deployments/{record['deployment_id']}")
        writer = next(a for a in current["agents"] if a["name"] == "writer")
        if writer["container_id"] == old_writer["container_id"]:
            return None
        try:
            health = fetch(f"http://127.0.0.1:{writer['control_port']}/v1/health")
        except Exception:  # noqa: BLE001 — 새 컨테이너가 아직 뜨는 중
            return None
        return writer if health["status"] in ("healthy", "degraded") else None

    new_writer = until(replaced, what="writer restarted after being killed")
    assert new_writer["container_id"] != old_writer["container_id"]
    assert deployed_containers()["aaestro-writer-0"].startswith("Up")

    # --- 해체: 컨테이너가 남지 않고 기록은 남는다
    status, stopped = api("DELETE", f"/v1/deployments/{record['deployment_id']}")
    assert status == 200 and stopped["status"] == "stopped"
    until(lambda: not deployed_containers(), what="containers removed", timeout_s=60)
    status, listed = api("GET", "/v1/deployments")
    assert [d["status"] for d in listed["items"]] == ["stopped"]


def test_a_graph_made_in_the_ui_can_be_deployed_run_and_destroyed(plane):
    """#239 완료 조건 — 화면에서 만든 그래프가 컨테이너까지 가서 돌고, 흔적 없이 사라진다.

    다른 테스트는 저장소에 이미 있는 레퍼런스 그래프를 배포한다. 여기서는 **화면이
    만드는 문서**로 시작한다 — 그 문서가 배포되고 완주하는지가 메인 이슈가 묻는 것이다.
    편집기가 `input_map` 을 만들지 못하던 동안에는 이 흐름이 run 에서 `MOD_004` 로
    죽었다 (#260).
    """
    made = {
        "apiVersion": "aaestro/v1",
        "kind": "Graph",
        "metadata": {"name": "ui-made", "version": "0.1.0", "description": "made in the ui"},
        "spec": {
            "mode": "mission",
            "goal": "made in the ui",
            "state": {"schema": "aaestro.graphs.schemas:ResearchState"},
            "nodes": [
                {
                    "id": "planner",
                    "agent": "agents/planner@0.4.0",
                    # 편집기의 노드 행이 만드는 매핑 — 없으면 템플릿의 필수 변수가 빈다
                    "input_map": {"query": "state.query"},
                    "output_map": {"plan": "output.plan"},
                }
            ],
            "edges": [{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
        },
    }

    # --- 매핑이 없으면 저장 전에 걸린다 (run 까지 가지 않는다)
    bare = json.loads(json.dumps(made))
    del bare["spec"]["nodes"][0]["input_map"]
    status, verdict = api("POST", "/v1/validate", {"graphs": [bare]})
    assert status == 200 and not verdict["ok"], verdict
    assert [f["check"] for f in verdict["findings"]] == ["node_templates"], verdict

    status, verdict = api("POST", "/v1/validate", {"graphs": [made]})
    assert status == 200 and verdict["ok"], verdict

    deployment_id = None
    try:
        status, saved = api("PUT", "/v1/graphs/ui-made", made)
        assert status == 200, saved

        # --- 만든 그래프를 그대로 배포한다
        status, deployment = api("POST", "/v1/deployments", {"graph": "ui-made"})
        assert status == 201, deployment
        deployment_id = deployment["deployment_id"]
        assert deployment["status"] == "ready", deployment
        assert [a["name"] for a in deployment["agents"]] == ["planner"]
        assert sorted(deployed_containers()) == ["aaestro-planner-0"]

        # --- 그 배포에 run 을 낸다
        status, submitted = api(
            "POST", "/v1/runs", {"deployment_id": deployment_id, "input": {"query": "why"}}
        )
        assert status == 202, submitted

        def finished() -> dict | None:
            _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
            return current if current["status"] != "running" else None

        done = until(finished, what="the run made in the ui to finish")
        assert done["status"] == "completed", done
        assert done["state"].get("plan"), done
    finally:
        if deployment_id is not None:
            api("DELETE", f"/v1/deployments/{deployment_id}")
        api("DELETE", "/v1/graphs/ui-made")

    # --- 해체하면 컨테이너도 선언도 남지 않는다
    until(lambda: not deployed_containers(), what="containers removed", timeout_s=60)
    status, listed = api("GET", "/v1/graphs")
    assert "ui-made" not in [g["name"] for g in listed["items"]]
