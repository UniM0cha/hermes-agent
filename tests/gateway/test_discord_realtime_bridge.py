"""Tests for Discord Realtime voice bridge lifecycle."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.voice.realtime_audio import DISCORD_FRAME_BYTES, discord_pcm_to_realtime_pcm


class _FakeReceiver:
    def __init__(self):
        self.callback = None
        self.buffer_utterances = None

    def set_pcm_stream_callback(self, callback, *, buffer_utterances=True):
        self.callback = callback
        self.buffer_utterances = buffer_utterances


class _FakeVoiceClient:
    def __init__(self):
        self.played_source = None
        self.stopped = False

    def is_playing(self):
        return self.played_source is not None and not self.stopped

    def play(self, source, after=None):  # noqa: ARG002
        self.played_source = source

    def stop(self):
        self.stopped = True


class _FakeTextChannel:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)


class _FakeSessionEntry:
    def __init__(self, session_id="session-1"):
        self.session_id = session_id
        self.session_key = "discord:777:1234"


class _FakeSessionStore:
    def __init__(self, transcript=None):
        self.entry = _FakeSessionEntry()
        self.transcript = list(transcript or [])

    def get_or_create_session(self, _source):
        return self.entry

    def load_transcript(self, _session_id):
        return list(self.transcript)


class _StubRealtimeSession:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.connected = False
        self.closed = False
        self.appended = []
        self.tool_results = []
        self.conversation_messages = []
        self.cancel_count = 0
        _StubRealtimeSession.instances.append(self)

    def connect(self):
        self.connected = True

    def append_audio(self, pcm):
        self.appended.append(pcm)

    def run_read_loop(self, **kwargs):  # noqa: ARG002
        return None

    def send_tool_result(self, call_id, output):
        self.tool_results.append((call_id, output))

    def send_conversation_message(self, text, *, create_response=False):
        self.conversation_messages.append((text, create_response))

    def cancel_response(self):
        self.cancel_count += 1
        return True

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("HERMES_DISCORD_REALTIME_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_MEET_REALTIME_KEY", raising=False)
    _StubRealtimeSession.instances.clear()


def test_voice_receiver_pcm_stream_callback_can_bypass_utterance_buffering(monkeypatch):
    from plugins.platforms.discord.adapter import VoiceReceiver

    receiver = VoiceReceiver(MagicMock())
    receiver._running = True
    receiver.map_ssrc(42, 1234)
    seen = []

    receiver.set_pcm_stream_callback(lambda ssrc, user_id, pcm: seen.append((ssrc, user_id, pcm)), buffer_utterances=False)
    receiver._handle_decoded_pcm(42, b"pcm")

    assert seen == [(42, 1234, b"pcm")]
    assert receiver._buffers[42] == bytearray()


@pytest.mark.asyncio
async def test_bridge_missing_api_key_returns_safe_error(monkeypatch):
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True},
    )

    ok, message = await bridge.start()

    assert ok is False
    assert "HERMES_DISCORD_REALTIME_KEY" in message
    assert "sk-" not in message


@pytest.mark.asyncio
async def test_bridge_start_wires_receiver_callback_and_starts_playback(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    receiver = _FakeReceiver()
    voice_client = _FakeVoiceClient()
    text_channel = _FakeTextChannel()
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=voice_client,
        receiver=receiver,
        text_channel=text_channel,
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True, "model": "gpt-realtime-2", "voice": "alloy"},
    )

    ok, message = await bridge.start()

    assert ok is True, message
    assert receiver.callback is not None
    assert receiver.buffer_utterances is False
    assert voice_client.played_source is bridge.audio_source
    instance = _StubRealtimeSession.instances[-1]
    assert instance.connected is True
    assert "Your name is 니코" in instance.kwargs["instructions"]
    assert "Do not introduce yourself as Hermes" in instance.kwargs["instructions"]
    assert "존댓말" in instance.kwargs["instructions"]
    assert "반말" in instance.kwargs["instructions"]
    assert "You are Hermes Agent" not in instance.kwargs["instructions"]
    assert instance.kwargs["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": True,
        "interrupt_response": True,
    }
    assert instance.kwargs["reasoning"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_bridge_start_injects_recent_session_context_into_instructions(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.config import Platform
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    source = SessionSource(platform=Platform.DISCORD, chat_id="777", user_id="1234", chat_type="channel")
    runner = SimpleNamespace(
        adapters={},
        session_store=_FakeSessionStore([
            {"role": "user", "content": "방금 Realtime 작업 결과를 음성으로 돌려받고 싶다고 말했다."},
            {"role": "assistant", "content": "A안으로 비동기 bridge를 만들기로 했다."},
        ]),
    )
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data=source.to_dict(),
        runner=runner,
        config={"enabled": True, "model": "gpt-realtime-2", "voice": "alloy"},
    )

    ok, message = await bridge.start()

    assert ok is True, message
    instructions = _StubRealtimeSession.instances[-1].kwargs["instructions"]
    assert "Voice session startup context" in instructions
    assert "Realtime 작업 결과를 음성으로" in instructions
    assert "A안으로 비동기 bridge" in instructions


@pytest.mark.asyncio
async def test_bridge_passes_custom_turn_detection_to_realtime_session(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={
            "enabled": True,
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.4,
                "silence_duration_ms": 250,
            },
        },
    )

    ok, message = await bridge.start()

    assert ok is True, message
    turn_detection = _StubRealtimeSession.instances[-1].kwargs["turn_detection"]
    assert turn_detection["threshold"] == 0.4
    assert turn_detection["silence_duration_ms"] == 250
    assert turn_detection["prefix_padding_ms"] == 200
    assert turn_detection["create_response"] is True


@pytest.mark.asyncio
async def test_bridge_passes_semantic_turn_detection_without_server_vad_fields(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True, "turn_detection": {"type": "semantic_vad"}},
    )

    ok, message = await bridge.start()

    assert ok is True, message
    turn_detection = _StubRealtimeSession.instances[-1].kwargs["turn_detection"]
    assert turn_detection == {
        "type": "semantic_vad",
        "eagerness": "high",
        "interrupt_response": True,
        "create_response": True,
    }


def test_bridge_trailing_silence_window_starts_after_real_pcm():
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True, "silence_frame_ms": 50, "trailing_silence_seconds": 0.2},
    )

    assert bridge._should_stream_trailing_silence(now=1000.0) is False
    bridge._mark_real_pcm_appended(now=1000.0)

    assert bridge._should_stream_trailing_silence(now=1000.02) is False
    assert bridge._should_stream_trailing_silence(now=1000.06) is True
    assert bridge._should_stream_trailing_silence(now=1000.25) is False


def test_bridge_trailing_silence_worker_appends_silence_after_discord_stops():
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    class _OneShotStop:
        def __init__(self):
            self.calls = 0

        def wait(self, _interval):
            self.calls += 1
            return self.calls > 1

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={
            "enabled": True,
            "controller_only": True,
            "silence_frame_ms": 20,
            "trailing_silence_seconds": 0.12,
        },
    )
    session = _StubRealtimeSession()
    bridge.session = session  # type: ignore[assignment]
    bridge._stop_event = _OneShotStop()  # type: ignore[assignment]
    bridge._mark_real_pcm_appended(now=time.monotonic() - 0.05)

    bridge._trailing_silence_worker()

    assert len(session.appended) == 1
    assert set(session.appended[0]) == {0}


@pytest.mark.asyncio
async def test_bridge_passes_custom_reasoning_to_realtime_session(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True, "reasoning": {"effort": "medium"}},
    )

    ok, message = await bridge.start()

    assert ok is True, message
    assert _StubRealtimeSession.instances[-1].kwargs["reasoning"] == {"effort": "medium"}


@pytest.mark.asyncio
async def test_bridge_streams_controller_pcm_to_realtime_session(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    receiver = _FakeReceiver()
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=receiver,
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True, "controller_only": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]
    discord_pcm = b"\x01\x00" * (DISCORD_FRAME_BYTES // 2)

    receiver.callback(99, 1234, discord_pcm)
    receiver.callback(100, 9999, discord_pcm)

    assert session.appended == [discord_pcm_to_realtime_pcm(discord_pcm)]


@pytest.mark.asyncio
async def test_bridge_skips_controller_pcm_when_realtime_session_unusable(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    receiver = _FakeReceiver()
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=receiver,
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True, "controller_only": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]
    session.closed = True

    receiver.callback(99, 1234, b"\x01\x00" * (DISCORD_FRAME_BYTES // 2))

    assert session.appended == []


@pytest.mark.asyncio
async def test_bridge_does_not_cancel_response_on_raw_pcm_noise(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    receiver = _FakeReceiver()
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=receiver,
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True, "controller_only": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]
    assert bridge.audio_source is not None
    bridge.audio_source.enqueue_realtime_pcm(b"\x01\x00" * 480)
    assert bridge.audio_source.has_pending_audio() is True

    receiver.callback(99, 1234, b"\x01\x00" * (DISCORD_FRAME_BYTES // 2))

    assert session.cancel_count == 0
    assert bridge.audio_source.has_pending_audio() is True


@pytest.mark.asyncio
async def test_bridge_clears_queued_audio_when_realtime_detects_speech_started(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True},
    )
    await bridge.start()
    assert bridge.audio_source is not None
    bridge.audio_source.enqueue_realtime_pcm(b"\x01\x00" * 480)

    bridge._on_input_speech_started()

    assert bridge.audio_source.has_pending_audio() is False


@pytest.mark.asyncio
async def test_bridge_stop_clears_receiver_callback_and_closes_session(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    receiver = _FakeReceiver()
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=receiver,
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}),
        config={"enabled": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]

    await bridge.stop("test")

    assert receiver.callback is None
    assert bridge.audio_source.read() == b""
    assert session.closed is True


@pytest.mark.asyncio
async def test_bridge_tool_call_runs_hermes_job_without_blocking_and_injects_result(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.config import Platform
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    class _FakeAdapter:
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, content, metadata=None):
            self.sent.append((chat_id, content, metadata))
            return SimpleNamespace(success=True, message_id="m1")

    started = asyncio.Event()
    release = asyncio.Event()
    captured_events = []

    async def _run_agent(event):
        captured_events.append(event)
        started.set()
        await release.wait()
        return "Hermes 작업 완료입니다."

    adapter = _FakeAdapter()
    runner = SimpleNamespace(adapters={Platform.DISCORD: adapter}, _handle_message=AsyncMock(side_effect=_run_agent))
    source = SessionSource(platform=Platform.DISCORD, chat_id="777", user_id="1234", chat_type="channel")
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data=source.to_dict(),
        runner=runner,
        config={"enabled": True, "tool_bridge_enabled": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]

    await bridge._handle_tool_call("run_hermes_task", {"prompt": "테스트 작업"}, "call_1")

    assert session.tool_results == [
        (
            "call_1",
            {
                "accepted": True,
                "job_id": "rtjob_1",
                "message": "Hermes 작업을 백그라운드에서 시작했습니다. 완료되면 음성으로 알려드리겠습니다.",
            },
        )
    ]
    await asyncio.wait_for(started.wait(), timeout=0.5)
    assert session.conversation_messages == []
    job_task = bridge._jobs["rtjob_1"].task
    release.set()
    await asyncio.wait_for(job_task, timeout=0.5)

    runner._handle_message.assert_awaited_once()
    event = captured_events[0]
    assert isinstance(event, MessageEvent)
    assert event.message_type is MessageType.TEXT
    assert event.source.chat_id == "777"
    assert event.source.user_id == "1234"
    assert event.source.user_id_alt == "1234:realtime:rtjob_1"
    assert event.text == "[Realtime voice task rtjob_1]\n테스트 작업"
    assert adapter.sent[-1][0] == "777"
    assert adapter.sent[-1][1] == "Hermes 작업 완료입니다."
    assert session.conversation_messages[-1][1] is True
    assert "rtjob_1" in session.conversation_messages[-1][0]
    assert "Hermes 작업 완료입니다." in session.conversation_messages[-1][0]


@pytest.mark.asyncio
async def test_bridge_exposes_task_status_cancel_and_list_tools(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}, _handle_message=AsyncMock(return_value="done")),
        config={"enabled": True, "tool_bridge_enabled": True},
    )

    ok, message = await bridge.start()

    assert ok is True, message
    tool_names = {tool["name"] for tool in _StubRealtimeSession.instances[-1].kwargs["tools"]}
    assert tool_names == {
        "run_hermes_task",
        "get_hermes_task_status",
        "cancel_hermes_task",
        "list_hermes_tasks",
    }


@pytest.mark.asyncio
async def test_bridge_task_status_tool_reports_running_and_completed_jobs(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.config import Platform
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    started = asyncio.Event()
    release = asyncio.Event()

    async def _run_agent(_event):
        started.set()
        await release.wait()
        return "작업 결과입니다."

    runner = SimpleNamespace(adapters={Platform.DISCORD: None}, _handle_message=AsyncMock(side_effect=_run_agent))
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=runner,
        config={"enabled": True, "tool_bridge_enabled": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]

    await bridge._handle_tool_call("run_hermes_task", {"prompt": "오래 걸리는 작업"}, "call_1")
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await bridge._handle_tool_call("get_hermes_task_status", {"job_id": "rtjob_1"}, "call_2")

    running_result = session.tool_results[-1][1]
    assert running_result["accepted"] is True
    assert running_result["job"]["job_id"] == "rtjob_1"
    assert running_result["job"]["status"] == "running"
    assert "아직 작업 중" in running_result["message"]

    release.set()
    await asyncio.wait_for(bridge._jobs["rtjob_1"].task, timeout=0.5)
    await bridge._handle_tool_call("get_hermes_task_status", {"job_id": "rtjob_1"}, "call_3")

    completed_result = session.tool_results[-1][1]
    assert completed_result["job"]["status"] == "completed"
    assert "작업 결과입니다." in completed_result["job"]["result_preview"]


@pytest.mark.asyncio
async def test_bridge_cancel_task_tool_cancels_running_job_and_injects_notice(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.config import Platform
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _run_agent(_event):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runner = SimpleNamespace(adapters={Platform.DISCORD: None}, _handle_message=AsyncMock(side_effect=_run_agent))
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=runner,
        config={"enabled": True, "tool_bridge_enabled": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]

    await bridge._handle_tool_call("run_hermes_task", {"prompt": "취소할 작업"}, "call_1")
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await bridge._handle_tool_call("cancel_hermes_task", {"job_id": "rtjob_1"}, "call_2")

    cancel_result = session.tool_results[-1][1]
    assert cancel_result["accepted"] is True
    assert cancel_result["job"]["status"] == "cancelled"
    await asyncio.wait_for(cancelled.wait(), timeout=0.5)
    assert any("중단" in text for text, _create_response in session.conversation_messages)


@pytest.mark.asyncio
async def test_bridge_queues_voice_notice_when_realtime_session_is_unavailable(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}, _handle_message=AsyncMock(return_value="done")),
        config={"enabled": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]
    session.closed = True

    sent = await bridge._send_voice_notice("작업이 완료됐습니다.")

    assert sent is False
    assert session.conversation_messages == []
    assert bridge._pending_voice_notices == ["작업이 완료됐습니다."]

    session.closed = False
    await bridge._replay_pending_voice_notices()

    assert bridge._pending_voice_notices == []
    assert session.conversation_messages == [("작업이 완료됐습니다.", True)]


@pytest.mark.asyncio
async def test_bridge_replay_pending_voice_notices_preserves_remainder_on_failure(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=SimpleNamespace(adapters={}, _handle_message=AsyncMock(return_value="done")),
        config={"enabled": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]
    bridge._pending_voice_notices = ["첫 번째", "두 번째", "세 번째"]

    def _fail_first(_text, *, create_response=False):  # noqa: ARG001
        raise RuntimeError("temporary send failure")

    session.send_conversation_message = _fail_first

    await bridge._replay_pending_voice_notices()

    assert bridge._pending_voice_notices == ["첫 번째", "두 번째", "세 번째"]


@pytest.mark.asyncio
async def test_bridge_rejects_new_tool_calls_after_stop(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    runner = SimpleNamespace(adapters={}, _handle_message=AsyncMock(return_value="done"))
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=runner,
        config={"enabled": True, "tool_bridge_enabled": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]
    await bridge.stop("test")

    await bridge._handle_tool_call("run_hermes_task", {"prompt": "멈춘 뒤 작업"}, "call_after_stop")

    assert bridge._jobs == {}
    assert runner._handle_message.await_count == 0
    assert session.tool_results[-1] == (
        "call_after_stop",
        {"accepted": False, "message": "Realtime voice is stopping; start a new voice session before launching work."},
    )


@pytest.mark.asyncio
async def test_bridge_stop_during_tool_result_send_does_not_launch_job(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    runner = SimpleNamespace(adapters={}, _handle_message=AsyncMock(return_value="done"))
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=runner,
        config={"enabled": True, "tool_bridge_enabled": True},
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]
    send_started = threading.Event()
    release_send = threading.Event()

    def _blocking_send_tool_result(call_id, output):
        send_started.set()
        release_send.wait(timeout=1.0)
        session.tool_results.append((call_id, output))

    session.send_tool_result = _blocking_send_tool_result

    tool_task = asyncio.create_task(bridge._handle_tool_call("run_hermes_task", {"prompt": "race 작업"}, "call_race"))
    await asyncio.to_thread(send_started.wait, 1.0)
    await bridge.stop("race")
    release_send.set()
    await asyncio.wait_for(tool_task, timeout=0.5)

    assert bridge._jobs == {}
    assert runner._handle_message.await_count == 0


@pytest.mark.asyncio
async def test_bridge_long_running_job_sends_sparse_progress_notice(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.config import Platform
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    started = asyncio.Event()
    release = asyncio.Event()

    async def _run_agent(_event):
        started.set()
        await release.wait()
        return "늦게 끝난 결과입니다."

    runner = SimpleNamespace(adapters={Platform.DISCORD: None}, _handle_message=AsyncMock(side_effect=_run_agent))
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=runner,
        config={
            "enabled": True,
            "tool_bridge_enabled": True,
            "progress_notice_first_seconds": 0.01,
            "progress_notice_interval_seconds": 60,
            "max_progress_notices_per_job": 1,
        },
    )
    await bridge.start()
    session = _StubRealtimeSession.instances[-1]

    await bridge._handle_tool_call("run_hermes_task", {"prompt": "진행 알림 테스트"}, "call_1")
    await asyncio.wait_for(started.wait(), timeout=0.5)
    await asyncio.sleep(0.05)

    assert any("아직 작업 중" in text for text, _create_response in session.conversation_messages)
    release.set()
    await asyncio.wait_for(bridge._jobs["rtjob_1"].task, timeout=0.5)


@pytest.mark.asyncio
async def test_bridge_startup_context_includes_active_jobs_and_recent_voice_results(monkeypatch):
    monkeypatch.setenv("HERMES_DISCORD_REALTIME_KEY", "sk-test")
    monkeypatch.setattr(
        "gateway.voice.discord_realtime_bridge.OpenAIRealtimeVoiceSession",
        _StubRealtimeSession,
    )
    from gateway.config import Platform
    from gateway.voice.discord_realtime_bridge import DiscordRealtimeBridge

    started = asyncio.Event()

    async def _run_agent(_event):
        started.set()
        await asyncio.Event().wait()

    runner = SimpleNamespace(adapters={Platform.DISCORD: None}, _handle_message=AsyncMock(side_effect=_run_agent))
    bridge = DiscordRealtimeBridge(
        guild_id=1,
        controller_user_id="1234",
        voice_client=_FakeVoiceClient(),
        receiver=_FakeReceiver(),
        text_channel=_FakeTextChannel(),
        source_data={},
        runner=runner,
        config={"enabled": True, "tool_bridge_enabled": True},
    )
    await bridge.start()
    await bridge._handle_tool_call("run_hermes_task", {"prompt": "맥락에 들어갈 실행 중 작업"}, "call_1")
    await asyncio.wait_for(started.wait(), timeout=0.5)
    bridge._recent_voice_results.append("rtjob_0 완료: 이전 음성 작업 결과")

    context = bridge._build_startup_context()

    assert "Active Realtime voice tasks" in context
    assert "맥락에 들어갈 실행 중 작업" in context
    assert "Recent Realtime voice task results" in context
    assert "이전 음성 작업 결과" in context
    bridge._jobs["rtjob_1"].task.cancel()
