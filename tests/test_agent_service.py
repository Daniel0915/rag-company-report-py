from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agent import service as service_module
from app.agent.claude_agent import ClaudeAgentResult
from app.agent.loop import LoopResult
from app.agent.prompts import build_agent_system_prompt
from app.agent.service import AgentResult, AgentService
from app.agent.trace import ToolCallRecord
from app.models import ChatTurnDto

CORP = "00126380"


def _chunk(rcept="1", title="매출"):
    return {
        "kind": "chunk", "corp_name": "삼성전자", "report_nm": "사업보고서", "rcept_no": rcept,
        "source_file": "f", "section_order": 1, "section_title": title, "doc_type": "ANNUAL",
        "score": 0.9, "text": "t",
    }


def _rec(seq, name, sources, text="결과" * 200):
    return ToolCallRecord(seq, name, {"q": "x"}, True, text, sources)


def _service():
    settings = SimpleNamespace(agent_max_tool_calls=6)
    return AgentService(settings, MagicMock(), MagicMock())


def _stub_run(svc, records, answer="답"):
    svc.run = MagicMock(return_value=AgentResult(answer, records, None))


def test_prompt_contains_rules_and_context():
    p = build_agent_system_prompt("삼성전자", CORP, date(2026, 11, 25))
    for needle in ["삼성전자", CORP, "2026-11-25", "web_search", "calculate", "모른다"]:
        assert needle in p
    for tool in ["search_disclosures", "get_financials", "list_recent_disclosures", "query_ownership_graph"]:
        assert tool in p


def test_agent_sources_mark_web_results():
    svc = _service()
    _stub_run(svc, [
        _rec(1, "search_disclosures", [_chunk()]),
        _rec(2, "web_search", [{"kind": "web", "title": "기사", "url": "http://x"}]),
    ])
    resp = svc.chat("q", CORP, [], None)
    assert resp.sources[0].rceptNo == "1" and resp.sources[0].docType == "ANNUAL"
    assert resp.sources[1].docType == "WEB"
    assert resp.sources[1].rceptNo == "http://x"
    assert resp.sources[1].reportNm == "기사"
    assert [t.name for t in resp.toolCalls] == ["search_disclosures", "web_search"]
    assert len(resp.toolCalls[0].summary) == 200


def test_agent_sources_deduplicate_in_call_order():
    svc = _service()
    _stub_run(svc, [
        _rec(1, "search_disclosures", [_chunk("1"), _chunk("2")]),
        _rec(2, "search_disclosures", [_chunk("1")]),
    ])
    resp = svc.chat("q", CORP, [], None)
    assert [s.rceptNo for s in resp.sources] == ["1", "2"]


def test_agent_chat_rewrites_with_history():
    svc = _service()
    svc._chat.rewrite_query.return_value = "재작성"
    _stub_run(svc, [])
    history = [ChatTurnDto(role="user", content="a")]
    svc.chat("후속", CORP, history, "claude")
    svc._chat.rewrite_query.assert_called_once_with("후속", history, "claude")
    svc.run.assert_called_once_with("재작성", CORP, "claude")


def test_agent_chat_without_history_skips_rewrite():
    svc = _service()
    _stub_run(svc, [])
    svc.chat("q", CORP, [], None)
    svc._chat.rewrite_query.assert_not_called()
    svc.run.assert_called_once_with("q", CORP, None)


def test_agent_run_local_uses_tool_loop(monkeypatch):
    closed = []
    monkeypatch.setattr(
        service_module, "open_tool_context", lambda s, c: (object(), lambda: closed.append(1))
    )
    monkeypatch.setattr(service_module, "build_tools", lambda ctx: [])
    captured = {}

    def fake_loop(model, executor, system_prompt, question):
        captured["prompt"] = system_prompt
        executor.records.append(_rec(1, "calculate", []))
        return LoopResult("로컬답", 77)

    monkeypatch.setattr(service_module, "run_tool_loop", fake_loop)
    claude = MagicMock()
    monkeypatch.setattr(service_module, "run_claude_agent", claude)

    result = _service().run("q", CORP, None)

    assert result.answer == "로컬답" and result.prompt_tokens == 77
    assert [r.name for r in result.records] == ["calculate"]
    assert "삼성전자" in captured["prompt"]
    assert closed == [1]
    claude.assert_not_called()


def test_agent_run_closes_context_on_error(monkeypatch):
    closed = []
    monkeypatch.setattr(
        service_module, "open_tool_context", lambda s, c: (object(), lambda: closed.append(1))
    )
    monkeypatch.setattr(service_module, "build_tools", lambda ctx: [])
    monkeypatch.setattr(service_module, "run_tool_loop", MagicMock(side_effect=RuntimeError("x")))
    try:
        _service().run("q", CORP, None)
    except RuntimeError:
        pass
    assert closed == [1]


def test_agent_run_claude_uses_cli_agent(monkeypatch):
    rec = _rec(1, "calculate", [])
    claude = MagicMock(return_value=ClaudeAgentResult("클로드답", [rec]))
    monkeypatch.setattr(service_module, "run_claude_agent", claude)
    opened = MagicMock()
    monkeypatch.setattr(service_module, "open_tool_context", opened)

    result = _service().run("q", CORP, "claude")

    assert result == AgentResult("클로드답", [rec], None)
    assert claude.call_args.args[0] == "q" and claude.call_args.args[2] == CORP
    opened.assert_not_called()


def test_agent_run_deepseek_uses_openai_step_model(monkeypatch):
    seen = {}

    def fake_loop(model, executor, system_prompt, question):
        seen["model"] = model
        return LoopResult("딥시크답", 321)

    monkeypatch.setattr(service_module, "run_tool_loop", fake_loop)
    monkeypatch.setattr(service_module, "open_tool_context", lambda s, c: (MagicMock(), lambda: None))
    monkeypatch.setattr(service_module, "build_tools", lambda ctx: [])
    chat = MagicMock()
    svc = AgentService(SimpleNamespace(agent_max_tool_calls=6), chat, MagicMock())

    result = svc.run("q", CORP, "deepseek")

    assert result.answer == "딥시크답" and result.prompt_tokens == 321
    assert type(seen["model"]).__name__ == "OpenAIStepModel"
    chat.deepseek_client.assert_called_once()
