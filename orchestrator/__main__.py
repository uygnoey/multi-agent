"""CLI 진입점: python -m orchestrator ..."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .backends import all_backends
from .config import DEFAULT_BACKEND, ROLES, VALID_BACKENDS, RunConfig
from .scheduler import Scheduler


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="orchestrator",
        description="멀티에이전트 · 멀티백엔드 웹서비스 빌드 오케스트레이터",
    )
    p.add_argument("--spec", type=Path, help="기획서 마크다운 경로")
    p.add_argument("--project-dir", type=Path, help="산출물을 생성할 타깃 디렉터리")
    p.add_argument(
        "--backend", default=DEFAULT_BACKEND, choices=VALID_BACKENDS, help="전역 기본 백엔드"
    )
    p.add_argument(
        "--backends",
        metavar="B1,B2,...",
        help="우선순위 풀(앞에서부터 가용 사용·실패 시 폴오버). --backend 보다 우선",
    )
    p.add_argument(
        "--distribute",
        action="store_true",
        help="역할들을 --backends 풀에 라운드로빈 분산 (4종 동시 가동)",
    )
    p.add_argument(
        "--role-backend",
        action="append",
        default=[],
        metavar="ROLE=B1[,B2,...]",
        help="역할별 백엔드(우선순위 리스트) override. 반복 가능",
    )
    p.add_argument("--max-units", type=int, help="처리할 unit 수 상한")
    p.add_argument("--concurrency", type=int, default=3, help="동시 처리 unit 수")
    p.add_argument("--budget", type=float, help="세션당 예산(USD) 상한 (지원 백엔드)")
    p.add_argument("--model", help="모델 override (미지정 시 백엔드 기본값)")
    p.add_argument("--poll-interval", type=float, default=20.0, help="PM/PL 감독 주기(초)")
    p.add_argument(
        "--delegate",
        action="store_true",
        help="역할 세션이 팀원을 네이티브 서브에이전트로 위임 호출 (Claude 백엔드)",
    )
    p.add_argument("--max-attempts", type=int, default=2, help="unit별 dev→test→qa 재작업 횟수")
    p.add_argument("--retries", type=int, default=1, help="역할 호출 전이성 실패 재시도 횟수")
    p.add_argument("--mock", action="store_true", help="무비용 mock 백엔드로 전체 실행")
    p.add_argument("--check", action="store_true", help="백엔드 가용성 진단 후 종료")
    p.add_argument(
        "--watch",
        action="store_true",
        help="실행 대신 --project-dir 의 진행을 실시간 모니터 TUI 로 본다",
    )
    p.add_argument(
        "--web",
        action="store_true",
        help="웹 UI 서버 실행 (브라우저에서 기획서 업로드·실행·모니터링)",
    )
    p.add_argument("--port", type=int, default=8765, help="--web 포트 (기본 8765)")
    p.add_argument("--host", default="127.0.0.1", help="--web 바인드 호스트")
    return p.parse_args(argv)


def cmd_check() -> int:
    print("backend availability:")
    for name, b in all_backends().items():
        ok, reason = b.available()
        print(f"  {'✅' if ok else '❌'} {name:<14} {reason}")
    return 0


def _parse_backend_list(value: str) -> list[str]:
    names = [b.strip() for b in value.split(",") if b.strip()]
    for b in names:
        if b not in VALID_BACKENDS:
            raise SystemExit(f"알 수 없는 백엔드: {b} (가능: {', '.join(VALID_BACKENDS)})")
    if not names:
        raise SystemExit(f"빈 백엔드 목록: {value!r}")
    return names


def build_config(a: argparse.Namespace) -> RunConfig:
    backend_priority = _parse_backend_list(a.backends) if a.backends else []
    role_priority: dict[str, list[str]] = {}
    for item in a.role_backend:
        if "=" not in item:
            raise SystemExit(f"--role-backend 형식 오류: {item} (ROLE=B1[,B2,...])")
        role, backends = item.split("=", 1)
        if role not in ROLES:
            raise SystemExit(f"알 수 없는 역할: {role} (가능: {', '.join(ROLES)})")
        role_priority[role] = _parse_backend_list(backends)
    return RunConfig(
        spec_path=a.spec.resolve(),
        project_dir=a.project_dir.resolve(),
        default_backend=a.backend,
        backend_priority=backend_priority,
        role_priority=role_priority,
        distribute=a.distribute,
        max_units=a.max_units,
        concurrency=a.concurrency,
        budget=a.budget,
        model=a.model,
        poll_interval=a.poll_interval,
        mock=a.mock,
        delegate=a.delegate,
        max_attempts=a.max_attempts,
        retries=a.retries,
    )


def _print_summary(snap: dict, cfg: RunConfig) -> None:
    units = snap.get("units", [])
    print("\n=== RUN SUMMARY ===")
    print(f"project-dir : {cfg.project_dir}")
    print(f"phase       : {snap.get('phase')}")
    for u in units:
        print(f"  {u['id']:<6} {u['status']:<11} test={str(u.get('test_status')):<5} {u['title']}")
    done = sum(1 for u in units if u["status"] == "done")
    print(f"units       : {done}/{len(units)} done")
    print(f"cost        : ${snap.get('total_cost_usd', 0.0):.4f}")
    print(f"board       : {cfg.project_dir / '.orchestrator' / 'board.json'}")
    print(f"report      : {cfg.project_dir / '.orchestrator' / 'report.md'}")


def main(argv=None) -> int:
    a = parse_args(argv)
    if a.check:
        return cmd_check()
    if a.web:
        from .webui import serve

        serve(a.port, a.project_dir, a.host)
        return 0
    if a.watch:
        if not a.project_dir:
            raise SystemExit("--watch 에는 --project-dir 가 필요합니다.")
        from .monitor import run_tui

        run_tui(a.project_dir.resolve())
        return 0
    if not a.spec or not a.project_dir:
        raise SystemExit("--spec 와 --project-dir 는 필수입니다 (또는 --check 만 사용).")
    if not a.spec.exists():
        raise SystemExit(f"spec 파일을 찾을 수 없음: {a.spec}")

    cfg = build_config(a)
    snap = asyncio.run(Scheduler(cfg).run())
    _print_summary(snap, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
