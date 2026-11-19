"""FastAPI 진입점. 정적 프론트엔드(채팅/관리자 페이지)와 REST API를 함께 서빙한다."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.dependencies import Container
from app.routers import admin_documents, chunks, company_report, graph, news

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s : %(message)s"
)
# httpx는 요청 URL(DART API 키 포함)을 INFO로 남기므로 콘솔에 찍히지 않게 한다.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.container = Container.build()
    try:
        yield
    finally:
        app.state.container.close()


app = FastAPI(title="기업 리포트 챗봇(RAG)", version="0.1.0", lifespan=lifespan)

app.include_router(company_report.router)
app.include_router(news.router)
app.include_router(graph.router)
app.include_router(chunks.router)
app.include_router(admin_documents.router)

# 라우터 뒤에 마운트해야 "/"가 API 경로를 가리지 않는다.
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def run() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=get_settings().port, reload=False)


if __name__ == "__main__":
    run()
