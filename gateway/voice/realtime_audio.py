"""Audio primitives for Discord <-> OpenAI Realtime voice streaming."""

from __future__ import annotations

import audioop
import threading

try:  # pragma: no cover - exercised through tests with a stub when absent
    import discord
except Exception:  # pragma: no cover
    discord = None  # type: ignore[assignment]


DISCORD_RATE = 48000
DISCORD_CHANNELS = 2
OPENAI_RATE = 24000
OPENAI_CHANNELS = 1
SAMPLE_WIDTH = 2
DISCORD_FRAME_BYTES = int(0.020 * DISCORD_RATE) * DISCORD_CHANNELS * SAMPLE_WIDTH
SILENCE_FRAME = b"\x00" * DISCORD_FRAME_BYTES


class _FallbackAudioSource:
    def read(self) -> bytes:
        return b""

    def is_opus(self) -> bool:
        return False


_CandidateAudioSource = getattr(discord, "AudioSource", _FallbackAudioSource) if discord is not None else _FallbackAudioSource
_BaseAudioSource = _CandidateAudioSource if isinstance(_CandidateAudioSource, type) else _FallbackAudioSource


def _fit_length(payload: bytes, target_len: int) -> bytes:
    """Pad/truncate audioop output to deterministic frame lengths."""
    if len(payload) == target_len:
        return payload
    if len(payload) > target_len:
        return payload[:target_len]
    return payload + (b"\x00" * (target_len - len(payload)))


def discord_pcm_to_realtime_pcm(pcm_48k_stereo: bytes) -> bytes:
    """Convert Discord PCM16 48 kHz stereo to OpenAI Realtime PCM16 24 kHz mono."""
    if not pcm_48k_stereo:
        return b""
    source_frames = len(pcm_48k_stereo) // (DISCORD_CHANNELS * SAMPLE_WIDTH)
    target_frames = round(source_frames * OPENAI_RATE / DISCORD_RATE)
    target_len = target_frames * OPENAI_CHANNELS * SAMPLE_WIDTH
    mono = audioop.tomono(pcm_48k_stereo, SAMPLE_WIDTH, 0.5, 0.5)
    converted, _state = audioop.ratecv(
        mono,
        SAMPLE_WIDTH,
        OPENAI_CHANNELS,
        DISCORD_RATE,
        OPENAI_RATE,
        None,
    )
    return _fit_length(converted, target_len)


def realtime_pcm_to_discord_pcm(pcm_24k_mono: bytes) -> bytes:
    """Convert OpenAI Realtime PCM16 24 kHz mono to Discord PCM16 48 kHz stereo."""
    if not pcm_24k_mono:
        return b""
    source_frames = len(pcm_24k_mono) // (OPENAI_CHANNELS * SAMPLE_WIDTH)
    target_frames = round(source_frames * DISCORD_RATE / OPENAI_RATE)
    target_len = target_frames * DISCORD_CHANNELS * SAMPLE_WIDTH
    upsampled, _state = audioop.ratecv(
        pcm_24k_mono,
        SAMPLE_WIDTH,
        OPENAI_CHANNELS,
        OPENAI_RATE,
        DISCORD_RATE,
        None,
    )
    stereo = audioop.tostereo(upsampled, SAMPLE_WIDTH, 1.0, 1.0)
    return _fit_length(stereo, target_len)


def split_discord_frames(pcm_48k_stereo: bytes) -> tuple[list[bytes], bytes]:
    """Split PCM into complete 20 ms Discord frames plus a trailing remainder."""
    if not pcm_48k_stereo:
        return [], b""
    frames = [
        pcm_48k_stereo[i : i + DISCORD_FRAME_BYTES]
        for i in range(0, len(pcm_48k_stereo) - (len(pcm_48k_stereo) % DISCORD_FRAME_BYTES), DISCORD_FRAME_BYTES)
    ]
    remainder_len = len(pcm_48k_stereo) % DISCORD_FRAME_BYTES
    remainder = pcm_48k_stereo[-remainder_len:] if remainder_len else b""
    return frames, remainder


class RealtimeDiscordAudioSource(_BaseAudioSource):
    """Streaming Discord audio source fed by OpenAI Realtime PCM chunks.

    Discord calls ``read()`` every 20 ms.  While no model audio is queued we
    return silence so playback can stay alive for future chunks.  ``stop()``
    makes subsequent reads return ``b""`` to let discord.py end playback.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._stopped = False

    def enqueue_realtime_pcm(self, pcm_24k_mono: bytes) -> None:
        if not pcm_24k_mono:
            return
        discord_pcm = realtime_pcm_to_discord_pcm(pcm_24k_mono)
        if not discord_pcm:
            return
        with self._lock:
            if self._stopped:
                return
            self._buffer.extend(discord_pcm)

    def read(self) -> bytes:
        with self._lock:
            if self._stopped:
                return b""
            if len(self._buffer) >= DISCORD_FRAME_BYTES:
                frame = bytes(self._buffer[:DISCORD_FRAME_BYTES])
                del self._buffer[:DISCORD_FRAME_BYTES]
                return frame
        return SILENCE_FRAME

    def is_opus(self) -> bool:
        return False

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()

    def has_pending_audio(self) -> bool:
        with self._lock:
            return bool(self._buffer)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._buffer.clear()
