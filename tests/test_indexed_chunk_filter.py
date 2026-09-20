from app.services.indexed_chunk import _type_condition
from app.vector.registry import label_for


def test_type_conditions():
    assert "metadata.pblntf_ty` = 'A'" in _type_condition("A")
    # 레거시(태깅 전) 정기공시 청크도 A 필터에 포함돼야 한다.
    assert "IS NULL" in _type_condition("A")
    assert _type_condition("NEWS") == "n.`metadata.doc_type` = 'NEWS'"
    assert _type_condition(None) == "true"
    assert _type_condition("") == "true"


def test_label_matches_java_convention():
    assert label_for("00126380") == "CompanyReportChunk_00126380"
