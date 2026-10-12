# aaestro/agent-base — 모든 에이전트 이미지의 출발점 (02 Container Standards)
#
# 에이전트별 이미지는 이 base 를 FROM 으로 확장하고 manifest 와 추가 의존성만
# 얹는다. agentd 는 여기에 이미 들어있다.
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# 의존성 레이어를 소스와 분리해 소스 변경 시 재설치를 피한다
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --prefix=/install .

FROM python:3.12-slim

# uid/gid 를 고정한다 — 볼륨 소유권이 호스트와 예측 가능하게 맞물려야 한다
RUN groupadd --gid 1000 agent \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin agent

COPY --from=builder /install /usr/local

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AAESTRO_MANIFEST=/app/manifest.yaml

WORKDIR /app

# read-only rootfs 전제 — 쓰기가 필요한 경로는 여기 둘 뿐이고, 런타임이
# tmpfs 로 마운트한다 (02 Security 3)
VOLUME ["/tmp", "/workspace"]

USER 1000:1000

EXPOSE 8080

# Docker healthcheck 는 /v1/health 를 직접 호출한다 — 무인증인 이유 (02 API Rules 4).
# 이미지에 curl 을 넣지 않기 위해 표준 라이브러리로 확인한다
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/v1/health', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["python", "-m", "aaestro.agentd"]
