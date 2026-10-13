"""The ``aaestro`` command-line interface.

운영자가 프레임워크를 다루는 표면. 명령은 얇게 유지하고 판단은 각 레이어에
위임한다 — CLI 가 로직을 들고 있으면 API/대시보드에서 같은 것을 다시 만들어야
한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from aaestro.authoring import Author
from aaestro.catalog import MODULE_TYPES, Catalog, Problem, load_yaml, parse_declaration
from aaestro.cli.control import CONTROL_TOKEN_ENV, DEFAULT_CONTROL_URL
from aaestro.cli.integrity import (
    dangling_module_refs,
    ghost_containers,
    orphan_checkpoints,
)
from aaestro.config import DEFAULT_CONFIG_DIR, ENVIRONMENT_ENV, load_config, resolve_environment
from aaestro.core.errors import AaestroError
from aaestro.deploy import ValidationReport
from aaestro.observability.logging import configure
from aaestro.orchestrator.models import CatalogModels, resolve_node_model
from aaestro.orchestrator.topology import GraphTopology

if TYPE_CHECKING:
    from collections.abc import Sequence

EXIT_OK = 0
EXIT_FAILED = 1
"""검증/점검 실패 — 운영 스크립트가 분기할 수 있도록 0 과 구분한다."""

EXIT_USAGE = 2

BUILD_POLL_S = 2.0
"""`agent-build --wait` 의 조회 간격 — 빌드는 분 단위라 촘촘히 물을 이유가 없다."""

DEFAULT_BUILD_TIMEOUT_S = 1800.0

_SKIPPED_MATERIAL_PARTS = frozenset({"__pycache__", ".git", ".venv", "node_modules"})


def validate_root(
    root: Path,
    topologies: Sequence[GraphTopology],
    *,
    a2a_port_range: tuple[int, int] | None = None,
    providers: frozenset[str] | None = None,
) -> ValidationReport:
    """Validate topologies against the repository's declarations.

    저장소 선언을 기준으로 토폴로지를 검증합니다.

    세 명령(`deploy` / `validate` / `run`)이 **같은 입력으로 같은 판정**을
    내리도록 한 곳에 모읍니다 — 흩어지면 한 명령만 통과하는 상태가 생깁니다.
    """
    # 조립은 Author.validate 한 곳 — CLI 는 지목한 그래프만 판정한다 (with_saved_graphs=False)
    author = Author(catalog=Catalog.under(root), a2a_port_range=a2a_port_range, providers=providers)
    return author.validate(graphs=topologies, with_saved_graphs=False)


def node_models(catalog: Catalog, topologies: Sequence[GraphTopology]) -> list[dict[str, Any]]:
    """Resolve each agent node's model, for display.

    에이전트 노드마다 해석된 모델 (manifest 기본값 위에 노드 override). 매니페스트가 없거나
    해석 결과가 무효인 노드는 건너뛴다 — 검증이 그 이유를 보고한다.
    """
    resolved: list[dict[str, Any]] = []
    for topology in topologies:
        for node in topology.spec.nodes:
            try:
                model = resolve_node_model(catalog, node)
            except (AaestroError, ValidationError):
                continue
            if model is None:
                continue
            resolved.append(
                {
                    "graph": topology.metadata.name,
                    "node_id": node.id,
                    "agent": node.agent,
                    "provider": model.provider,
                    "name": model.name,
                    "fallbacks": [
                        {"provider": f.provider, "name": f.name, "on": list(f.on)}
                        for f in model.fallbacks
                    ],
                    "source": "manifest" if node.model is None else "node",
                }
            )
    return resolved


def _model_line(entry: dict[str, Any]) -> str:
    """`graph/node: provider/name → fallback, ... (출처)` — 사람이 읽는 한 줄."""
    line = f"{entry['graph']}/{entry['node_id']}: {entry['provider']}/{entry['name']}"
    if entry["fallbacks"]:
        line += " → " + ", ".join(f"{f['provider']}/{f['name']}" for f in entry["fallbacks"])
    source = "node override" if entry["source"] == "node" else "manifest"
    return f"{line} ({source})"


def models_entry(
    catalog: Catalog, topologies: Sequence[GraphTopology], *, as_json: bool
) -> list[Any]:
    """`models` payload 항목 — JSON 은 구조화된 목록, 텍스트는 노드마다 한 줄."""
    resolved = node_models(catalog, topologies)
    return resolved if as_json else [_model_line(entry) for entry in resolved]


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    """Print a command result.

    명령 결과를 출력합니다 — ``--json`` 은 스크립트가 파싱할 수 있는 형태입니다.
    """
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    for key, value in payload.items():
        if isinstance(value, list):
            print(f"{key}:")
            for item in value:
                print(f"  - {item}")
        else:
            print(f"{key}: {value}")


def cmd_deploy(args: argparse.Namespace) -> int:
    """Validate a deployment before anything starts.

    배포 전 계약을 검증합니다. **검증 실패 시 아무것도 기동하지 않습니다** —
    이것이 이 명령의 요점입니다.
    """
    root = Path(args.root)
    topology = parse_declaration(Path(args.graph), GraphTopology)
    catalog = Catalog.under(root)
    manifests = catalog.agents().items

    report = validate_root(
        root, [topology], a2a_port_range=args.a2a_port_range, providers=args.providers
    )

    emit(
        {
            "graph": topology.metadata.name,
            "validated": len(manifests),
            "status": "ok" if report.ok else "failed",
            "failures": [_finding_line(f) for f in report.findings],
            "models": models_entry(catalog, [topology], as_json=args.json),
        },
        as_json=args.json,
    )
    return EXIT_OK if report.ok else EXIT_FAILED


def _finding_line(finding: Any) -> str:
    """finding 한 줄 — 카탈로그 finding 은 파일 경로 · 읽기 실패 사유 · 필드 목록까지 싣는다.

    메시지만 있으면 운영자는 어느 파일의 어느 키인지 알 수 없다. ``--json`` 도 같은 문자열이다.
    """
    details = finding.details or {}
    line = f"{finding.check}: {finding.message}"
    if details.get("path"):
        line += f" [{details['path']}]"
    if details.get("reason"):
        line += f"; {details['reason']}"
    for entry in details.get("errors", ()):
        line += f"; {entry['field']}: {entry['problem']}"
    return line


def _problem_line(problem: Problem) -> str:
    """One line for a declaration that could not be read: file, code, and which key.

    읽지 못한 선언 한 줄 — 어느 파일이 왜, 스키마 위반이면 어느 키가 왜.
    """
    return (
        f"{problem.path}: [{problem.code}] {problem.message}"
        + (f"; {problem.reason}" if problem.reason else "")
        + "".join(f"; {e['field']}: {e['problem']}" for e in problem.errors)
    )


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate every graph in the repository.

    저장소의 모든 그래프를 검증합니다 — 배포 전 일괄 점검용입니다.
    """
    root = Path(args.root)
    catalog = Catalog.under(root)
    manifests = catalog.agents().items
    # 깨진 그래프 하나가 나머지 검증을 막지 않는다 — 일괄 점검은 전부 보고 전부 말한다
    graphs = catalog.graphs()
    topologies = [graphs.items[name] for name in sorted(graphs.items)]

    report = validate_root(
        root, topologies, a2a_port_range=args.a2a_port_range, providers=args.providers
    )
    ok = report.ok and not graphs.problems

    emit(
        {
            "graphs": len(topologies),
            "agents": len(manifests),
            "status": "ok" if ok else "failed",
            "failures": [
                *(_problem_line(p) for p in graphs.problems),
                *(_finding_line(f) for f in report.findings),
            ],
            "models": models_entry(catalog, topologies, as_json=args.json),
        },
        as_json=args.json,
    )
    return EXIT_OK if ok else EXIT_FAILED


