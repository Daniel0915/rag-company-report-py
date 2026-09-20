# 프로젝트 작업 지침

## 과금/인증
- 이 프로젝트에서는 Anthropic API 키(종량제 API 과금)를 사용하지 않는다. 반드시 claude.ai 계정 로그인(구독 플랜) 기반으로만 작업할 것.
- `ANTHROPIC_API_KEY` 등 API 키 환경변수를 설정하거나 요구하는 기능은 사용 전 사용자에게 먼저 확인받는다.
- 답변 provider `claude`는 로컬에 로그인된 `claude` CLI를 그대로 호출한다(`app/llm/claude_cli.py`).
