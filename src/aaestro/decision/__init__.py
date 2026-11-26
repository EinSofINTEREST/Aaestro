"""Decision model implementations (bands, providers).

결정 provider 호출은 이그레스 프록시가 종단한다 (#595). 아래 이름은 프록시 · runtime · agentd 가
공유한다 — runtime 은 egress · agentd 를 import 하지 않으므로 여기 둔다.
"""

from typing import Final

DECISION_PROXY_PREFIX: Final = "decision"
"""프록시 provider 창구의 결정 provider 경로 — ``/decision/{provider}/v1/...``."""
DECISION_URL_ENV: Final = "AAESTRO_DECISION_URL"
"""agentd 가 결정 provider 를 부르는 base URL — 프록시 창구의 ``/decision/{provider}``."""
DECISION_KIND_ENV: Final = "AAESTRO_DECISION_KIND"
"""설정 ``decision.providers`` 의 어댑터 ``kind`` — agentd 는 설정 파일을 읽지 않는다."""
DECISION_TIMEOUT_ENV: Final = "AAESTRO_DECISION_TIMEOUT_S"
"""설정 ``decision.timeout_s`` — 재시도 대기를 포함한 결정 호출 전체 상한."""