def cmd_status(args: argparse.Namespace) -> int:
    """Summarise what is declared in the repository.

    저장소에 선언된 것을 요약합니다. 실행 중 상태 조회는 runtime 연결이
    필요하므로, 이 명령은 **선언 상태**를 보고합니다.
    """
    catalog = Catalog.under(Path(args.root))
    agents, groups, graphs = catalog.agents(), catalog.groups(), catalog.graphs()

    payload: dict[str, Any] = {
        "agents": sorted(agents.items),
        "groups": sorted(groups.items),
        "graphs": sorted(graphs.items),
        "modules": sorted(catalog.module_refs()),
    }
    # 깨진 선언은 조용히 빼지 않는다 — 운영자가 빠진 줄 모른다
    # 모듈도 같다: 스키마가 깨진 모듈은 목록에서 빠지므로 여기서 말하지 않으면 그냥 사라진다
    problems = [
        *agents.problems,
        *groups.problems,
        *graphs.problems,
        *(p for module_type in MODULE_TYPES for p in catalog.modules(module_type).problems),
    ]
    if problems:
        payload["problems"] = [_problem_line(p) for p in problems]
    emit(payload, as_json=args.json)
    return EXIT_OK if not problems else EXIT_FAILED


def cmd_config(args: argparse.Namespace) -> int:
    """Show the resolved configuration for an environment.

    환경의 해석된 설정을 보여줍니다 — 환경변수 오버라이드가 반영된 결과입니다.
    """
    config = load_config(resolve_environment(args.environment), config_dir=args.config_dir)
    emit(config.model_dump(mode="json"), as_json=True)
    return EXIT_OK


