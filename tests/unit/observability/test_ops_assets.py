"""Validation for alerting rules, dashboards, and runbooks.

알림 PromQL 과 대시보드 패널이 참조하는 메트릭이 실제 레지스트리에 존재해야 한다 —
오타 하나가 조용히 죽은 알림을 만들고, 그건 장애 때 알림이 안 온다는 뜻이다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from aaestro.observability.metrics import METRIC_SPECS

REPO_ROOT = Path(__file__).resolve().parents[3]
MONITORING = REPO_ROOT / "deployments" / "monitoring"
ALERTS = MONITORING / "alerts.yaml"
DASHBOARDS = sorted((MONITORING / "dashboards").glob("*.json"))
RUNBOOKS_EN = REPO_ROOT / "docs" / "en" / "runbooks"
RUNBOOKS_KO = REPO_ROOT / "docs" / "ko" / "runbooks"
RULES = REPO_ROOT / ".claude" / "rules"

REGISTERED = {spec.name for spec in METRIC_SPECS}

# histogram 은 _bucket/_count/_sum 파생 시계열로 조회된다
_HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum")


def documented_alerts() -> set[str]:
    """05 의 Alerting Rules 블록에 선언된 알림 이름 — 손으로 옮긴 목록은 룰셋과 어긋난다."""
    text = (RULES / "05-error-handling.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\s*- alert:\s*(\w+)", text, re.MULTILINE))


def base_metric(name: str) -> str:
    """파생 시계열 접미사를 떼어 원래 메트릭 이름을 얻는다."""
    for suffix in _HISTOGRAM_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def referenced_metrics(text: str) -> set[str]:
    """텍스트에서 참조된 aaestro_* 메트릭 이름을 모은다."""
    return {base_metric(name) for name in re.findall(r"aaestro_[a-z0-9_]+", text)}


@pytest.fixture(scope="module")
def alert_rules() -> dict:
    return yaml.safe_load(ALERTS.read_text(encoding="utf-8"))


# --- 알림 규칙 --------------------------------------------------------------


def test_alert_file_parses(alert_rules):
    assert alert_rules["groups"][0]["name"] == "aaestro"


def test_all_documented_alerts_are_defined(alert_rules):
    """05 에 선언된 알림이 전부, 그리고 그것만 있어야 한다."""
    defined = {r["alert"] for r in alert_rules["groups"][0]["rules"]}
    documented = documented_alerts()

    assert len(documented) >= 7, "05 의 알림 블록을 찾지 못했다"
    assert defined == documented


def test_alert_expressions_reference_registered_metrics(alert_rules):
    """PromQL 의 메트릭 오타는 조용히 죽은 알림이 된다."""
    referenced = referenced_metrics(ALERTS.read_text(encoding="utf-8"))

    assert referenced <= REGISTERED, f"unknown metrics: {sorted(referenced - REGISTERED)}"


def test_every_alert_declares_severity(alert_rules):
    for rule in alert_rules["groups"][0]["rules"]:
        assert rule["labels"]["severity"] in {"warning", "critical"}, rule["alert"]


def test_every_alert_has_a_summary(alert_rules):
    for rule in alert_rules["groups"][0]["rules"]:
        assert rule["annotations"]["summary"], rule["alert"]


def test_every_alert_links_an_existing_runbook_section(alert_rules):
    """알림에서 runbook 으로 이어지지 않으면 대응이 지연된다."""
    for rule in alert_rules["groups"][0]["rules"]:
        link = rule["annotations"]["runbook"]
        path, _, anchor = link.partition("#")
        doc = REPO_ROOT / path

        assert doc.is_file(), f"{rule['alert']} -> missing {path}"
        headings = {
            re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-")
            for h in re.findall(r"^#+\s+(.+)$", doc.read_text(encoding="utf-8"), re.MULTILINE)
        }
        assert anchor in headings, f"{rule['alert']} -> missing anchor #{anchor}"


def label_filters(text: str) -> list[tuple[str, str]]:
    """`metric{a="x", b="y"}` 의 **모든** 라벨을 추출한다.

    첫 라벨만 보면 두 번째 이후의 오타를 놓친다.
    """
    found: list[tuple[str, str]] = []
    # JSON 안에서는 따옴표가 \" 로 이스케이프되므로 백슬래시를 먼저 걷어낸다 —
    # 그러지 않으면 대시보드의 라벨을 하나도 못 잡고 테스트가 공허하게 통과한다
    normalized = text.replace('\\"', '"')
    for metric, selector in re.findall(r"(aaestro_[a-z0-9_]+)\{([^}]*)\}", normalized):
        for label in re.findall(r"(\w+)\s*[=!]~?\s*[\"\']", selector):
            found.append((metric, label))
    return found


def assert_label_filters_exist(text: str, source: str) -> None:
    """참조된 라벨이 전부 레지스트리에 존재하는지 확인한다."""
    labels_by_metric = {spec.name: set(spec.labels) for spec in METRIC_SPECS}

    for metric, label in label_filters(text):
        known = labels_by_metric[base_metric(metric)]
        assert label in known, f"{source}: {metric} has no label {label!r} (has {sorted(known)})"


def test_alert_expressions_use_label_filters_that_exist(alert_rules):
    """존재하지 않는 라벨로 필터하면 알림이 영원히 발화하지 않는다."""
    assert_label_filters_exist(ALERTS.read_text(encoding="utf-8"), "alerts.yaml")


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_dashboard_label_filters_exist(path):
    """대시보드의 라벨 오타는 빈 패널이 되어 조용히 넘어간다."""
    assert_label_filters_exist(path.read_text(encoding="utf-8"), path.name)


def test_label_filter_extraction_catches_every_label():
    """추출기 자체의 회귀 방지 — 첫 라벨만 잡으면 뒤의 오타를 놓친다."""
    sample = 'aaestro_agent_tasks_total{agent="a", status="failed", graph="g"}'

    assert label_filters(sample) == [
        ("aaestro_agent_tasks_total", "agent"),
        ("aaestro_agent_tasks_total", "status"),
        ("aaestro_agent_tasks_total", "graph"),
    ]


def test_label_filter_extraction_handles_json_escaping():
    """대시보드는 JSON 이라 따옴표가 이스케이프된다 — 못 읽으면 검증이 공허해진다."""
    sample = r'{"expr": "aaestro_agent_tasks_total{status=\"completed\"}"}'

    assert label_filters(sample) == [("aaestro_agent_tasks_total", "status")]


def test_dashboards_actually_contain_label_filters():
    """추출기가 대시보드에서 라벨을 하나도 못 찾으면 검증이 무의미하다."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in DASHBOARDS)

    assert label_filters(text), "no label filters extracted — the validation is vacuous"


