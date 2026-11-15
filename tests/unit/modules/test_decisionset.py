"""Decisionset module — registry, schema, calibration label sets, catalog publication."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from aaestro.catalog import Catalog
from aaestro.core.decision import QuestionKind
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.modules.decisionset import DecisionsetLoader
from aaestro.modules.registry import ModuleRegistry

REPO_ROOT = Path(__file__).resolve().parents[3]
REF = "decisionsets/research-triage@0.1.0"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "modules" / "decisionsets", tmp_path / "modules" / "decisionsets")
    return tmp_path


def pack_dir(repo: Path) -> Path:
    return repo / "modules" / "decisionsets" / "research-triage" / "0.1.0"


def edit(repo: Path, mutate) -> None:
    path = pack_dir(repo) / "decisionset.yaml"
    doc = yaml.safe_load(path.read_text())
    mutate(doc["spec"]["questions"])
    path.write_text(yaml.safe_dump(doc))


def load(repo: Path):
    return DecisionsetLoader(ModuleRegistry.under(repo)).load(REF)


def test_resolve_returns_path():
    path = ModuleRegistry.under(REPO_ROOT).resolve(REF)
    assert path.manifest_file.name == "decisionset.yaml"


def test_example_loads_into_contract_questions():
    loaded = load(REPO_ROOT)
    rating = loaded.questions["draft_meets_goal"]
    assert rating.kind is QuestionKind.RATING
    assert rating.options == ("unusable", "weak", "acceptable", "strong")
    assert loaded.questions["contains_instructions"].bands.act_at == 0.7
    assert loaded.questions["needs_research"].bands.act_at == 0.8


def test_eleven_choice_options_rejected(repo: Path):
    edit(
        repo,
        lambda q: q.update(
            pick={
                "kind": "choice",
                "ask": "Pick one",
                "state": ["x"],
                "options": [f"o{i}" for i in range(11)],
            }
        ),
    )
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


def test_bands_must_be_ordered(repo: Path):
    edit(repo, lambda q: q["needs_research"].update(bands={"act_at": 0.3, "reject_at": 0.3}))
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


def test_levels_on_predicate_rejected(repo: Path):
    edit(repo, lambda q: q["needs_research"].update(levels=["a", "b"]))
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


def append_row(repo: Path, name: str, row: str) -> None:
    with (pack_dir(repo) / "calibration" / name).open("a") as handle:
        handle.write(row + "\n")


@pytest.mark.parametrize(
    ("file", "row"),
    [
        ("draft_meets_goal.jsonl", '{"state": {"query": "q", "draft": "d"}, "label": "great"}'),
        ("memory_is_relevant.jsonl", '{"state": {"task": "t", "memory": "m"}, "label": "yes"}'),
        ("memory_is_relevant.jsonl", '{"state": {"task": "t"}, "label": true}'),
        ("memory_is_relevant.jsonl", "not json"),
        ("ghost.jsonl", '{"state": {}, "label": true}'),
    ],
)
def test_bad_calibration_rejected_without_echoing_values(repo: Path, file: str, row: str):
    append_row(repo, file, row)
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003
    assert "great" not in str(exc.value.details) + exc.value.message


def test_catalog_publishes_only_valid(repo: Path):
    assert Catalog.under(repo).modules("decisionsets").items == {"research-triage": ("0.1.0",)}
    append_row(repo, "draft_meets_goal.jsonl", json.dumps({"state": {}, "label": "x"}))
    listing = Catalog.under(repo).modules("decisionsets")
    assert listing.items == {}
    assert len(listing.problems) == 1
    with pytest.raises(AaestroError):
        Catalog.under(repo).module("decisionsets", "research-triage", "0.1.0")


def test_non_utf8_calibration_is_mod_003(repo: Path):
    (pack_dir(repo) / "calibration" / "needs_research.jsonl").write_bytes(b"\xff\xfe\x00\n")
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


def test_deeply_nested_json_is_mod_003(repo: Path):
    (pack_dir(repo) / "calibration" / "needs_research.jsonl").write_text("[" * 100000 + "\n")
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


def test_unreadable_calibration_is_mod_003(repo: Path, monkeypatch: pytest.MonkeyPatch):
    # root 환경에서는 chmod 000 이 읽기를 막지 못해 open 의 EACCES 를 대신 만든다
    target = pack_dir(repo) / "calibration" / "needs_research.jsonl"
    target.write_text("{}\n")
    real_open = Path.open

    def deny(self: Path, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "denied")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", deny)
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


def test_calibration_directory_entry_limit(repo: Path):
    from aaestro.modules.decisionset import MAX_CALIBRATION_FILES

    directory = pack_dir(repo) / "calibration"
    for i in range(MAX_CALIBRATION_FILES + 1):
        (directory / f"x{i}.txt").write_text("")
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


def test_partial_bands_merge_field_wise(repo: Path):
    edit(repo, lambda q: q["needs_research"].update(bands={"act_at": 0.7}))
    bands = load(repo).questions["needs_research"].bands
    assert (bands.act_at, bands.reject_at) == (0.7, 0.2)


def test_partial_bands_inverting_defaults_rejected(repo: Path):
    edit(repo, lambda q: q["needs_research"].update(bands={"act_at": 0.1}))
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


@pytest.mark.parametrize(
    "content",
    [b'{"state": {"secret": 1}, "label": tru\n', b"\xff\xfe\x00\n", b"[" * 100000 + b"\n"],
)
def test_rejection_leaves_no_input_in_exception_chain(repo: Path, content: bytes):
    (pack_dir(repo) / "calibration" / "needs_research.jsonl").write_bytes(content)
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_trailing_newline_in_names_rejected(repo: Path):
    edit(repo, lambda q: q.update({"plan\n": q.pop("needs_research")}))
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003


def test_trailing_newline_in_state_name_rejected(repo: Path):
    edit(repo, lambda q: q["needs_research"].update(state=["plan\n"]))
    with pytest.raises(AaestroError) as exc:
        load(repo)
    assert exc.value.code == ErrorCode.MOD_003
