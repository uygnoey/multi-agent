# 멀티에이전트 오케스트레이터 — 웹 UI 컨테이너.
# mock 백엔드는 키 없이 즉시 동작. 실 백엔드(claude-cli/codex/openai-agents/claude-sdk)는
# 각 CLI 설치/마운트와 API 키·로그인이 필요하다 (아래 README 배포 섹션 참고).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 프레임워크 구성요소 (config.FRAMEWORK_ROOT = /app)
COPY pyproject.toml README.md README.en.md ./
COPY orchestrator ./orchestrator
COPY .claude ./.claude
COPY templates ./templates
COPY examples ./examples
# docs/ is intentionally not copied (excluded by .dockerignore); the runtime does not need it.

# 코어를 먼저 설치(반드시 성공) → 선택 백엔드 SDK([all])는 기본적으로 비치명적으로 시도한다.
#
# REQUIRE_ALL_BACKENDS 빌드 ARG (#52 / #10):
#   0 (기본) — soft 모드. [all] 설치가 실패해도 빌드는 계속되지만(이미지에 mock/CLI 백엔드는
#               항상 동작), 빌드 로그에 매우 눈에 띄는 경고 배너를 남겨 "실 SDK 백엔드 누락"을
#               가린 채 성공하지 않게 한다.
#   1         — hard 모드. `pip install -e ".[all]"` 가 실패하면 `||` 폴백 없이 빌드를 즉시
#               실패시킨다. claude-sdk/openai-agents 백엔드가 반드시 필요한 운영 이미지용.
#   사용 예:   docker build --build-arg REQUIRE_ALL_BACKENDS=1 -t dev-crew .
ARG REQUIRE_ALL_BACKENDS=0
RUN pip install -e . \
    && if [ "$REQUIRE_ALL_BACKENDS" = "1" ]; then \
            echo ">>> REQUIRE_ALL_BACKENDS=1: optional SDK backends are MANDATORY; build fails if [all] does not install."; \
            pip install -e ".[all]"; \
        else \
            pip install -e ".[all]" \
            || { \
                echo "============================================================================"; \
                echo "!! WARNING (#52): optional backend SDKs ([all]: claude-agent-sdk /"; \
                echo "!! openai-agents) FAILED to install. This image has mock + CLI backends"; \
                echo "!! ONLY; the claude-sdk and openai-agents backends will be UNAVAILABLE at"; \
                echo "!! runtime. The build was NOT failed (soft mode). To make this a hard build"; \
                echo "!! failure, rebuild with: --build-arg REQUIRE_ALL_BACKENDS=1"; \
                echo "============================================================================"; \
            }; \
        fi

EXPOSE 8765
VOLUME ["/data"]

# 산출물은 /data/runs 에 생성. 컨테이너 안이라 0.0.0.0 바인딩(컨테이너는 외부에서 접근하려면 필요).
# 🔒 인증(#8/#17): 이 웹 UI 는 run 을 제어하므로, 0.0.0.0 처럼 비-루프백에 바인딩하면 인증을
#   요구한다. WEB_UI_TOKEN 이 설정되지 않으면 서버는 fail-closed 로 기동을 거부한다(무인증
#   노출 사고 방지). 따라서 컨테이너 실행 시 반드시 강한 난수 토큰을 주입하라:
#     docker run -e WEB_UI_TOKEN=<강한 난수> -p 8765:8765 ...
#   접속은 http://<host>:8765/?token=<토큰> (토큰은 HttpOnly 쿠키로 저장된다). 추가 권장:
#   리버스 프록시 TLS · 방화벽 · 루프백 한정 publish(-p 127.0.0.1:8765:8765). 또한 상태변경
#   POST 는 same-origin(Origin 검사)만 허용된다. 신뢰되지 않은 네트워크에 무방비 노출 말 것.
CMD ["python", "-m", "orchestrator", "--web", "--host", "0.0.0.0", "--port", "8765", \
     "--base-dir", "/data/runs"]