def test_binary_operations_align_label_sets(alert_rules):
    """이항 연산은 라벨셋이 맞아야 한다.

    `/` 의 양변이나 `and` 의 좌우가 서로 다른 라벨을 들고 있으면 매칭이
    어긋나 항상 참(오탐) 이거나 영원히 빈 벡터(미발화) 가 된다.
    """
    for rule in alert_rules["groups"][0]["rules"]:
        expr = " ".join(rule["expr"].split())
        if " and " in expr:
            assert "on (" in expr or "on(" in expr, (
                f"{rule['alert']}: `and` needs an explicit on(...) matcher"
            )
        if "/" in expr:
            assert expr.count("sum without") >= 2, (
                f"{rule['alert']}: ratio needs both sides aggregated to the same label set"
            )


# --- 대시보드 --------------------------------------------------------------


def test_five_dashboards_exist():
    """05 Dashboards — Overview / Agent Detail / Protocol / Graph / System."""
    assert len(DASHBOARDS) == 5


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_dashboard_parses_and_declares_identity(path):
    board = json.loads(path.read_text(encoding="utf-8"))

    assert board["title"].startswith("Aaestro")
    assert board["uid"]
    assert board["description"]
    assert board["panels"]


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_dashboard_panels_reference_registered_metrics(path):
    """패널이 없는 메트릭을 그리면 빈 그래프가 되고 아무도 눈치채지 못한다."""
    referenced = referenced_metrics(path.read_text(encoding="utf-8"))

    assert referenced <= REGISTERED, f"unknown metrics: {sorted(referenced - REGISTERED)}"


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_every_panel_has_a_target(path):
    board = json.loads(path.read_text(encoding="utf-8"))

    for panel in board["panels"]:
        assert panel["targets"], f"{board['uid']} / {panel['title']}"
        for target in panel["targets"]:
            assert target["expr"].strip()


