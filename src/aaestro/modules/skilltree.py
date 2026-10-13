"""Containment checks for an Agent Skills directory.

skill 디렉토리 하나를 **읽기만 해서** 검사한다 — 안의 어떤 것도 skill 루트 밖을
가리키지 못하게 하고, 크기와 개수를 묶는다. skill 디렉토리는 외부에서 받아 온 것일
수 있으므로 파일 이름과 링크 대상은 전부 신뢰하지 않는 입력이다.

여기서는 아무것도 실행하거나 import 하지 않는다. ``scripts/`` 는 신뢰 경계 안의
코드가 아니다 — 열어 보는 파일은 ``SKILL.md`` 하나이고, 나머지는 ``lstat`` 만 한다.

검사는 **로드 시점의 트리**에 대한 것이다. 그 뒤에 트리를 바꿀 수 있는 주체로부터는
지키지 않는다 — 트리를 복사하거나 마운트하는 쪽은 그때 다시 검증해야 한다.
"""

from __future__ import annotations

import errno
import itertools
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

SKILL_FILE = "SKILL.md"

MAX_SKILL_FILES = 200
"""skill 하나의 항목 수 상한 (파일 · 디렉토리 · 링크) — 재료 스토어의 파일 수 상한과 같은 결이다.
모듈 트리가 아티팩트 저장소로 흘러가는 것을 막고, 검사가 도는 양을 묶는다."""

MAX_SKILL_FILE_BYTES = 1024 * 1024
"""파일 하나의 상한 — ``assets/`` 의 템플릿 · 도표는 들어가고 데이터셋 · 바이너리는 걸린다."""

MAX_SKILL_TOTAL_BYTES = 8 * 1024 * 1024
"""skill 하나의 총 크기 상한 — 파일 수 × 단일 상한 조합으로 수백 MB 가 되는 것을 막는다."""

MAX_SKILL_DEPTH = 8
"""디렉토리 깊이 상한 — 표준은 참조를 한 단계로 두라고 권한다. 깊은 중첩은 구조가 아니다."""

MAX_LINK_HOPS = 16
"""링크 하나를 해석하며 거치는 링크 수 상한 — 순환 링크를 끝낸다."""

_LABEL_LIMIT = 120
_READ_CHUNK = 64 * 1024
# O_NOFOLLOW: 마지막 구성 요소가 링크면 열지 않는다. O_NONBLOCK: 검사와 열기 사이에 FIFO 로
# 바뀌어도 멈추지 않는다 (일반 파일에는 효과가 없다)
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class SkillViolation(Exception):  # noqa: N818 — 경계에서 MOD_003 으로 바뀌는 내부 신호다
    """A rule a skill directory broke — which field, and why.

    skill 디렉토리가 어긴 규칙 하나. 로더 경계가 ``MOD_003`` 으로 바꾼다.
    ``problem`` 에는 신뢰하지 않는 값을 통째로 싣지 않는다 (정제한 이름 · 숫자만).
    """

    def __init__(self, field: str, problem: str) -> None:
        super().__init__(f"{field}: {problem}")
        self.field = field
        self.problem = problem


@dataclass(frozen=True)
class SkillTree:
    """What a skill directory holds — relative paths and total size."""

    files: tuple[str, ...]
    total_bytes: int


def safe_label(value: str) -> str:
    """신뢰하지 않는 이름을 에러 · 로그에 실을 수 있게 — 제어 · 서식 문자를 지우고 자른다.

    인쇄 가능한 유니코드 (``한글.md``) 는 남긴다: 운영자가 어느 파일인지 알아봐야 한다.
    바꾸는 것은 범주 ``C*`` (제어 · 서식 · surrogate) 와 백슬래시 — 보이는 것과 실제가
    달라지게 하는 문자다.
    """
    cleaned = "".join("?" if _is_deceptive(ch) else ch for ch in value)
    return cleaned[:_LABEL_LIMIT]


def bounded_scandir(directory: str | Path | int, limit: int) -> list[os.DirEntry[str]]:
    """Read at most ``limit + 1`` entries of a directory, sorted by name.

    디렉토리 항목을 ``limit + 1`` 개까지만 읽습니다. 전부 읽은 뒤 세면 항목 수십만 개짜리
    디렉토리가 상한에 걸리기 전에 메모리에 올라온다 — 상한 검사가 비싼 일보다 먼저여야 한다.
    돌려준 길이가 ``limit`` 을 넘으면 호출자가 거절한다.
    """
    with os.scandir(directory) as entries:
        taken = list(itertools.islice(entries, limit + 1))
    return sorted(taken, key=lambda entry: entry.name)


