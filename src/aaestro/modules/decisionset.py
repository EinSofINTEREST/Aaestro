"""Decisionset schema and loader.

결정 모델에 던지는 질문 (문구 · 보기 · 구간) 을 선언하는 모듈 (04 Decisionset Modules).
질문의 형태 검증은 ``core.decision.Question`` 이 맡고, 여기서는 yaml 을 그것으로 바꾸고
calibration 라벨 세트의 형태를 읽기만 해서 검증한다 — provider 를 부르지 않는다.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aaestro.core.decision import Bands, Question, QuestionKind
from aaestro.core.errors import (
    AaestroError,
    ErrorCategory,
    ErrorCode,
    field_problems,
    raise_detached,
)
from aaestro.core.manifest import SemVer
from aaestro.modules.registry import ModuleRegistry, reject_module

CALIBRATION_DIR = "calibration"
MAX_QUESTIONS = 50
MAX_CALIBRATION_BYTES = 1024 * 1024
MAX_CALIBRATION_ROWS = 10_000
MAX_CALIBRATION_FILES = MAX_QUESTIONS
"""질문마다 세트 하나이므로 질문 상한이 곧 파일 수 상한이다."""

_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


class DecisionsetMetadata(BaseModel):
    """Decisionset metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: SemVer
    description: str | None = None


class BandsOverride(BaseModel):
    """A question's partial bands — unset fields keep the ``defaults.bands`` value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    act_at: float | None = Field(default=None, ge=0.0, le=1.0)
    reject_at: float | None = Field(default=None, ge=0.0, le=1.0)


class QuestionDecl(BaseModel):
    """One question as written in ``decisionset.yaml``.

    yaml 의 ``levels`` (rating) / ``options`` (choice) 는 ``Question.options`` 하나로 합쳐진다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: QuestionKind
    ask: str
    state: tuple[str, ...] = Field(min_length=1)
    levels: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    act_level: str | None = None
    bands: BandsOverride | None = None

    @model_validator(mode="after")
    def _shape(self) -> QuestionDecl:
        if len(set(self.state)) != len(self.state) or not all(
            _IDENT.fullmatch(s) for s in self.state
        ):
            raise ValueError("state must be unique snake_case names")
        if self.levels and self.kind is not QuestionKind.RATING:
            raise ValueError("levels is rating only")
        if self.options and self.kind is not QuestionKind.CHOICE:
            raise ValueError("options is choice only")
        return self

    def to_question(self, name: str, default_bands: Bands) -> Question:
        """Build the contract ``Question``; shape violations surface as ``ValueError``."""
        override = self.bands or BandsOverride()
        try:
            merged = Bands(
                act_at=default_bands.act_at if override.act_at is None else override.act_at,
                reject_at=(
                    default_bands.reject_at if override.reject_at is None else override.reject_at
                ),
            )
            return Question(
                name=name,
                kind=self.kind,
                ask=self.ask,
                options=self.levels or self.options,
                act_level=self.act_level,
                bands=merged,
            )
        except ValidationError as err:
            # 입력 값은 싣지 않고 필드 이름과 사유만 옮긴다
            raise ValueError(
                "; ".join(f"{p['field']}: {p['problem']}" for p in field_problems(err))
            ) from None


class DecisionsetDefaults(BaseModel):
    """Defaults a question may override."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bands: Bands = Field(default_factory=Bands)


class DecisionsetSpec(BaseModel):
    """``spec`` of a decisionset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    locale: str = Field(min_length=1)
    defaults: DecisionsetDefaults = Field(default_factory=DecisionsetDefaults)
    questions: dict[str, QuestionDecl] = Field(min_length=1, max_length=MAX_QUESTIONS)

    @model_validator(mode="after")
    def _questions_valid(self) -> DecisionsetSpec:
        for name, decl in self.questions.items():
            if not _IDENT.fullmatch(name):
                raise ValueError("question names must be snake_case")
            decl.to_question(name, self.defaults.bands)
        return self


