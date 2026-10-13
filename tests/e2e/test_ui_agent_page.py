"""The whole agent editor page against a fake control plane (#377).

실제 index.html + app.js + client.js 를 `page.route` 로 서빙하고 `/v1/**` 를 가짜 서버로
답한다 — 스택 없이 페이지의 DOM 경로(불러오기 · 저장 · 삭제 · 카탈로그 재채움)를 실행한다.
가짜 서버는 GET 에 `AgentManifest` 의 실제 직렬화(api_version · alias)를, PUT 에 같은 스키마
검증을 쓴다.
"""

from __future__ import annotations

import copy
import json
import re

import pytest
from pydantic import ValidationError

from aaestro.core.manifest import AgentManifest
from aaestro.ui import UI_ROOT
from tests.e2e.test_stack import requires_docker
from tests.e2e.test_ui_browser import browser  # noqa: F401 — 같은 브라우저 fixture

pytestmark = [pytest.mark.e2e, requires_docker]
ORIGIN = "http://ui.test"


def decl(
    name,
    *,
    group=None,
    promptset="promptsets/researcher@0.1.0",
    skillsets=(),
    memory=None,
    hidden=False,
    a2a=None,
):
    d = {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": {"name": name, "version": "0.2.0", "description": "before"},
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": promptset},
            "skillsets": [{"ref": r} for r in skillsets],
            "runtime": {"env_allowlist": ["ANTHROPIC_API_KEY"]},
        },
    }
    if group:
        d["metadata"]["group"] = group
    if memory is not None:
        d["spec"]["memory"] = {"spaces": memory}
    if a2a is not None:
        d["spec"]["a2a"] = a2a
    if hidden:
        d["spec"]["model"].update(
            {
                "max_tokens": 4096,
                "effort": "high",
                "fallbacks": [
                    {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]}
                ],
            }
        )
        d["spec"]["mcp"] = {
            "servers": [{"name": "fs", "transport": "stdio", "command": ["mcp-server-filesystem"]}]
        }
        d["spec"]["runtime"]["resources"] = {"cpu": "2.0", "memory": "2Gi"}
        d["spec"]["runtime"]["image"] = "aaestro/agent-base:0.1.0"
    return d


def seed():
    return {
        "rich": decl(
            "rich",
            group="research",
            hidden=True,
            skillsets=["skillsets/web-search@0.2.0", "skillsets/summarize@0.1.0"],
            memory=[
                {"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm", "mode": "ro"},
                {"ref": "memorysets/notes@0.1.0", "as": "mynotes"},
            ],
            a2a={"enabled": True, "capabilities": {"streaming": True}},
        ),
        "ghost": decl(
            "ghost",
            group="phantom",
            hidden=True,
            skillsets=["skillsets/web-search@0.2.0", "skillsets/gone@9.9.9"],
            memory=[{"ref": "memorysets/vanished@0.3.0", "as": "old"}],
        ),
        "plain": decl(
            "plain", promptset="promptsets/writer@0.1.0", skillsets=["skillsets/extra@0.1.0"]
        ),
        "min": decl("min"),
        "zz-last": decl("zz-last"),
    }


MODULES = {
    "skillsets": [("web-search", ["0.2.0"]), ("summarize", ["0.1.0"]), ("extra", ["0.1.0"])],
    "promptsets": [("researcher", ["0.1.0"]), ("writer", ["0.1.0"])],
    "memorysets": [("agent-longterm", ["0.1.0"]), ("notes", ["0.1.0"]), ("m3", ["0.1.0"])],
}


