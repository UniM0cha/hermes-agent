"""OpenAI Realtime WebSocket client for Discord voice streaming."""

from __future__ import annotations

import base64
import json
import threading
from typing import Any, Callable, Optional
from urllib.parse import urlencode


REALTIME_URL = "wss://api.openai.com/v1/realtime"


def _require_websockets():
    """Import websockets sync connect lazily with a user-actionable error."""
    try:
        from websockets.sync.client import connect as _connect  # type: ignore
    except ImportError as exc:  # pragma: no cover - covered by adjacent Meet tests
        raise RuntimeError(
            "websockets package is required for OpenAI Realtime; install with: pip install websockets"
        ) from exc
    return _connect


class OpenAIRealtimeVoiceSession:
    """Small synchronous client for OpenAI Realtime bidirectional audio."""

    def __init__(
        self,
        api_key: str,
        model: str,
        voice: str = "alloy",
        instructions: str = "",
        tools: list[dict] | None = None,
        turn_detection: dict | None = None,
        reasoning: dict | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.tools = list(tools or [])
        self.turn_detection = turn_detection or {
            "type": "semantic_vad",
            "eagerness": "high",
            "create_response": True,
            "interrupt_response": True,
        }
        self.reasoning = reasoning or {"effort": "low"}
        self._ws: Any = None
        self._send_lock = threading.Lock()
        self._response_active = False
        self._closed = True
        self._handled_tool_call_ids: set[str] = set()
        self._pending_tool_call_arguments: dict[str, dict] = {}

    @property
    def is_connected(self) -> bool:
        """Return whether the underlying Realtime websocket is currently usable."""
        return self._ws is not None and not self._closed

    def connect(self) -> None:
        connect = _require_websockets()
        url = f"{REALTIME_URL}?{urlencode({'model': self.model})}"
        # The GA Realtime API rejects the legacy beta header with
        # beta_api_shape_disabled. Keep the URL at /v1/realtime and send only
        # bearer auth for gpt-realtime-2.
        headers = [
            ("Authorization", f"Bearer {self.api_key}"),
        ]
        try:
            self._ws = connect(url, additional_headers=headers)
        except TypeError:
            self._ws = connect(url, extra_headers=headers)
        self._closed = False

        session_payload: dict[str, Any] = {
            "type": "realtime",
            "model": self.model,
            "instructions": self.instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "turn_detection": self.turn_detection,
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": self.voice,
                },
            },
        }
        if self.reasoning:
            session_payload["reasoning"] = self.reasoning
        if self.tools:
            session_payload["tools"] = self.tools
        self._send_json({"type": "session.update", "session": session_payload})

    def append_audio(self, pcm_24k_mono: bytes) -> None:
        if not pcm_24k_mono:
            return
        self._send_json(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm_24k_mono).decode("ascii"),
            }
        )

    def commit_audio(self) -> None:
        self._send_json({"type": "input_audio_buffer.commit"})
        self._send_json(self._response_create_payload())

    def cancel_response(self) -> bool:
        if self._ws is None or not self._response_active:
            return False
        try:
            self._send_json({"type": "response.cancel"})
            return True
        except Exception:
            return False

    def send_tool_result(self, call_id: str, output: dict) -> None:
        self._send_json(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                },
            }
        )
        self._send_json(self._response_create_payload())

    def send_conversation_message(self, text: str, *, create_response: bool = False) -> None:
        """Inject an application-side text message into the Realtime conversation.

        The bridge uses this for Hermes job completion updates: the Realtime
        model receives the worker result as conversation context and can speak a
        concise follow-up without blocking the live voice session while Hermes
        runs the actual task.
        """
        if not text.strip():
            return
        self._send_json(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        if create_response:
            self._send_json(self._response_create_payload())

    def run_read_loop(
        self,
        on_audio_delta: Callable[[bytes], None],
        on_text_delta: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, dict, str], None]] = None,
        on_input_speech_started: Optional[Callable[[], None]] = None,
        stop_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        if self._ws is None:
            raise RuntimeError("OpenAIRealtimeVoiceSession.connect() must be called first")
        should_stop = stop_fn or (lambda: False)
        while not should_stop():
            try:
                raw = self._recv()
            except Exception:
                self._closed = True
                raise
            if raw is None:
                self._closed = True
                break
            try:
                frame = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            except (TypeError, ValueError):
                continue
            if not isinstance(frame, dict):
                continue
            ftype = frame.get("type")
            if ftype in {"response.created", "response.output_item.added"}:
                self._response_active = True
            elif ftype == "input_audio_buffer.speech_started":
                if on_input_speech_started:
                    on_input_speech_started()
            elif ftype in {"response.done", "response.cancelled"}:
                self._response_active = False
            elif ftype in {"response.audio.delta", "response.output_audio.delta", "audio.delta"}:
                delta = frame.get("delta") or frame.get("audio") or ""
                if delta:
                    try:
                        chunk = base64.b64decode(delta)
                    except (TypeError, ValueError):
                        chunk = b""
                    if chunk:
                        on_audio_delta(chunk)
            elif ftype in {"response.text.delta", "response.output_text.delta", "response.audio_transcript.delta"}:
                if on_text_delta and frame.get("delta"):
                    on_text_delta(str(frame.get("delta")))
            elif ftype == "response.function_call_arguments.done":
                call_id = str(frame.get("call_id") or frame.get("item_id") or "")
                arguments = self._parse_arguments(frame.get("arguments"))
                name = str(frame.get("name") or "")
                if call_id and not name:
                    self._pending_tool_call_arguments[call_id] = arguments
                    continue
                self._emit_tool_call_once(on_tool_call, name, arguments, call_id)
            elif ftype == "response.output_item.done":
                item = frame.get("item") or {}
                if isinstance(item, dict) and item.get("type") == "function_call":
                    call_id = str(item.get("call_id") or "")
                    arguments = self._parse_arguments(item.get("arguments"))
                    if call_id and call_id in self._pending_tool_call_arguments:
                        arguments = self._pending_tool_call_arguments.pop(call_id)
                    self._emit_tool_call_once(
                        on_tool_call,
                        str(item.get("name") or ""),
                        arguments,
                        call_id,
                    )
            elif ftype == "error":
                err = frame.get("error") or frame
                if isinstance(err, dict) and err.get("code") == "response_cancel_not_active":
                    # Barge-in handling may optimistically send response.cancel
                    # when Discord playback still has queued audio but the
                    # provider has already finished the response. This is a
                    # harmless race; keep the read loop alive.
                    self._response_active = False
                    continue
                self._closed = True
                raise RuntimeError(f"realtime error: {err}")

    def close(self) -> None:
        if self._ws is None:
            return
        try:
            self._ws.close()
        finally:
            self._ws = None
            self._closed = True

    @staticmethod
    def _parse_arguments(raw: Any) -> dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except ValueError:
                return {"_raw": raw}
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        return {}

    @staticmethod
    def _response_create_payload() -> dict:
        # GA Realtime accepts one output modality per response.create. Audio
        # responses include transcripts, so spoken follow-ups request audio only.
        return {"type": "response.create", "response": {"output_modalities": ["audio"]}}

    def _emit_tool_call_once(
        self,
        on_tool_call: Optional[Callable[[str, dict, str], None]],
        name: str,
        arguments: dict,
        call_id: str,
    ) -> None:
        if not on_tool_call:
            return
        if not name:
            if call_id:
                self._pending_tool_call_arguments[call_id] = arguments
            return
        dedupe_key = call_id or f"{name}:{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
        if dedupe_key in self._handled_tool_call_ids:
            return
        self._handled_tool_call_ids.add(dedupe_key)
        if call_id:
            self._pending_tool_call_arguments.pop(call_id, None)
        on_tool_call(name, arguments, call_id)

    def _send_json(self, payload: dict) -> None:
        if self._ws is None:
            raise RuntimeError("OpenAIRealtimeVoiceSession.connect() must be called first")
        with self._send_lock:
            try:
                self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception:
                self._closed = True
                raise

    def _recv(self):
        assert self._ws is not None
        return self._ws.recv()
