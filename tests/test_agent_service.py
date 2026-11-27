from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.agent import service as service_module
from app.agent.claude_agent import ClaudeAgentResult
from app.agent.loop import LoopResult
from app.agent.external_mcp import EXTERNAL_TOOL_NAMES
from app.agent.prompts import AGENT_SYSTEM_PROMPT, build_agent_system_prompt
from app.agent.tools import TOOL_NAMES, ToolResult, ToolSpec
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


def _settings(external=False):
    return SimpleNamespace(agent_max_tool_calls=6, agent_external_mcp=external)


def _service(external=False):
    return AgentService(_settings(external), MagicMock(), MagicMock())


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
    svc = AgentService(_settings(), chat, MagicMock())

    result = svc.run("q", CORP, "deepseek")

    assert result.answer == "딥시크답" and result.prompt_tokens == 321
    assert type(seen["model"]).__name__ == "OpenAIStepModel"
    chat.deepseek_client.assert_called_once()


def _named_specs(names):
    return [ToolSpec(n, n, {"type": "object", "properties": {}}, lambda a: ToolResult(True, "")) for n in names]


def _wire_loop(monkeypatch, loop=None):
    seen = {}
    monkeypatch.setattr(service_module, "open_tool_context", lambda s, c: (object(), lambda: None))
    monkeypatch.setattr(service_module, "build_tools", lambda ctx: _named_specs(TOOL_NAMES))

    def fake_loop(model, executor, system_prompt, question):
        seen["names"] = list(executor._specs)
        seen["prompt"] = system_prompt
        if loop:
            loop()
        return LoopResult("답", None)

    monkeypatch.setattr(service_module, "run_tool_loop", fake_loop)
    return seen


def test_external_tools_absent_when_disabled(monkeypatch):
    seen = _wire_loop(monkeypatch)
    opened = MagicMock()
    monkeypatch.setattr(service_module, "open_external_pool", opened)
    _service(external=False).run("q", CORP, None)
    assert seen["names"] == list(TOOL_NAMES)
    assert "krx_stock_price" not in seen["prompt"]
    opened.assert_not_called()


def test_external_tools_added_and_pool_closed_when_enabled(monkeypatch):
    seen = _wire_loop(monkeypatch, loop=MagicMock(side_effect=RuntimeError("x")))
    pool = MagicMock()
    monkeypatch.setattr(service_module, "open_external_pool", lambda s: pool)
    monkeypatch.setattr(service_module, "secrets_of", lambda s: [])
    try:
        _service(external=True).run("q", CORP, None)
    except RuntimeError:
        pass
    assert seen["names"] == list(TOOL_NAMES) + list(EXTERNAL_TOOL_NAMES)
    assert "krx_stock_price" in seen["prompt"]
    pool.close.assert_called_once()


def test_external_tools_skipped_for_unknown_company(monkeypatch):
    seen = _wire_loop(monkeypatch)
    opened = MagicMock()
    monkeypatch.setattr(service_module, "open_external_pool", opened)
    _service(external=True).run("q", "99999999", None)
    assert seen["names"] == list(TOOL_NAMES)
    opened.assert_not_called()


def test_collect_sources_maps_external():
    svc = _service()
    _stub_run(svc, [
        _rec(1, "dart_dividend", [{"kind": "external", "server": "opendart", "tool": "dart_dividend",
                                   "corp_name": "삼성전자", "rcept_no": "20260310002820",
                                   "label": "DART API · 배당"}]),
        _rec(2, "krx_stock_price", [{"kind": "external", "server": "pykrx", "tool": "krx_stock_price",
                                     "label": "KRX 시세 (pykrx) 2026-11-20~2026-11-26"}]),
    ])
    resp = svc.chat("q", CORP, [], None)
    dart, krx = resp.sources
    assert (dart.corpName, dart.reportNm, dart.rceptNo, dart.sectionTitle, dart.docType) == (
        "삼성전자", "DART API · 배당", "20260310002820", None, "DART_API")
    assert (krx.corpName, krx.reportNm, krx.rceptNo, krx.docType) == (
        None, "KRX 시세 (pykrx) 2026-11-20~2026-11-26", None, "KRX")


def test_prompt_unchanged_without_external():
    today = date(2026, 11, 27)
    assert build_agent_system_prompt("삼성전자", CORP, today) == AGENT_SYSTEM_PROMPT.format(
        corp_name="삼성전자", corp_code=CORP, today="2026-11-27")


def test_prompt_with_external_adds_rules():
    p = build_agent_system_prompt("삼성전자", CORP, date(2026, 11, 27), external=True)
    for needle in ["krx_stock_price", "dart_dividend", "dart_financial_index", "31일"]:
        assert needle in p
    assert "주가, 업계 동향" not in p
