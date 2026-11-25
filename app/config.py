"""application.yml 대응 설정. 환경변수(.env)로 덮어쓴다."""
from functools import lru_cache

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

    # 에이전트(도구 호출)
    tavily_api_key: str = ""
    tavily_base_url: str = "https://api.tavily.com"
    agent_ollama_num_ctx: int = 16384
    agent_claude_timeout_seconds: int = 180
    agent_max_tool_calls: int = 6

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
