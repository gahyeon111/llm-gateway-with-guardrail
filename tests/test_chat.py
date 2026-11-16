"""Integration tests for /v1/chat/completions."""

from tests.fake_upstream import Reply, chat_chunk


def body(stream=True, text="hello"):
    return {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": text}],
        "stream": stream,
        "stream_options": {"include_usage": True},
    }


def content(events):
    out = ""
    for e in events:
        if isinstance(e, dict) and e.get("choices"):
            out += e["choices"][0]["delta"].get("content") or ""
    return out


def test_chat_input_block(harness):
    r = harness.client.post("/v1/chat/completions", json=body(text="BLOCKME"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "guardrail_blocked"


def test_chat_input_mask(harness):
    harness.stream_events("/v1/chat/completions", body(text="card 1234-5678-9012-3456"))
    sent = harness.upstream_requests[0][2]
    assert sent["messages"][1]["content"] == "card ****-****-****-3456"
    assert sent["model"] == "xenon-sol-rev1"


def test_chat_stream_output_mask_and_usage(harness):
    usage = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [], "usage": {"total_tokens": 5}}
    chunks = [
        chat_chunk({"role": "assistant", "content": ""}),
        chat_chunk({"content": "Your card 1234-"}),
        chat_chunk({"content": "5678-9012-3456 is "}),
        chat_chunk({"content": "on file, nothing else to add here."}),
        chat_chunk({}, "stop"),
        usage,
        "[DONE]",
    ]
    harness.script(lambda p, b: Reply(events=chunks, named_events=False))
    events = harness.stream_events("/v1/chat/completions", body())
    assert content(events) == "Your card ****-****-****-3456 is on file, nothing else to add here."
    assert events[-1] == "[DONE]"
    assert events[-2]["usage"] == {"total_tokens": 5}
    finish = [e for e in events if isinstance(e, dict) and e.get("choices") and e["choices"][0]["finish_reason"]]
    assert finish[0]["choices"][0]["finish_reason"] == "stop"
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"


def test_chat_stream_output_block(harness):
    chunks = [chat_chunk({"content": "this is BLOCKME text."}), chat_chunk({}, "stop"), "[DONE]"]
    harness.script(lambda p, b: Reply(events=chunks, named_events=False))
    events = harness.stream_events("/v1/chat/completions", body())
    assert events[-2]["error"]["code"] == "guardrail_blocked"
    assert events[-1] == "[DONE]"
    assert "BLOCKME" not in content(events)


def test_chat_stream_tool_calls_held_until_finish(harness):
    tc0 = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "shell", "arguments": ""}}
    chunks = [
        chat_chunk({"role": "assistant", "tool_calls": [tc0]}),
        chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"cmd":'}}]}),
        chat_chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"ls"}'}}]}),
        chat_chunk({}, "tool_calls"),
        "[DONE]",
    ]
    harness.script(lambda p, b: Reply(events=chunks, named_events=False))
    events = harness.stream_events("/v1/chat/completions", body())
    args = "".join(
        tc["function"].get("arguments", "")
        for e in events
        if isinstance(e, dict) and e.get("choices")
        for tc in e["choices"][0]["delta"].get("tool_calls") or []
    )
    assert args == '{"cmd":"ls"}'
    assert events[-1] == "[DONE]"


def test_chat_stream_tool_block(harness):
    chunks = [
        chat_chunk({"tool_calls": [{"index": 0, "id": "c", "function": {"name": "sh", "arguments": "BLOCKME"}}]}),
        chat_chunk({}, "tool_calls"),
        "[DONE]",
    ]
    harness.script(lambda p, b: Reply(events=chunks, named_events=False))
    events = harness.stream_events("/v1/chat/completions", body())
    assert events[-2]["error"]["code"] == "guardrail_blocked"


def test_chat_non_stream_output_mask(harness):
    resp = {
        "id": "x",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "1234-5678-9012-3456"}}],
    }
    harness.script(lambda p, b: Reply(json_body=resp))
    r = harness.client.post("/v1/chat/completions", json=body(stream=False))
    assert r.json()["choices"][0]["message"]["content"] == "****-****-****-3456"
