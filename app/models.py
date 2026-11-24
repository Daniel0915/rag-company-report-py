"""프론트엔드와 주고받는 DTO. 필드명은 Java record와 동일(camelCase)하게 유지한다."""
from pydantic import BaseModel, Field


class WatchedCompanyDto(BaseModel):
    name: str
    corpCode: str
    stockCode: str


class ChatTurnDto(BaseModel):
    """프론트엔드(IndexedDB)에 저장된 과거 대화 한 턴. role은 "user" 또는 "assistant"."""

    role: str
    content: str


class ChatRequest(BaseModel):
    """topK는 근거로 쓸 청크 수(기본 4). provider는 local(Ollama)/claude."""

    question: str
    corpCode: str
    history: list[ChatTurnDto] = Field(default_factory=list)
    topK: int = 0
    provider: str | None = None


class SourceItem(BaseModel):
    corpName: str | None = None
    reportNm: str | None = None
    rceptNo: str | None = None
    sectionTitle: str | None = None
    docType: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[SourceItem]


class IndexResult(BaseModel):
    corpName: str
    rceptNo: str
    reportNm: str
    status: str
    chunks: int


class IndexedChunkDto(BaseModel):
    id: str | None = None
    corpCode: str | None = None
    corpName: str | None = None
    reportNm: str | None = None
    rceptNo: str | None = None
    pblntfTy: str | None = None
    docType: str | None = None
    sectionTitle: str | None = None
    rceptDt: str | None = None
    textPreview: str | None = None


class IndexedChunkPage(BaseModel):
    items: list[IndexedChunkDto]
    total: int


class AdminDocumentDto(BaseModel):
    id: str
    corpCode: str
    corpName: str
    title: str
    category: str | None = None
    description: str | None = None
    docDate: str | None = None
    fileName: str
    uploadedAt: str
    chunkCount: int


class FilerDisclosureDto(BaseModel):
    filerName: str
    reportNm: str = ""
    rceptDt: str = ""
    rceptNo: str = ""


class RelatedCompanyDto(BaseModel):
    corpCode: str = ""
    corpName: str = ""


class GraphExpansionRow(BaseModel):
    """VectorCypher 확장 쿼리 1행. kind는 "DISCLOSURE" 또는 "NEWS"."""

    kind: str
    anchor: str
    primaryName: str
    secondaryName: str
    relatedCorpCode: str | None = None
    relatedCorpName: str | None = None
