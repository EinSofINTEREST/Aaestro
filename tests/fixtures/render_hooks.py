"""Child hooks for render sandbox tests (#715).

렌더 자식 프로세스 진입 직후에 도는 대역. 자식은 hook 의 모듈을 import 하므로 표준 라이브러리만
쓴다 — 테스트 모듈 (``aaestro.agentd`` · fastapi 를 끌어온다) 에 두면 자식 기동이 수백 ms 늘어
타이밍을 재는 테스트가 hook 이 돌기 전에 마감에 닿는다.
"""

from __future__ import annotations

import json
import os
import struct
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

LINGER_S = 3.0
"""응답 뒤에도 자식이 살아 있는 시간 — 부모의 유예 (0.5초) 보다 넉넉히 길다."""


def reply_then_linger(conn: Connection) -> None:
    """응답을 보낸 뒤 끝나지 않는다 — 부모는 유예 동안 기다리고 그 뒤 죽인다."""
    conn.send_bytes(json.dumps({"text": "lingered", "logs": []}).encode())
    time.sleep(LINGER_S)
    os._exit(0)


def header_only(conn: Connection) -> None:
    """프레임 머리 (길이) 만 쓰고 본문 전에 멈춘다."""
    os.write(conn.fileno(), struct.pack("!i", 100))
    time.sleep(LINGER_S)
    os._exit(0)
