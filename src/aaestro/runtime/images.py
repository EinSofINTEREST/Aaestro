"""Baking an agent image from stored materials (#265).

02 의 lifecycle 은 `Declared ──build──▶ Built ──start──▶ Starting` 이고 Rule 1 은 "이미지
빌드는 배포 파이프라인에서 — 런타임 중 빌드 금지" 다. 그 `build` 화살표를 당기는 코드가
없어서, 커스텀 에이전트는 사람이 `make build` 로 굽고 control plane 은 태그만 참조했다.

여기서는 **명시적 단계**로 당긴다: 스토어(#264)의 재료와 카탈로그의 선언을 임시 디렉토리에
조립해 굽고, 그 디렉토리를 지운다. 저장도 배포도 굽지 않는다 — 저장이 분 단위가 되어서도,
배포가 런타임 빌드가 되어서도 안 된다.
"""

from __future__ import annotations

import asyncio
import json
import posixpath
import re
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog
import yaml

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.materials import DOCKERFILE, SOURCE_ROOT

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.catalog import Catalog
    from aaestro.materials import Materials
    from aaestro.runtime.docker.engine import DockerClient

log = structlog.get_logger(__name__)

BASE_IMAGE_PREFIX = "aaestro/agent-base:"
IMAGE_PREFIX = "aaestro/agent-"
MANIFEST_NAME = "manifest.yaml"
MODULES_NAME = "modules"
MODULE_TYPES = ("skillsets", "promptsets", "memorysets")

SKELETON_DOCKERFILE = f"""\
# 스켈레톤 기본 — 재료에 Dockerfile 이 없을 때 쓴다 (#265).
# 컨텍스트 루트는 임시 디렉토리이고, 경로는 전부 그 기준이다.
ARG BASE_TAG=0.1.0
FROM {BASE_IMAGE_PREFIX}${{BASE_TAG}}

COPY --chown=1000:1000 {MANIFEST_NAME} /app/{MANIFEST_NAME}
COPY --chown=1000:1000 {MODULES_NAME}/ /app/{MODULES_NAME}/
COPY --chown=1000:1000 {SOURCE_ROOT}/ /app/{SOURCE_ROOT}/

ENV PYTHONPATH=/app/{SOURCE_ROOT} \\
    AAESTRO_ROOT=/app
"""

_FLAG = re.compile(r"^--\S+")
# 해석을 바꾸는 파서 지시문 모양 — 위치와 무관하게 거절한다 (BuildKit 의 관용성을 확인할 수 없다).
# `check` 는 lint 동작만 바꾸므로 허용한다
# `//syntax=` 와 JSON 형태 `{"syntax": …}` 도 BuildKit 이 읽을 수 있다 (추정) — 같이 거절한다
_REFUSED_DIRECTIVE = re.compile(
    r"^(?:#|//)\s*(?:escape|syntax)\s*=|^\{.*\"\s*(?:escape|syntax)\s*\"", re.IGNORECASE
)
_ASCII_SPACE = frozenset(" \t\n\v\f\r")
# Docker 의 지시문 · 인자 구분과 이어 쓰기 (`\` 뒤 공백 허용) — `str.split()` 과 다르다
_SEPARATOR = re.compile(r"[ \t\v\f\r]+")
_CONTINUATION = re.compile(r"\\[ \t]*$")
# 해석이 갈릴 수 있는 표기 (따옴표 · `\` · `$` · `:` · `@`) 는 허용 목록 밖이다
_SAFE_SOURCE = re.compile(r"[A-Za-z0-9._/*?-]+")
_SAFE_USER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(:[A-Za-z0-9_][A-Za-z0-9_.-]*)?")
_QUOTING = frozenset("'\"\\$")
_DIGEST = r"@sha256:[0-9a-f]{64}"
_TAG = r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*|\$\{[A-Za-z_][A-Za-z0-9_]*\})"
# 태그 자리의 `${NAME}` 과 다이제스트 고정 (태그 없이도) 만 허용 — 저장소 이름은 고정이다
_BASE_IMAGE = re.compile(
    re.escape(BASE_IMAGE_PREFIX.removesuffix(":")) + rf"(?::{_TAG}(?:{_DIGEST})?|{_DIGEST})"
)


