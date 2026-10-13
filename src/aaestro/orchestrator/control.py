"""Control Plane HTTP surface for run operations.

01 은 Control Plane 의 책임으로 "run submission and result retrieval" 을
규정하지만 구현이 없었다 (🔭 Future). 이 표면이 그 최소 조각 —
**프로세스 밖에서 run 을 보고 조작하는 경로**를 연다 (#102).

조회와 drain 은 저장소만 있으면 되므로 어느 프로세스에서든 서빙할 수 있다.
Resume 은 다르다: 이어갈 state 가 구동 프로세스의 핸들에 있으므로 그 프로세스가
서빙해야 한다 — state 를 저장소에 복제하면 어느 쪽이 진실인지 모호해진다.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import AgentManifest, ModelOverride
from aaestro.http_auth import require_token
from aaestro.http_errors import request_validation_error, status_for
from aaestro.materials import Materials
from aaestro.orchestrator.bodies import parsed as _parsed
from aaestro.orchestrator.topology import GraphTopology
from aaestro.runtime.images import BuildRecord, image_tag
from aaestro.ui import UI_ROOT

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from aaestro.access.registry import AccessRegistry
    from aaestro.authoring import Author
    from aaestro.catalog import Catalog, Listing
    from aaestro.deploy import ValidationReport
    from aaestro.orchestrator.runs import RunService
    from aaestro.orchestrator.runstore import RunRecord, RunStore
    from aaestro.runtime.deployments import DeploymentManager, DeploymentRecord
    from aaestro.runtime.images import ImageBuilder

DEFAULT_STEERED_BY = "operator"
"""steer 본문이 ``by`` 를 생략했을 때의 감사 이름 — control plane 토큰은 사람을 가르지 않는다."""


log = structlog.get_logger(__name__)


def unknown_run(run_id: str) -> AaestroError:
    """미지의 run — 조용히 200 을 돌려주면 호출자가 조작이 먹혔다고 오해한다."""
    return AaestroError(
        category=ErrorCategory.NOT_FOUND,
        code=ErrorCode.NF_001,
        message=f"unknown run: {run_id}",
        details={"run_id": run_id},
    )


def not_driven_here(run_id: str, message: str) -> JSONResponse:
    """이 표면이 해 줄 수 없는 run 조작 — 200 으로 삼키지 않고 501 로 답한다.

    조용히 성공하면 운영자가 조작이 먹혔다고 믿고 손을 뗀다.
    """
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"error": {"code": str(ErrorCode.GRAPH_001), "message": message, "run_id": run_id}},
    )


class SteerRequest(BaseModel):
    """`/v1/runs/{run_id}/steer` 본문."""

    # 오타 난 키(``nodes``)를 무시하면 지시가 엉뚱한 노드로 간다
    model_config = ConfigDict(extra="forbid")

    text: str
    by: str = DEFAULT_STEERED_BY
    """감사용 이름 — **호출자의 주장**이다. 인증된 신원이 아니다."""
    node: str | None = None
    """생략하면 지금 실행 중인 노드."""


class RunView(BaseModel):
    """A run as the control plane reports it."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    graph: str
    mode: str
    status: str
    iteration: int
    failure_streak: int
    drain_requested: bool
    updated_at: str


def view_of(record: RunRecord) -> RunView:
    """저장 기록을 응답 표현으로."""
    return RunView(
        run_id=record.run_id,
        graph=record.graph,
        mode=record.mode,
        status=record.status,
        iteration=record.iteration,
        failure_streak=record.failure_streak,
        drain_requested=record.drain,
        updated_at=record.updated_at,
    )


