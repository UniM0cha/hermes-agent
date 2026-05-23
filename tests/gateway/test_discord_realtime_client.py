"""Tests for the Discord OpenAI Realtime voice client."""

from __future__ import annotations

import base64
import json
import sys
import types

import pytest


FAKE_OPENAI_REALTIME_KEY = "not-a-real-key"


class _FakeWS:
    def __init__(self, recv_frames: list | None = None):
        self.sent: list[dict] = []
        self._recv_q = list(recv_frames or [])
        self.closed = False

    def send(self, payload):
        if isinstance(payload, (bytes, bytearray)):
            payload = payload.decode()
        self.sent.append(json.loads(payload))

    def recv(self, timeout=None):  # noqa: ARG002
        if not self._recv_q:
            return None
        frame = self._recv_q.pop(0)
        if isinstance(frame, BaseException):
            raise frame
        if isinstance(frame, dict):
            return json.dumps(frame)
        return frame

    def close(self):
        self.closed = True


def _install_fake_websockets(monkeypatch, fake_ws):
    mod_websockets = types.ModuleType("websockets")
    mod_sync = types.ModuleType("websockets.sync")
    mod_sync_client = types.ModuleType("websockets.sync.client")
    captured = {"url": None, "headers": None, "kwargs": None}

    def _connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        captured["headers"] = kwargs.get("additional_headers") or kwargs.get("extra_headers")
        return fake_ws

    mod_sync_client.connect = _connect
    mod_sync.client = mod_sync_client
    mod_websockets.sync = mod_sync
    monkeypatch.setitem(sys.modules, "websockets", mod_websockets)
    monkeypatch.setitem(sys.modules, "websockets.sync", mod_sync)
    monkeypatch.setitem(sys.modules, "websockets.sync.client", mod_sync_client)
    return captured