class Fake:
    def __init__(self):
        self.agents = seed()
        self.raw = {}  # name → 서버 모델을 거치지 않은 원시 응답
        self.validates, self.puts, self.log = [], [], []
        self.fail_put = 0
        self.fail_validate = 0
        self.reloads = 0

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

        if path == "/v1/health":
            return ok({"status": "ok"})
        if path == "/v1/graphs":
            return ok(
                {
                    "items": [
                        {
                            "name": n,
                            "version": "1.0.0",
                            "description": "",
                            "mode": "mission",
                            "goal": "g",
                            "nodes": 1,
                        }
                        for n in ("g-one", "g-two")
                    ],
                    "problems": [],
                }
            )
        if path.startswith("/v1/graphs/"):
            return ok({"spec": {"nodes": []}})
        if path == "/v1/groups":
            return ok(
                {
                    "items": [
                        {"name": n, "description": "", "quotas": {}} for n in ("research", "ops")
                    ],
                    "problems": [],
                }
            )
        m = re.fullmatch(r"/v1/modules/(\w+)", path)
        if m:
            if m.group(1) == "memorysets":
                self.reloads += 1
            return ok(
                {
                    "items": [{"name": n, "versions": v} for n, v in MODULES[m.group(1)]],
                    "problems": [],
                }
            )
        if path == "/v1/deployments":
            return ok({"items": []})
        if path == "/v1/runs":
            return ok([])
        if path == "/v1/agents" and method == "GET":
            items = []
            for name, d in self.agents.items():
                items.append(
                    {
                        "name": name,
                        "version": d["metadata"]["version"],
                        "group": d["metadata"].get("group"),
                        "description": "",
                        "model": d["spec"]["model"],
                    }
                )
            return ok({"items": items, "problems": []})
        if path == "/v1/validate":
            self.validates.append(body)
            if self.fail_validate:
                self.fail_validate -= 1
                return err(
                    400, "VAL_002", "forced", {"errors": [{"field": "x", "problem": "forced"}]}
                )
            try:
                for a in body["agents"]:
                    AgentManifest.model_validate(a)
            except ValidationError as e:
                return err(
                    400,
                    "VAL_002",
                    "invalid",
                    {
                        "errors": [
                            {"field": ".".join(map(str, x["loc"])), "problem": x["msg"]}
                            for x in e.errors()
                        ]
                    },
                )
            return ok({"ok": True, "findings": []})
        m = re.fullmatch(r"/v1/agents/([^/]+)", path)
        if m:
            name = m.group(1)
            if method == "GET":
                if name in self.raw:
                    return ok(self.raw[name])
                if name not in self.agents:
                    return err(404, "NF_001", "no such agent")
                return ok(AgentManifest.model_validate(self.agents[name]).model_dump(mode="json"))
            if method == "PUT":
                self.puts.append((name, body))
                if self.fail_put:
                    self.fail_put -= 1
                    return err(
                        400,
                        "VAL_002",
                        "forced put failure",
                        {"errors": [{"field": "y", "problem": "forced"}]},
                    )
                try:
                    AgentManifest.model_validate(body)
                except ValidationError as e:
                    return err(400, "VAL_002", str(e)[:200])
                self.agents[name] = body
                return ok({"name": name, "path": f"agents/{name}/manifest.yaml"})
            if method == "DELETE":
                self.agents.pop(name, None)
                return ok(None, 204)
        if path.endswith("/image") or path.endswith("/materials"):
            return ok({"detail": "Not Found"}, 404)
        return ok({"detail": f"unhandled {method} {path}"}, 404)


def static(src, ctype):
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
        page.wait_for_function("() => document.querySelectorAll('#catalog-agents li').length > 0")
        page.wait_for_function(
            "() => document.querySelector('select[name=memorysets]').options.length > 0"
        )
        page.click("#tabs button[data-tab=agent]")

    def close(self):
        self.context.close()

    def selects(self, *sels):
        return self.page.evaluate(
            """(sels) => Object.fromEntries(sels.map((s) => { const e = document.querySelector(s);
          return [s, { value: e.value, selectedIndex: e.selectedIndex,
            selected: [...e.selectedOptions].map((o) => o.value),
            options: [...e.options].map((o) => o.textContent) }]; }))""",
            list(sels),
        )

    def form_selects(self):
        return self.selects(
            "select[name=group]",
            "select[name=promptset]",
            "select[name=skillsets]",
            "select[name=memorysets]",
        )

    def load(self, name):
        self.answer = name
        self.page.evaluate("() => { document.querySelector('#status').textContent = ''; }")
        self.page.click("#agent-load")
        self.page.wait_for_function("() => document.querySelector('#status').textContent !== ''")
        return self.page.text_content("#status")

    def save(self):
        """저장을 누르고 끝(성공 뒤 카탈로그 재채움 / 실패 표시)까지 기다린다. 반환: 상태 문구"""
        page, fake = self.page, self.fake
        reloads = fake.reloads
        page.evaluate(
            "() => { document.querySelector('#status').textContent = '';"
            " document.querySelector('#agent-findings').replaceChildren(); }"
        )
        page.click("#agent-form button[type=submit]")
        page.wait_for_function(
            """() => document.querySelector('#status').textContent !== '' ||
          [...document.querySelectorAll('#agent-findings li')]
            .some((li) => li.textContent !== '검증 통과')"""
        )
        text = page.text_content("#status")
        if text.startswith("저장됨"):
            for _ in range(100):
                if fake.reloads > reloads:
                    break
                page.wait_for_timeout(50)
            page.wait_for_timeout(200)
        findings = page.evaluate(
            "() => [...document.querySelectorAll('#agent-findings li')].map((li) => li.textContent)"
        )
        return text, findings

    def delete(self):
        reloads = self.fake.reloads
        self.page.click("#agent-delete")
        for _ in range(100):
            if self.fake.reloads > reloads:
                break
            self.page.wait_for_timeout(50)
        self.page.wait_for_timeout(200)
        return self.page.text_content("#status")


