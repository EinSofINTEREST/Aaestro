"""Control Plane client for the CLI.

CLI 가 다른 프로세스의 run 을 조작하는 창구 (#102). 연결 실패를 그대로
노출하지 않는다 — 운영자가 보는 것은 ``ConnectionRefusedError`` 가 아니라
"어디에 무엇을 기대했는데 닿지 않았다" 여야 한다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_CONTROL_URL = "http://127.0.0.1:8700"
CONTROL_TOKEN_ENV = "AAESTRO_CONTROL_TOKEN"  # noqa: S105 — 키 이름이지 값이 아니다
DEFAULT_TIMEOUT_S = 10.0


def unreachable(url: str, err: Exception) -> AaestroError:
    """Control Plane 에 닿지 못함 — 연결 거부를 그대로 보여주지 않는다."""
    return AaestroError(
        category=ErrorCategory.NETWORK,
        code=ErrorCode.NET_001,
        message=f"control plane is unreachable at {url}",
        retryable=True,
        details={"url": url, "cause": type(err).__name__},
    )


def _failed(
    url: str, response: httpx.Response, *, run_scoped: bool, server_details: bool = False
) -> AaestroError:
    """서비스가 돌려준 실패를 구조화 에러로.

    404 를 무조건 "unknown run" 으로 읽지 않는다: 목록 조회에는 run id 가
    없으므로, 그 404 는 **엔드포인트가 없다**는 뜻이다 (버전이 안 맞는 Control
    Plane). 둘을 뭉개면 운영자가 없는 run 을 찾아 헤맨다.
    """
    # `status` 가 아니라 `http_status` 다 — CLI 출력의 `status` 는 명령의 결과(failed)이고,
    # 세부가 같은 키를 쓰면 "failed" 가 "400" 으로 덮여 스크립트가 분기하지 못한다
    details = {"url": url, "http_status": str(response.status_code)}
    if response.status_code == httpx.codes.NOT_FOUND and run_scoped:
        return AaestroError(
            category=ErrorCategory.NOT_FOUND,
            code=ErrorCode.NF_001,
            message="unknown run",
            details=details,
        )
    category, code = _server_code(response)
    if server_details:
        # 서버 세부를 먼저 — 같은 키(`url`)를 서버가 덮지 못하게
        details = {**_server_details(response), **details}
    return AaestroError(category=category, code=code, message=_detail(response), details=details)


def _server_details(response: httpx.Response) -> dict[str, Any]:
    """서버가 에러 봉투에 실은 details — 봉투가 아니면 빈 dict."""
    try:
        body = response.json()
    except ValueError:
        return {}
    error = body.get("error") if isinstance(body, dict) else None
    found = error.get("details") if isinstance(error, dict) else None
    return dict(found) if isinstance(found, dict) else {}


def _server_code(response: httpx.Response) -> tuple[ErrorCategory, str]:
    """서버가 준 카테고리와 코드를 그대로 옮긴다.

    전부 한 코드로 뭉개면 운영자는 "빌드가 이미 진행 중(RT_011)" 과 "재료가 규약 위반
    (VAL_002)" 을 구분하지 못한다 — 둘의 조치는 정반대다. 본문이 구조화 에러가 아니면
    (버전이 다른 control plane, 프록시 페이지) 이전과 같은 일반 실패로 둔다.
    """
    fallback = (ErrorCategory.RUNTIME, str(ErrorCode.GRAPH_001))
    try:
        body = response.json()
    except ValueError:
        return fallback
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        return fallback
    try:
        category = ErrorCategory(str(error.get("category")))
    except ValueError:
        category = ErrorCategory.RUNTIME
    return category, error["code"]


def _detail(response: httpx.Response) -> str:
    """응답 본문에서 사람이 읽을 사유를 꺼낸다."""
    try:
        body = response.json()
    except ValueError:
        return response.text or "control plane request failed"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    return "control plane request failed"


class ControlClient:
    """Talks to the Control Plane over HTTP.

    Control Plane 과 대화하는 클라이언트.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_CONTROL_URL,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        token: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout_s = timeout_s
        # 값을 속성에 그대로 두지 않는다 — repr/로그로 새는 경로를 하나 줄인다
        self._headers = {"authorization": f"Bearer {token}"} if token else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        run_scoped: bool = True,
        body: dict[str, Any] | None = None,
        server_details: bool = False,
    ) -> Any:
        """요청 한 건 — 연결 실패와 서비스 실패를 나눠 옮긴다.

        Args:
            method: HTTP method.
            path: Request path.
            run_scoped: Whether a 404 means "that run does not exist" — 목록
                조회는 False 다 (그 404 는 엔드포인트 부재를 뜻한다).
            server_details: 서버가 실은 details 를 에러에 옮길지 — 사유가 details 에 있는
                조작(steer 의 실행 중 노드 목록)만 켠다.
        """
        url = f"{self._base}{path}"
        try:
            # 본문이 없는 요청에 `json=None` 을 넘기면 httpx 가 리터럴 null 을 싣는다
            if body is None:
                response = httpx.request(
                    method, url, timeout=self._timeout_s, headers=self._headers
                )
            else:
                response = httpx.request(
                    method, url, timeout=self._timeout_s, headers=self._headers, json=body
                )
        except httpx.HTTPError as err:
            raise unreachable(self._base, err) from err

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise _failed(url, response, run_scoped=run_scoped, server_details=server_details)
        return response.json()

    def list_runs(self, *, mode: str | None = None) -> Sequence[dict[str, Any]]:
        """진행 중 run 목록 — mode 로 좁힐 수 있다."""
        query = f"?mode={mode}" if mode else ""
        listed: list[dict[str, Any]] = self._request("GET", f"/v1/runs{query}", run_scoped=False)
        return listed

    def submit_run(
        self,
        deployment_id: str,
        initial_state: dict[str, Any],
        *,
        mode: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """배포에 run 을 낸다 — 주소는 control plane 이 안다 (#244). 즉시 돌아온다."""
        result: dict[str, Any] = self._request(
            "POST",
            "/v1/runs",
            run_scoped=False,
            body={
                "deployment_id": deployment_id,
                "input": initial_state,
                "mode": mode,
                "run_id": run_id,
            },
        )
        return result

    def get_run(self, run_id: str) -> dict[str, Any]:
        """run 하나의 상태."""
        found: dict[str, Any] = self._request("GET", f"/v1/runs/{run_id}")
        return found

    def push_materials(self, agent: str, files: Mapping[str, str]) -> dict[str, Any]:
        """에이전트 현재 버전의 빌드 재료를 올린다 (#264). 같은 내용이면 그대로 돌아온다."""
        stored: dict[str, Any] = self._request(
            "PUT", f"/v1/agents/{agent}/materials", run_scoped=False, body={"files": dict(files)}
        )
        return stored

    def build_image(self, agent: str) -> dict[str, Any]:
        """빌드를 제출한다 — 즉시 돌아온다 (#265). 진행은 `image_status` 로 본다."""
        started: dict[str, Any] = self._request(
            "POST", f"/v1/agents/{agent}/image", run_scoped=False
        )
        return started

    def image_status(self, agent: str) -> dict[str, Any]:
        """현재 버전의 빌드 기록 — 굽힌 적 없으면 `status` 가 None 이다."""
        found: dict[str, Any] = self._request("GET", f"/v1/agents/{agent}/image", run_scoped=False)
        return found

    def drain(self, run_id: str) -> dict[str, Any]:
        """drain 을 요청한다 — 정지는 구동 프로세스가 iteration 경계에서 한다."""
        result: dict[str, Any] = self._request("POST", f"/v1/runs/{run_id}/drain")
        return result

    def resume(self, run_id: str) -> dict[str, Any]:
        """halted run 을 재개한다 — 구동 프로세스만 응답할 수 있다."""
        result: dict[str, Any] = self._request("POST", f"/v1/runs/{run_id}/resume")
        return result

    def steer(
        self, run_id: str, text: str, *, node: str | None = None, by: str | None = None
    ) -> dict[str, Any]:
        """진행 중 run 에 운영자 지시를 대기시킨다 — 접수이지 반영이 아니다 (#364)."""
        body: dict[str, Any] = {"text": text}
        # 생략한 값은 싣지 않는다 — null 을 보내면 서버 기본값(`by`)을 덮는다
        if node is not None:
            body["node"] = node
        if by is not None:
            body["by"] = by
        # steer 의 404 는 셋이다 (모르는 run / 진행 중 태스크 없음 / 그 노드가 실행 중 아님) —
        # "unknown run" 으로 뭉개면 있는 run 을 없다고 답한다. 서버의 사유를 그대로 옮긴다
        result: dict[str, Any] = self._request(
            "POST",
            f"/v1/runs/{run_id}/steer",
            run_scoped=False,
            body=body,
            server_details=True,
        )
        return result


__all__ = [
    "CONTROL_TOKEN_ENV",
    "DEFAULT_CONTROL_URL",
    "ControlClient",
    "unreachable",
]
