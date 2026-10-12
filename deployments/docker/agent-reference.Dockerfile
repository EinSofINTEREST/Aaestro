# aaestro/agent-reference — 레퍼런스 그래프의 노드 에이전트 이미지
#
# echo 대역과 달리 **표준 실행기**로 돈다: manifest 의 promptset 을 렌더하고
# 모델을 호출하며 출력을 정형한다. 모델은 fake-provider 가 받는다 —
# 실 LLM 없이 실제 경로를 태우기 위해서다 (06 / #153).
#
# 에이전트별 manifest 는 build arg 로 고른다 — 이미지를 셋 만들지 않는다.
ARG BASE_TAG=0.1.0
FROM aaestro/agent-base:${BASE_TAG}

ARG AGENT=planner

COPY --chown=1000:1000 agents/${AGENT}/manifest.yaml /app/manifest.yaml
# promptset 은 registry 로 해석된다 — 루트를 /app 으로 두고 modules 를 담는다
COPY --chown=1000:1000 modules/ /app/modules/

ENV AAESTRO_ROOT=/app