def test_connect_sends_auth_headers_and_session_update(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS()
    captured = _install_fake_websockets(monkeypatch, ws)
    tools = [{"type": "function", "name": "run_hermes_task", "parameters": {"type": "object"}}]

    session = OpenAIRealtimeVoiceSession(
        api_key="sk-test-secret",
        model="gpt-realtime-2",
        voice="verse",
        instructions="Be concise.",
        tools=tools,
    )
    session.connect()

    assert captured["url"].startswith("wss://api.openai.com/v1/realtime")
    assert "model=gpt-realtime-2" in captured["url"]
    headers = dict(captured["headers"] or [])
    assert headers["Authorization"].startswith("Bearer ")
    assert len(headers["Authorization"]) > len("Bearer ")
    assert "OpenAI-Beta" not in headers

    assert ws.sent[0]["type"] == "session.update"
    realtime_cfg = ws.sent[0]["session"]
    assert realtime_cfg["type"] == "realtime"
    assert realtime_cfg["model"] == "gpt-realtime-2"
    assert realtime_cfg["instructions"] == "Be concise."
    assert realtime_cfg["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert realtime_cfg["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "high",
        "create_response": True,
        "interrupt_response": True,
    }
    assert realtime_cfg["reasoning"] == {"effort": "low"}
    assert realtime_cfg["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert realtime_cfg["audio"]["output"]["voice"] == "verse"
    assert realtime_cfg["tools"] == tools


def test_append_audio_sends_base64_input_buffer_append(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS()
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()

    session.append_audio(b"pcm-bytes")

    frame = ws.sent[-1]
    assert frame["type"] == "input_audio_buffer.append"
    assert base64.b64decode(frame["audio"]) == b"pcm-bytes"


def test_run_read_loop_emits_audio_delta_and_tool_call(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    audio = b"\x01\x02pcm"
    ws = _FakeWS([
        {"type": "response.output_audio.delta", "delta": base64.b64encode(audio).decode()},
        {
            "type": "response.function_call_arguments.done",
            "name": "run_hermes_task",
            "arguments": json.dumps({"prompt": "테스트"}),
            "call_id": "call_1",
        },
        {"type": "response.done"},
    ])
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()
    audio_chunks = []
    tool_calls = []

    session.run_read_loop(
        on_audio_delta=audio_chunks.append,
        on_tool_call=lambda name, arguments, call_id: tool_calls.append((name, arguments, call_id)),
    )

    assert audio_chunks == [audio]
    assert tool_calls == [("run_hermes_task", {"prompt": "테스트"}, "call_1")]


def test_run_read_loop_deduplicates_tool_call_events_by_call_id(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS([
        {
            "type": "response.function_call_arguments.done",
            "name": "run_hermes_task",
            "arguments": json.dumps({"prompt": "중복 방지"}),
            "call_id": "call_1",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "name": "run_hermes_task",
                "arguments": json.dumps({"prompt": "중복 방지"}),
                "call_id": "call_1",
            },
        },
    ])
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()
    tool_calls = []

    session.run_read_loop(
        on_audio_delta=lambda _chunk: None,
        on_tool_call=lambda name, arguments, call_id: tool_calls.append((name, arguments, call_id)),
    )

    assert tool_calls == [("run_hermes_task", {"prompt": "중복 방지"}, "call_1")]


def test_run_read_loop_waits_for_function_call_name_before_dedupe(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS([
        {
            "type": "response.function_call_arguments.done",
            "arguments": json.dumps({"prompt": "이름 없는 arguments 이벤트"}),
            "call_id": "call_1",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "name": "run_hermes_task",
                "arguments": json.dumps({"prompt": "이름 없는 arguments 이벤트"}),
                "call_id": "call_1",
            },
        },
    ])
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()
    tool_calls = []

    session.run_read_loop(
        on_audio_delta=lambda _chunk: None,
        on_tool_call=lambda name, arguments, call_id: tool_calls.append((name, arguments, call_id)),
    )

    assert tool_calls == [("run_hermes_task", {"prompt": "이름 없는 arguments 이벤트"}, "call_1")]


def test_run_read_loop_reports_input_speech_started(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS([
        {"type": "input_audio_buffer.speech_started"},
        {"type": "response.done"},
    ])
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()
    events = []

    session.run_read_loop(
        on_audio_delta=lambda _chunk: None,
        on_input_speech_started=lambda: events.append("speech_started"),
    )

    assert events == ["speech_started"]


def test_run_read_loop_ignores_cancel_not_active_race(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    audio = b"ok"
    ws = _FakeWS([
        {
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "code": "response_cancel_not_active",
                "message": "Cancellation failed: no active response found",
            },
        },
        {"type": "response.output_audio.delta", "delta": base64.b64encode(audio).decode()},
    ])
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()
    audio_chunks = []

    session.run_read_loop(on_audio_delta=audio_chunks.append)

    assert audio_chunks == [audio]


def test_cancel_response_only_sends_when_response_is_active(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS()
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()

    assert session.cancel_response() is False
    assert all(frame["type"] != "response.cancel" for frame in ws.sent)

    session._response_active = True
    assert session.cancel_response() is True
    assert ws.sent[-1] == {"type": "response.cancel"}


def test_send_tool_result_sends_output_then_response_create(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS()
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()

    session.send_tool_result("call_1", {"accepted": True})

    assert ws.sent[-2] == {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": json.dumps({"accepted": True}, ensure_ascii=False),
        },
    }
    assert ws.sent[-1] == {"type": "response.create", "response": {"output_modalities": ["audio"]}}


def test_send_conversation_message_injects_text_and_can_create_response(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS()
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()

    session.send_conversation_message("Hermes 작업 결과입니다.", create_response=True)

    assert ws.sent[-2] == {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Hermes 작업 결과입니다."}],
        },
    }
    assert ws.sent[-1] == {"type": "response.create", "response": {"output_modalities": ["audio"]}}


def test_realtime_session_exposes_connection_state(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS()
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")

    assert session.is_connected is False
    session.connect()
    assert session.is_connected is True
    session.close()
    assert session.is_connected is False
    assert ws.closed is True


def test_error_frame_raises_runtime_error(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS([{"type": "error", "error": {"message": "bad realtime"}}])
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()

    with pytest.raises(RuntimeError, match="bad realtime"):
        session.run_read_loop(on_audio_delta=lambda _chunk: None)
    assert session.is_connected is False


def test_recv_exception_marks_session_disconnected(monkeypatch):
    from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession

    ws = _FakeWS([RuntimeError("socket closed")])
    _install_fake_websockets(monkeypatch, ws)
    session = OpenAIRealtimeVoiceSession(api_key=FAKE_OPENAI_REALTIME_KEY, model="gpt-realtime-2")
    session.connect()

    with pytest.raises(RuntimeError, match="socket closed"):
        session.run_read_loop(on_audio_delta=lambda _chunk: None)
    assert session.is_connected is False
