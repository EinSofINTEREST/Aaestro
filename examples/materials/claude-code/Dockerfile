# claude-code — Claude Code CLI 를 실행기로 쓰는 에이전트의 빌드 재료 (#266)
#
# 이 디렉토리는 **시드**다. 프레임워크는 여기를 읽지 않는다 — 스토어에 올려야 굽힌다:
#
#   aaestro agent-push claude-code examples/materials/claude-code
#   aaestro agent-build claude-code --wait
#
# 컨텍스트 루트는 빌더가 조립하는 임시 디렉토리다 (#263 레이아웃):
#   Dockerfile, manifest.yaml(카탈로그의 선언), modules/(모듈 루트), src/(이 재료)
FROM aaestro/agent-base:0.1.0

# base 는 non-root 로 끝난다 — 설치 동안만 root 로 올라갔다 되돌린다
USER root

# --no-install-recommends 로 이미지 표면을 줄인다. apt 캐시는 같은 레이어에서
# 지워야 이미지에 남지 않는다
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && apt-get purge -y npm \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# 커스텀 실행기 — PYTHONPATH 에 올려야 spec.entrypoint 가 해석된다
COPY --chown=1000:1000 src/ /app/src/
COPY --chown=1000:1000 manifest.yaml /app/manifest.yaml
COPY --chown=1000:1000 modules/ /app/modules/

ENV PYTHONPATH=/app/src \
    AAESTRO_ROOT=/app

# read-only rootfs 전제 — Claude Code 가 쓰는 홈 경로도 tmpfs 로 받는다
ENV HOME=/tmp

USER 1000:1000