class BuildStatus(StrEnum):
    """What the `build` arrow is doing."""

    BUILDING = "building"
    BUILT = "built"
    FAILED = "failed"


@dataclass(frozen=True)
class BuildRecord:
    """One build attempt — the evidence a deploy checks (#266)."""

    agent: str
    version: str
    status: str
    image: str
    log: str = ""
    error: str | None = None
    updated_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status == BuildStatus.BUILT


@runtime_checkable
class BuildStore(Protocol):
    """Where build outcomes live so a restarted control plane still knows."""

    def upsert(self, record: BuildRecord) -> None: ...
    def get(self, agent: str, version: str) -> BuildRecord | None: ...
    def list(self) -> Sequence[BuildRecord]: ...


def image_tag(agent: str, version: str) -> str:
    """The tag the framework owns — 버전이 곧 "이 선언으로 구운 이미지" 의 식별자다."""
    return f"{IMAGE_PREFIX}{agent}:{version}"


def _invalid(message: str, **details: object) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message=message,
        details=dict(details),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _tokens(text: str) -> list[str]:
    return [t for t in _SEPARATOR.split(text) if t]


def _instructions(text: str) -> list[tuple[int, str, str]]:
    """Dockerfile 을 (시작 줄 번호, 대문자 지시문, 나머지) 로 푼다 — 모든 검사가 같은 해석을 쓴다.

    Docker 의 규칙 그대로다: `\\` (뒤의 공백 · 탭 허용) 로 이어 쓴 줄은 **구분자 없이** 결합하고
    (`USER ro\\` + `ot` 는 `USER root`), 주석 · 빈 줄은 이어 쓰는 중에도 건너뛴다. 지시문과 인자는
    `[ \\t\\v\\f\\r]+` 로 가른다. 줄 번호는 1-based.
    shortcut: 이스케이프 문자는 `\\` 만 안다 — 바꾸는 `# escape=` 는 `check_dockerfile` 이 거절한다.
    """
    found: list[tuple[int, str, str]] = []
    start = 0
    buffer: str | None = None
    for number, raw in enumerate(text.split("\n"), 1):
        raw = raw.rstrip("\r")  # CRLF — Docker 는 줄 끝을 먼저 떼고 이어 쓰기를 본다
        stripped = raw.strip(" \t\v\f\r")
        if not stripped or stripped.startswith("#"):
            continue
        if buffer is None:
            start, buffer = number, ""
            raw = stripped  # 첫 줄만 앞 공백을 뗀다 — 이어 붙는 줄은 그대로 둔다
        if raw.rstrip(" \t").endswith("\\\\"):
            # BuildKit 은 `\\` 를 이스케이프된 백슬래시로 보아 이어 쓰기가 아니다 — 해석이 갈린다
            raise _invalid("Dockerfile must not end a line with an escaped backslash", line=number)
        cont = _CONTINUATION.search(raw)
        buffer += raw[: cont.start()] if cont else raw
        if cont is None:
            found.append((start, *_split_keyword(buffer)))
            buffer = None
    if buffer is not None:
        found.append((start, *_split_keyword(buffer)))
    return found


def _split_keyword(line: str) -> tuple[str, str]:
    parts = _SEPARATOR.split(line.strip(" \t\v\f\r"), maxsplit=1)
    return parts[0].upper(), parts[1] if len(parts) > 1 else ""


def _check_lexical(text: str) -> None:
    """줄 단위로 해석이 갈릴 수 있는 것을 거절한다 — 줄 번호는 그 줄이다.

    Docker 는 앞 공백 · 빈 줄 · 주석 판정에 유니코드 공백을 쓰지만 이 검사는 ASCII 공백만 안다 —
    ASCII 밖의 공백 (NBSP · NEL · U+2028 등) 은 둘의 해석을 가르므로 어디에 있든 거절한다.
    파서 지시문 `escape` 는 이어 쓰기 해석을, `syntax` 는 프런트엔드를 바꾼다.
    shortcut: 폭 없는 서식 문자 (범주 Cf) 는 양쪽이 같은 일반 문자로 읽어 거절하지 않는다.
    """
    for number, raw in enumerate(text.split("\n"), 1):
        if any(c.isspace() and c not in _ASCII_SPACE for c in raw):
            raise _invalid("Dockerfile must not contain non-ASCII whitespace", line=number)
        if _REFUSED_DIRECTIVE.search(raw.strip(" \t\v\f\r")):
            raise _invalid("Dockerfile must not use this parser directive", line=number)


