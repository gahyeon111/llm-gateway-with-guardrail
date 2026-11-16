"""Integration tests for /v1/responses through fake AIM + fake upstream."""

import re

from tests.fake_upstream import Reply, response_stream, text_item_events, tool_item_events

RAW_CARD = re.compile(r"\d{4}-\d{4}-\d{4}")


def body(stream=True, text="hello", **kw):
    b = {
        "model": "gpt-5.6-terra",
        "instructions": "You are Codex.",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}],
        "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
        "reasoning": {"effort": "medium"},
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "stream": stream,
    }
    b.update(kw)
    return b


def types(events):
    return [e["type"] if isinstance(e, dict) else e for e in events]


def streamed_text(events):
    return "".join(e["delta"] for e in events if isinstance(e, dict) and e["type"] == "response.output_text.delta")


# --- M1: passthrough -----------------------------------------------------------------------------


def test_passthrough_without_guards(make_harness):
    h = make_harness(guard_input_enabled=False, guard_output_enabled=False)
    events = h.stream_events("/v1/responses", body())
    assert types(events)[0] == "response.created"
    assert types(events)[-1] == "response.completed"
    assert h.aim_calls == []
    path, headers, sent = h.upstream_requests[0]
    assert path == "/v1/responses"
    assert sent["model"] == "xenon-terra-rev1"
    # every other field forwarded untouched
    for k in ("instructions", "input", "tools", "reasoning", "include", "store", "stream"):
        assert sent[k] == body()[k]


def test_headers_filtered_and_auth_replaced(make_harness):
    h = make_harness(xenon_upstream_api_key="xenon-key")
    h.stream_events(
        "/v1/responses",
        body(),
        headers={
            "Authorization": "Bearer user-token",
            "session_id": "s-1",
            "x-codex-turn": "3",
            "x-forwarded-for": "10.0.0.1",
            "openai-beta": "responses=experimental",
        },
    )
    _, headers, _ = h.upstream_requests[0]
    assert headers["authorization"] == "Bearer xenon-key"
    assert headers["session_id"] == "s-1"
    assert headers["x-codex-turn"] == "3"
    assert headers["openai-beta"] == "responses=experimental"
    assert headers["accept-encoding"] == "identity"
    assert "x-forwarded-for" not in headers


def test_model_routing_per_entry_base_url(make_harness):
    h = make_harness()
    # luna has its own base_url (http://luna-router/v1); the fake transport answers for any host
    h.stream_events("/v1/responses", body(model="gpt-5.6-luna"))
    assert h.upstream_requests[0][2]["model"] == "xenon-luna-rev1"


def test_bedrock_mode_uses_bedrock_model_and_token(make_harness):
    h = make_harness(upstream_mode="bedrock", aws_bearer_token_bedrock="br-key", bedrock_base_url="http://bedrock/v1")
    h.stream_events("/v1/responses", body())
    _, headers, sent = h.upstream_requests[0]
    assert sent["model"] == "global.openai.gpt-5.6-terra"
    assert headers["authorization"] == "Bearer br-key"


def test_strip_request_fields(make_harness):
    h = make_harness(strip_request_fields="store,include")
    h.stream_events("/v1/responses", body())
    sent = h.upstream_requests[0][2]
    assert "store" not in sent and "include" not in sent


def test_stream_pass_sequence_renumbered_and_completed(harness):
    events = harness.stream_events("/v1/responses", body())
    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert streamed_text(events) == "Hello from fake upstream."
    assert types(events)[-1] == "response.completed"
    done = next(e for e in events if e["type"] == "response.output_text.done")
    assert done["text"] == "Hello from fake upstream."


def test_non_stream_pass(harness):
    r = harness.client.post("/v1/responses", json=body(stream=False))
    assert r.status_code == 200
    assert r.json()["output"][0]["content"][0]["text"] == "Hello from fake upstream."


# --- M2: input guard -----------------------------------------------------------------------------


def test_input_block_returns_400_without_upstream_call(harness):
    r = harness.client.post("/v1/responses", json=body(text="please BLOCKME"))
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "guardrail_blocked"
    assert err["type"] == "guardrail_error"
    assert "stage=input" in err["message"]
    assert harness.upstream_requests == []


def test_input_block_in_tool_output(harness):
    b = body()
    b["input"] += [
        {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "file contents BLOCKME"},
    ]
    r = harness.client.post("/v1/responses", json=b)
    assert r.status_code == 400