@pytest.fixture
def ui(browser):  # noqa: F811
    ui = Ui(browser)
    yield ui
    ui.close()
    # 404/400 은 새 이름 조회 · 강제 실패 시나리오의 정상 응답이다.
    assert not [e for e in ui.errors if "Failed to load resource" not in e], ui.errors


def norm(doc):
    return AgentManifest.model_validate(doc).model_dump(mode="json")


def expect(original, **meta):
    expected = copy.deepcopy(original)
    expected["metadata"].update(meta)
    return norm(expected)


def last_put(ui):
    return ui.fake.puts[-1]


def selected(ui):
    return {k: v["selected"] for k, v in ui.form_selects().items()}


def hidden_blocks(body):
    """폼에 없는 선언 — 서버 스키마로 정규화해 기본값과 구별한다."""
    spec = norm(body)["spec"]
    return {
        "mcp": spec["mcp"],
        "fallbacks": spec["model"]["fallbacks"],
        "resources": spec["runtime"]["resources"],
    }


def no_hidden_blocks():
    return hidden_blocks(decl("bare"))


def test_the_first_load_selects_the_first_catalog_option_of_single_selects(ui):
    s = ui.form_selects()

    assert s["select[name=promptset]"]["selected"] == ["promptsets/researcher@0.1.0"]
    assert s["select[name=group]"]["selected"] == [""]
    assert s["select[name=skillsets]"]["selected"] == []


def test_three_saves_in_a_row_keep_the_declaration(ui):
    original = ui.fake.agents["rich"]
    ui.load("rich")
    ui.page.fill("#agent-form [name=description]", "after")
    want = expect(original, version="0.2.1", description="after")

    for _ in range(3):
        text, _findings = ui.save()
        assert text.startswith("저장됨")
        # 매번 PUT 본문이 서버 스키마를 통과하고 원본과 모델 동등이다
        assert norm(last_put(ui)[1]) == want
        assert selected(ui)["select[name=skillsets]"] == [
            "skillsets/web-search@0.2.0",
            "skillsets/summarize@0.1.0",
        ]
    assert len(ui.fake.puts) == 3
    # 저장한 문서가 다음 저장의 원본이다 — 서버를 다시 읽지 않는다 (불러오기 때의 한 번뿐)
    assert ui.fake.log.count("GET /v1/agents/rich") == 1


def test_refs_outside_the_catalog_are_shown_kept_and_dropped_when_deselected(ui):
    original = ui.fake.agents["ghost"]
    ui.load("ghost")

    s = ui.form_selects()
    assert "phantom (카탈로그에 없음)" in s["select[name=group]"]["options"]
    assert s["select[name=group]"]["selected"] == ["phantom"]
    assert "skillsets/gone@9.9.9" in s["select[name=skillsets]"]["selected"]
    ui.save()
    assert norm(last_put(ui)[1]) == expect(original, version="0.2.1")

    # 재채움 뒤에도 남아 있고, 사용자가 풀면 지워진다
    assert selected(ui)["select[name=memorysets]"] == ["memorysets/vanished@0.3.0"]
    ui.page.evaluate(
        """() => {
          const f = document.querySelector('#agent-form');
          [...f.skillsets.options].forEach((o) => {
            o.selected = o.value === 'skillsets/web-search@0.2.0';
          });
          [...f.memorysets.options].forEach((o) => { o.selected = false; });
          f.group.value = '';
        }"""
    )
    ui.save()
    body = last_put(ui)[1]
    assert "group" not in body["metadata"] or body["metadata"]["group"] is None
    assert [s["ref"] for s in body["spec"]["skillsets"]] == ["skillsets/web-search@0.2.0"]
    assert not body["spec"].get("memory", {}).get("spaces")
    norm(body)


def test_loading_b_after_a_leaves_nothing_of_a(ui):
    original = ui.fake.agents["plain"]
    ui.load("rich")
    ui.load("ghost")
    ui.load("plain")

    s = ui.form_selects()
    assert all("카탈로그에 없음" not in o for v in s.values() for o in v["options"])
    ui.save()
    body = last_put(ui)[1]
    assert norm(body) == expect(original, version="0.2.1")
    assert hidden_blocks(body) == no_hidden_blocks()


