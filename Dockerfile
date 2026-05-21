# 멀티에이전트 오케스트레이터 — 웹 UI 컨테이너.
# mock 백엔드는 키 없이 즉시 동작. 실 백엔드(claude-cli/codex/openai-agents/claude-sdk)는
# 각 CLI 설치/마운트와 API 키·로그인이 필요하다 (아래 README 배포 섹션 참고).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 프레임워크 구성요소 (config.FRAMEWORK_ROOT = /app)
COPY pyproject.toml README.md ./
COPY orchestrator ./orchestrator
COPY .claude ./.claude
COPY templates ./templates
COPY examples ./examples

RUN pip install -e ".[all]" || pip install -e .

EXPOSE 8765
VOLUME ["/data"]

# 산출물은 /data/runs 에 생성. 컨테이너 안이라 0.0.0.0 바인딩.
CMD ["python", "-m", "orchestrator", "--web", "--host", "0.0.0.0", "--port", "8765", \
     "--base-dir", "/data/runs"]