def cmd_check(args: argparse.Namespace) -> int:
    """Report integrity discrepancies.

    기록과 실체의 불일치를 보고합니다. 입력은 운영 도구가 수집한 상태를
    받습니다 — CLI 가 저장소나 Docker 에 직접 붙지 않습니다.
    """
    state = load_yaml(Path(args.state))

    found = [
        *orphan_checkpoints(state.get("runs", []), state.get("checkpoints", [])),
        *dangling_module_refs(state.get("deployed_refs", {}), state.get("resolvable_refs", [])),
        *ghost_containers(state.get("running", []), state.get("known_agents", [])),
    ]

    emit(
        {
            "status": "ok" if not found else "discrepancies",
            "discrepancies": [f"{d.kind}: {d.subject} — {d.detail}" for d in found],
        },
        as_json=args.json,
    )
    return EXIT_OK if not found else EXIT_FAILED


def checkpointer_for(args: argparse.Namespace) -> Any:
    """Build the checkpointer a run should use.

    설정이 backend 와 URL 을 쥐고, ``--checkpointer`` 는 override 입니다.
    URL 을 받을 자리가 없어 `--checkpointer postgres` 가 **어떤 조합으로도**
    동작하지 않았습니다 (#220) — `configs/prod.yaml` 의 `checkpointer: postgres`
    선언도 도달 불가능했습니다.

    자격증명은 파일에 굽지 않는 것이 기본이므로 URL 은
    ``AAESTRO_ORCHESTRATOR__CHECKPOINTER_URL`` 로 덮어쓸 수 있습니다.
    """
    from aaestro.orchestrator.checkpoint import build_checkpointer

    orchestrator = orchestrator_config(args)
    return build_checkpointer(
        args.checkpointer or orchestrator.checkpointer,
        url=orchestrator.checkpointer_url,
    )


def orchestrator_config(args: argparse.Namespace) -> Any:
    """Resolve the orchestrator settings this invocation should use."""
    return load_config(
        resolve_environment(getattr(args, "environment", None)),
        config_dir=getattr(args, "config_dir", DEFAULT_CONFIG_DIR),
    ).orchestrator