def check_dockerfile(text: str) -> None:
    """Validate a Dockerfile against the layout rules (#263, #690).

    빌드가 시작되고 나면 이미지가 절반 만들어진 채 실패한다 — 규약 위반은 **굽기 전에**
    잡는다. Docker 해석을 흉내 내지 않는다: 해석이 갈릴 수 있는 표기 (heredoc · 따옴표 ·
    역슬래시 · 변수) 는 fail closed 다.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` on any rule violation.
    """
    text = text.removeprefix("\ufeff")  # Docker 는 첫 줄의 UTF-8 BOM 을 뗀 뒤 파서 지시문을 본다
    _check_lexical(text)
    instructions = _instructions(text)
    for line, _, rest in instructions:
        if "<<" in rest:
            # heredoc 본문은 지시문처럼 보이는 줄을 숨기고 Docker 는 그 줄을 지시문으로 읽지 않는다
            raise _invalid("Dockerfile must not use heredocs", line=line)
    # 스테이지 = (FROM 줄, 이미지, 별칭, 그 스테이지의 USER 지시문들)
    stages: list[tuple[int, str, str, list[tuple[int, str]]]] = []
    for line, keyword, rest in instructions:
        if keyword == "FROM":
            image, alias = _from_parts(rest)
            stages.append((line, image, alias, []))
        elif keyword == "USER" and stages:
            stages[-1][3].append((line, rest))
    if not stages:
        raise _invalid("Dockerfile must declare a FROM")
    # 굽히는 이미지는 마지막 스테이지다 — 앞 스테이지 별칭을 따라 base 에 닿아야 한다.
    # 빌더 스테이지 (COPY --from 으로 산출물만 가져오는) 는 base 일 필요가 없다
    index = len(stages) - 1
    user: tuple[int, str] | None = None
    while True:
        line, image, _, users = stages[index]
        if user is None and users:
            user = users[-1]  # 스테이지마다 가장 가까운 USER — 별칭 상속이면 부모의 것이 이어진다
        parent = next((i for i in range(index - 1, -1, -1) if stages[i][2] == image.lower()), None)
        if parent is None:
            break
        index = parent
    if not _BASE_IMAGE.fullmatch(image):
        # agentd 가 base 에 들어 있다 — 다른 이미지에서 시작하면 Control API 가 없다.
        # 태그 자리의 `${NAME}` 만 변수를 받는다 (저장소 이름은 고정이다)
        raise _invalid(f"Dockerfile must end FROM {BASE_IMAGE_PREFIX}<tag>", line=stages[-1][0])
    if user is not None:
        if not _SAFE_USER.fullmatch(user[1]):
            # 변수 · 따옴표 · 역슬래시는 검사할 수 없다 — fail closed
            raise _invalid("USER must be a plain user[:group]", line=user[0])
        if _is_root(user[1]):
            # 설치 동안 root 로 올라가는 것은 흔하다 — 되돌리지 않고 끝나는 것이 문제다
            raise _invalid("Dockerfile must not end as root (02 Security)", line=user[0])
    _check_sources(instructions)


def _from_parts(rest: str) -> tuple[str, str]:
    """`FROM [--flag] image [AS name]` 의 (이미지, 소문자 별칭)."""
    tokens = [t for t in _tokens(rest) if not _FLAG.match(t)]
    alias = tokens[2].lower() if len(tokens) >= 3 and tokens[1].lower() == "as" else ""
    return (tokens[0] if tokens else ""), alias


def _is_root(spec: str) -> bool:
    """`user[:group]` 의 user 쪽이 root 인가 — group 만 0 인 `agent:0` 은 root 가 아니다."""
    user = spec.split(":")[0]
    return user.lower() == "root" or (user.isdigit() and int(user) == 0)


