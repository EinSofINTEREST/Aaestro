.PHONY: help install lint fmt typecheck test test-integration test-e2e check build build-base up down \
	e2e-up e2e-down clean

UV ?= uv
RUN := $(UV) run

help:
	@grep -E "^[a-zA-Z_-]+:.*?## .*$$" $(MAKEFILE_LIST) | \
		awk "BEGIN {FS = \":.*?## \"}; {printf \"  \\033[36m%-18s\\033[0m %s\\n\", \$$1, \$$2}"

install: ## 고정된 의존성 설치 (uv.lock 기준)
	$(UV) sync --frozen

lint: ## ruff check + format 검사
	$(RUN) ruff check src tests scripts
	$(RUN) ruff format --check src tests scripts

fmt: ## ruff format 적용 + import 정렬
	$(RUN) ruff format src tests scripts
	$(RUN) ruff check --fix src tests scripts

# CI 스크립트는 패키지가 아니라 경로로 본다 — pyproject 의 packages 설정과 따로 돈다
typecheck: ## mypy (aaestro.core 는 strict) + CI 스크립트 (strict)
	$(RUN) mypy
	$(RUN) mypy --strict --explicit-package-bases scripts/ci

test: ## unit 테스트 + 커버리지 게이트 (>= 70%)
	$(RUN) pytest tests/unit

# 레이어 구현 전에는 수집 대상이 없다 — pytest 의 "no tests collected"(5) 만 통과시키고
# 실제 실패(1)는 그대로 게이트를 막는다
test-integration: ## Docker 기반 통합 테스트
	@$(RUN) pytest tests/integration -m integration --no-cov; \
		status=$$?; [ $$status -eq 0 ] || [ $$status -eq 5 ]

test-e2e: ## 전체 스택 E2E 테스트 (nightly)
	@$(RUN) pytest tests/e2e -m e2e --no-cov; \
		status=$$?; [ $$status -eq 0 ] || [ $$status -eq 5 ]

e2e-up: build-base ## E2E 스택 기동 (fake provider + 에이전트)
	IMAGE_TAG=$(IMAGE_TAG) docker compose -f $(DOCKER_DIR)/compose.e2e.yaml up -d --build

e2e-down: ## E2E 스택 정지
	IMAGE_TAG=$(IMAGE_TAG) docker compose -f $(DOCKER_DIR)/compose.e2e.yaml down -v

check: lint typecheck test ## 머지 전 로컬 전체 검증

DOCKER_DIR := deployments/docker
# latest 태그 배포 금지 — 이미지 태그는 semver (02 Base Image 2)
IMAGE_TAG ?= 0.1.0

# compose 파일들이 `FROM` 으로 확장하는 바탕 — compose 는 이것을 빌드하지 않는다.
# 스택을 올리는 타깃은 전부 이것을 선행해야 한다 (#222)
build-base: ## agent-base 이미지 빌드
	docker build -t aaestro/agent-base:$(IMAGE_TAG) -f $(DOCKER_DIR)/agent-base.Dockerfile .

build: build-base ## 에이전트 base image + 테스트 echo image 빌드
	docker build -t aaestro/agent-echo:$(IMAGE_TAG) \
		--build-arg BASE_TAG=$(IMAGE_TAG) \
		-f $(DOCKER_DIR)/agent-echo.Dockerfile .

up: build-base ## 개발 스택 기동
	IMAGE_TAG=$(IMAGE_TAG) docker compose -f $(DOCKER_DIR)/compose.yaml up -d --build

down: ## 개발 스택 정지
	IMAGE_TAG=$(IMAGE_TAG) docker compose -f $(DOCKER_DIR)/compose.yaml down -v

clean: ## 빌드/캐시 산출물 정리
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
