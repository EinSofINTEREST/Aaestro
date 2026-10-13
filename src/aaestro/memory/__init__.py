"""Memory Service — context memory spaces and access control.

컨텍스트 메모리 space 와 접근 제어. 저장소 자격증명은 서비스만 보유한다.

여기서 re-export 하는 것은 **에이전트 쪽(클라이언트) 심볼뿐**이다 — agentd 가 이 패키지를
import 해도 store · service · index 같은 서버 코드가 올라오지 않아야 한다 (09 Access Enforcement 1).
서버 심볼은 하위 모듈에서 직접 import 한다 (``from aaestro.memory.service import MemoryService``).
"""

from aaestro.memory.client import (
    DEFAULT_TIMEOUT_S,
    MEMORY_TOKEN_ENV,
    MEMORY_URL_ENV,
    HttpMemoryAccess,
)
from aaestro.memory.entry import MAX_CONTENT_BYTES, MemoryEntry, MemorySource

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_CONTENT_BYTES",
    "MEMORY_TOKEN_ENV",
    "MEMORY_URL_ENV",
    "HttpMemoryAccess",
    "MemoryEntry",
    "MemorySource",
]
