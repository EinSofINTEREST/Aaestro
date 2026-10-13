"""Skillpack schema and loader — Agent Skills directories, carried unmodified.

Agent Skills 표준 (https://agentskills.io/specification) 의 skill 디렉토리를 **수정 없이**
담는 모듈. skillset (Python tool 묶음) 을 대체하지 않는다 — skillset 은 프레임워크가
import 해 도구로 등록하는 코드이고, skillpack 은 모델이 읽는 지시문과 그 부속 파일이다.

로드는 선언과 검증까지다: ``SKILL.md`` 의 frontmatter 를 표준대로 검사하고, 디렉토리 안의
어떤 것도 skill 루트 밖을 가리키지 못하게 한다. ``scripts/`` 는 **실행하지도 import 하지도
않는다** — skill 디렉토리는 밖에서 받아 온 것일 수 있고, 신뢰 경계 안의 코드가 아니다.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
)

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import SemVer
from aaestro.modules.registry import ModuleRegistry
from aaestro.modules.skilltree import (
    SKILL_FILE,
    SkillViolation,
    bounded_scandir,
    open_directory,
    read_skill_file,
    safe_label,
    scan_skill_tree,
)

SKILLS_DIR = "skills"

MAX_SKILLS_PER_PACK = 50
"""skillpack 하나의 skill 수 상한 — skill 마다 name/description 이 기동 시 컨텍스트에 실린다."""

MAX_FRONTMATTER_BYTES = 16 * 1024
"""frontmatter 상한 — 필드 여섯 개의 길이 상한을 다 채워도 2 KiB 남짓이다. YAML 파싱은
바이트당 비용이 커서 (순수 Python), 파일 상한만 두면 1 MiB frontmatter 하나가 몇 초를 쓴다."""

MAX_METADATA_ENTRIES = 64
"""``metadata`` 항목 수 상한 — 부가 속성 자리이지 데이터 저장소가 아니다."""

SKILL_NAME_MAX = 64
SKILL_DESCRIPTION_MAX = 1024
SKILL_COMPATIBILITY_MAX = 500
SKILL_MD_RECOMMENDED_LINES = 500
"""표준의 권고 ("Keep your main SKILL.md under 500 lines") — 넘으면 거절이 아니라 경고다."""

STANDARD_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)
"""표준이 정의한 frontmatter 필드. 그 밖의 최상위 키는 읽지 않고 경고로 알린다."""

_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FRONTMATTER_FENCE = "---"
_VALUE_ERROR_PREFIX = "Value error, "
_YAML_STR = "tag:yaml.org,2002:str"
_YAML_NULL = "tag:yaml.org,2002:null"


class SkillFrontmatter(BaseModel):
    """``SKILL.md`` frontmatter, as the Agent Skills specification defines it.

    표준이 정의한 여섯 필드. 표준 밖의 최상위 키는 로더가 **검증 전에** 걷어내고 경고로
    알린다 (시중 skill 상당수가 쓴다) — 이 모델에는 여섯 필드만 닿는다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: StrictStr
    description: StrictStr
    license: StrictStr | None = None
    compatibility: StrictStr | None = None
    metadata: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    # 실험 필드 — 형식 (문자열) 만 확인하고 해석하지 않는다. 표준의 이름은 하이픈이다
    allowed_tools: StrictStr | None = Field(default=None, alias="allowed-tools")

    @field_validator("name")
    @classmethod
    def _name_rule(cls, value: str) -> str:
        if not 1 <= len(value) <= SKILL_NAME_MAX:
            raise ValueError(f"must be 1-{SKILL_NAME_MAX} characters, got {len(value)}")
        if not _SKILL_NAME.fullmatch(value):
            raise ValueError(
                "may only contain a-z, 0-9 and single hyphens, and must not start or end with one"
            )
        return value

    @field_validator("description")
    @classmethod
    def _description_rule(cls, value: str) -> str:
        return _bounded_text(value, SKILL_DESCRIPTION_MAX)

    @field_validator("compatibility")
    @classmethod
    def _compatibility_rule(cls, value: str | None) -> str | None:
        return None if value is None else _bounded_text(value, SKILL_COMPATIBILITY_MAX)

    @field_validator("metadata")
    @classmethod
    def _metadata_rule(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_METADATA_ENTRIES:
            raise ValueError(f"must have at most {MAX_METADATA_ENTRIES} entries, got {len(value)}")
        return value


class PackedSkill(BaseModel):
    """One validated skill directory inside a skillpack."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frontmatter: SkillFrontmatter
    root: Path
    files: tuple[str, ...]
    total_bytes: int
    lines: int

    @property
    def name(self) -> str:
        """skill 이름 — 디렉토리 이름과 같다."""
        return self.frontmatter.name

    @property
    def description(self) -> str:
        """무엇을 하고 언제 쓰는지 — 모델이 skill 을 고르는 근거."""
        return self.frontmatter.description


class SkillpackMetadata(BaseModel):
    """Skillpack metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: SemVer
    description: str | None = None


class SkillpackManifest(BaseModel):
    """``skillpack.yaml`` document.

    skill 목록은 여기 적지 않는다 — ``skills/`` 아래 디렉토리가 곧 목록이다. 선언에 한 번
    더 적게 하면 받아 온 skill 을 "수정 없이" 담을 수 없고, 두 목록이 어긋난다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    api_version: Literal["aaestro/v1"] = Field(alias="apiVersion")
    kind: Literal["Skillpack"]
    metadata: SkillpackMetadata


class LoadedSkillpack(BaseModel):
    """A loaded skillpack — declaration, validated skills, and advisory warnings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    manifest: SkillpackManifest
    root: Path
    skills: tuple[PackedSkill, ...]
    warnings: tuple[str, ...] = ()

    @property
    def skill_names(self) -> tuple[str, ...]:
        """담긴 skill 이름 — 정렬된 순서."""
        return tuple(skill.name for skill in self.skills)


@dataclass
class _Progress:
    """로드가 어디까지 왔는지 — 경계 한 곳에서 에러에 skill 이름을 붙이기 위한 자리."""

    skill: str | None = None
    skills: list[PackedSkill] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SkillpackLoader:
    """Loads skillpacks through the registry — reads and validates, never executes."""

    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry

    def load(self, ref: str) -> LoadedSkillpack:
        """Load and validate a skillpack.

        skillpack 을 로드하고 담긴 skill 을 표준대로 검증합니다. 파일은 읽기만 하고
        ``scripts/`` 의 코드는 실행하거나 import 하지 않습니다.

        Args:
            ref: Skillpack reference (``skillpacks/{name}@{version}``).

        Returns:
            The loaded skillpack. ``warnings`` carries advisories that do not reject it.

        Raises:
            AaestroError: MODULE/``MOD_001`` if unresolved, ``MOD_003`` if the declaration
                or any skill breaks a rule — the error names the skill and the field.
        """
        path, document = self._registry.load_document(ref)
        try:
            manifest = SkillpackManifest.model_validate(document)
        except ValidationError as err:
            # 원인 체인을 끊는다 — pydantic 에러의 문자열은 거절된 입력 값을 싣는다
            raise _invalid(ref, None, _problems(err)) from None

        # 파일시스템에 닿는 일은 전부 이 한 경계 안이다 — 단계마다 따로 감싸면 하나가 빠지고,
        # 빠진 OSError 하나가 카탈로그 목록 전체를 500 으로 만든다
        progress = _Progress()
        try:
            _load_skills(path.root, progress)
        except SkillViolation as violation:
            # frontmatter 값이 YAML · pydantic 에러 문자열에 실려 나가지 않게 체인을 끊는다
            problem = [(violation.field, violation.problem)]
            raise _invalid(ref, progress.skill, problem) from None
        except OSError:
            # OSError 문자열은 경로를 싣는다 — skill 안의 파일 이름은 신뢰하지 않는 입력이다
            subject = "files" if progress.skill is not None else SKILLS_DIR
            raise _invalid(ref, progress.skill, [(subject, "cannot be read")]) from None
        return LoadedSkillpack(
            ref=ref,
            manifest=manifest,
            root=path.root,
            skills=tuple(progress.skills),
            warnings=tuple(progress.warnings),
        )


def parse_skill_md(text: str) -> tuple[dict[Any, Any], str]:
    """Split ``SKILL.md`` into its frontmatter mapping and Markdown body.

    ``SKILL.md`` 를 frontmatter 와 본문으로 나눕니다. 닫는 ``---`` 는 **첫 번째** 것이다 —
    본문에 다시 나오는 ``---`` (수평선) 는 본문이다. frontmatter 가 ``MAX_FRONTMATTER_BYTES``
    를 넘으면 YAML 로 읽기 **전에** 거절합니다.

    ``metadata`` 의 스칼라 값은 **적힌 그대로의 문자열** 로 돌려줍니다 (``version: 1.10`` →
    ``"1.10"``) — YAML 이 읽은 값의 ``str()`` 이면 ``"1.1"`` 이 된다.

    Raises:
        SkillViolation: the frontmatter is missing, unclosed, too large, not YAML, or not
            a mapping.
    """
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[0].rstrip(" \t") != _FRONTMATTER_FENCE:
        raise SkillViolation(SKILL_FILE, "must start with YAML frontmatter (---)")
    closing = _closing_fence(lines)
    try:
        document = _load_frontmatter("\n".join(lines[1:closing]))
    except (yaml.YAMLError, RecursionError):
        # YAML 에러 문자열은 문제 줄을 그대로 인용한다 — 사유만 남긴다
        raise SkillViolation(SKILL_FILE, "frontmatter is not valid YAML") from None
    if not isinstance(document, dict):
        raise SkillViolation(SKILL_FILE, "frontmatter must be a mapping")
    return document, "\n".join(lines[closing + 1 :])


def _closing_fence(lines: list[str]) -> int:
    """닫는 펜스의 줄 번호 — 상한 안에서 찾지 못하면 거절한다 (파싱 전에)."""
    size = 0
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip(" \t") == _FRONTMATTER_FENCE:
            return index
        size += len(line.encode("utf-8")) + 1
        if size > MAX_FRONTMATTER_BYTES:
            raise SkillViolation(
                SKILL_FILE,
                f"frontmatter is larger than {MAX_FRONTMATTER_BYTES} bytes or not closed",
            )
    raise SkillViolation(SKILL_FILE, "frontmatter is not closed (---)")


def _load_frontmatter(text: str) -> Any:
    """YAML 을 읽되, ``metadata`` 의 스칼라는 해석 전의 표기를 쓴다."""
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        document = loader.construct_document(node)
    finally:
        loader.dispose()
    if isinstance(document, dict) and isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if key.value == "metadata" and isinstance(value, yaml.MappingNode):
                document["metadata"] = _as_written(value, document["metadata"])
    return document


def _as_written(node: yaml.MappingNode, constructed: Any) -> Any:
    """``metadata`` 의 스칼라 값을 적힌 표기 그대로 — 숫자 · 불리언 · 날짜가 문자열이 된다.

    받아 온 skill 은 ``version: 1.0`` 처럼 따옴표 없이 적는 일이 흔하다. 컨테이너 값과 빈 값
    (null), 문자열이 아닌 키는 그대로 두어 검증이 거절하게 한다.
    """
    if not isinstance(constructed, dict):
        return constructed
    written: dict[Any, Any] = {}
    for key, value in node.value:
        if not (isinstance(key, yaml.ScalarNode) and key.tag == _YAML_STR):
            return constructed
        if isinstance(value, yaml.ScalarNode) and value.tag != _YAML_NULL:
            written[key.value] = value.value
        else:
            written[key.value] = constructed.get(key.value)
    return written


def _bounded_text(value: str, limit: int) -> str:
    if not value.strip() or len(value) > limit:
        raise ValueError(f"must be 1-{limit} non-blank characters, got {len(value)}")
    return value


def _load_skills(pack_root: Path, progress: _Progress) -> None:
    """``skills/`` 를 디스크립터로 열고, 그 아래 디렉토리를 하나씩 검증한다.

    ``skills/`` 와 skill 디렉토리는 **링크를 따라가지 않고** 연다 — 링크면 대상이 팩 안이든
    밖이든 받지 않는다: 밖이면 탈출이고, 안이면 이름 (= 디렉토리 이름) 과 실체가 갈라진다.
    ``SKILL.md`` 는 그렇게 연 디스크립터를 기준으로 읽으므로, 검사와 읽기 사이에 경로의
    어느 구간이 링크로 바뀌어도 다른 파일을 읽지 않는다.
    """
    skills_root = Path(os.path.realpath(pack_root)) / SKILLS_DIR
    _require_directory(skills_root, SKILLS_DIR)
    skills_fd = open_directory(skills_root)
    try:
        entries = bounded_scandir(skills_fd, MAX_SKILLS_PER_PACK)
        if len(entries) > MAX_SKILLS_PER_PACK:
            raise SkillViolation(SKILLS_DIR, f"more than {MAX_SKILLS_PER_PACK} entries")
        for entry in entries:
            progress.skill = safe_label(entry.name)
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISREG(mode):
                # README · .DS_Store · .gitkeep — 받아 온 묶음에 흔하다. 팩을 거절할 일이 아니다
                progress.warnings.append(
                    f"{SKILLS_DIR}/{progress.skill} is not a skill directory and is skipped"
                )
                continue
            if not stat.S_ISDIR(mode):
                kind = "a link" if stat.S_ISLNK(mode) else "a special file"
                raise SkillViolation(SKILLS_DIR, f"entry is {kind}, not a skill directory")
            skill_fd = open_directory(entry.name, dir_fd=skills_fd)
            try:
                _load_skill(skills_root / entry.name, skill_fd, progress)
            finally:
                os.close(skill_fd)
        progress.skill = None
        if not progress.skills:
            raise SkillViolation(SKILLS_DIR, "holds no skill")
    finally:
        os.close(skills_fd)


def _require_directory(path: Path, label: str) -> None:
    """없음 · 링크 · 디렉토리 아님을 가려 말한다 — 운영자가 고칠 것이 서로 다르다."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise SkillViolation(label, "is missing") from None
    if stat.S_ISLNK(mode):
        raise SkillViolation(label, "must be a real directory, not a link")
    if not stat.S_ISDIR(mode):
        raise SkillViolation(label, "must be a directory")


def _load_skill(directory: Path, skill_fd: int, progress: _Progress) -> None:
    tree = scan_skill_tree(directory)
    try:
        text = read_skill_file(skill_fd).decode("utf-8")
    except UnicodeDecodeError:
        raise SkillViolation(SKILL_FILE, "is not valid UTF-8") from None
    document, _body = parse_skill_md(text)
    for key in document:
        if key not in STANDARD_FIELDS:
            progress.warnings.append(
                f"skill '{progress.skill}': frontmatter key '{safe_label(str(key))}' is not in "
                "the Agent Skills specification and is ignored"
            )
    standard = {key: value for key, value in document.items() if key in STANDARD_FIELDS}
    try:
        frontmatter = SkillFrontmatter.model_validate(standard)
    except ValidationError as err:
        name, problem = _problems(err)[0]
        raise SkillViolation(name, problem) from None
    if frontmatter.name != directory.name:
        raise SkillViolation("name", "does not match its directory name")
    lines = len(text.splitlines())
    if lines > SKILL_MD_RECOMMENDED_LINES:
        progress.warnings.append(
            f"skill '{frontmatter.name}': {SKILL_FILE} has {lines} lines "
            f"(recommended at most {SKILL_MD_RECOMMENDED_LINES})"
        )
    progress.skills.append(
        PackedSkill(
            frontmatter=frontmatter,
            root=directory,
            files=tree.files,
            total_bytes=tree.total_bytes,
            lines=lines,
        )
    )


def _problems(err: ValidationError) -> list[tuple[str, str]]:
    """pydantic 에러에서 **필드 이름과 사유만** — ``input`` · ``ctx`` 는 거절된 값을 싣는다.

    ``metadata`` 의 키 이름도 신뢰하지 않는 입력이라 정제한다.
    """
    return [
        (
            safe_label(".".join(str(part) for part in error["loc"])) or "document",
            error["msg"].removeprefix(_VALUE_ERROR_PREFIX),
        )
        for error in err.errors(include_url=False, include_input=False, include_context=False)
    ]


def _invalid(ref: str, skill: str | None, problems: list[tuple[str, str]]) -> AaestroError:
    """skillpack 검증 실패 — 어느 skill 의 어느 필드가 왜 인지 메시지에서 읽힌다."""
    name, problem = problems[0]
    subject = f"skill '{skill}'" if skill is not None else "skillpack declaration"
    details: dict[str, Any] = {
        "module_ref": ref,
        "errors": [{"field": label, "problem": reason} for label, reason in problems],
    }
    if skill is not None:
        details["skill"] = skill
    return AaestroError(
        category=ErrorCategory.MODULE,
        code=ErrorCode.MOD_003,
        message=f"invalid {subject} in {ref}: {name}: {problem}",
        details=details,
    )


__all__ = [
    "MAX_FRONTMATTER_BYTES",
    "MAX_METADATA_ENTRIES",
    "MAX_SKILLS_PER_PACK",
    "SKILLS_DIR",
    "SKILL_MD_RECOMMENDED_LINES",
    "STANDARD_FIELDS",
    "LoadedSkillpack",
    "PackedSkill",
    "SkillFrontmatter",
    "SkillpackLoader",
    "SkillpackManifest",
    "parse_skill_md",
]