def _check_sources(instructions: list[tuple[int, str, str]]) -> None:
    """`COPY`/`ADD` 의 소스가 컨텍스트 안의 로컬 경로인지.

    컨텍스트 밖 경로는 Docker 자신도 막지만 **빌드 도중에** 막는다 — 이미지가 절반
    만들어진 뒤 실패하고, 운영자는 로그를 읽어야 안다. 여기서 잡으면 저장 시점의
    finding 이 된다.

    원격 `ADD` 는 Docker 가 막지 않는다: 빌드가 임의의 URL 을 당겨 이미지에 넣는다.
    02 는 이미지를 배포 파이프라인이 굽고 버전을 고정하라고 규정하므로, 굽는 도중의
    임의 다운로드는 그 계약 밖이다.
    """
    names: list[str] = []  # 선언된 스테이지 별칭 — 인덱스는 이 목록의 위치와 같다
    for line, directive, rest in instructions:
        if directive == "FROM":
            names.append(_from_parts(rest)[1])
            continue
        if directive not in ("COPY", "ADD"):
            continue
        tokens = _tokens(rest)
        source_stage: str | None = None
        while tokens and _FLAG.match(tokens[0]):
            if tokens[0].lower().startswith("--from="):
                source_stage = tokens[0][len("--from=") :].lower()
            tokens.pop(0)
        if source_stage is not None:
            # --from 은 이미지 참조도 받는다 — 앞 스테이지가 아니면 굽는 시점에 외부 이미지를 당긴다
            earlier = names[:-1]  # 마지막은 지금 스테이지
            if directive == "ADD":
                raise _invalid("ADD does not support --from", line=line)
            is_index = source_stage.isascii() and source_stage.isdigit()
            if (
                source_stage
                and not _QUOTING.intersection(source_stage)
                and (source_stage in earlier or (is_index and int(source_stage) < len(earlier)))
            ):
                continue  # 다른 스테이지의 파일 — 컨텍스트 경로가 아니다
            raise _invalid(f"{directive} --from must name an earlier stage", line=line)
        parsed = None
        if tokens and tokens[0].startswith("["):
            try:
                parsed = json.loads(" ".join(tokens))
            except ValueError:
                parsed = (
                    None  # Docker 도 JSON 이 아니면 셸 형식으로 읽는다 — 아래 허용 목록이 거른다
                )
        if isinstance(parsed, list):
            tokens = [str(t) for t in parsed]
        # 마지막 토큰은 목적지다 — 소스만 본다
        for source in tokens[:-1]:
            # 허용 목록: 컨텍스트 안의 상대 경로 문자만. URL · git · ssh (`:` `@`) 와 셸 단어 처리 ·
            # 치환 (따옴표 · `\` · `$`) 으로 해석이 갈리는 표기는 여기서 걸린다
            if not _SAFE_SOURCE.fullmatch(source):
                raise _invalid(f"{directive} source must be a plain relative path", line=line)
            normal = posixpath.normpath(source)
            if normal.startswith("/") or normal == ".." or normal.startswith("../"):
                raise _invalid(f"{directive} source must stay inside the build context", line=line)


