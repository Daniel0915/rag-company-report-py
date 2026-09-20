from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.dependencies import Container, get_container
from app.models import AdminDocumentDto

router = APIRouter(prefix="/api/admin/documents")


@router.get("", response_model=list[AdminDocumentDto])
def list_documents(container: Container = Depends(get_container)) -> list[AdminDocumentDto]:
    return container.admin_service.list_documents()


@router.post("", response_model=AdminDocumentDto)
def upload(
    file: UploadFile = File(...),
    corpCode: str = Form(...),
    title: str = Form(...),
    category: str | None = Form(default=None),
    description: str | None = Form(default=None),
    docDate: str | None = Form(default=None),
    container: Container = Depends(get_container),
) -> AdminDocumentDto:
    content = file.file.read()
    if len(content) > container.settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="파일이 너무 큽니다 (최대 30MB)")
    try:
        return container.admin_service.upload_document(
            content, file.filename, corpCode, title, category, description, docDate
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/{doc_id}")
def delete(doc_id: str, container: Container = Depends(get_container)) -> None:
    try:
        container.admin_service.delete_document(doc_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
