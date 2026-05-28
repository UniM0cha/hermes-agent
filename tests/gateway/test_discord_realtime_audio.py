"""Audio conversion primitives for Discord OpenAI Realtime voice."""

from __future__ import annotations

import sys
import types


class _AudioSource:
    def read(self) -> bytes:  # pragma: no cover - interface stub
        return b""

    def is_opus(self) -> bool:
        return False


if "discord" not in sys.modules:
    sys.modules["discord"] = types.SimpleNamespace(AudioSource=_AudioSource)
elif not hasattr(sys.modules["discord"], "AudioSource"):
    sys.modules["discord"].AudioSource = _AudioSource

from gateway.voice.realtime_audio import (  # noqa: E402
    DISCORD_FRAME_BYTES,
    OPENAI_RATE,
    RealtimeDiscordAudioSource,
    discord_pcm_to_realtime_pcm,
    realtime_pcm_to_discord_pcm,
    split_discord_frames,
)


def _ramp_pcm(sample_count: int, channels: int) -> bytes:
    values = bytearray()
    for i in range(sample_count):
        sample = ((i % 200) - 100) * 100
        for _ in range(channels):
            values.extend(int(sample).to_bytes(2, "little", signed=True))
    return bytes(values)


def test_empty_pcm_conversions_return_empty_bytes():
    assert discord_pcm_to_realtime_pcm(b"") == b""
    assert realtime_pcm_to_discord_pcm(b"") == b""


def test_discord_pcm_to_realtime_pcm_downmixes_20ms_to_24k_mono():
    discord_20ms = _ramp_pcm(sample_count=960, channels=2)

    converted = discord_pcm_to_realtime_pcm(discord_20ms)

    assert len(converted) == int(OPENAI_RATE * 0.020) * 2


def test_realtime_pcm_to_discord_pcm_expands_20ms_to_one_discord_frame():
    realtime_20ms = _ramp_pcm(sample_count=480, channels=1)

    converted = realtime_pcm_to_discord_pcm(realtime_20ms)

    assert len(converted) == DISCORD_FRAME_BYTES


def test_split_discord_frames_returns_full_frames_and_remainder():
    payload = b"a" * DISCORD_FRAME_BYTES + b"b" * DISCORD_FRAME_BYTES + b"tail"

    frames, remainder = split_discord_frames(payload)

    assert frames == [b"a" * DISCORD_FRAME_BYTES, b"b" * DISCORD_FRAME_BYTES]
    assert remainder == b"tail"


def test_audio_source_read_returns_silence_when_queue_empty():
    source = RealtimeDiscordAudioSource()

    frame = source.read()

    assert frame == b"\x00" * DISCORD_FRAME_BYTES
    assert source.is_opus() is False


def test_audio_source_emits_queued_realtime_pcm_in_order():
    source = RealtimeDiscordAudioSource()
    first = _ramp_pcm(sample_count=480, channels=1)
    second = _ramp_pcm(sample_count=480, channels=1)[::-1]

    source.enqueue_realtime_pcm(first)
    source.enqueue_realtime_pcm(second)

    assert source.read() == realtime_pcm_to_discord_pcm(first)
    assert source.read() == realtime_pcm_to_discord_pcm(second)


def test_audio_source_stop_makes_read_end_cleanly():
    source = RealtimeDiscordAudioSource()

    source.stop()

    assert source.read() == b""
