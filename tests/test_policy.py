import pytest

from app.guard.aim_client import AimResult, AimUnavailable, Verdict
from app.guard.cache import MemoryCache
from app.guard.policy import GuardPolicy, GuardUnavailable, merge_chunk_results, split_chunks
from tests.fake_aim import judge


class FakeAim:
    def __init__(self, fail: bool = False):
        self.calls: list[str] = []
        self.fail = fail

    async def check(self, text, direction, meta=None):
        self.calls.append(text)
        if self.fail:
            raise AimUnavailable("down")
        r = judge(text)
        return AimResult(Verdict(r["verdict"]), r.get("masked_text"), r.get("reason"))

    async def health(self):
        return True


def make_policy(aim, **kw):
    params = dict(policy_version="1", max_chars=50, chunk_overlap=20, concurrency=4, fail_mode="closed")
    params.update(kw)
    return GuardPolicy(aim, MemoryCache(100, 60), retry_backoff_s=0, **params)


def test_split_chunks_covers_text_with_overlap():
    text = "x" * 125
    chunks = split_chunks(text, 50, 20)
    assert [s for s, _ in chunks] == [0, 30, 60, 90]
    assert chunks[-1][0] + len(chunks[-1][1]) == 125


def test_merge_mask_straddling_chunk_midpoint():
    # card starts 35 chars into chunk 0 (cut off there) and is whole only in chunk 1
    text = "a" * 35 + "1234-5678-9012-3456" + "b" * 60
    chunks = split_chunks(text, 50, 20)
    results = []
    for _, c in chunks:
        r = judge(c)
        results.append(AimResult(Verdict(r["verdict"]), r.get("masked_text")))
    merged = merge_chunk_results(text, chunks, results)
    assert merged.masked_text == text.replace("1234-5678-9012", "****-****-****")


def test_merge_length_changing_mask():
    text = "x" * 45 + "1234567890123456" + "y" * 60  # undashed → masked text is longer
    chunks = split_chunks(text, 50, 20)
    results = []
    for _, c in chunks:
        r = judge(c)
        results.append(AimResult(Verdict(r["verdict"]), r.get("masked_text")))
    merged = merge_chunk_results(text, chunks, results)
    assert merged.masked_text == "x" * 45 + "****-****-****-3456" + "y" * 60


def test_merge_pass_and_mask_lengths():
    text = "a" * 40 + "1234-5678-9012-3456" + "b" * 60
    chunks = split_chunks(text, 50, 24)
    results = []
    for _, c in chunks:
        r = judge(c)
        results.append(AimResult(Verdict(r["verdict"]), r.get("masked_text")))
    merged = merge_chunk_results(text, chunks, results)
    assert merged.verdict is Verdict.MASK
    assert merged.masked_text == text.replace("1234-5678-9012", "****-****-****")


async def test_long_text_chunked_and_masked():
    aim = FakeAim()
    policy = make_policy(aim)
    text = ("lorem ipsum " * 10) + "card 1234-5678-9012-3456 end " + ("dolor sit " * 10)
    res = await policy.check(text, "input")
    assert len(aim.calls) > 1
    assert res.verdict is Verdict.MASK
    assert "1234-5678-9012" not in res.masked_text
    assert res.masked_text == text.replace("1234-5678-9012", "****-****-****")


async def test_block_in_any_chunk_blocks():
    aim = FakeAim()
    policy = make_policy(aim)
    res = await policy.check("ok " * 40 + "BLOCKME" + " ok" * 40, "input")
    assert res.verdict is Verdict.BLOCK


async def test_cache_hit_skips_aim():
    aim = FakeAim()
    policy = make_policy(aim)
    await policy.check("hello world", "input")
    await policy.check("hello world", "input")
    assert aim.calls == ["hello world"]
    # direction is part of the key
    await policy.check("hello world", "output")
    assert len(aim.calls) == 2


async def test_blank_text_skips_aim():
    aim = FakeAim()
    assert (await make_policy(aim).check("   ", "input")).verdict is Verdict.PASS
    assert aim.calls == []


async def test_check_many_dedupes():
    aim = FakeAim()
    res = await make_policy(aim).check_many(["a", "b", "a"], "input")
    assert [r.verdict for r in res] == [Verdict.PASS] * 3
    assert sorted(aim.calls) == ["a", "b"]


async def test_fail_closed_raises_after_retry():
    aim = FakeAim(fail=True)
    with pytest.raises(GuardUnavailable):
        await make_policy(aim).check("hello", "input")
    assert len(aim.calls) == 2  # one retry


async def test_fail_open_passes_and_is_not_cached():
    aim = FakeAim(fail=True)
    policy = make_policy(aim, fail_mode="open")
    assert (await policy.check("hello", "input")).verdict is Verdict.PASS
    aim.fail = False
    await policy.check("hello", "input")
    assert len(aim.calls) == 3  # fail-open result was not cached
