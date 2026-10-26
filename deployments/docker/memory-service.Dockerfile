# aaestro/memory-service — Memory Service 프로세스 (09 Access Enforcement 1)
#
# **저장소 자격증명은 이 컨테이너만 갖는다.** 에이전트는 불투명 토큰으로 HTTP
# 를 통해서만 닿으므로, 이 이미지가 없으면 09 가 규정한 배치를 세울 수 없다.
#
# base 를 재사용한다 — 프레임워크 코드가 이미 들어 있고, 갈라두면 memory 와
# agentd 가 서로 다른 버전을 실행하게 된다
ARG BASE_TAG=0.1.0
FROM aaestro/agent-base:${BASE_TAG}

USER root

# 토큰을 쓸 자리를 **이미지 안에** uid 1000 소유로 만들어 둔다: named volume 은
# 마운트 지점의 소유권을 물려받으므로, 없으면 root 소유로 생겨 쓰지 못한다
RUN install -d -o 1000 -g 1000 /tokens

# 선언을 읽어 space 와 토큰을 조립한다 — 읽기만 하므로 read-only 로 마운트한다
ENV AAESTRO_REPO_ROOT=/repo \
    AAESTRO_CONFIG_DIR=/repo/configs

USER 1000:1000

EXPOSE 8090

# 무인증 /v1/health 로 liveness 를 본다 — 이미지에 curl 을
# 넣지 않기 위해 표준 라이브러리로 확인한다
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/v1/health', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["python", "-m", "aaestro.memory"]