def test_dashboard_uids_are_unique():
    uids = [json.loads(p.read_text(encoding="utf-8"))["uid"] for p in DASHBOARDS]

    assert len(uids) == len(set(uids))


def test_histogram_metrics_are_queried_through_buckets():
    """histogram 을 원본 이름으로 조회하면 값이 나오지 않는다."""
    histograms = {s.name for s in METRIC_SPECS if s.kind == "histogram"}
    text = "\n".join(p.read_text(encoding="utf-8") for p in DASHBOARDS)

    for name in re.findall(r"aaestro_[a-z0-9_]+", text):
        if base_metric(name) in histograms:
            assert name.endswith(_HISTOGRAM_SUFFIXES), f"{name} needs a bucket/count/sum suffix"


def test_every_registered_metric_appears_somewhere():
    """등록만 하고 아무도 보지 않는 메트릭은 죽은 계측이다."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in [*DASHBOARDS, ALERTS])
    observed = referenced_metrics(text)

    assert REGISTERED - observed == set()


# --- runbook ---------------------------------------------------------------


def test_runbooks_mirror_across_locales():
    """docs/en 과 docs/ko 의 구조가 어긋나면 Docs Sync Check 가 막는다."""
    en = {p.name for p in RUNBOOKS_EN.glob("*.md")}
    ko = {p.name for p in RUNBOOKS_KO.glob("*.md")}

    assert en == ko
    assert en


@pytest.mark.parametrize("locale", ["en", "ko"])
def test_runbooks_carry_a_language_selector(locale):
    directory = RUNBOOKS_EN if locale == "en" else RUNBOOKS_KO

    for doc in directory.glob("*.md"):
        head = doc.read_text(encoding="utf-8").split("\n\n")[1]
        assert "한국어" in head and "English" in head, doc.name


def test_runbooks_reference_real_error_codes():
    """존재하지 않는 코드를 안내하면 대응자가 헤맨다."""
    from aaestro.core.errors import ErrorCode

    known = {code.value for code in ErrorCode}
    text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [*RUNBOOKS_EN.glob("*.md"), *RUNBOOKS_KO.glob("*.md")]
    )
    referenced = set(re.findall(r"\b([A-Z]{2,5}_\d{3})\b", text))

    assert referenced <= known, f"unknown codes: {sorted(referenced - known)}"


def test_runbooks_reference_real_cli_commands():
    """runbook 이 안내하는 명령은 CLI 계약(#15)에 존재해야 한다."""
    documented = {
        "aaestro status",
        "aaestro agent logs",
        "aaestro agent inspect",
        "aaestro run trace",
        "aaestro run resume",
        "aaestro replay",
        "aaestro memory reindex",
    }
    text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in [*RUNBOOKS_EN.glob("*.md"), *RUNBOOKS_KO.glob("*.md")]
    )
    referenced = {
        " ".join(m.split()[:3])
        if m.split()[1] in {"agent", "run", "memory"}
        else " ".join(m.split()[:2])
        for m in re.findall(r"aaestro [a-z]+(?: [a-z]+)?", text)
    }

    assert referenced <= documented, f"undocumented commands: {sorted(referenced - documented)}"


# --- 알림이 기대하는 status 값 --------------------------------------------------


def status_filters(text: str) -> set[tuple[str, str]]:
    """알림 표현식에서 ``metric{status="value"}`` 쌍을 뽑는다."""
    pattern = re.compile(r"(aaestro_\w+)\{[^}]*status\s*=\s*\"([^\"]+)\"")
    return {(base_metric(metric), value) for metric, value in pattern.findall(text)}