def scan_skill_tree(root: Path) -> SkillTree:
    """Walk a skill directory without following links or opening files.

    skill 디렉토리를 훑습니다 — 링크를 따라가지 않고, 파일을 열지 않습니다.

    Args:
        root: The skill directory (must be a real directory).

    Returns:
        Relative POSIX paths of every regular file and link, and their total size.

    Raises:
        SkillViolation: a name, link, file type, or size limit is violated.
    """
    base = Path(os.path.realpath(root))
    files: list[str] = []
    directory_links: list[tuple[Path, Path]] = []
    total = 0
    seen = 0
    pending: list[tuple[Path, int]] = [(base, 0)]
    while pending:
        directory, depth = pending.pop()
        entries = bounded_scandir(directory, MAX_SKILL_FILES - seen)
        seen += len(entries)
        if seen > MAX_SKILL_FILES:
            raise SkillViolation("files", f"more than {MAX_SKILL_FILES} entries")
        for entry in entries:
            path = directory / entry.name
            relative = path.relative_to(base).as_posix()
            _check_name(entry.name, relative)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                if depth + 1 > MAX_SKILL_DEPTH:
                    raise SkillViolation("files", f"nested deeper than {MAX_SKILL_DEPTH}")
                pending.append((path, depth + 1))
                continue
            if stat.S_ISLNK(info.st_mode):
                target = follow_link(base, path)
                if target.is_dir():
                    directory_links.append((path, target))
            else:
                total += _regular_size(info, relative)
                if total > MAX_SKILL_TOTAL_BYTES:
                    raise SkillViolation(
                        "files", f"total size exceeds {MAX_SKILL_TOTAL_BYTES} bytes"
                    )
            files.append(relative)
    _reject_link_cycles(base, directory_links)
    return SkillTree(files=tuple(sorted(files)), total_bytes=total)


def read_skill_file(skill_fd: int) -> bytes:
    """Read ``SKILL.md`` through a descriptor — a regular file, never a link.

    ``SKILL.md`` 를 **디스크립터로** 읽습니다. 경로로 검사하고 경로로 다시 열면 그 사이에
    파일이 루트 밖 링크로 바뀔 수 있다 — 연 것을 ``fstat`` 하고 그 디스크립터에서 읽으면
    검사한 것과 읽는 것이 같은 파일이다. 그래서 ``SKILL.md`` 는 링크일 수 없다 (루트 안을
    가리켜도): 링크를 허용하려면 경로 해석이 다시 끼어든다.

    Args:
        skill_fd: A descriptor for the skill directory, itself opened without following links.

    Returns:
        The file content — at most ``MAX_SKILL_FILE_BYTES``.

    Raises:
        SkillViolation: it is missing, a link, hard-linked, not a regular file, or too large.
        OSError: the file cannot be opened or read (the loader boundary converts it).
    """
    try:
        fd = os.open(SKILL_FILE, _FILE_FLAGS, dir_fd=skill_fd)
    except FileNotFoundError:
        raise SkillViolation(SKILL_FILE, "is missing") from None
    except OSError as err:
        if err.errno in (errno.ELOOP, errno.EMLINK):
            raise SkillViolation(SKILL_FILE, "must be a regular file, not a link") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SkillViolation(SKILL_FILE, "is not a regular file")
        if info.st_nlink > 1:
            raise SkillViolation(SKILL_FILE, "must not be hard-linked")
        chunks: list[bytes] = []
        remaining = MAX_SKILL_FILE_BYTES + 1
        # fstat 의 크기를 믿지 않는다 — 읽는 동안 자랄 수 있다. 상한 + 1 바이트에서 끊는다
        while remaining > 0 and (chunk := os.read(fd, min(_READ_CHUNK, remaining))):
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    if remaining <= 0:
        raise SkillViolation(SKILL_FILE, f"is larger than {MAX_SKILL_FILE_BYTES} bytes")
    return b"".join(chunks)


def open_directory(name: str | Path, *, dir_fd: int | None = None) -> int:
    """링크를 따라가지 않고 디렉토리를 연다 — 마지막 구성 요소가 링크면 ``OSError``."""
    return os.open(name, _DIR_FLAGS, dir_fd=dir_fd)


