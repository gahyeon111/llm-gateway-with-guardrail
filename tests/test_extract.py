from app.guard import extract_chat, extract_responses


def texts(refs):
    return [r.get() for r in refs]


def test_responses_input_refs_codex_shape():
    body = {
        "instructions": "SYS",
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "DEV"}]},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "USER"}, {"type": "input_image", "image_url": "x"}],
            },
            {"role": "user", "content": "PLAIN"},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ASSISTANT"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "REASON"}]},
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": '{"cmd":"ls"}'},
            {"type": "function_call_output", "call_id": "c1", "output": "TOOL_OUT"},
            {"type": "custom_tool_call", "call_id": "c2", "name": "apply_patch", "input": "PATCH"},
            {"type": "custom_tool_call_output", "call_id": "c2", "output": "PATCH_OUT"},
            {
                "type": "function_call_output",
                "call_id": "c3",
                "output": [{"type": "input_text", "text": "LIST_OUT"}, {"type": "input_image", "image_url": "y"}],
            },
        ],
    }
    assert texts(extract_responses.input_refs(body)) == [
        "SYS",
        "DEV",
        "USER",
        "PLAIN",
        "TOOL_OUT",
        "PATCH_OUT",
        "LIST_OUT",
    ]


def test_responses_input_string_and_mask_in_place():
    body = {"input": "hello", "model": "m"}
    refs = extract_responses.input_refs(body)
    refs[0].set("masked")
    assert body == {"input": "masked", "model": "m"}


def test_responses_output_refs():
    resp = {
        "output": [
            {"type": "reasoning", "id": "r", "summary": [{"type": "summary_text", "text": "R"}]},
            {"type": "message", "id": "m", "content": [{"type": "output_text", "text": "T"}]},
            {"type": "function_call", "id": "f", "arguments": "{}"},
            {"type": "custom_tool_call", "id": "c", "input": "P"},
        ]
    }
    refs = extract_responses.output_refs(resp, check_reasoning=False)
    assert texts(refs) == ["T", "{}", "P"]
    assert [r.kind for r in refs] == ["text", "tool", "tool"]
    assert texts(extract_responses.output_refs(resp, check_reasoning=True))[0] == "R"


def test_chat_refs():
    body = {
        "messages": [
            {"role": "system", "content": "S"},
            {"role": "user", "content": [{"type": "text", "text": "U"}, {"type": "image_url", "image_url": {}}]},
            {"role": "assistant", "content": "A", "tool_calls": [{"function": {"arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "1", "content": "TOOL"},
        ]
    }
    assert texts(extract_chat.input_refs(body)) == ["S", "U", "TOOL"]
    resp = {"choices": [{"message": {"content": "C", "tool_calls": [{"function": {"arguments": '{"a":1}'}}]}}]}
    refs = extract_chat.output_refs(resp)
    assert texts(refs) == ["C", '{"a":1}']
    assert [r.kind for r in refs] == ["text", "tool"]
