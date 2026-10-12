# aaestro/agent-echo — 테스트 전용 에이전트 이미지
#
# 모델을 호출하지 않고 입력을 echo 한다. 프로토콜/lifecycle 검증에서
# 모델 비결정성과 API 비용을 배제하기 위한 대역이다 (06 Integration Testing).
ARG BASE_TAG=0.1.0
FROM aaestro/agent-base:${BASE_TAG}

# 이 이미지는 echo 대역을 쓴다 — base 는 manifest 의 실행기를 그대로 따른다
ENV AAESTRO_EXECUTOR=echo

COPY --chown=1000:1000 agents/echo/manifest.yaml /app/manifest.yaml
