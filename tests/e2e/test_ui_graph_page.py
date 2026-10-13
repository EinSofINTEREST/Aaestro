"""The whole graph editor page against a fake control plane (#385).

실제 index.html + app.js + client.js 를 `page.route` 로 서빙하고 `/v1/**` 를 가짜 서버로 답한다.
가짜 서버는 GET 에 `GraphTopology` 의 실제 직렬화(api_version · source/target · schema_ref ·
null 기본값)를, PUT · validate 에 같은 스키마 검증을 쓴다. `patchGraph` 만 부르는
`test_ui_graph_preserve.py` 가 못 보는 페이지 글루(불러오기 · 원본 해석 · select 채움 · 저장 뒤
원본 갱신)를 실행한다.
"""

from __future__ import annotations

import copy
import json
import re

import pytest
from pydantic import ValidationError

from aaestro.core.topology import GraphTopology
from aaestro.ui import UI_ROOT
from tests.e2e.test_stack import requires_docker
from tests.e2e.test_ui_browser import browser  # noqa: F401 — 같은 브라우저 fixture
from tests.e2e.test_ui_graph_preserve import DECLARATION

pytestmark = [pytest.mark.e2e, requires_docker]
ORIGIN = "http://ui.test"
WRITER = "agents/writer@0.4.0"
PLANNER = "agents/planner@0.4.0"
GONE = "agents/gone@9.9.9"


def seed() -> dict[str, dict]:
    other = copy.deepcopy(DECLARATION)
    other["metadata"]["name"] = "other"
    other["spec"]["nodes"] = [
        {"id": "writer", "agent": WRITER, "retry": 9},
        {"id": "extra", "agent": PLANNER},
    ]
    ghost = copy.deepcopy(DECLARATION)
    ghost["metadata"]["name"] = "ghost"
    ghost["spec"]["nodes"] = [{"id": "lost", "agent": GONE, "timeout_s": 12}]
    return {"rich": copy.deepcopy(DECLARATION), "other": other, "ghost": ghost}


class Fake:
    def __init__(self):
        self.graphs = seed()
        self.puts, self.validates, self.log = [], [], []
        self.fail_get = 0
        self.catalog_loads = 0

    def handle(self, route):
        req = route.request
        path = req.url.split(ORIGIN, 1)[1]
        method = req.method
        self.log.append(f"{method} {path}")
        body = json.loads(req.post_data) if req.post_data else None

        def ok(payload, status=200):
            route.fulfill(
                status=status,
                body="" if payload is None else json.dumps(payload),
                content_type="application/json",
            )

        def err(status, code, message, details=None):
            ok({"error": {"code": code, "message": message, "details": details or {}}}, status)

        def schema_errors(e):
            return {
                "errors": [
                    {"field": ".".join(map(str, x["loc"])), "problem": x["msg"]} for x in e.errors()
                ]
            }

        if path == "/v1/health":
            return ok({"status": "ok"})
        if path == "/v1/graphs" and method == "GET":
            self.catalog_loads += 1
            items = [
                {
                    "name": n,
                    "version": d["metadata"]["version"],
                    "description": "",
                    "mode": d["spec"]["mode"],
                    "goal": d["spec"]["goal"],
                    "nodes": len(d["spec"]["nodes"]),
                }
                for n, d in self.graphs.items()
            ]
            return ok({"items": items, "problems": []})
        if path == "/v1/agents":
            items = [
                {
                    "name": n,
                    "version": "0.4.0",
                    "group": None,
                    "description": "",
                    "model": {"provider": "anthropic", "name": "m"},
                }
                for n in ("writer", "planner")
            ]
            return ok({"items": items, "problems": []})
        if path == "/v1/groups":
            return ok({"items": [], "problems": []})
        if re.fullmatch(r"/v1/modules/\w+", path):
            return ok({"items": [], "problems": []})
        if path == "/v1/deployments":
            return ok({"items": []})
        if path == "/v1/runs":
            return ok([])
        if path == "/v1/validate":
            self.validates.append(body)
            try:
                for g in body["graphs"]:
                    GraphTopology.model_validate(g)
            except ValidationError as e:
                return err(400, "VAL_002", "invalid", schema_errors(e))
            return ok({"ok": True, "findings": []})
        m = re.fullmatch(r"/v1/graphs/([^/]+)", path)
        if m:
            name = m.group(1)
            if method == "GET":
                if self.fail_get:
                    self.fail_get -= 1
                    return err(500, "INTERNAL_001", "boom")
                if name not in self.graphs:
                    return err(404, "NF_001", "no such graph")
                return ok(GraphTopology.model_validate(self.graphs[name]).model_dump(mode="json"))
            if method == "PUT":
                self.puts.append((name, body))
                try:
                    GraphTopology.model_validate(body)
                except ValidationError as e:
                    return err(400, "VAL_002", "invalid", schema_errors(e))
                self.graphs[name] = body
                return ok({"name": name, "path": f"graphs/{name}.yaml"})
            if method == "DELETE":
                self.graphs.pop(name, None)
                return ok(None, 204)
        return ok({"detail": f"unhandled {method} {path}"}, 404)


