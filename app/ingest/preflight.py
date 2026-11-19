"""수집 실행 전 점검: Neo4j, Ollama 임베딩, DART 키를 확인한다."""

from __future__ import annotations

from datetime import date, timedelta

from app.config import Settings
from app.dart.client import DartApiClient
from app.llm.ollama import OllamaClient

SAMSUNG_CORP_CODE = "00126380"


def run_preflight(driver, ollama: OllamaClient, dart: DartApiClient, settings: Settings) -> list[str]:
    """문제 설명 목록을 돌려준다. 빈 목록이면 통과."""
    problems: list[str] = []

    try:
        driver.verify_connectivity()
    except Exception as e:
        problems.append(f"Neo4j 연결 실패: {e}")

    try:
        dimension = len(ollama.embed_one("ping"))
        if dimension != settings.embedding_dimension:
            problems.append(
                f"Ollama 임베딩 차원 불일치: {dimension} (기대 {settings.embedding_dimension})"
            )
    except Exception as e:
        problems.append(f"Ollama 임베딩 실패: {e}")

    if not settings.dart_api_key.strip():
        problems.append("DART API 키가 비어 있습니다")
    else:
        try:
            today = date.today()
            # 000/013 외 상태는 search_disclosures가 DartApiError로 던진다.
            dart.search_disclosures(SAMSUNG_CORP_CODE, today - timedelta(days=7), today)
        except Exception as e:
            problems.append(f"DART 조회 실패: {e}")

    return problems