def test_input_mask_forwarded_with_structure_intact(harness):
    b = body(text="my card 1234-5678-9012-3456 please")
    b["input"].append({"type": "function_call_output", "call_id": "c9", "output": "num 1111-2222-3333-4444"})
    harness.stream_events("/v1/responses", b)
    sent = harness.upstream_requests[0][2]
    assert sent["input"][0]["content"][0]["text"] == "my card ****-****-****-3456 please"
    assert sent["input"][1] == {"type": "function_call_output", "call_id": "c9", "output": "num ****-****-****-4444"}


def test_input_cache_avoids_repeat_aim_calls(harness):
    harness.stream_events("/v1/responses", body())
    input_calls = [c for c in harness.aim_calls if c[0] == "input"]
    harness.stream_events("/v1/responses", body())
    assert [c for c in harness.aim_calls if c[0] == "input"] == input_calls
    assert len(input_calls) == 2  # instructions + user text, once each


def test_assistant_and_function_call_items_not_input_checked(harness):
    b = body()
    b["input"] += [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "BLOCKME"}]},
        {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "BLOCKME"},
    ]
    r = harness.client.post("/v1/responses", json={**b, "stream": False})
    assert r.status_code == 200


# --- M3: output guard (stream) -------------------------------------------------------------------


def test_output_mask_across_deltas_and_windows(harness):
    deltas = ["Sure, the card ", "number is 1234-", "5678-9012", "-3456. Anything ", "else I can help with today?"]
    harness.script(lambda p, b: Reply(events=response_stream("resp_x", [text_item_events("msg_1", 0, deltas)])))
    events = harness.stream_events("/v1/responses", body())
    text = streamed_text(events)
    assert not RAW_CARD.search(text)
    expected = "".join(deltas).replace("1234-5678-9012", "****-****-****")
    assert text == expected
    done = next(e for e in events if e["type"] == "response.output_text.done")
    part_done = next(e for e in events if e["type"] == "response.content_part.done")
    item_done = next(e for e in events if e["type"] == "response.output_item.done")
    completed = events[-1]
    assert done["text"] == expected
    assert part_done["part"]["text"] == expected
    assert item_done["item"]["content"][0]["text"] == expected
    assert completed["type"] == "response.completed"
    assert completed["response"]["output"][0]["content"][0]["text"] == expected
    assert completed["response"]["usage"]["total_tokens"] == 30
    # synthesized deltas keep item/index metadata
    for e in events:
        if e["type"] == "response.output_text.delta":
            assert (e["item_id"], e["output_index"], e["content_index"]) == ("msg_1", 0, 0)
    assert [e["sequence_number"] for e in events] == list(range(len(events)))


def test_output_text_block_emits_response_failed(harness):
    deltas = ["Here is something ", "you asked: BLOCKME ", "and more text follows here."]
    harness.script(lambda p, b: Reply(events=response_stream("resp_b", [text_item_events("msg_1", 0, deltas)])))
    events = harness.stream_events("/v1/responses", body())
    last = events[-1]
    assert last["type"] == "response.failed"
    assert last["response"]["id"] == "resp_b"
    assert last["response"]["error"]["code"] == "guardrail_blocked"
    assert "BLOCKME" not in streamed_text(events)
    assert "response.completed" not in types(events)


def test_tool_call_pass_emits_held_deltas_then_done(harness):
    harness.script(
        lambda p, b: Reply(
            events=response_stream("r", [tool_item_events("fc_1", 0, "shell", ['{"command":', '["ls"]}'])])
        )
    )
    events = harness.stream_events("/v1/responses", body())
    t = types(events)
    i = t.index("response.function_call_arguments.delta")
    assert t[i : i + 3] == [
        "response.function_call_arguments.delta",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
    ]
    assert events[-1]["response"]["output"][0]["arguments"] == '{"command":["ls"]}'


def test_tool_call_block(harness):
    harness.script(
        lambda p, b: Reply(
            events=response_stream("r", [tool_item_events("fc_1", 0, "shell", ['{"command":', '["BLOCKME"]}'])])
        )
    )
    events = harness.stream_events("/v1/responses", body())
    assert events[-1]["type"] == "response.failed"
    assert "response.function_call_arguments.delta" not in types(events)


def test_tool_call_mask_is_block_by_default(harness):
    args = ['{"command":["echo","1234-5678-9012-3456"]}']
    harness.script(lambda p, b: Reply(events=response_stream("r", [tool_item_events("fc_1", 0, "shell", args)])))
    events = harness.stream_events("/v1/responses", body())
    assert events[-1]["type"] == "response.failed"


