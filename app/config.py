"""application.yml 대응 설정. 환경변수(.env)로 덮어쓴다."""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 서버
    port: int = 8080

    # Neo4j
    neo4j_uri: str = "neo4j://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "companyreport"

    # Ollama (로컬 채팅 + 임베딩)
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_chat_model: str = "qwen2.5:3b"
    ollama_embedding_model: str = "bge-m3"
    # bge-m3 임베딩 차원. Neo4j 벡터 인덱스 생성 시 필요하다.
    embedding_dimension: int = 1024

    # DART
    dart_base_url: str = "https://opendart.fss.or.kr/api"
    dart_api_key: str = ""

    # Claude CLI (구독 로그인 기반, API 키 사용 안 함)
    claude_model: str = "sonnet"
    claude_timeout_seconds: int = 60

    # DeepSeek 공식 API(OpenAI 호환, 선불 크레딧). base_url·모델을 바꾸면 OpenRouter 같은 호환 API도 쓸 수 있다
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout_seconds: int = 120
    # 출력 상한(추론 포함). OpenRouter는 이 값이 없으면 모델 최대치만큼 잔액을 미리 요구한다
    deepseek_max_tokens: int = 8192

    # 에이전트(도구 호출)
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    agent_ollama_num_ctx: int = 16384
    agent_claude_timeout_seconds: int = 180
    agent_max_tool_calls: int = 6
    # 외부 MCP 서버(opendart-mcp, pykrx-mcp) 도구. 2026-11-27 켜기 판단 평가를 통과해 기본으로 켠다.
    agent_external_mcp: bool = True
    agent_external_mcp_timeout_seconds: int = 30
    krx_id: str = Field(default="", repr=False)
    krx_pw: str = Field(default="", repr=False)

    # 사이드카 파일 / 업로드 경로
    company_report_index_file: str = "data/company-report-index.json"
    admin_document_registry_file: str = "data/admin-documents.json"
    admin_document_upload_dir: str = "data/admin-uploads"
    news_index_file: str = "data/news-index.json"
    news_articles_per_company: int = 20

    # 공시 수집 배치
    ingest_state_file: str = "data/ingest-state.json"
    ingest_lock_file: str = "data/ingest.lock"
    ingest_log_dir: str = "logs/ingest"

    max_upload_bytes: int = 30 * 1024 * 1024

    @property
    def krx_auth_configured(self) -> bool:
        return bool(self.krx_id and self.krx_pw)


@lru_cache
def get_settings() -> Settings:
    return Settings()