def emitted_status_values() -> set[str]:
    """코드가 실제로 내는 status 값 — 05 의 허용 표와 대조하는 기준이다."""
    from aaestro.agentd import telemetry as agent_telemetry
    from aaestro.orchestrator import telemetry as orchestrator_telemetry
    from aaestro.orchestrator.run import RunStatus

    return {
        agent_telemetry.STATUS_COMPLETED,
        agent_telemetry.STATUS_FAILED,
        orchestrator_telemetry.STATUS_COMPLETED,
        orchestrator_telemetry.STATUS_FAILED,
        # release 가 받는 종료 상태만 라벨이 된다 — running/draining 은 진행 상태다
        *(
            status.value
            for status in RunStatus
            if status not in (RunStatus.RUNNING, RunStatus.DRAINING)
        ),
        # memory 계층은 감사 로그와 같은 어휘를 쓴다
        "ok",
        "denied",
        # 모델 provider 가 분류하는 값 — 바인딩(#77) 이후 executor 가 낸다
        "rate_limited",
    }


def test_documented_status_values_match_what_the_code_emits():
    """05 의 허용 표와 코드가 갈라지면 알림이 조용히 죽는다.

    실제로 checkpoint 실패를 `failed` 로 기록했는데 알림은 `error` 를 보고 있어
    영원히 침묵할 뻔했다 (#104).
    """
    text = (RULES / "05-error-handling.md").read_text(encoding="utf-8")
    # status 허용 표만 본다 — 05 에는 로그 필드 표 등 다른 표도 있다
    section = text.split("**`status` 라벨의 허용 값**", 1)[1].split("```", 1)[0]
    documented = set(re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE))

    assert documented, "05 의 status 허용 표를 찾지 못했다"
    assert documented == emitted_status_values()


def test_alerts_only_filter_on_status_values_the_code_emits():
    """알림이 코드가 내지 않는 값을 보면 **영원히 침묵한다**.

    라벨 이름 검사는 이 오류를 잡지 못한다 — 이름은 맞고 값만 어긋나기 때문이다.
    실제로 checkpoint 실패를 `failed` 로 기록해 `status="error"` 알림이 죽어
    있었다.
    """
    emitted = emitted_status_values()

    referenced = status_filters(ALERTS.read_text(encoding="utf-8"))
    unknown = sorted(
        f"{metric}{{status={value!r}}}" for metric, value in referenced if value not in emitted
    )

    assert unknown == [], f"알림이 코드가 내지 않는 status 를 본다: {unknown}"


def test_documented_memory_ops_match_what_the_code_emits():
    """05/09 가 나열한 ``op`` 값과 코드가 내는 값이 같아야 한다.

    어긋나면 둘 중 하나를 고쳐야 하는데, **문서를 코드에 맞추는 쪽이 언제나
    쉬워서** 계약이 조용히 코드를 따라가게 된다. 그 순환을 막는다.
    """
    from aaestro.memory.telemetry import OP_RECALL, OP_SEARCH

    emitted = {"append", "read", "latest", OP_SEARCH, OP_RECALL}
    pattern = re.compile(r"aaestro_memory_operations_total\{[^}]*\}\s*#\s*op:\s*([\w|]+)")

    for rules in (RULES / "05-error-handling.md", RULES / "09-memory-context.md"):
        match = pattern.search(rules.read_text(encoding="utf-8"))
        assert match, f"{rules.name}: op 값 주석을 찾지 못했다"
        documented = set(match.group(1).split("|"))
        assert documented == emitted, (
            f"{rules.name}: 문서 {sorted(documented)} vs 코드 {sorted(emitted)}"
        )


def test_the_documented_direct_graph_label_is_the_one_the_code_emits():
    """05 가 문서화한 값과 코드가 갈라지면 direct 태스크 필터가 조용히 빈다.

    #104 의 status 값과 같은 실패 방식이다 — 이름은 맞고 값만 어긋난다.
    """
    from aaestro.agentd.telemetry import DIRECT_GRAPH

    text = (RULES / "05-error-handling.md").read_text(encoding="utf-8")

    assert f'`"{DIRECT_GRAPH}"` 로 기록한다' in text