def follow_link(base: Path, link: Path) -> Path:
    """Resolve a link one component at a time, never leaving ``base``.

    링크를 **구성 요소 하나씩** 해석합니다. 최종 경로만 보면 밖으로 나갔다가 돌아오는
    경로 (``../<루트 이름>/x``, 밖의 링크를 거쳐 되돌아오는 체인) 가 통과한다 — 그런 링크는
    루트 밖의 이름에 기대므로 모듈을 다른 곳에 두는 순간 다른 것을 가리킨다.
    절대 경로 대상도 같은 이유로 받지 않는다 (루트 안을 가리켜도).

    ``Path.resolve()`` 를 쓰지 않는 이유가 이것이다 — 그것은 최종 경로만 돌려준다.

    Args:
        base: The real path of the skill root.
        link: The real location of the link itself (inside ``base``).

    Returns:
        The real path the link names — a regular file or directory inside ``base``.

    Raises:
        SkillViolation: the link is absolute, escapes, dangles, loops, or names a special file.
    """
    label = safe_label(link.relative_to(base).as_posix())
    current = link.parent
    parts = [link.name]
    hops = 0
    while parts:
        # OS 는 디렉토리가 아닌 것을 지나가지 못한다 (`f.txt/../x` 는 ENOTDIR) — ".." 를 글자로만
        # 처리하면 OS 에서는 깨진 링크가 통과한다
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise SkillViolation("files", f"link target does not exist: {label}")
        part = parts.pop(0)
        if part == "..":
            current = current.parent
            if not current.is_relative_to(base):
                raise SkillViolation("files", f"link leaves the skill root: {label}")
            continue
        candidate = current / part
        if candidate.is_symlink():
            hops += 1
            if hops > MAX_LINK_HOPS:
                raise SkillViolation("files", f"link chain is too long or loops: {label}")
            target = candidate.readlink()
            if target.is_absolute():
                raise SkillViolation("files", f"link target is absolute: {label}")
            # Path.parts 는 "." 과 빈 구간을 이미 걷어낸다 — ".." 은 남는다
            parts = [*target.parts, *parts]
            continue
        if not candidate.exists(follow_symlinks=False):
            raise SkillViolation("files", f"link target does not exist: {label}")
        current = candidate
    mode = current.lstat().st_mode
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise SkillViolation("files", f"link names a special file: {label}")
    return current


def _reject_link_cycles(base: Path, links: list[tuple[Path, Path]]) -> None:
    """디렉토리 링크를 따라가면 끝나지 않는 트리를 거절한다.

    검사 자체는 링크를 따라가지 않지만, 트리를 **링크를 따라** 복사 · 마운트하는 쪽은
    순환에서 끝나지 않는다. 링크 L 을 따라간 곳 (대상 디렉토리) 안에 링크 M 이 있으면
    L → M 이고, 이 관계에 순환이 있으면 거절한다. 자기 조상을 가리키는 링크 (``self -> .``,
    ``up -> ..``) 는 L → L 인 가장 짧은 순환이다.
    """
    reaches = {
        link: [other for other, _ in links if other.is_relative_to(target)]
        for link, target in links
    }
    finished: dict[Path, bool] = {}  # False 는 지금 따라가는 중, True 는 순환 없이 끝남

    def visit(link: Path) -> None:
        finished[link] = False
        for following in reaches[link]:
            if finished.get(following) is False:
                label = safe_label(following.relative_to(base).as_posix())
                raise SkillViolation(
                    "files", f"directory link leads back to its own ancestor: {label}"
                )
            if following not in finished:
                visit(following)
        finished[link] = True

    for link in reaches:
        if link not in finished:
            visit(link)


def _is_deceptive(ch: str) -> bool:
    return ch == "\\" or unicodedata.category(ch).startswith("C")


def _check_name(name: str, relative: str) -> None:
    """파일 이름의 트릭을 막는다 — 제어 · 서식 문자 (RTL override 등), 백슬래시, 깨진 인코딩.

    유니코드 범주 ``C*`` 전체를 본다: 값 목록이 아니라 "보이는 이름과 실제 이름이 달라지는
    문자" 라는 성질이다. 디코딩되지 않는 바이트는 surrogate (``Cs``) 로 들어와 같이 걸린다.
    """
    if any(_is_deceptive(ch) for ch in name):
        raise SkillViolation(
            "files", f"file name has control characters or a backslash: {safe_label(relative)}"
        )


def _regular_size(info: os.stat_result, relative: str) -> int:
    label = safe_label(relative)
    if not stat.S_ISREG(info.st_mode):
        # FIFO · 소켓 · 디바이스 — 읽는 쪽이 멈추거나 호스트 자원에 닿는다
        raise SkillViolation("files", f"special file is not allowed: {label}")
    if info.st_nlink > 1:
        # 하드링크는 어디와 이어졌는지 알 수 없다 — 루트 밖 파일의 다른 이름일 수 있다
        raise SkillViolation("files", f"hard-linked file is not allowed: {label}")
    if info.st_size > MAX_SKILL_FILE_BYTES:
        raise SkillViolation(
            "files", f"file is {info.st_size} bytes (limit {MAX_SKILL_FILE_BYTES}): {label}"
        )
    return info.st_size


__all__ = [
    "MAX_LINK_HOPS",
    "MAX_SKILL_DEPTH",
    "MAX_SKILL_FILES",
    "MAX_SKILL_FILE_BYTES",
    "MAX_SKILL_TOTAL_BYTES",
    "SKILL_FILE",
    "SkillTree",
    "SkillViolation",
    "bounded_scandir",
    "follow_link",
    "open_directory",
    "read_skill_file",
    "safe_label",
    "scan_skill_tree",
]