def static(src, ctype):
    # 묶는 값은 클로저로 — 기본 인자로 받으면 playwright 가 넘기는 두 번째 인자(request)가 덮어쓴다
    return lambda route: route.fulfill(body=src, content_type=ctype)


class Ui:
    def __init__(self, browser):  # noqa: F811
        self.fake = Fake()
        self.context = browser.new_context()
        self.page = page = self.context.new_page()
        page.set_default_timeout(15_000)
        self.errors = []
        self.answer = ""
        page.on("pageerror", lambda e: self.errors.append(f"pageerror: {e}"))
        page.on(
            "console",
            lambda m: (
                self.errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None
            ),
        )
        page.on("dialog", lambda d: d.accept(self.answer) if d.type == "prompt" else d.accept())
        types = {
            "index.html": "text/html",
            "app.js": "text/javascript",
            "client.js": "text/javascript",
            "app.css": "text/css",
        }
        for name, ctype in types.items():
            src = (UI_ROOT / name).read_text(encoding="utf-8")
            url = f"{ORIGIN}/ui/" if name == "index.html" else f"{ORIGIN}/ui/{name}"
            page.route(url, static(src, ctype))
        page.route(f"{ORIGIN}/v1/**", self.fake.handle)
        page.goto(f"{ORIGIN}/ui/")
        page.wait_for_function("() => document.querySelector('#health').textContent === '연결됨'")
        page.wait_for_function("() => document.querySelectorAll('#catalog-graphs li').length > 0")
        page.click("#tabs button[data-tab=graph]")

    def close(self):
        self.context.close()

    def load(self, name):
        self.answer = name
        self.page.evaluate("() => { document.querySelector('#status').textContent = ''; }")
        self.page.click("#graph-load")
        self.page.wait_for_function("() => document.querySelector('#status').textContent !== ''")
        return self.page.text_content("#status")

    def nodes(self):
        return self.page.evaluate(
            """() => [...document.querySelectorAll('#graph-nodes tbody tr')].map((tr) => {
          const agent = tr.querySelector('[name=node_agent]');
          return { id: tr.querySelector('[name=node_id]').value,
            agent: agent.value, selectedIndex: agent.selectedIndex,
            selected: [...agent.selectedOptions].map((o) => [o.value, o.textContent]),
            options: [...agent.options].map((o) => o.value) }; })"""
        )

    def cells(self, table):
        return self.page.evaluate(
            """(table) => [...document.querySelectorAll(`${table} tbody tr`)]
          .map((tr) => [...tr.querySelectorAll('input, select')].map((i) => i.value))""",
            table,
        )

    def row_of(self, node_id):
        ids = [n["id"] for n in self.nodes()]
        return ids.index(node_id)

    def remove_node(self, node_id):
        index = self.row_of(node_id)
        self.page.locator("#graph-nodes tbody tr").nth(index).locator("button").click()

    def add_node(self, node_id, agent):
        self.page.click("[data-add=node]")
        row = self.page.locator("#graph-nodes tbody tr").last
        row.locator("[name=node_id]").fill(node_id)
        row.locator("[name=node_agent]").select_option(agent)

    def set_agent(self, node_id, agent):
        row = self.page.locator("#graph-nodes tbody tr").nth(self.row_of(node_id))
        row.locator("[name=node_agent]").select_option(agent)

    def save(self):
        """저장을 누르고 끝(성공 뒤 카탈로그 재채움 / 실패 표시)까지 기다린다. 반환: 상태 문구"""
        page, fake = self.page, self.fake
        loads = fake.catalog_loads
        page.evaluate(
            "() => { document.querySelector('#status').textContent = '';"
            " document.querySelector('#graph-findings').replaceChildren(); }"
        )
        page.click("#graph-form button[type=submit]")
        page.wait_for_function(
            """() => document.querySelector('#status').textContent !== '' ||
          [...document.querySelectorAll('#graph-findings li')]
            .some((li) => li.textContent !== '검증 통과')"""
        )
        text = page.text_content("#status")
        if text.startswith("저장됨"):
            for _ in range(100):
                if fake.catalog_loads > loads:
                    break
                page.wait_for_timeout(50)
            page.wait_for_timeout(200)
        return text

    def findings(self):
        return self.page.evaluate(
            "() => [...document.querySelectorAll('#graph-findings li')].map((li) => li.textContent)"
        )


