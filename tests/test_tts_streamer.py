"""Tests for adapters.tts_streamer.segment_for_tts — the sentence-splitter
that chops the agent's spoken text into a small number of chunks the
Telegram bot can synthesize in parallel.

The Edge-TTS + ffmpeg paths are network/subprocess-dependent and are
exercised by the integration smoke-test in tts_streamer itself; this file
only covers the pure-Python segmenter."""

from __future__ import annotations

from adapters.tts_streamer import segment_for_tts


def test_empty_input():
    assert segment_for_tts("") == []
    assert segment_for_tts("   ") == []


def test_short_text_below_threshold_is_single_chunk():
    """Texts under 40 chars shouldn't be split — otherwise a 20-char reply
    would become two tiny voice bubbles."""
    text = "你好，今天天气不错。"
    assert segment_for_tts(text) == [text]


def test_long_text_is_split_at_sentence_boundaries():
    text = "你好世界。今天天气真不错啊，我们一起去公园散步好不好？顺便买点菜回家做饭。晚上看电影放松。"
    segs = segment_for_tts(text, max_chunks=3)
    assert len(segs) >= 2
    # Reassembly should equal the input (allowing only whitespace differences).
    rejoined = "".join(segs).replace(" ", "")
    assert rejoined == text.replace(" ", "")


def test_short_leading_fragment_is_merged_forward():
    """A 2-char sentence like '嗯。' shouldn't become a lone voice bubble."""
    text = "嗯。今天我们来测试一下多段并行合成的分段逻辑，看看短句是否会被正确合并到相邻的长句中去。"
    segs = segment_for_tts(text)
    assert all(len(s) >= 10 for s in segs), segs


def test_max_chunks_cap_is_respected():
    text = "一。二。三。四。五。六。七。八。九。十。" + "十一个字的句子用来凑长度。" * 3
    segs = segment_for_tts(text, max_chunks=3)
    assert len(segs) <= 3


def test_tail_merged_when_exceeding_max_chunks():
    """Anything beyond max_chunks should collapse into the final chunk, not
    be dropped. Uses long sentences (≥25 chars each) so the forward-merge
    step doesn't already collapse them, forcing the max-chunks cap to kick in."""
    sentences = [
        f"这是足够长的第{i}句话用来测试分段器的上限裁剪行为是否正确。"
        for i in range(1, 6)
    ]
    text = "".join(sentences)
    segs = segment_for_tts(text, max_chunks=3)
    assert len(segs) == 3
    # Everything from sentence 3 onward should be folded into the final chunk.
    assert "第3" in segs[-1]
    assert "第4" in segs[-1]
    assert "第5" in segs[-1]


def test_english_punctuation_also_splits():
    text = ("This is a longer reply that has several sentences. "
            "The segmenter should split on periods too. And handle a third one.")
    segs = segment_for_tts(text, max_chunks=3)
    assert len(segs) >= 2
