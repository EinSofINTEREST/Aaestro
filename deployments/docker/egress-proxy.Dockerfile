# aaestro/egress-proxy — 에이전트 네트워크 밖으로 나가는 유일한 길 (03 Egress, #293)
#
# **모델 API 키를 비롯한 외부 자격증명은 이 컨테이너만 갖는다.** 에이전트는 신원만 내밀고,
# 프록시가 요청마다 레지스트리 판정을 받아 목적지로 보내거나 막는다. 도구를 실행하지 않는다.
#
# base 를 재사용한다 — 프레임워크 코드가 이미 들어 있고, 갈라두면 agentd 와 프록시가
# 서로 다른 판정 클라이언트를 실행하게 된다
ARG BASE_TAG=0.1.0
FROM aaestro/agent-base:${BASE_TAG}

USER 1000:1000

# 8080: CONNECT 터널 / 8081: provider 종단
EXPOSE 8080 8081

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/openapi.json', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["python", "-m", "aaestro.egress"]