def test_tool_call_mask_policy_mask(make_harness):
    h = make_harness(tool_mask_policy="mask")
    args = ['{"command":["echo",', '"1234-5678-9012-3456"]}']
    h.script(lambda p, b: Reply(events=response_stream("r", [tool_item_events("fc_1", 0, "shell", args)])))
    events = h.stream_events("/v1/responses", body())
    deltas = [e for e in events if e["type"] == "response.function_call_arguments.delta"]
    assert len(deltas) == 1
    masked = '{"command":["echo","****-****-****-3456"]}'
    assert deltas[0]["delta"] == masked
    assert next(e for e in events if e["type"] == "response.function_call_arguments.done")["arguments"] == masked
    assert events[-1]["response"]["output"][0]["arguments"] == masked


def test_custom_tool_call_apply_patch_block(harness):
    patch = ["*** Begin Patch\n", "+secret BLOCKME\n", "*** End Patch"]
    harness.script(
        lambda p, b: Reply(
            events=response_stream("r", [tool_item_events("ct_1", 0, "apply_patch", patch, custom=True)])
        )
    )
    events = harness.stream_events("/v1/responses", body())
    assert events[-1]["type"] == "response.failed"


def test_text_then_tool_call_loop_shape(harness):
    items = [
        text_item_events("msg_1", 0, ["Let me check. "]),
        tool_item_events("fc_1", 1, "shell", ['{"command":["ls"]}']),
    ]
    harness.script(lambda p, b: Reply(events=response_stream("r", items)))
    events = harness.stream_events("/v1/responses", body())
    assert types(events)[-1] == "response.completed"
    assert [i["type"] for i in events[-1]["response"]["output"]] == ["message", "function_call"]


def test_unknown_and_reasoning_events_forwarded(harness):
    extra = [
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_1",
            "output_index": 0,
            "summary_index": 0,
            "delta": "thinking BLOCKME",
        },
        {"type": "response.some_future_event", "foo": "bar"},
    ]
    harness.script(lambda p, b: Reply(events=response_stream("r", [text_item_events("msg_1", 1, ["hi"])], extra=extra)))
    events = harness.stream_events("/v1/responses", body())
    t = types(events)
    assert "response.some_future_event" in t
    assert "response.reasoning_summary_text.delta" in t  # GUARD_REASONING=skip
    assert t[-1] == "response.completed"


def test_reasoning_checked_when_enabled(make_harness):
    h = make_harness(guard_reasoning="check")
    extra = [
        {
            "type": "response.reasoning_summary_text.delta",
            "item_id": "rs_1",
            "output_index": 0,
            "summary_index": 0,
            "delta": "thinking BLOCKME.",
        },
    ]
    h.script(lambda p, b: Reply(events=response_stream("r", [], extra=extra)))
    events = h.stream_events("/v1/responses", body())
    assert events[-1]["type"] == "response.failed"


def test_completed_without_streamed_deltas_is_still_checked(harness):
    # upstream sends only the final object (no deltas) → gateway must still guard the text
    def script(p, b):
        evs = response_stream("r", [])
        evs[-1]["response"]["output"] = [
            {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "card 1234-5678-9012-3456"}],
            }
        ]
        return Reply(events=evs)

    harness.script(script)
    events = harness.stream_events("/v1/responses", body())
    assert events[-1]["response"]["output"][0]["content"][0]["text"] == "card ****-****-****-3456"


# --- M3: output guard (non-stream) ---------------------------------------------------------------


def test_non_stream_output_mask(harness):
    harness.script(
        lambda p, b: Reply(
            json_body=response_stream("r", [text_item_events("m", 0, ["card 1234-5678-9012-3456"])])[-1]["response"]
        )
    )
    r = harness.client.post("/v1/responses", json=body(stream=False))
    assert r.status_code == 200
    assert r.json()["output"][0]["content"][0]["text"] == "card ****-****-****-3456"


def test_non_stream_output_block(harness):
    final = response_stream("r", [tool_item_events("f", 0, "shell", ["BLOCKME"])])[-1]["response"]
    harness.script(lambda p, b: Reply(json_body=final))
    r = harness.client.post("/v1/responses", json=body(stream=False))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "guardrail_blocked"
    assert "stage=output" in r.json()["error"]["message"]


# --- AIM failure ---------------------------------------------------------------------------------


def test_aim_down_fail_closed_input(harness):
    harness.aim.state.fail = True
    r = harness.client.post("/v1/responses", json=body(stream=False))
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "guardrail_unavailable"
    assert harness.upstream_requests == []


def test_aim_down_fail_closed_mid_stream(make_harness):
    h = make_harness(guard_input_enabled=False)
    h.aim.state.fail = True
    events = h.stream_events("/v1/responses", body())
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["error"]["code"] == "guardrail_unavailable"


def test_aim_down_fail_open(make_harness):
    h = make_harness(aim_fail_mode="open")
    h.aim.state.fail = True
    events = h.stream_events("/v1/responses", body())
    assert types(events)[-1] == "response.completed"
