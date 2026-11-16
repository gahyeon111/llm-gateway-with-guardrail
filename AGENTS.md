# AGENTS.md

## 프로젝트
AIM Guardrail Gateway — Codex 등 LLM 클라이언트 요청의 입력/출력을 사내 가드레일 AIM으로 검사한 뒤
Bedrock GPT-5.6(제논 외부 모델 경유)으로 전달하는 FastAPI 게이트웨이.
설계 전체는 docs/DESIGN.md를 먼저 읽을 것.

## 개발 환경
- Python 3.12+, FastAPI, httpx, pydantic-settings
- 설치: `pip install -e ".[dev]"`
- 실행: `uvicorn app.main:app --reload --port 8080`
- 테스트: `pytest -q`
- 린트/포맷: `ruff check . && ruff format .`
- 로컬 개발 시 tests/fake_aim.py, tests/fake_upstream.py 사용 (README 참고)

## 반드시 지킬 규칙
1. **AIM 규격은 추정값이다.** AIM 관련 HTTP 세부사항(경로, 헤더, 요청/응답 바디)은
   `app/guard/aim_client.py` 한 파일에만 둔다. 사내 AIM 가이드가 확보되면 이 파일만 가이드에 맞춰 수정한다.
   다른 모듈은 `AimClient.check()`와 `AimResult`/`Verdict`(PASS/MASK/BLOCK) 인터페이스에만 의존한다.
2. **원문 패스스루.** 요청/응답은 같은 포맷 그대로 전달한다. 바꿔도 되는 것: `model` 필드, 업스트림 인증 헤더,
   MASK된 텍스트, SSE `sequence_number` 재부여. 그 외 필드를 임의로 추가/삭제/정규화하지 않는다.
3. **스트림 전체 버퍼링 금지.** 텍스트는 윈도우 hold-back, 툴 인자만 완성까지 버퍼링(DESIGN 6장).
   `response.completed`는 반드시 클라이언트에 도달해야 한다.
4. **알 수 없는 SSE 이벤트는 그대로 전달**한다 (forward-compatible).
5. **비밀정보는 환경변수로만.** 키/URL 하드코딩 금지. `.env`는 커밋 금지, `.env.example`만 관리.
6. **본문 로깅 금지.** 요청/응답 본문, 키, AIM 원문 결과를 로그/에러 메시지에 남기지 않는다.
   메타데이터(모델, 판정, 지연, status)만 기록.
7. **에러는 OpenAI 포맷 + 구분된 code**(DESIGN 9장). 일괄 500 금지.
   가드레일 차단은 4xx(Codex 재시도 방지), AIM/업스트림 장애는 5xx.
8. 새 기능은 fake AIM/fake upstream 기반 테스트를 같이 추가한다.
   스트리밍 로직(`text_buffer.py`, `stream_*.py`)은 윈도우 경계, MASK, BLOCK 케이스 테스트 필수.

## 작업 순서
docs/DESIGN.md 13장 마일스톤(M0 → M7) 순서로 진행한다.
M1(가드레일 없는 패스스루)에서 Codex 툴 루프가 동작하는 걸 먼저 확인한 뒤 가드레일을 붙인다.
(`GUARD_INPUT_ENABLED=false GUARD_OUTPUT_ENABLED=false`로 M1 모드 재현 가능)

## 커밋
- 마일스톤 단위로 브랜치, 작은 단위 커밋
- 메시지: `feat(responses): ...`, `fix(guard): ...`, `test(stream): ...` 형식