@pytest.fixture
def ui(browser):  # noqa: F811
    ui = Ui(browser)
    yield ui
    ui.close()
    # 404/400/500 은 새 이름 조회 · 강제 실패 시나리오의 정상 응답이다.
    # `pattern="[a-z0-9-]+"` 은 Chromium 에서 무효라 콘솔 오류가 난다 — 별도 이슈 (index.html)
    assert not [
        e for e in ui.errors if "Pattern attribute" not in e and "Failed to load resource" not in e
    ], ui.errors


def last_put(ui):
    return ui.fake.puts[-1]


def node_of(body, node_id):
    return next(n for n in body["spec"]["nodes"] if n["id"] == node_id)


def test_loading_fills_every_cell_from_the_aliased_response(ui):
    ui.load("rich")

    nodes = {n["id"]: n for n in ui.nodes()}
    assert nodes["writer"]["agent"] == WRITER
    # 서브그래프 노드에는 에이전트가 없다 — 첫 옵션이 골라지면 저장 본문에 조용히 실린다
    assert nodes["sub"]["selectedIndex"] == -1
    assert nodes["sub"]["agent"] == ""
    # 응답은 source/target · schema_ref/declared_fields 로 나온다 — 정규화해야 칸이 채워진다
    edges = ui.cells("#graph-edges")
    assert edges[0][:2] == ["START", "writer"]
    assert edges[1][:3] == ["writer", "sub", "state.query"]
    assert "query" in ui.page.input_value("#graph-form [name=state_fields]")
    assert ui.page.input_value("#graph-form [name=idle_min]") == "30"
    assert ui.page.input_value("#graph-form [name=failure_streak]") == "7"


def test_saving_a_loaded_graph_keeps_what_the_form_does_not_show(ui):
    ui.load("rich")
    # 불러온 뒤 서버의 선언을 바꿔 둔다 — 저장이 서버를 다시 읽으면 이 값이 본문에 실린다
    ui.fake.graphs["rich"]["spec"]["nodes"][0]["retry"] = 99
    ui.page.fill("#graph-form [name=description]", "after")

    text = ui.save()

    assert text.startswith("저장됨"), ui.findings()
    name, body = last_put(ui)
    assert name == "rich"
    assert body["metadata"]["description"] == "after"
    assert "api_version" not in body and body["apiVersion"] == "aaestro/v1"
    sub = node_of(body, "sub")
    assert sub["graph"] == "graphs/sub-review@1.0.0" and "agent" not in sub and sub["retry"] == 1
    writer = node_of(body, "writer")
    assert (writer["retry"], writer["timeout_s"]) == (2, 90)
    assert writer["model"] == {"provider": "anthropic", "name": "claude-haiku-4-5"}
    # 키의 유무가 의미다 — 명시한 null 이 사라지면 "지운다" 가 "덮지 않는다" 로 바뀐다
    assert node_of(body, "extra")["model"] == {
        "max_tokens": None,
        "effort": None,
        "fallbacks": None,
        "retry": None,
        "circuit": None,
    }
    # 숨은 선언은 조회 응답 모양(`alias` · 기본값 채움) 그대로 보존된다
    served = GraphTopology.model_validate(DECLARATION).model_dump(mode="json")
    assert body["spec"]["memory"] == served["spec"]["memory"]
    assert GraphTopology.model_validate(body).spec.memory == (
        GraphTopology.model_validate(DECLARATION).spec.memory
    )
    assert body["spec"]["state"]["checkpointer"] == "postgres"
    assert body["spec"]["service"]["idle"]["multiplier"] == 3.0
    # 불러올 때 쥔 원본을 쓴다 (서버가 바뀌었어도 불러올 때의 값) — 서버를 다시 읽지 않는다
    assert writer["retry"] == 2