def test_a_new_name_does_not_copy_the_hidden_declaration(ui):
    ui.load("rich")
    ui.page.fill("#agent-form [name=name]", "brand-new")

    ui.save()

    name, body = last_put(ui)
    assert name == "brand-new"
    assert hidden_blocks(body) == no_hidden_blocks()
    norm(body)


def test_deleting_another_name_keeps_the_loaded_declaration(ui):
    original = ui.fake.agents["rich"]
    ui.load("rich")
    ui.page.fill("#agent-form [name=name]", "plain")
    ui.delete()
    ui.page.fill("#agent-form [name=name]", "rich")

    ui.save()

    assert norm(last_put(ui)[1]) == expect(original, version="0.2.1")
    # 다른 이름을 지웠으므로 쥐고 있던 원본을 그대로 쓴다
    assert ui.fake.log.count("GET /v1/agents/rich") == 1


def test_deleting_the_loaded_name_drops_it(ui):
    ui.load("rich")
    ui.delete()
    # 같은 이름이 서버에도 없으므로 폼 값만으로 조립된다
    ui.save()

    assert hidden_blocks(last_put(ui)[1]) == no_hidden_blocks()


def test_saving_an_existing_name_without_loading_patches_the_server_declaration(ui):
    original = ui.fake.agents["rich"]
    ui.page.fill("#agent-form [name=name]", "rich")
    ui.page.fill("#agent-form [name=version]", "0.2.1")
    ui.page.evaluate("() => { document.querySelector('#agent-form').group.value = 'research'; }")

    ui.save()

    body = last_put(ui)[1]
    assert hidden_blocks(body) == hidden_blocks(original)
    norm(body)
    # 저장한 문서가 다음 저장의 원본이다 — 서버를 다시 읽지 않는다
    ui.save()
    assert ui.fake.log.count("GET /v1/agents/rich") == 1
    assert hidden_blocks(last_put(ui)[1]) == hidden_blocks(original)


def test_another_loaded_agent_does_not_become_the_base_of_an_existing_name(ui):
    original = ui.fake.agents["rich"]
    ui.load("plain")
    ui.page.fill("#agent-form [name=name]", "rich")

    ui.save()

    assert hidden_blocks(last_put(ui)[1]) == hidden_blocks(original)


def test_a_failed_save_does_not_poison_the_retry(ui):
    original = ui.fake.agents["rich"]
    ui.load("rich")
    ui.page.fill("#agent-form [name=description]", "first")
    ui.fake.fail_put = 1
    ui.save()
    ui.fake.fail_validate = 1
    ui.save()
    ui.page.fill("#agent-form [name=description]", "second")

    text, _ = ui.save()

    assert text.startswith("저장됨")
    assert norm(last_put(ui)[1]) == expect(original, version="0.2.1", description="second")


def test_duplicate_aliases_and_order_survive(ui):
    d = ui.fake.agents["rich"]
    d["spec"]["memory"]["spaces"] = [
        {"ref": "memorysets/notes@0.1.0", "as": "second-first"},
        {"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm", "mode": "ro"},
        {"ref": "memorysets/notes@0.1.0", "as": "dup-of-notes"},
    ]
    d["spec"]["skillsets"] = [
        {"ref": "skillsets/summarize@0.1.0"},
        {"ref": "skillsets/web-search@0.2.0"},
    ]
    original = copy.deepcopy(d)
    ui.load("rich")

    ui.save()

    assert norm(last_put(ui)[1]) == expect(original, version="0.2.1")


def test_a_document_without_a_promptset_does_not_get_one_from_the_form(ui):
    ui.fake.raw["rawdoc"] = {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": {"name": "rawdoc", "version": "1.0.0"},
        "spec": {"model": {"provider": "anthropic", "name": "m"}, "promptset": {"ref": ""}},
    }
    ui.load("rawdoc")
    validates = len(ui.fake.validates)

    ui.page.click("#agent-validate")
    ui.page.wait_for_function(
        "(n) => document.querySelectorAll('#agent-findings li').length > 0", arg=0
    )
    for _ in range(100):
        if len(ui.fake.validates) > validates:
            break
        ui.page.wait_for_timeout(50)

    assert ui.selects("select[name=promptset]")["select[name=promptset]"]["selectedIndex"] == -1
    assert "ref" not in ui.fake.validates[-1]["agents"][0]["spec"]["promptset"]