class DecisionsetManifest(BaseModel):
    """``decisionset.yaml`` document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["aaestro/v1"] = Field(alias="apiVersion")
    kind: Literal["Decisionset"]
    metadata: DecisionsetMetadata
    spec: DecisionsetSpec


class LoadedDecisionset(BaseModel):
    """A loaded decisionset with its questions built as contract objects."""

    model_config = ConfigDict(frozen=True)

    ref: str
    manifest: DecisionsetManifest
    questions: dict[str, Question]


def _bad_calibration(ref: str, question: str, reason: str) -> AaestroError:
    # 라벨 세트는 합성 데이터라도 신뢰하지 않는 입력이다 — 값은 싣지 않고 위치만 싣는다
    return AaestroError(
        category=ErrorCategory.MODULE,
        code=ErrorCode.MOD_003,
        message=f"calibration set rejected: {reason}",
        details={"module_ref": ref, "question": question},
    )


def _label_ok(decl: QuestionDecl, label: Any) -> bool:
    if decl.kind is QuestionKind.PREDICATE:
        return isinstance(label, bool)
    return isinstance(label, str) and label in (decl.levels or decl.options)


def _check_calibration_file(ref: str, path: Path, question: str, decl: QuestionDecl) -> None:
    try:
        _scan_rows(ref, path, question, decl)
    except OSError:
        raise_detached(_bad_calibration(ref, question, "unreadable file"))
    except UnicodeDecodeError:
        raise_detached(_bad_calibration(ref, question, "not valid UTF-8"))


def _scan_rows(ref: str, path: Path, question: str, decl: QuestionDecl) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CALIBRATION_BYTES:
        raise _bad_calibration(ref, question, "not a regular file within the size limit")
    wanted = set(decl.state)
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if number > MAX_CALIBRATION_ROWS:
                raise _bad_calibration(ref, question, "too many rows")
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                raise_detached(_bad_calibration(ref, question, f"row {number} is not JSON"))
            if not (
                isinstance(row, dict)
                and set(row) == {"state", "label"}
                and isinstance(row["state"], dict)
                and set(row["state"]) == wanted
            ):
                raise _bad_calibration(ref, question, f"row {number} has the wrong shape")
            if not _label_ok(decl, row["label"]):
                raise _bad_calibration(
                    ref, question, f"row {number} label is outside the declaration"
                )


def _bounded_listing(ref: str, directory: Path) -> list[Path]:
    """Entries sorted, reading at most the limit + 1 so an oversized directory is not walked."""
    entries: list[Path] = []
    try:
        with os.scandir(directory) as scan:
            for item in scan:
                entries.append(Path(item.path))
                if len(entries) > MAX_CALIBRATION_FILES:
                    raise _bad_calibration(ref, "-", "too many entries")
    except OSError:
        raise_detached(_bad_calibration(ref, "-", "unreadable directory"))
    return sorted(entries)


class DecisionsetLoader:
    """Loads decisionsets through the registry."""

    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry

    def load(self, ref: str) -> LoadedDecisionset:
        """Load and validate a decisionset including its calibration sets.

        Raises:
            AaestroError: ``MOD_001`` if unresolved, ``MOD_003`` on schema or calibration
                violations.
        """
        path, document = self._registry.load_document(ref)
        try:
            manifest = DecisionsetManifest.model_validate(document)
        except ValidationError as err:
            reject_module(ref, err)
        spec = manifest.spec
        questions = {n: d.to_question(n, spec.defaults.bands) for n, d in spec.questions.items()}
        directory = path.root / CALIBRATION_DIR
        if os.path.lexists(directory):
            if directory.is_symlink() or not directory.is_dir():
                raise _bad_calibration(ref, "-", "calibration is not a plain directory")
            for entry in _bounded_listing(ref, directory):
                if entry.suffix != ".jsonl":
                    continue  # report.json 등 다른 산출물은 이 검증의 대상이 아니다
                decl = spec.questions.get(entry.stem)
                if decl is None:
                    raise _bad_calibration(ref, entry.stem, "set for an undeclared question")
                _check_calibration_file(ref, entry, entry.stem, decl)
        return LoadedDecisionset(ref=ref, manifest=manifest, questions=questions)
