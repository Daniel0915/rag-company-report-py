from app.vector.document import Document
from app.vector.splitter import TokenTextSplitter


def test_short_text_stays_single_chunk():
    chunks = TokenTextSplitter().split("짧은 문장입니다.")
    assert chunks == ["짧은 문장입니다."]


def test_blank_text_produces_no_chunks():
    assert TokenTextSplitter().split("   \n  ") == []


def test_long_text_splits_into_multiple_chunks():
    text = ("가나다라마바사아자차카타파하 " * 400).strip()
    chunks = TokenTextSplitter(chunk_size=100).split(text)
    assert len(chunks) > 1
    # 청크를 다시 이어붙이면 원문이 복원된다(공백 정규화 제외).
    assert "".join(c.replace(" ", "") for c in chunks) == text.replace(" ", "")


def test_trailing_fragment_shorter_than_min_length_is_dropped():
    # Spring AI TokenTextSplitter와 같이, 마지막에 남은 5자 이하 조각은 버린다.
    text = ("가나다라마바사아자차카타파하 " * 400).strip() + "."
    assert not TokenTextSplitter(chunk_size=100).split(text)[-1].endswith(".")


def test_apply_preserves_metadata_per_chunk():
    document = Document(text="문장 하나." * 500, metadata={"corp_code": "00126380"})
    chunks = TokenTextSplitter(chunk_size=50).apply([document])
    assert len(chunks) > 1
    assert all(c.metadata["corp_code"] == "00126380" for c in chunks)
    # 청크마다 고유 id가 부여돼야 벡터스토어에서 서로 덮어쓰지 않는다.
    assert len({c.id for c in chunks}) == len(chunks)