def run_manager_for(args: argparse.Namespace) -> Any:
    """Build the run manager, recording to the configured store when there is one.

    기록을 남기지 않으면 **프로세스 밖에서 run 을 볼 수 없습니다** — control plane
    이 떠 있어도 빈 목록을 돌려주고, `run-drain` 요청도 구동 프로세스에 전달되지
    않습니다 (#221). 저장소는 설정이 정합니다.
    """
    from aaestro.orchestrator.run import RunManager
    from aaestro.orchestrator.runstore import SqliteRunStore

    orchestrator = orchestrator_config(args)
    store = None if orchestrator.run_store is None else SqliteRunStore(path=orchestrator.run_store)
    return RunManager(
        max_concurrent_runs=orchestrator.max_concurrent_runs,
        max_service_runs=orchestrator.max_service_runs,
        store=store,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Submit a mission run and wait for it to finish.

    mission run 을 제출하고 완주를 기다립니다.

    실행에는 살아있는 에이전트가 필요합니다 — 컨테이너가 없으면 노드 실행이
    ``GRAPH_002`` 로 실패하므로, 여기서는 **제출 전 계약 검증**까지 수행하고
    실행 경로를 명시적으로 보고합니다.
    """
    import asyncio

    from aaestro.orchestrator.checkpoint import close_checkpointer
    from aaestro.orchestrator.submit import RunSubmitter
    from aaestro.runtime.nodes import ControlNodeRuntime

    payload = json.loads(args.input) if args.input else {}
    if getattr(args, "deployment", None):
        return _run_on_deployment(args, payload)
    if args.graph is None:
        emit(
            {"status": "rejected", "error": "graph path or --deployment is required"},
            as_json=args.json,
        )
        return EXIT_FAILED

    root = Path(args.root)
    topology = parse_declaration(Path(args.graph), GraphTopology)

    report = validate_root(root, [topology], providers=args.providers)
    if not report.ok:
        # 검증에 실패한 그래프를 굴리면 노드 실행 중에야 실패한다
        emit(
            {
                "graph": topology.metadata.name,
                "status": "rejected",
                "failures": [_finding_line(f) for f in report.findings],
            },
            as_json=args.json,
        )
        return EXIT_FAILED

    # 에이전트 주소는 runtime 이 제공한다 — CLI 가 포트를 추측하지 않는다
    from aaestro.observability.metrics import Metrics

    # CLI 는 단발 실행이라 scrape 대상이 아니다 — 서버는 띄우지 않고 registry 만
    # 물려 각 계층의 집계가 실제로 돌게 한다 (상주 프로세스는 agentd 쪽)
    checkpointer = checkpointer_for(args)
    submitter = RunSubmitter(
        runtime=ControlNodeRuntime(clients=_control_clients(args)),
        manager=run_manager_for(args),
        checkpointer=checkpointer,
        metrics=Metrics(),
        models=CatalogModels(Catalog.under(root)),
    )

    if getattr(args, "service", False):
        return _run_service(submitter, topology, payload, args, checkpointer=checkpointer)

    async def run_once() -> Any:
        # 여는 쪽이 닫는다 — checkpointer 를 만든 것이 여기이므로 정리도 여기서.
        # 프로세스 종료에 기대면 소유자가 코드에 드러나지 않는다
        try:
            return await submitter.submit(topology, payload, run_id=args.run_id)
        finally:
            await close_checkpointer(checkpointer)

    result = asyncio.run(run_once())

    emit(
        {
            "run_id": result.run_id,
            "graph": result.graph,
            "status": str(result.status),
            "state": result.state,
            "error": result.error.message if result.error else None,
        },
        as_json=args.json,
    )
    return EXIT_OK if result.ok else EXIT_FAILED


def _run_service(
    submitter: Any,
    topology: Any,
    payload: dict[str, Any],
    args: argparse.Namespace,
    *,
    checkpointer: Any = None,
) -> int:
    """Drive a service graph until interrupted or bounded.

    상주 그래프를 구동합니다. ``--iterations`` 가 없으면 인터럽트까지 돌고,
    인터럽트는 **즉시 취소가 아니라 drain** 입니다 — 진행 중 iteration 을 마친
    뒤 정지하므로 반쯤 진행된 회차가 남지 않습니다.
    """
    import asyncio
    import contextlib
    import signal

    async def drive() -> Any:
        handle = await submitter.start_service(
            topology, payload, run_id=args.run_id, max_iterations=args.iterations
        )
        task = submitter.services[handle.run_id]

        # 인터럽트를 **취소가 일어나기 전에** 잡아 drain 을 요청한다.
        # 취소된 뒤에 정리하려 하면 그 대기까지 함께 취소되고, shield 로 감싸도
        # 바깥이 끝나면서 이벤트 루프가 함께 닫혀 완료를 볼 수 없다
        loop = asyncio.get_running_loop()
        installed = False
        with contextlib.suppress(NotImplementedError):  # Windows 는 미지원
            loop.add_signal_handler(signal.SIGINT, handle.request_drain)
            installed = True

        try:
            await task
        finally:
            if installed:
                with contextlib.suppress(NotImplementedError):
                    loop.remove_signal_handler(signal.SIGINT)
            # 상주 실행이라 더 중요하다 — 정리하지 않으면 커넥션을 물고 있는다
            if checkpointer is not None:
                from aaestro.orchestrator.checkpoint import close_checkpointer

                await close_checkpointer(checkpointer)
        return handle

    try:
        handle = asyncio.run(drive())
    except KeyboardInterrupt:
        emit({"graph": topology.metadata.name, "status": "interrupted"}, as_json=args.json)
        return EXIT_FAILED

    emit(
        {
            "run_id": handle.run_id,
            "graph": topology.metadata.name,
            "status": str(handle.status),
            "iterations": handle.iteration,
            "error": handle.error.message if handle.error else None,
        },
        as_json=args.json,
    )
    return EXIT_OK if handle.error is None else EXIT_FAILED


def _finished_statuses() -> frozenset[str]:
    """서버 계약에서 끌어온다 — 여기 따로 적으면 `RunStatus` 와 어긋난 채 남는다."""
    from aaestro.orchestrator.run import RunStatus

    live = (RunStatus.RUNNING, RunStatus.DRAINING)
    return frozenset(str(s) for s in RunStatus if s not in live)


def _run_on_deployment(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    """배포에 run 을 낸다 — 에이전트 주소를 적지 않는다 (#244).

    control plane 이 즉시 run_id 를 돌려주고, ``--no-wait`` 가 아니면 끝날 때까지
    ``GET /v1/runs/{id}`` 로 본다. service run 은 끝이 없으므로 기다리지 않는다.
    """
    import time

    try:
        client = _control_client(args)
        submitted = client.submit_run(
            args.deployment, payload, mode=getattr(args, "mode", None), run_id=args.run_id
        )
        run_id = submitted["run_id"]
        if getattr(args, "no_wait", False) or submitted.get("mode") == "service":
            emit(submitted, as_json=args.json)
            return EXIT_OK
        deadline = time.monotonic() + args.wait_timeout_s
        finished = _finished_statuses()
        current = submitted
        while current.get("status") not in finished:
            if time.monotonic() >= deadline:
                emit({**current, "error": "timed out waiting for the run"}, as_json=args.json)
                return EXIT_FAILED
            time.sleep(args.poll_s)
            current = client.get_run(run_id)
    except AaestroError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(current, as_json=args.json)
    return EXIT_OK if current.get("status") == "completed" else EXIT_FAILED


def _control_clients(args: argparse.Namespace) -> dict[str, Any]:
    """Build Control API clients from ``--agent name=url`` pairs.

    ``--agent`` 로 받은 주소로 Control API 클라이언트를 만듭니다 —
    CLI 가 컨테이너 포트를 추측하지 않고 호출자가 명시합니다.

    Control API 는 per-agent 토큰을 요구하므로 **토큰도 함께 실어야** 합니다 —
    빠뜨리면 모든 노드 호출이 401 이 됩니다. ``--agent-token`` 이 우선하고,
    없으면 ``AAESTRO_AGENT_TOKEN`` 을 봅니다 (compose 와 같은 키).
    """
    from aaestro.core.errors import NETWORK_RETRY
    from aaestro.runtime.control import ControlClient
    from aaestro.runtime.tokens import AGENT_TOKEN_ENV

    # 빈 문자열은 미설정과 같게 다룬다 — compose 의 ${VAR:-default} 와 같은 규칙
    token = getattr(args, "agent_token", None) or os.environ.get(AGENT_TOKEN_ENV) or None

    clients: dict[str, Any] = {}
    for entry in args.agent or ():
        name, _, url = entry.partition("=")
        if not name or not url:
            continue
        # 05 Retry Layering — runtime 이 Control API 재시도 주체다.
        # 여기서 켜지 않으면 정책은 정의만 되고 아무 일도 하지 않는다.
        # 읽기(health/card)만 재시도한다 — invoke 는 부수효과를 낳고
        # node 재시도(NodeSpec.retry)와 곱해진다
        clients[name] = ControlClient(url, agent=name, token=token, retry=NETWORK_RETRY)
    return clients


def port_range(raw: str) -> tuple[int, int]:
    """Parse a ``low-high`` port range."""
    low, _, high = raw.partition("-")
    return int(low), int(high)


def provider_names(raw: str) -> frozenset[str]:
    """Parse a comma-separated list of egress proxy upstream names."""
    names = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        # 빈 목록은 "프록시 없음" 이 아니라 "모든 provider 거부" 가 된다 — 실수로 보고 거절
        raise argparse.ArgumentTypeError("provider list must not be empty")
    return names


def _control_client(args: argparse.Namespace) -> Any:
    """이 명령이 말할 Control Plane — 주소는 플래그 또는 기본값."""
    from aaestro.cli.control import ControlClient

    # 빈 문자열은 미설정과 같게 — --agent-token 과 같은 규칙
    token = getattr(args, "control_token", None) or os.environ.get(CONTROL_TOKEN_ENV) or None
    return ControlClient(getattr(args, "control_url", None) or DEFAULT_CONTROL_URL, token=token)


def _report_control_failure(err: AaestroError, *, as_json: bool) -> int:
    """조작 실패를 사람이 읽을 형태로 — 연결 거부를 그대로 던지지 않는다."""
    emit(
        # 세부를 먼저 — 세부의 키가 명령 결과(`status`)나 코드를 덮지 못하게
        {**err.details, "status": "failed", "error_code": str(err.code), "message": err.message},
        as_json=as_json,
    )
    return EXIT_FAILED


def cmd_run_list(args: argparse.Namespace) -> int:
    """List runs the control plane knows about.

    Control Plane 이 아는 run 목록을 보여줍니다 — 다른 프로세스가 띄운
    run 도 포함됩니다.
    """
    try:
        listed = _control_client(args).list_runs(mode=args.mode)
    except AaestroError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(
        {
            "runs": [
                {
                    "run_id": run["run_id"],
                    "graph": run["graph"],
                    "mode": run["mode"],
                    "status": run["status"],
                    "iteration": run["iteration"],
                    "drain_requested": run["drain_requested"],
                }
                for run in listed
            ]
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_run_drain(args: argparse.Namespace) -> int:
    """Ask a run to stop after its current iteration.

    진행 중 iteration 을 마친 뒤 정지하도록 요청합니다 — **요청만 남기고
    돌아옵니다.** 실제 정지는 구동 프로세스가 수행하므로, 이 명령이 성공해도
    run 은 아직 돌고 있을 수 있습니다.
    """
    try:
        result = _control_client(args).drain(args.run_id)
    except AaestroError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(
        {
            "run_id": result["run_id"],
            "status": result["status"],
            "drain_requested": result["drain_requested"],
            "note": "the run stops after its current iteration",
        },
        as_json=args.json,
    )
    return EXIT_OK


def cmd_run_resume(args: argparse.Namespace) -> int:
    """Restart a halted run from its last iteration.

    ``GRAPH_005`` 로 정지한 run 을 마지막 iteration **다음**부터 재개합니다
    (05 Incident Response).
    """
    try:
        result = _control_client(args).resume(args.run_id)
    except AaestroError as err:
        return _report_control_failure(err, as_json=args.json)

    emit({"run_id": result["run_id"], "status": result.get("status", "resumed")}, as_json=args.json)
    return EXIT_OK


def cmd_run_steer(args: argparse.Namespace) -> int:
    """Queue an operator instruction for the task a run is executing right now.

    진행 중인 run 의 현재 태스크에 지시를 넣습니다 — **접수만 하고 돌아옵니다.** 지시는
    에이전트의 다음 턴 경계에서 반영되고, 태스크가 먼저 끝나면 반영되지 않습니다.
    """
    try:
        result = _control_client(args).steer(args.run_id, args.text, node=args.node, by=args.by)
    except AaestroError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(
        {
            "run_id": result["run_id"],
            "node": result["node"],
            "task_id": result["task_id"],
            "status": result["status"],
            "note": "queued for the next turn boundary; not applied if the task ends first",
        },
        as_json=args.json,
    )
    return EXIT_OK


def read_materials(directory: Path) -> dict[str, str]:
    """디렉토리를 재료 표현으로 — 컨텍스트 상대 경로 → 내용.

    캐시·의존성 디렉토리는 싣지 않는다: 재료는 **소스**다. 경로·크기 규칙은 control
    plane 이 검사한다 — 같은 규칙을 여기 두 벌 두지 않는다.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` 디렉토리가 아니거나 텍스트가 아닌 파일.
    """
    from aaestro.core.errors import ErrorCategory, ErrorCode

    if not directory.is_dir():
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="materials must be a directory",
            details={"path": str(directory)},
        )
    files: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if _SKIPPED_MATERIAL_PARTS & set(relative.parts):
            continue
        if path.is_symlink():
            # 링크를 따라가면 디렉토리 밖의 파일(자격증명 등)을 재료로 올린다 — 따라가지 않고
            # 거절한다. 조용히 건너뛰면 올라간 재료에 무엇이 빠졌는지 모른다
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="materials must not contain symbolic links",
                details={"path": relative.as_posix()},
            )
        if not path.is_file():
            continue
        try:
            files[relative.as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as err:
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="materials must be utf-8 text",
                details={"path": relative.as_posix()},
            ) from err
    return files


def cmd_agent_push(args: argparse.Namespace) -> int:
    """Upload a directory as an agent's build materials.

    디렉토리를 에이전트 현재 버전의 빌드 재료로 올립니다 (#266). 같은 내용을 다시 올리면
    그대로 성공합니다 — 시드 스크립트가 여러 번 돌아도 안전합니다.
    """
    try:
        stored = _control_client(args).push_materials(
            args.agent, read_materials(Path(args.directory))
        )
    except AaestroError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(
        {"agent": stored["agent"], "version": stored["version"], "files": sorted(stored["files"])},
        as_json=args.json,
    )
    return EXIT_OK


def cmd_agent_build(args: argparse.Namespace) -> int:
    """Bake an agent's image; with ``--wait``, block until it is built or failed.

    빌드를 제출합니다. `--wait` 이면 끝날 때까지 기다리고, 실패하면 0 이 아닌 코드와 함께
    에러와 로그 꼬리를 보여줍니다 — 배포 게이트(#266)가 막기 전에 여기서 알 수 있습니다.
    """
    client = _control_client(args)
    try:
        record = client.build_image(args.agent)
        deadline = time.monotonic() + args.timeout_s
        while args.wait and record["status"] == "building":
            if time.monotonic() >= deadline:
                emit({**_build_summary(record), "note": "timed out waiting"}, as_json=args.json)
                return EXIT_FAILED
            time.sleep(BUILD_POLL_S)
            record = client.image_status(args.agent)
    except AaestroError as err:
        return _report_control_failure(err, as_json=args.json)

    emit(_build_summary(record), as_json=args.json)
    return EXIT_FAILED if record["status"] == "failed" else EXIT_OK


def _build_summary(record: dict[str, Any]) -> dict[str, Any]:
    summary = {key: record.get(key) for key in ("agent", "version", "status", "image")}
    if record.get("status") == "failed":
        summary["error"] = record.get("error")
        summary["log"] = record.get("log", "")
    return summary


def _add_providers(command: argparse.ArgumentParser) -> None:
    # deploy · validate · run 이 같은 입력으로 같은 판정을 내리도록 세 곳에 같은 플래그
    command.add_argument(
        "--providers",
        type=provider_names,
        default=None,
        help="egress proxy upstream names, comma-separated (default: no proxy)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    CLI 파서를 만듭니다.
    """
    parser = argparse.ArgumentParser(prog="aaestro", description="Aaestro framework CLI")
    parser.add_argument("--root", default=".", help="repository root")
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    subcommands = parser.add_subparsers(dest="command", required=True)

    deploy = subcommands.add_parser("deploy", help="validate a graph before deploying")
    deploy.add_argument("graph", help="path to the graph topology yaml")
    deploy.add_argument("--a2a-port-range", type=port_range, default=None, dest="a2a_port_range")
    _add_providers(deploy)
    deploy.set_defaults(handler=cmd_deploy)

    validate = subcommands.add_parser("validate", help="validate every graph")
    validate.add_argument("--a2a-port-range", type=port_range, default=None, dest="a2a_port_range")
    _add_providers(validate)
    validate.set_defaults(handler=cmd_validate)

    status = subcommands.add_parser("status", help="summarise declared artifacts")
    status.set_defaults(handler=cmd_status)

    config = subcommands.add_parser("config", help="show resolved configuration")
    config.add_argument("environment", nargs="?", default=None)
    config.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR, dest="config_dir")
    config.set_defaults(handler=cmd_config)

    run = subcommands.add_parser("run", help="submit a mission run")
    run.add_argument("graph", nargs="?", default=None, help="path to the graph topology yaml")
    run.add_argument(
        "--deployment",
        default=None,
        help="submit to a deployment through the control plane instead of driving the run here",
    )
    run.add_argument("--no-wait", action="store_true", dest="no_wait")
    run.add_argument(
        "--control-url",
        default=None,
        dest="control_url",
        help=f"control plane address for --deployment (default: {DEFAULT_CONTROL_URL})",
    )
    run.add_argument(
        "--control-token",
        default=None,
        dest="control_token",
        help=f"control plane token (defaults to ${CONTROL_TOKEN_ENV})",
    )
    run.add_argument("--wait-timeout", type=float, default=600.0, dest="wait_timeout_s")
    run.add_argument("--poll", type=float, default=1.0, dest="poll_s")
    run.add_argument("--input", default=None, help="initial state as json")
    run.add_argument("--run-id", default=None, dest="run_id")
    _add_providers(run)
    run.add_argument(
        "--checkpointer",
        default=None,
        help="override the configured checkpoint backend (memory|postgres|redis)",
    )
    run.add_argument(
        "--env",
        default=None,
        dest="environment",
        help=f"configuration environment (default: ${ENVIRONMENT_ENV}, else dev)",
    )
    run.add_argument("--config-dir", default=DEFAULT_CONFIG_DIR, dest="config_dir")
    run.add_argument(
        "--agent",
        action="append",
        metavar="NAME=URL",
        help="agent control api address (repeatable)",
    )
    run.add_argument(
        "--agent-token",
        default=None,
        dest="agent_token",
        help="control api token (defaults to $AAESTRO_AGENT_TOKEN)",
    )
    run.add_argument(
        "--service",
        action="store_true",
        help="drive a service graph's perpetual loop instead of a mission run",
    )
    run.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="stop a service run after N iterations (default: until interrupted)",
    )
    run.set_defaults(handler=cmd_run)

    # 다른 프로세스의 run 을 조작하는 명령 — 별도 subcommand 로 둔다.
    # `run` 아래 subparser 로 넣으면 기존 `aaestro run <graph>` 형태가 깨진다
    for name, handler, helptext in (
        ("run-list", cmd_run_list, "list runs the control plane knows about"),
        ("run-drain", cmd_run_drain, "ask a run to stop after its current iteration"),
        ("run-resume", cmd_run_resume, "resume a halted run from its last iteration"),
        ("run-steer", cmd_run_steer, "redirect the task a run is executing, at its next turn"),
    ):
        command = subcommands.add_parser(name, help=helptext)
        if name != "run-list":
            command.add_argument("run_id", help="the run to operate on")
        else:
            command.add_argument(
                "--mode", default=None, choices=["mission", "service"], help="narrow by run mode"
            )
        if name == "run-steer":
            command.add_argument("text", help="the instruction to add to the conversation")
            command.add_argument(
                "--node", default=None, help="node to steer (default: the one running now)"
            )
            command.add_argument(
                "--by", default=None, help="who issues it, for the audit trail (default: operator)"
            )
        command.add_argument(
            "--control-url",
            default=None,
            dest="control_url",
            help=f"control plane address (default: {DEFAULT_CONTROL_URL})",
        )
        command.add_argument(
            "--control-token",
            default=None,
            dest="control_token",
            help=f"control plane token (defaults to ${CONTROL_TOKEN_ENV})",
        )
        command.set_defaults(handler=handler)

    push = subcommands.add_parser("agent-push", help="upload a directory as build materials")
    push.add_argument("agent", help="the declared agent the materials belong to")
    push.add_argument("directory", help="build context: Dockerfile (optional) and src/")
    push.set_defaults(handler=cmd_agent_push)

    bake = subcommands.add_parser("agent-build", help="bake an agent image from its materials")
    bake.add_argument("agent", help="the declared agent to build")
    bake.add_argument("--wait", action="store_true", help="block until built or failed")
    bake.add_argument("--timeout-s", type=float, default=DEFAULT_BUILD_TIMEOUT_S, dest="timeout_s")
    bake.set_defaults(handler=cmd_agent_build)

    for command in (push, bake):
        command.add_argument(
            "--control-url",
            default=None,
            dest="control_url",
            help=f"control plane address (default: {DEFAULT_CONTROL_URL})",
        )
        command.add_argument(
            "--control-token",
            default=None,
            dest="control_token",
            help=f"control plane token (defaults to ${CONTROL_TOKEN_ENV})",
        )

    check = subcommands.add_parser("check", help="report integrity discrepancies")
    check.add_argument("state", help="path to a yaml document describing observed state")
    check.set_defaults(handler=cmd_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    CLI 를 실행합니다. 구조화 에러는 사람이 읽을 수 있게 출력하고 비정상 종료
    코드를 돌려줍니다 — 스택 트레이스를 그대로 뱉으면 운영자가 원인을 찾기
    어렵습니다.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    # 로그는 stderr 로 — stdout 은 명령 결과 전용이다. 섞이면 `--json` 출력을
    # 스크립트가 파싱할 수 없다 (출력과 진단의 분리는 CLI 의 기본 계약)
    configure(json_output=bool(args.json), stream_name="stderr")

    try:
        exit_code: int = args.handler(args)
    except AaestroError as err:
        print(f"error [{err.code}] {err.message}", file=sys.stderr)
        for key, value in err.details.items():
            print(f"  {key}: {value}", file=sys.stderr)
        return EXIT_FAILED
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
