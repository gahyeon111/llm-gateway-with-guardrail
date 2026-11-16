import re

import pytest

from app.guard.aim_client import AimResult, Verdict
from app.guard.text_buffer import GuardBlocked, TextGuardBuffer
from tests.fake_aim import judge

RAW_CARD = re.compile(r"\d{4}-?\d{4}-?\d{4}-?\d{4}")


class Checker:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, text: str) -> AimResult:
        self.calls.append(text)
        r = judge(text)
        return AimResult(Verdict(r["verdict"]), r.get("masked_text"), r.get("reason"))


async def run(deltas, window=40, overlap=24):
    checker = Checker()
    buf = TextGuardBuffer(checker, window, overlap)
    released = []
    for i, d in enumerate(deltas):
        released += await buf.feed(d, {"i": i})
    released += await buf.finish()
    return buf, released, checker


async def test_pass_releases_original_payloads_and_holds_tail():
    checker = Checker()
    buf = TextGuardBuffer(checker, 20, 8)
    out = []
    for i, d in enumerate(["abcde", "fghij", "klmno", "pqrst"]):  # 20 chars → window full
        out += await buf.feed(d, {"i": i})
    # minimal suffix holding >= 8 chars is the last two deltas (10 chars)
    assert [r.payload for r in out] == [{"i": 0}, {"i": 1}]
    assert buf.pending_text == "klmnopqrst"
    out = await buf.finish()
    # no new text since last check → released without another AIM call
    assert [r.payload for r in out] == [{"i": 2}, {"i": 3}]
    assert len(checker.calls) == 1
    assert buf.final_text == "abcdefghijklmnopqrst"


async def test_sentence_boundary_triggers_check():
    checker = Checker()
    buf = TextGuardBuffer(checker, 1000, 0)
    out = await buf.feed("Hi there.", {"i": 0})
    assert len(checker.calls) == 1
    assert [r.text for r in out] == ["Hi there."]


async def test_card_split_across_deltas_and_windows_is_masked():
    deltas = ["Your card is 1234-", "5678-", "9012-", "3456 and that is all", " for today, thanks!"]
    buf, released, _ = await run(deltas, window=20, overlap=24)
    full = "".join(r.text for r in released)
    assert not RAW_CARD.search(full)
    assert "****-****-****-3456" in full
    assert buf.final_text == full
    # every synthesized release carries a template for metadata
    assert all(r.template is not None for r in released)


@pytest.mark.parametrize("split", range(1, 30))
async def test_card_never_leaks_at_any_split_point(split):
    text = "prefix text 1111-2222-3333-4444 suffix text here."
    deltas = [text[i : i + split] for i in range(0, len(text), split)]
    buf, released, _ = await run(deltas, window=16, overlap=20)
    full = "".join(r.text for r in released)
    assert "1111-2222-3333" not in full
    # nothing from the raw number other than its last 4 digits leaked in any single release
    for r in released:
        assert "2222" not in r.text and "3333" not in r.text


async def test_block_raises():
    checker = Checker()
    buf = TextGuardBuffer(checker, 10, 4)
    with pytest.raises(GuardBlocked):
        await buf.feed("please BLOCKME now", {})


async def test_block_detected_on_finish():
    checker = Checker()
    buf = TextGuardBuffer(checker, 1000, 4)
    assert await buf.feed("BLOCK", {}) == []
    assert await buf.feed("ME", {}) == []
    with pytest.raises(GuardBlocked):
        await buf.finish()


async def test_block_keyword_split_across_window_boundary():
    # window fills right after "BLO"; the overlap keeps "BLO" so "BLOCKME" is seen whole later
    checker = Checker()
    buf = TextGuardBuffer(checker, 12, 6)
    with pytest.raises(GuardBlocked):
        for d in ["aaaaaaaaaBLO", "CKME and more text"]:
            await buf.feed(d, {})
        await buf.finish()