@dataclass
class InMemoryBuildStore:
    """테스트/단일 프로세스용."""

    _records: dict[tuple[str, str], BuildRecord] = field(default_factory=dict)

    def upsert(self, record: BuildRecord) -> None:
        self._records[record.agent, record.version] = record

    def get(self, agent: str, version: str) -> BuildRecord | None:
        return self._records.get((agent, version))

    def list(self) -> Sequence[BuildRecord]:
        return sorted(self._records.values(), key=lambda r: (r.agent, r.version))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    agent      TEXT NOT NULL,
    version    TEXT NOT NULL,
    status     TEXT NOT NULL,
    image      TEXT NOT NULL,
    log        TEXT NOT NULL DEFAULT '',
    error      TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent, version)
);
"""


@dataclass
class SqliteBuildStore:
    """material_store 와 같은 배치 — 파일 하나, 프로세스 재시작을 넘긴다."""

    path: str | Path
    _conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(
                    str(self.path), isolation_level=None, check_same_thread=False
                )
                self._conn.row_factory = sqlite3.Row
                self._conn.execute(_SCHEMA)
            except sqlite3.Error as err:
                raise AaestroError(
                    category=ErrorCategory.STORAGE,
                    code=ErrorCode.STOR_003,
                    message="build store could not be opened",
                    details={"path": str(self.path)},
                ) from err
        return self._conn

    def upsert(self, record: BuildRecord) -> None:
        with self._lock:
            self._connect().execute(
                "INSERT INTO builds VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(agent, version) DO UPDATE SET "
                "status=excluded.status, image=excluded.image, log=excluded.log, "
                "error=excluded.error, updated_at=excluded.updated_at",
                (
                    record.agent,
                    record.version,
                    record.status,
                    record.image,
                    record.log,
                    record.error,
                    record.updated_at or _now(),
                ),
            )

    def get(self, agent: str, version: str) -> BuildRecord | None:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT * FROM builds WHERE agent = ? AND version = ?", (agent, version))
                .fetchone()
            )
        return _row(row) if row else None

    def list(self) -> Sequence[BuildRecord]:
        with self._lock:
            rows = (
                self._connect().execute("SELECT * FROM builds ORDER BY agent, version").fetchall()
            )
        return [_row(row) for row in rows]


def _row(row: sqlite3.Row) -> BuildRecord:
    return BuildRecord(
        agent=row["agent"],
        version=row["version"],
        status=row["status"],
        image=row["image"],
        log=row["log"],
        error=row["error"],
        updated_at=row["updated_at"],
    )


@dataclass
class ImageBuilder:
    """Assembles a build context from the store and bakes it.

    조립 → 빌드 → **임시 디렉토리 삭제**. 작업 트리에 재료를 남기지 않는 것이 이 단계의
    존재 이유다 (#263).

    Attributes:
        catalog: 선언(매니페스트)과 모듈 루트의 출처.
        materials: 커스텀 재료 (`src/`, `Dockerfile`).
        builds: 빌드 결과 기록 — 배포가 이것을 본다 (#266).
        client: Docker. `DockerEngine` 과 같은 프로토콜을 쓴다.
        workspace: 임시 디렉토리를 만들 자리. None 이면 시스템 기본.
    """

    catalog: Catalog
    materials: Any
    builds: BuildStore
    client: DockerClient
    workspace: Path | None = None
    running: dict[tuple[str, str], asyncio.Task[BuildRecord]] = field(default_factory=dict)
    """진행 중인 빌드 — 소유자를 명시한다 (07 Async 5). 같은 (에이전트, 버전) 을 두 번
    굽게 두면 두 빌드가 같은 태그를 쓰고 기록이 늦게 끝난 쪽으로 뒤집힌다."""

    def needs_build(self, agent: str, version: str) -> bool:
        """재료가 있는 에이전트만 굽는다.

        declarative agent 는 base 이미지 + 선언 마운트로 돈다 (#243) — 프롬프트셋 하나
        바꿀 때마다 이미지를 굽게 만들면 모듈 시스템의 이점이 사라진다.
        """
        found = self.materials.get(agent, version)
        return bool(found and found.files)

    def record_of(self, agent: str, version: str) -> BuildRecord | None:
        return self.builds.get(agent, version)

    async def start(self, agent: str) -> BuildRecord:
        """Begin a build and return immediately.

        빌드는 분 단위가 될 수 있다 — HTTP 요청을 붙잡고 있어 봐야 누구에게도 도움이
        되지 않는다. 상태는 `record_of` 로 본다 (배포 제출과 같은 방식, #244).

        검증(선언·재료·Dockerfile 규약)은 **돌려주기 전에** 한다: 실패를 기록으로만
        남기면 호출자가 요청이 접수된 줄 안다.

        Raises:
            AaestroError: NOT_FOUND/``NF_001`` 선언되지 않은 에이전트,
                VALIDATION/``VAL_002`` 재료가 없거나 Dockerfile 이 규약을 어김,
                RUNTIME/``RT_011`` 같은 버전의 빌드가 이미 진행 중.
        """
        manifest = self.catalog.agent(agent)
        version = manifest.metadata.version
        key = (agent, version)
        if key in self.running and not self.running[key].done():
            raise AaestroError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_011,
                message="an image build for this version is already running",
                agent=agent,
                details={"version": version, "image": image_tag(agent, version)},
            )
        found, dockerfile = self._materials_for(agent, version)
        check_dockerfile(dockerfile)

        record = BuildRecord(
            agent=agent,
            version=version,
            status=BuildStatus.BUILDING,
            image=image_tag(agent, version),
            updated_at=_now(),
        )
        self.builds.upsert(record)

        async def run() -> BuildRecord:
            try:
                return await self._bake(agent, manifest, version, found, dockerfile)
            finally:
                self.running.pop(key, None)

        self.running[key] = asyncio.create_task(run(), name=f"build-{agent}-{version}")
        return record

    async def build(self, agent: str) -> BuildRecord:
        """Build and wait — the same path `start` drives, for callers that want the result.

        테스트와 CLI 처럼 결과가 필요한 호출자를 위한 것이다. HTTP 표면은 `start` 를 쓴다.
        """
        started = await self.start(agent)
        task = self.running.get((started.agent, started.version))
        return await task if task is not None else started

    async def _bake(
        self, agent: str, manifest: Any, version: str, found: Materials, dockerfile: str
    ) -> BuildRecord:
        """조립 → 굽기 → 임시 디렉토리 삭제. 실패도 기록으로 남는다."""
        tag = image_tag(agent, version)
        bound = log.bind(agent=agent, agent_version=version, image=tag)
        if self.workspace is not None:
            # mkdtemp 는 부모를 만들지 않는다 — 자기 작업 공간은 빌더가 챙긴다
            self.workspace.mkdir(parents=True, exist_ok=True)
        context = Path(tempfile.mkdtemp(prefix=f"aaestro-build-{agent}-", dir=self.workspace))
        try:
            self._assemble(context, manifest, found, dockerfile)
            built = await asyncio.to_thread(self.client.build, str(context), tag)
        except Exception as err:  # noqa: BLE001 — 실패도 기록으로 남는다 (05 Fail Gracefully)
            record = BuildRecord(
                agent=agent,
                version=version,
                status=BuildStatus.FAILED,
                image=tag,
                log=getattr(err, "log", ""),
                error=str(err),
                updated_at=_now(),
            )
            self.builds.upsert(record)
            bound.error("agent image build failed", error_code=ErrorCode.RT_004, exc_info=err)
            return record
        finally:
            # 조립한 것은 남기지 않는다 — 작업 트리에 재료가 남지 않는 것이 이 설계의 요점이다
            shutil.rmtree(context, ignore_errors=True)

        record = BuildRecord(
            agent=agent,
            version=version,
            status=BuildStatus.BUILT,
            image=tag,
            log=built,
            updated_at=_now(),
        )
        self.builds.upsert(record)
        bound.info("agent image built")
        return record

    def _materials_for(self, agent: str, version: str) -> tuple[Materials, str]:
        """재료와 쓸 Dockerfile — 재료가 없으면 굽지 않는다."""
        found = self.materials.get(agent, version)
        if not found or not found.files:
            raise _invalid(
                "agent has no build materials — declarative agents run on the base image",
                agent=agent,
                version=version,
            )
        return found, found.dockerfile or SKELETON_DOCKERFILE

    def _assemble(
        self, context: Path, manifest: Any, materials: Materials, dockerfile: str
    ) -> None:
        """스토어의 재료와 카탈로그의 선언을 컨텍스트에 모은다 (#263 레이아웃)."""
        (context / DOCKERFILE).write_text(dockerfile, encoding="utf-8")
        (context / MANIFEST_NAME).write_text(
            yaml.safe_dump(
                manifest.model_dump(mode="json", by_alias=True, exclude_none=True),
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        source = context / SOURCE_ROOT
        source.mkdir(exist_ok=True)
        for relative, content in materials.files.items():
            if relative == DOCKERFILE:
                continue
            target = context / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._copy_modules(context)

    def _copy_modules(self, context: Path) -> None:
        """모듈 루트를 컨텍스트로 — 없는 루트는 만들지 않는다."""
        modules = context / MODULES_NAME
        modules.mkdir(exist_ok=True)
        for module_type in MODULE_TYPES:
            root = self.catalog.roots.for_type(module_type)
            if root.is_dir():
                shutil.copytree(root, modules / module_type, dirs_exist_ok=True)


__all__ = [
    "SKELETON_DOCKERFILE",
    "BuildRecord",
    "BuildStatus",
    "BuildStore",
    "ImageBuilder",
    "InMemoryBuildStore",
    "SqliteBuildStore",
    "check_dockerfile",
    "image_tag",
]
