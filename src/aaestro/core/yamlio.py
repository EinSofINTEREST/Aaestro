"""Strict YAML reading for declarations.

선언 yaml 읽기 — 중복 키를 거절한다. ``yaml.safe_load`` 는 같은 키가 두 번 나오면 뒤 값이
조용히 이긴다. 좁힌 줄 (``allowed_tools``) 뒤에 넓은 줄이 붙으면 선언이 소리 없이 넓어지므로,
모르는 키를 거절하는 것과 같은 부류로 막는다 (#386).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from yaml.constructor import ConstructorError

from aaestro.core.errors import printable_label

if TYPE_CHECKING:
    from pathlib import Path

MERGE_TAG = "tag:yaml.org,2002:merge"
"""``<<`` 의 태그 — SafeLoader 는 이 키를 값으로 만들지 않고 ``flatten_mapping`` 에서 푼다."""
VALUE_TAG = "tag:yaml.org,2002:value"
"""plain ``=`` 의 태그 — ``flatten_mapping`` 이 문자열 키로 바꾼다 (``"="`` 와 같은 키가 된다)."""

NOT_UTF8 = "not valid UTF-8"
TOO_DEEP = "nesting too deep"
UNCONSTRUCTIBLE = "YAML value could not be constructed"


class DuplicateKeyError(ConstructorError):
    """A mapping repeats a key.

    메시지는 키 이름과 줄 번호만 싣는다 — 값이나 주변 문서 조각은 싣지 않는다 (비밀 노출 방지).
    키 이름은 선언의 구조라 의도적으로 싣되, 길이와 문자를 정제한다 (1만 자 키, 제어문자 · ANSI
    이스케이프가 로그 · 터미널로 그대로 나가지 않게).
    """

    def __init__(self, key: object, line: int) -> None:
        super().__init__(problem=f"duplicate key '{printable_label(str(key))}' at line {line}")
        self.key = key
        self.line = line

    def __str__(self) -> str:
        return str(self.problem)


class YamlReadError(Exception):
    """A YAML file could not be read, decoded, or parsed.

    파일 읽기 → 디코딩 → 파싱의 실패를 **한 종류로** 올린다. 호출부마다 ``except`` 목록을 따로
    적으면 하나씩 빠진다 (``UnicodeError`` 가 네 입구 중 세 곳에서 빠져 있었다). ``reason`` 은
    위치와 종류만 담고 입력 조각은 담지 않으며, 원인 체인도 끊겨 있다.
    """

    def __init__(self, reason: str, *, missing: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.missing = missing
        """파일이 없다 — 호출부가 "없음" 과 "읽을 수 없음" 을 구분할 때 쓴다."""


def _reject_duplicate_keys(loader: yaml.SafeLoader, root: yaml.Node) -> None:
    """Walk the composed node tree once and reject mappings that repeat an explicit key.

    검사를 구성 (construct) 단계가 아니라 **노드 트리** 에서 한다. 구성 중에는
    ``flatten_mapping`` 이 공유된 앵커 노드의 ``value`` 를 제자리에서 고쳐 (merge 로 들어온
    쌍을 앞에 붙이고 ``<<`` 를 지운다), 그 노드를 나중에 보면 merge 유래 키와 명시 키가
    구분되지 않는다 — 유효한 문서를 거절하게 된다. 구성 전의 트리에는 적힌 그대로만 있다.

    * 명시 키끼리만 본다 — merge 키 (``<<``) 는 건너뛰고, merge 값으로 쓰인 인라인 매핑도
      값 노드로서 걷는다.
    * 같은 노드를 alias 로 여러 번 만나도 한 번만 본다 (재귀 앵커에서도 끝난다).
    * 키 동등성은 구성된 값의 동등성이다 — ``safe_load`` 가 한 키로 합치는 것 (``1`` 과
      ``1.0``, ``true`` 와 ``1``) 을 중복으로 본다.
    """
    seen_nodes: set[int] = set()
    pending: list[yaml.Node] = [root]
    while pending:
        node = pending.pop()
        if id(node) in seen_nodes:
            continue
        seen_nodes.add(id(node))
        if isinstance(node, yaml.SequenceNode):
            pending.extend(node.value)
            continue
        if not isinstance(node, yaml.MappingNode):
            continue
        keys: set[Any] = set()
        for key_node, value_node in node.value:
            pending.append(value_node)
            if key_node.tag == MERGE_TAG:
                continue
            if not isinstance(key_node, yaml.ScalarNode):
                # 스칼라가 아닌 키 (리스트 · 매핑) 는 해시할 수 없어 구성 단계가 거절한다
                pending.append(key_node)
                continue
            key = _comparable_key(loader, key_node)
            if key is _UNCOMPARABLE:
                continue
            if key in keys:
                raise DuplicateKeyError(key, key_node.start_mark.line + 1)
            keys.add(key)
    # 검사용으로 만든 키 객체 (와 컬렉션 태그가 남긴 미완성 생성기) 를 구성 단계로 넘기지 않는다
    loader.constructed_objects = {}
    loader.recursive_objects = {}
    loader.state_generators = []


_UNCOMPARABLE: Any = object()
"""만들 수 없거나 해시할 수 없는 키 — 중복 검사에서 빼고 구성 단계에 맡긴다."""


def _comparable_key(loader: yaml.SafeLoader, key_node: yaml.ScalarNode) -> Any:
    """스칼라 키가 구성되는 값 — 비교할 수 없으면 :data:`_UNCOMPARABLE`.

    키를 만들다 실패하거나 (``!!int x`` 는 ``ValueError``) 만든 값이 해시 불가면 (``!!set x`` 는
    ``set()``) 여기서는 **아무 말도 하지 않는다**. 같은 노드를 구성 단계가 다시 만들면서
    ``yaml.safe_load`` 와 똑같은 에러를 낸다 — 검사가 먼저 다른 종류의 예외 (``TypeError``) 로
    터지면 호출자의 ``except yaml.YAMLError`` 를 빠져나가 깨진 파일 하나가 목록 전체를 무너뜨린다.
    삼키는 것이 아니다: 건너뛴 키는 구성 단계에서 반드시 거절된다 (같은 입력, 같은 constructor).
    """
    if key_node.tag == VALUE_TAG:
        return key_node.value
    try:
        key = loader.construct_object(key_node)
        hash(key)
    except Exception:  # noqa: BLE001 — 제3자 constructor 가 내는 예외 종류는 닫혀 있지 않다
        return _UNCOMPARABLE
    return key


class _PositionLoader(yaml.SafeLoader):
    """``SafeLoader`` that remembers the line of the node whose construction failed.

    스칼라 constructor 는 ``int()`` · ``float()`` · ``datetime.date()`` 를 그대로 불러 YAML 에러가
    아닌 예외 (``ValueError`` · ``KeyError``) 를 올린다. 그 예외에는 위치가 없으므로 실패한 가장
    안쪽 노드의 줄을 적어 둔다 — 값은 적지 않는다.
    """

    failed_line: int | None = None

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep=deep)
        except Exception:
            if self.failed_line is None:
                self.failed_line = node.start_mark.line + 1
            raise


def _load(loader: yaml.SafeLoader) -> Any:
    """compose → 중복 키 검사 → **같은 트리** 를 구성. 한 번만 파싱한다."""
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _reject_duplicate_keys(loader, node)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def safe_load_unique(text: str) -> Any:
    """Parse YAML like ``yaml.safe_load`` but reject duplicate mapping keys.

    한 번만 파싱한다: compose 한 노드 트리를 검사한 뒤 **그 트리를 그대로** 표준
    ``SafeLoader`` 로 구성한다. 두 번 파싱하면 검사한 것과 읽은 것이 어긋날 여지가 생긴다.

    거절하는 것은 중복 키뿐이다. 그 밖에는 ``yaml.safe_load`` 와 **같은 결과, 같은 종류의 에러** 를
    낸다 — 구성 단계의 비-YAML 예외 (``!!int x`` 의 ``ValueError``) 도 그대로다. 파일을 읽는 쪽은
    :func:`load_yaml_file` 을 쓴다: 그 예외들을 값 없는 사유로 바꿔 준다.

    Raises:
        yaml.YAMLError: Unparseable input; :class:`DuplicateKeyError` for repeated keys.
        Exception: Whatever a scalar constructor raises, exactly as ``yaml.safe_load`` does.
    """
    return _load(yaml.SafeLoader(text))


def yaml_problem(err: BaseException) -> dict[str, str]:
    """Error details for a failed read — where and what kind, never a piece of the input.

    ``ScannerError`` 등의 ``str()`` 은 해당 줄을 스니펫으로 싣고 ``problem`` / ``context`` 문자열도
    입력 조각 (잘못된 문자, 태그) 을 담을 수 있다. 줄·열 번호와 오류 종류만 싣는다.
    중복 키만 키 이름을 싣는다 (키는 값이 아니라 선언의 일부다).
    """
    if isinstance(err, YamlReadError):
        return {"reason": err.reason}
    if isinstance(err, DuplicateKeyError):
        return {"reason": str(err)}
    if isinstance(err, yaml.MarkedYAMLError):
        mark = err.problem_mark or err.context_mark
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return {"reason": f"{type(err).__name__}{where}"}
    if isinstance(err, yaml.YAMLError):
        return {"reason": "YAML syntax error"}
    return {}


def _document_or_failure(path: Path) -> YamlReadError | Any:
    """파일을 읽어 문서를 돌려주거나, 실패를 :class:`YamlReadError` 로 **돌려준다** (던지지 않는다).

    던지는 것은 호출자가 ``except`` 블록 밖에서 한다 — 블록 안에서 던지면 입력 조각을 실은
    원인 예외 (``UnicodeDecodeError.object`` 는 파일 바이트 전체다) 가 ``__context__`` 에 남는다.
    """
    loader: _PositionLoader | None = None
    try:
        loader = _PositionLoader(path.read_bytes().decode("utf-8"))
        return _load(loader)
    except FileNotFoundError:
        return YamlReadError("file not found", missing=True)
    except OSError as err:
        # strerror 는 OS 의 고정 문구다 ("Permission denied") — 파일 내용이 아니다
        return YamlReadError(err.strerror or type(err).__name__)
    except UnicodeError:
        return YamlReadError(NOT_UTF8)
    except RecursionError:
        # 수천 겹으로 중첩된 문서 — 파서가 재귀로 내려가다 터진다. traceback 대신 사유로
        return YamlReadError(TOO_DEEP)
    except yaml.YAMLError as err:
        return YamlReadError(yaml_problem(err)["reason"])
    except Exception:  # noqa: BLE001
        # 07 의 "Exception 광역 캐치 금지" 의 의도된 예외다. PyYAML 의 constructor 가 올리는 예외는
        # 닫힌 집합이 아니고 (``ValueError`` · ``KeyError`` · ``AttributeError`` · ``TypeError`` …)
        # 그 메시지는 값을 싣는다 (``invalid literal for int(): '<값>'``). 종류를 하나씩 막으면 다음
        # constructor 에서 새므로 "구성 단계의 실패" 라는 부류로 받아 고정 문구와 줄 번호만 남긴다.
        # ``KeyboardInterrupt`` 등 ``BaseException`` 은 받지 않는다
        line = loader.failed_line if loader is not None else None
        return YamlReadError(UNCONSTRUCTIBLE + (f" at line {line}" if line else ""))


def load_yaml_file(path: Path) -> Any:
    """Read one YAML file with unique keys — the single way declarations and settings are read.

    선언 · 설정 파일을 읽는 **한 가지 길**: 읽기 → UTF-8 디코딩 → 중복 키를 거절하는 파싱.

    Raises:
        YamlReadError: Missing, unreadable, non-UTF-8, unparseable, or duplicate-key input.
            The reason carries no piece of the file and the cause chain is cut.
    """
    result = _document_or_failure(path)
    if isinstance(result, YamlReadError):
        raise result
    return result


__all__ = [
    "DuplicateKeyError",
    "YamlReadError",
    "load_yaml_file",
    "safe_load_unique",
    "yaml_problem",
]