def test_a_new_name_does_not_copy_the_hidden_declarations(ui):
    ui.load("rich")
    ui.page.fill("#graph-form [name=name]", "copy")
    ui.set_agent("sub", PLANNER)

    text = ui.save()

    assert text.startswith("저장됨"), ui.findings()
    name, body = last_put(ui)
    assert name == "copy"
    assert "retry" not in node_of(body, "writer") and "timeout_s" not in node_of(body, "writer")
    assert "model" not in node_of(body, "extra")
    assert "graph" not in node_of(body, "sub")
    assert "memory" not in body["spec"] or not body["spec"]["memory"].get("spaces")
    assert "checkpointer" not in body["spec"]["state"]


def test_an_existing_name_is_patched_over_its_own_server_declaration(ui):
    ui.load("rich")
    ui.page.fill("#graph-form [name=name]", "other")
    ui.set_agent("sub", PLANNER)

    ui.save()

    body = last_put(ui)[1]
    # 불러온 rich 의 writer(retry 2)가 아니라 서버의 other 의 writer(retry 9)가 원본이다
    assert node_of(body, "writer")["retry"] == 9


def test_an_agent_outside_the_catalog_stays_selected_and_saved(ui):
    ui.load("ghost")

    node = ui.nodes()[0]
    assert node["agent"] == GONE
    assert node["selected"] == [[GONE, f"{GONE} (카탈로그에 없음)"]]
    ui.save()
    saved = node_of(last_put(ui)[1], "lost")
    assert saved["agent"] == GONE and saved["timeout_s"] == 12


def test_deleting_the_loaded_graph_drops_the_remembered_declaration(ui):
    ui.load("rich")
    ui.set_agent("sub", PLANNER)
    ui.page.click("#graph-delete")
    ui.page.wait_for_function(
        "() => document.querySelector('#status').textContent.startsWith('삭제됨')"
    )

    ui.save()

    body = last_put(ui)[1]
    assert "retry" not in node_of(body, "writer")
    assert "memory" not in body["spec"] or not body["spec"]["memory"].get("spaces")


def test_a_failed_lookup_other_than_not_found_stops_the_save(ui):
    ui.fake.fail_get = 1
    ui.page.fill("#graph-form [name=name]", "flaky")
    ui.page.fill("#graph-form [name=version]", "0.1.0")
    ui.page.fill("#graph-form [name=goal]", "g")
    ui.add_node("only", WRITER)

    ui.save()

    assert ui.fake.puts == []
    assert any("INTERNAL_001" in f for f in ui.findings())


def test_what_was_removed_before_a_save_does_not_come_back_on_the_next(ui):
    ui.load("rich")
    ui.remove_node("extra")
    assert ui.save().startswith("저장됨"), ui.findings()
    assert [n["id"] for n in last_put(ui)[1]["spec"]["nodes"]] == ["writer", "sub"]

    ui.add_node("extra", PLANNER)
    assert ui.save().startswith("저장됨"), ui.findings()

    extra = node_of(last_put(ui)[1], "extra")
    # 저장한 문서가 원본이다 — 지운 노드의 retry · model 을 새 노드가 물려받지 않는다
    assert extra == {"id": "extra", "agent": PLANNER}


def test_a_mode_round_trip_does_not_bring_back_the_old_idle_policy(ui):
    ui.load("rich")
    ui.page.select_option("#graph-form [name=mode]", "mission")
    assert ui.save().startswith("저장됨"), ui.findings()
    assert "service" not in last_put(ui)[1]["spec"]

    ui.page.select_option("#graph-form [name=mode]", "service")
    assert ui.save().startswith("저장됨"), ui.findings()

    idle = last_put(ui)[1]["spec"]["service"]["idle"]
    assert "multiplier" not in idle


def test_validate_and_save_send_the_same_document(ui):
    ui.load("rich")

    ui.save()

    assert ui.fake.validates[-1]["graphs"][0] == last_put(ui)[1]