def create_app(
    store: RunStore,
    *,
    resume: Callable[[str], Any] | None = None,
    catalog: Catalog | None = None,
    token: str | None = None,
    author: Author | None = None,
    builder: ImageBuilder | None = None,
    deployments: DeploymentManager | None = None,
    runs: RunService | None = None,
    access: AccessRegistry | None = None,
    enforcer_token: str | None = None,
) -> FastAPI:
    """Build the Control Plane app.

    run 조작 표면을 만듭니다.

    Args:
        store: Where runs are recorded — 다른 프로세스가 쓴 것도 보입니다.
        resume: Resumes a halted run; 이 프로세스가 그 run 을 구동할 때만
            제공됩니다. 없으면 resume 은 501 로 거절합니다 — 조용히 성공하면
            운영자가 재개됐다고 오해합니다.
        catalog: What the repository declares (#240). 없으면 카탈로그 라우트를
            열지 않는다 — 빈 목록을 돌려주면 "선언이 없다" 로 읽힌다.
        deployments: Turns graphs into running containers (#243). 없으면 배포
            라우트를 열지 않는다. 기동 시 살아 있는 컨테이너에 다시 붙는다.
        runs: Submits and resumes runs against deployments (#244). 있으면
            ``resume`` 콜백보다 우선한다 — 이 프로세스가 구동 프로세스다.
        author: Validates and writes graphs and manifests (#242). 없으면 쓰기
            라우트를 열지 않는다 — 읽기 전용 배포가 있을 수 있다.
        access: The access registry (#277). 없으면 권한 라우트를 열지 않는다.
        enforcer_token: 강제 지점이 판정·변경 알림 라우트에 내미는 토큰 — control plane 토큰과
            따로 둔다. 강제 지점이 운영자 권한을 덤으로 갖지 않게.
        token: Bearer token every request must present (#241). None 이면 검사하지
            않는다 — 그것이 안전한지(loopback 인지)는 진입점이 판단한다.
            ``/v1/health`` 만 예외다 (02 API Rules 4 와 같은 이유).

    Returns:
        The FastAPI application.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # 재시작 뒤 Docker 가 들고 있는 컨테이너를 기록과 대조해 다시 붙는다 —
        # 서버의 이벤트 루프 안에서 해야 health 감시 태스크가 살아남는다
        if deployments is not None:
            await deployments.reattach()
        try:
            yield
        finally:
            if runs is not None:
                await runs.close()

    app = FastAPI(title="Aaestro Control Plane", lifespan=lifespan)
    # 읽기도 보호한다 — 카탈로그에는 env_allowlist 같은 운영 정보가 있다
    api = APIRouter(dependencies=[Depends(require_token(token, realm="control plane token"))])

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        """무인증 — 살아 있는지만 답한다. 운영 정보는 싣지 않는다."""
        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def _on_bad_body(_request: Request, err: RequestValidationError) -> JSONResponse:
        """본문이 JSON 도 아니거나 라우트 시그니처에 못 미치면 FastAPI 가 여기 오기 전에
        422 를 낸다 — 카탈로그·authoring 이 내는 `VAL_002` 와 같은 모양으로 맞춘다.
        UI 가 한 가지 모양만 다루게 (#242 리뷰)."""
        # bodies.parsed() 도 필수 누락을 VAL_002 로 답한다 — 한 앱 안에서 코드가 갈리지 않게
        problem = request_validation_error(err, distinguish_missing=False)
        return JSONResponse(
            status_code=status_for(problem), content={"error": problem.payload().model_dump()}
        )

    @app.exception_handler(AaestroError)
    async def _on_error(_request: Request, err: AaestroError) -> JSONResponse:
        """구조화 에러를 상태 코드로 — 매핑은 공용 규칙을 따른다 (#234).

        여기서 자체 매핑을 갖고 있던 동안 저장소 실패(`STOR_003`)가 400 으로
        나갔다 — 서버 장애를 클라이언트 잘못으로 보고한 셈이다.
        """
        return JSONResponse(
            status_code=status_for(err), content={"error": err.payload().model_dump()}
        )

    @api.get("/v1/runs")
    async def list_runs(mode: str | None = None) -> list[dict[str, Any]]:
        """기록된 run 목록 — mode 로 좁힐 수 있습니다."""
        return [view_of(record).model_dump() for record in store.list(mode=mode)]

    @api.get("/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """run 하나의 상태."""
        record = store.get(run_id)
        if record is None:
            raise unknown_run(run_id)
        view = view_of(record).model_dump()
        result = runs.result_of(run_id) if runs is not None else None
        if result is not None:
            # 완주한 mission 의 최종 state — 이 프로세스가 구동한 것만 안다
            view["state"] = result.state
            view["error"] = result.error.payload().model_dump() if result.error else None
        return view

    @api.post("/v1/runs/{run_id}/drain")
    async def drain_run(run_id: str) -> dict[str, Any]:
        """Ask a run to stop after its current iteration.

        **요청만 남기고 즉시 반환합니다** — 진행 중 iteration 완료를 여기서
        기다리면 HTTP timeout 과 drain timeout 이 뒤엉킵니다. 실제 정지는
        구동 프로세스가 iteration 경계에서 수행합니다.
        """
        if not store.request_drain(run_id):
            raise unknown_run(run_id)
        record = store.get(run_id)
        if record is None:  # pragma: no cover - 방금 갱신했다
            raise unknown_run(run_id)
        return view_of(record).model_dump()

    @api.post("/v1/runs/{run_id}/resume")
    async def resume_run(run_id: str) -> dict[str, Any]:
        """Restart a halted run from its last iteration.

        재개는 **구동 프로세스만** 할 수 있습니다 — 이어갈 state 가 그 프로세스의
        핸들에 있기 때문입니다.
        """
        if store.get(run_id) is None:
            raise unknown_run(run_id)
        if runs is not None:
            resumed = await runs.resume(run_id)
            return {**view_of(resumed).model_dump(), "status": "resumed"}
        if resume is None:
            # 조용히 성공하면 운영자가 재개됐다고 믿고 손을 뗀다
            return not_driven_here(  # type: ignore[return-value]
                run_id, "this control plane does not drive the run"
            )

        handle = await resume(run_id)
        return {"run_id": getattr(handle, "run_id", run_id), "status": "resumed"}

    @api.post("/v1/runs/{run_id}/steer", status_code=status.HTTP_202_ACCEPTED)
    async def steer_run(run_id: str, body: Annotated[Any, Body()]) -> dict[str, Any]:
        """Queue an operator instruction for the task a run is executing right now.

        **접수만 하고 돌아옵니다** — 지시는 그 에이전트의 다음 턴 경계에서 대화에 들어가고,
        태스크가 먼저 끝나면 반영되지 않습니다 (agentd 가 WARN 으로 남깁니다).

        진행 중 태스크는 **구동 프로세스만** 압니다. 이 표면이 run 을 구동하지 않거나 대상
        에이전트의 실행기가 steer 를 지원하지 않으면 501 입니다.
        """
        record = store.get(run_id)
        if record is None:
            raise unknown_run(run_id)
        request = _parsed(body, SteerRequest)
        # 거절도 남긴다 — 누가 어느 run 에 넣으려다 왜 막혔는지가 감사의 절반이다. 본문은 길이만
        audit: dict[str, Any] = {
            "graph": record.graph,
            "run_id": run_id,
            "steered_by": request.by,
            "text_chars": len(request.text),
            **({"node_id": request.node} if request.node else {}),
        }
        if runs is None:
            log.warning("run steer refused", reason="control plane does not drive runs", **audit)
            return not_driven_here(  # type: ignore[return-value]
                run_id, "this control plane does not drive the run"
            )
        try:
            receipt = await runs.steer(run_id, request.text, by=request.by, node=request.node)
        except AaestroError as err:
            log.warning("run steer refused", error_code=str(err.code), **audit)
            raise
        if receipt is None:
            log.warning("run steer refused", reason="node runtime cannot forward a steer", **audit)
            return not_driven_here(  # type: ignore[return-value]
                run_id, "this control plane cannot forward a steer to the run"
            )
        if not receipt.supported:
            log.warning(
                "run steer refused",
                reason="executor does not support steer",
                agent=receipt.agent,
                **audit,
            )
            return not_driven_here(  # type: ignore[return-value]
                run_id, f"agent executor does not support steer: {receipt.agent}"
            )
        return {
            "run_id": run_id,
            "node": receipt.node_id,
            "task_id": receipt.task_id,
            "agent": receipt.agent,
            "status": "queued",
        }

    if catalog is not None:
        _mount_catalog(api, catalog)
    if author is not None:
        _mount_authoring(api, author)
    if builder is not None:
        _mount_images(api, builder)
    if deployments is not None:
        _mount_deployments(api, deployments)
    if runs is not None:
        _mount_runs(api, runs)
    if access is not None:
        from aaestro.orchestrator.access_api import mount_access

        mount_access(app, api, access, enforcer_token=enforcer_token)

    app.include_router(api)
    _mount_ui(app)
    return app


def _mount_ui(app: FastAPI) -> None:
    """운영자 화면 — 정적 파일. 인증은 화면이 아니라 화면이 부르는 `/v1/*` 에 있다.

    페이지 자체는 비밀이 아니다 (토큰 없이는 카탈로그도 못 읽는다). 별도 프로세스 없이
    같은 포트에서 서빙한다 (#245 기술 결정 — 빌드 없는 정적 자산).
    """
    app.mount("/ui", StaticFiles(directory=str(UI_ROOT), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/ui/")


def _mount_catalog(api: APIRouter, catalog: Catalog) -> None:
    """읽기 전용 카탈로그 — UI 가 "무엇으로 조립할 수 있는가" 를 보는 표면.

    목록은 파싱된 것과 **깨진 것을 함께** 돌려준다. 하나가 깨졌다고 전체를
    500 으로 답하면 운영자는 어느 파일인지 모른다.
    """

    def listing(found: Listing[Any], summary: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        return {
            "items": [summary(item) for item in found.items.values()],
            "problems": [asdict(problem) for problem in found.problems],
        }

    @api.get("/v1/agents")
    async def list_agents() -> dict[str, Any]:
        return listing(
            catalog.agents(),
            lambda m: {
                "name": m.name,
                "version": m.metadata.version,
                "group": m.metadata.group,
                "description": m.metadata.description,
                "model": {"provider": m.spec.model.provider, "name": m.spec.model.name},
            },
        )

    @api.get("/v1/agents/{name}")
    async def get_agent(name: str) -> dict[str, Any]:
        return catalog.agent(name).model_dump(mode="json")

    @api.get("/v1/graphs")
    async def list_graphs() -> dict[str, Any]:
        return listing(
            catalog.graphs(),
            lambda g: {
                "name": g.metadata.name,
                "version": g.metadata.version,
                "description": g.metadata.description,
                "mode": str(g.spec.mode),
                "goal": g.spec.goal,
                "nodes": len(g.spec.nodes),
            },
        )

    @api.get("/v1/graphs/{name}")
    async def get_graph(name: str) -> dict[str, Any]:
        return catalog.graph(name).model_dump(mode="json")

    @api.get("/v1/groups")
    async def list_groups() -> dict[str, Any]:
        return listing(
            catalog.groups(),
            lambda g: {
                "name": g.metadata.name,
                "description": g.metadata.description,
                "quotas": g.spec.quotas.model_dump(mode="json"),
            },
        )

    @api.get("/v1/groups/{name}")
    async def get_group(name: str) -> dict[str, Any]:
        return catalog.group(name).model_dump(mode="json")

    @api.get("/v1/modules/{module_type}")
    async def list_modules(module_type: str) -> dict[str, Any]:
        found = catalog.modules(module_type)
        return {
            "items": [
                {"name": name, "versions": list(versions)} for name, versions in found.items.items()
            ],
            "problems": [asdict(problem) for problem in found.problems],
            "warnings": list(found.warnings),
        }

    @api.get("/v1/modules/{module_type}/{name}/{version}")
    async def get_module(module_type: str, name: str, version: str) -> dict[str, Any]:
        return catalog.module(module_type, name, version)


def _report(report: ValidationReport) -> dict[str, Any]:
    return {
        "ok": report.ok,
        "findings": [
            {"check": f.check, "code": str(f.code), "message": f.message, **f.details}
            for f in report.findings
        ],
        "warnings": [
            {"check": f.check, "code": str(f.code), "message": f.message, **f.details}
            for f in report.warnings
        ],
    }


class Declarations(BaseModel):
    """`/v1/declarations` 본문 — 이름 → 문서. 이름이 곧 경로다."""

    # 오타 난 키가 조용히 빠지면 빈 본문으로 처리된다 (초안을 안 보고 ok, 빈 재료 저장)
    model_config = ConfigDict(extra="forbid")

    graphs: dict[str, Any] = {}
    agents: dict[str, Any] = {}


class MaterialUpload(BaseModel):
    """`/v1/agents/{name}/materials` 본문 — 컨텍스트 상대 경로 → 내용."""

    # 오타 난 키가 조용히 빠지면 빈 본문으로 처리된다 (초안을 안 보고 ok, 빈 재료 저장)
    model_config = ConfigDict(extra="forbid")

    files: dict[str, str] = Field(default_factory=dict)


def _materials_view(materials: Materials) -> dict[str, Any]:
    """재료를 응답 표현으로 — 내용까지 싣는다 (화면이 편집한다).

    경로만 돌려주면 편집기가 파일마다 다시 물어야 한다. 재료는 소스이고 상한이 걸려 있어
    (`MAX_FILE_BYTES`) 한 번에 실어도 된다.
    """
    return {
        "agent": materials.agent,
        "version": materials.version,
        "files": dict(materials.files),
        "updated_at": materials.updated_at,
    }


class Draft(BaseModel):
    """`/v1/validate` 본문 — 저장하지 않을 초안들.

    모듈 수준에 두는 이유: `from __future__ import annotations` 아래에서 FastAPI 는
    annotation 을 문자열로 해석한다 — 함수 안의 클래스는 못 찾고 본문을 쿼리
    파라미터로 오해한다 (#241 의 `Request` 와 같은 함정).
    """

    # 오타 난 키가 조용히 빠지면 빈 본문으로 처리된다 (초안을 안 보고 ok, 빈 재료 저장)
    model_config = ConfigDict(extra="forbid")

    graphs: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []


def _mount_authoring(api: APIRouter, author: Author) -> None:
    """검증 후 저장 — 검증 없이 쓰는 경로는 없다 (01 Contract Validation)."""

    @api.post("/v1/validate")
    async def validate(body: Annotated[Any, Body()]) -> dict[str, Any]:
        draft = _parsed(body, Draft)
        """초안을 저장된 것과 함께 검증만 한다 — 아무것도 쓰지 않는다."""
        return _report(
            author.validate(
                graphs=[_parsed(g, GraphTopology) for g in draft.graphs],
                agents=[_parsed(a, AgentManifest) for a in draft.agents],
            )
        )

    @api.put("/v1/declarations")
    async def put_declarations(body: Annotated[Any, Body()]) -> dict[str, Any]:
        """그래프와 에이전트를 **함께** 검증하고 함께 쓴다 — 에이전트 버전을 올리면서
        그것을 참조하는 그래프를 같이 올릴 때 따로는 어느 순서로도 통과하지 못한다."""
        draft = _parsed(body, Declarations)
        written = author.save_all(
            graphs={n: _parsed(g, GraphTopology) for n, g in draft.graphs.items()},
            agents={n: _parsed(a, AgentManifest) for n, a in draft.agents.items()},
        )
        return {"written": [str(p) for p in written]}

    @api.put("/v1/graphs/{name}")
    async def put_graph(name: str, body: Annotated[Any, Body()]) -> dict[str, Any]:
        path = author.save_graph(name, _parsed(body, GraphTopology))
        return {"name": name, "path": str(path)}

    @api.delete("/v1/graphs/{name}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_graph(name: str) -> None:
        author.delete_graph(name)

    @api.put("/v1/agents/{name}")
    async def put_agent(name: str, body: Annotated[Any, Body()]) -> dict[str, Any]:
        path = author.save_agent(name, _parsed(body, AgentManifest))
        return {"name": name, "path": str(path)}

    @api.get("/v1/agents/{name}/materials")
    async def get_materials(name: str) -> dict[str, Any]:
        return _materials_view(author.read_materials(name))

    @api.put("/v1/agents/{name}/materials")
    async def put_materials(name: str, body: Annotated[Any, Body()]) -> dict[str, Any]:
        request = _parsed(body, MaterialUpload)
        return _materials_view(author.save_materials(name, request.files))

    @api.delete("/v1/agents/{name}/materials", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_materials(name: str) -> None:
        author.delete_materials(name)

    @api.delete(
        "/v1/agents/{name}",
        # 성공이 두 갈래다 — 생성된 클라이언트가 한쪽만 알면 다른 쪽을 실패로 읽는다 (#301 리뷰)
        responses={
            status.HTTP_204_NO_CONTENT: {"description": "선언과 빈 디렉토리를 지웠다"},
            status.HTTP_200_OK: {
                "description": "선언은 지웠고, 사람이 둔 파일이 남았다",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "retained": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["retained"],
                        },
                        "example": {"retained": ["Dockerfile", "src/agent.py"]},
                    }
                },
            },
        },
    )
    async def delete_agent(name: str) -> Response:
        """선언을 지운다 — 빈 디렉토리는 함께, 사람이 둔 파일이 남으면 그 목록을 답한다 (#258)."""
        retained = author.delete_agent(name)
        if not retained:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JSONResponse(status_code=status.HTTP_200_OK, content={"retained": retained})


def _deployment_view(record: DeploymentRecord) -> dict[str, Any]:
    return {
        "deployment_id": record.deployment_id,
        "graph": record.graph,
        "version": record.version,
        "status": record.status,
        "error": record.error,
        "updated_at": record.updated_at,
        "model_overrides": record.model_overrides,
        "agents": [
            {
                "name": a.name,
                "replica": a.replica,
                "container_id": a.container_id[:12],
                "image": a.image,
                "control_port": a.control_port,
                "a2a_port": a.a2a_port,
            }
            for a in record.agents
        ],
    }


class DeployRequest(BaseModel):
    # 모르는 키(``model_override`` 오타)를 무시하면 override 없이 조용히 배포된다
    model_config = ConfigDict(extra="forbid")

    graph: str
    model_overrides: dict[str, ModelOverride] = Field(default_factory=dict)
    """노드 id → 부분 모델 선언. 모르는 노드 id 는 배포가 ``VAL_002`` 로 거절한다."""


def _mount_deployments(api: APIRouter, deployments: DeploymentManager) -> None:
    """배포 lifecycle — 검증 → 실제 컨테이너 기동 → health → 기록 → 해체 (#243).

    토큰은 응답에 싣지 않는다 — 기록에는 있지만 API 로 내보낼 이유가 없다.
    """

    @api.post("/v1/deployments", status_code=status.HTTP_201_CREATED)
    async def create_deployment(body: Annotated[Any, Body()]) -> dict[str, Any]:
        request = _parsed(body, DeployRequest)
        return _deployment_view(
            await deployments.deploy(request.graph, model_overrides=request.model_overrides)
        )

    @api.get("/v1/deployments")
    async def list_deployments() -> dict[str, Any]:
        return {"items": [_deployment_view(r) for r in deployments.deployments()]}

    @api.get("/v1/deployments/{deployment_id}")
    async def get_deployment(deployment_id: str) -> dict[str, Any]:
        return _deployment_view(deployments.get(deployment_id))

    @api.delete("/v1/deployments/{deployment_id}")
    async def delete_deployment(deployment_id: str) -> dict[str, Any]:
        return _deployment_view(await deployments.teardown(deployment_id))


class RunSubmission(BaseModel):
    # DeployRequest 와 같은 이유 — 오타 난 키가 조용히 빠진 채 run 이 시작되지 않게
    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    input: dict[str, Any] = Field(default_factory=dict)
    mode: str | None = None
    run_id: str | None = None


def _mount_runs(api: APIRouter, runs: RunService) -> None:
    """run 제출 — 주소는 배포가 안다 (#244). 제출은 즉시 돌아온다."""

    @api.post("/v1/runs", status_code=status.HTTP_202_ACCEPTED)
    async def submit_run(body: Annotated[Any, Body()]) -> dict[str, Any]:
        request = _parsed(body, RunSubmission)
        record = await runs.submit(
            request.deployment_id, request.input, mode=request.mode, run_id=request.run_id
        )
        return view_of(record).model_dump()


def _build_view(record: BuildRecord) -> dict[str, Any]:
    return {
        "agent": record.agent,
        "version": record.version,
        "status": record.status,
        "image": record.image,
        # 게이트와 같은 함수 — UI 가 기록의 태그와 대조할 기준
        "expected_image": image_tag(record.agent, record.version),
        "error": record.error,
        "log": record.log,
        "updated_at": record.updated_at,
    }


def _mount_images(api: APIRouter, builder: ImageBuilder) -> None:
    """`Declared → Built` 를 당기는 표면 (#265).

    저장도 배포도 굽지 않는다 — 저장이 분 단위가 되어서도, 배포가 런타임 빌드가 되어서도
    안 되기 때문이다 (02 Lifecycle 1).
    """

    @api.post("/v1/agents/{name}/image", status_code=status.HTTP_202_ACCEPTED)
    async def build_image(name: str) -> dict[str, Any]:
        """제출만 하고 돌아온다 — 빌드는 분 단위이고, 진행은 GET 으로 본다 (#244 와 같은 결).

        같은 버전을 두 번 굽는 요청은 `RT_011` → 409 로 거절한다: 두 빌드가 같은 태그를
        쓰므로 결과가 늦게 끝난 쪽으로 뒤집힌다.
        """
        return _build_view(await builder.start(name))

    @api.get("/v1/agents/{name}/image")
    async def get_image(name: str) -> dict[str, Any]:
        manifest = builder.catalog.agent(name)
        version = manifest.metadata.version
        record = builder.record_of(name, version)
        if record is None:
            return {
                "agent": name,
                "version": version,
                "status": None,
                "image": image_tag(name, version),
                "expected_image": image_tag(name, version),
                "needs_build": builder.needs_build(name, version),
                "error": None,
                "log": "",
                "updated_at": "",
            }
        return {**_build_view(record), "needs_build": builder.needs_build(name, version)}


__all__ = ["RunView", "SteerRequest", "create_app", "unknown_run", "view_of"]
