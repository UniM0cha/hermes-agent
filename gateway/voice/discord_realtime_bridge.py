"""Discord voice channel bridge for OpenAI Realtime audio sessions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import logging
import os
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, Optional

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.voice.openai_realtime import OpenAIRealtimeVoiceSession
from gateway.voice.realtime_audio import (
    RealtimeDiscordAudioSource,
    discord_pcm_to_realtime_pcm,
)

logger = logging.getLogger(__name__)


RUN_HERMES_TASK_TOOL = {
    "type": "function",
    "name": "run_hermes_task",
    "description": (
        "Ask the full Hermes Agent to perform a concrete task asynchronously. Use this for calendar, "
        "files, web, code, messages, or long-running work. The tool returns a job_id immediately; "
        "Hermes will continue in the background and the completed result will be injected back into "
        "this voice conversation for a short spoken follow-up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Self-contained Korean task instruction for Hermes.",
            },
            "speak_summary": {
                "type": "boolean",
                "description": "Whether to speak a short acknowledgement in the voice channel.",
            },
        },
        "required": ["prompt"],
    },
}

GET_HERMES_TASK_STATUS_TOOL = {
    "type": "function",
    "name": "get_hermes_task_status",
    "description": (
        "Check the status of a Realtime-launched Hermes background job. Use this when the user asks "
        "what is still running, where a job is up to, or whether an earlier task finished."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Optional job id such as rtjob_1. If omitted, the bridge will infer the only active job when possible.",
            }
        },
    },
}

CANCEL_HERMES_TASK_TOOL = {
    "type": "function",
    "name": "cancel_hermes_task",
    "description": "Cancel a Realtime-launched Hermes background job when the user asks to stop it.",
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "Optional job id such as rtjob_1. If omitted, the bridge will infer the only active job when possible.",
            }
        },
    },
}

LIST_HERMES_TASKS_TOOL = {
    "type": "function",
    "name": "list_hermes_tasks",
    "description": "List recent Realtime-launched Hermes jobs and their statuses.",
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Optional status filter: queued, running, completed, failed, or cancelled.",
            }
        },
    },
}

REALTIME_HERMES_TOOLS = [
    RUN_HERMES_TASK_TOOL,
    GET_HERMES_TASK_STATUS_TOOL,
    CANCEL_HERMES_TASK_TOOL,
    LIST_HERMES_TASKS_TOOL,
]


@dataclass
class RealtimeHermesJob:
    """State for a Hermes job launched from a Realtime voice session."""

    job_id: str
    prompt: str
    source: SessionSource
    speak_summary: bool = True
    status: str = "queued"
    task: asyncio.Task | None = None
    progress_task: asyncio.Task | None = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    completed_at: float | None = None
    last_progress_at: float | None = None
    result_text: str | None = None
    error_text: str | None = None
    progress_notices_sent: int = 0
    voice_notice_sent: bool = False
    text_notice_sent: bool = False
    cancel_notice_sent: bool = False


class DiscordRealtimeBridge:
    """Own one Discord guild's OpenAI Realtime voice session."""

    def __init__(
        self,
        *,
        guild_id: int,
        controller_user_id: str,
        voice_client,
        receiver,
        text_channel,
        source_data: dict,
        runner,
        config: dict,
    ) -> None:
        self.guild_id = int(guild_id)
        self.controller_user_id = str(controller_user_id)
        self.voice_client = voice_client
        self.receiver = receiver
        self.text_channel = text_channel
        self.source_data = dict(source_data or {})
        self.runner = runner
        self.config = dict(config or {})
        self.audio_source: Optional[RealtimeDiscordAudioSource] = None
        self.session: Optional[OpenAIRealtimeVoiceSession] = None
        self._stop_event = threading.Event()
        self._read_thread: Optional[threading.Thread] = None
        self._silence_thread: Optional[threading.Thread] = None
        self._audio_timing_lock = threading.Lock()
        self._last_real_pcm_at = 0.0
        self._silence_until = 0.0
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopped = False
        self._jobs: dict[str, RealtimeHermesJob] = {}
        self._job_counter = 0
        self._pending_voice_notices: list[str] = []
        self._recent_voice_results: list[str] = []

    @staticmethod
    def resolve_api_key() -> Optional[str]:
        for name in ("HERMES_DISCORD_REALTIME_KEY", "OPENAI_API_KEY", "HERMES_MEET_REALTIME_KEY"):
            value = os.getenv(name, "").strip()
            if value:
                return value
        return None

    def _base_instructions(self) -> str:
        return self.config.get("instructions") or (
            "Your name is 니코. You are the user's AI assistant in a Discord voice channel, "
            "running on the Hermes Agent runtime. Do not introduce yourself as Hermes or Hermes Agent; "
            "use Korean polite speech (존댓말), do not use 반말, and use run_hermes_task for real work. "
            "For quick conversational replies, answer directly in 1-2 short Korean polite sentences. "
            "For tasks requiring tools, current facts, files, calendar, memory, web, code, or long reasoning, call run_hermes_task. "
            "When run_hermes_task returns a job_id, keep the voice conversation available; "
            "Hermes job results will be injected back into this conversation when they finish. "
            "If the user asks about a running job, use get_hermes_task_status or list_hermes_tasks. "
            "If the user asks to stop a running job, use cancel_hermes_task. "
            "Do not claim a Hermes job is complete until its result is injected back into the conversation."
        )

    def _build_realtime_instructions(self) -> str:
        instructions = self._base_instructions()
        context = self._build_startup_context()
        if context:
            instructions = f"{instructions}\n\n{context}"
        return instructions

    def _build_startup_context(self) -> str:
        """Return a compact context note for a fresh or reconnected Realtime session."""
        sections: list[str] = []

        store = getattr(self.runner, "session_store", None)
        if store is not None:
            try:
                source = self._build_task_source()
                entry = store.get_or_create_session(source)
                transcript = store.load_transcript(entry.session_id)
            except Exception:
                transcript = []
            lines: list[str] = []
            for msg in transcript[-8:]:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role") or "").strip().lower()
                if role not in {"user", "assistant"}:
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    content = " ".join(str(part.get("text") or part) if isinstance(part, dict) else str(part) for part in content)
                text = self._compact_text(content, limit=500)
                if not text:
                    continue
                label = "User" if role == "user" else "Assistant"
                lines.append(f"- {label}: {text}")
            if lines:
                sections.append(
                    "Voice session startup context from the existing Hermes text session. "
                    "Use it only as background; do not recite it unless it is relevant.\n"
                    + "\n".join(lines[-6:])
                )

        active_lines = []
        for job in self._jobs.values():
            if job.status not in {"queued", "running"}:
                continue
            active_lines.append(
                f"- {job.job_id}: {job.status}; request={self._compact_text(job.prompt, limit=180)}; "
                f"elapsed={self._job_elapsed_seconds(job)}s"
            )
        if active_lines:
            sections.append(
                "Active Realtime voice tasks. Use get_hermes_task_status if the user asks about them.\n"
                + "\n".join(active_lines[-5:])
            )

        recent = [self._compact_text(item, limit=240) for item in self._recent_voice_results[-3:]]
        recent = [item for item in recent if item]
        if recent:
            sections.append(
                "Recent Realtime voice task results. Use only as background context.\n"
                + "\n".join(f"- {item}" for item in recent)
            )

        return "\n\n".join(sections)[:2500]

    async def start(self) -> tuple[bool, str]:
        if self.config.get("enabled", True) is False:
            return False, "Discord Realtime voice is disabled in config: discord.realtime.enabled=false"
        api_key = self.resolve_api_key()
        if not api_key:
            return (
                False,
                "Missing OpenAI Realtime API key. Set HERMES_DISCORD_REALTIME_KEY in ~/.hermes/.env "
                "(fallbacks: OPENAI_API_KEY, HERMES_MEET_REALTIME_KEY), then restart the gateway.",
            )

        self._loop = asyncio.get_running_loop()
        self._stop_event.clear()
        self.audio_source = RealtimeDiscordAudioSource()
        tools = REALTIME_HERMES_TOOLS if self.config.get("tool_bridge_enabled", True) else []
        instructions = self._build_realtime_instructions()
        self.session = OpenAIRealtimeVoiceSession(
            api_key=api_key,
            model=self.config.get("model") or "gpt-realtime-2",
            voice=self.config.get("voice") or "alloy",
            instructions=instructions,
            tools=tools,
            turn_detection=self._turn_detection_config(),
            reasoning=self._reasoning_config(),
        )

        try:
            await asyncio.to_thread(self.session.connect)
            if hasattr(self.voice_client, "play") and not self._voice_client_is_playing():
                self.voice_client.play(self.audio_source)
            self.receiver.set_pcm_stream_callback(self._on_discord_pcm, buffer_utterances=False)
            self._read_thread = threading.Thread(
                target=self._read_loop_worker,
                name=f"discord-realtime-{self.guild_id}",
                daemon=True,
            )
            self._read_thread.start()
            self._silence_thread = threading.Thread(
                target=self._trailing_silence_worker,
                name=f"discord-realtime-silence-{self.guild_id}",
                daemon=True,
            )
            self._silence_thread.start()
            await self._send_text_notice("Realtime voice started. Use `/voice realtime off` or `/voice leave` to stop.")
            return True, "Realtime voice started."
        except Exception as exc:
            logger.warning("Failed to start Discord realtime bridge: %s", exc, exc_info=True)
            await self.stop("start failed")
            return False, f"Failed to start Realtime voice: {exc}"

    async def stop(self, reason: str = "stopped") -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        try:
            if self.receiver is not None:
                self.receiver.set_pcm_stream_callback(None)
        except Exception:
            logger.debug("Failed to clear realtime PCM callback", exc_info=True)
        if self.audio_source is not None:
            self.audio_source.stop()
        for job in list(self._jobs.values()):
            if job.progress_task and not job.progress_task.done():
                job.progress_task.cancel()
            if job.task and not job.task.done():
                job.status = "cancelled"
                job.task.cancel()
        self._jobs.clear()
        if self.session is not None:
            try:
                await asyncio.to_thread(self.session.close)
            except Exception:
                logger.debug("Failed to close realtime session", exc_info=True)
        if self._read_thread and self._read_thread.is_alive() and threading.current_thread() is not self._read_thread:
            self._read_thread.join(timeout=1.0)
        if self._silence_thread and self._silence_thread.is_alive() and threading.current_thread() is not self._silence_thread:
            self._silence_thread.join(timeout=1.0)
        await self._send_text_notice(f"Realtime voice stopped ({reason}).")

    def _voice_client_is_playing(self) -> bool:
        try:
            return bool(self.voice_client.is_playing())
        except Exception:
            return False

    def _turn_detection_config(self) -> dict:
        """Build the turn detection config used by the Realtime session.

        Discord PCM is appended continuously as packets are decoded. Server
        VAD can use a short silence window, but semantic VAD lets the Realtime
        model decide end-of-turn more naturally than a fixed silence timer.
        """
        raw = self.config.get("turn_detection")
        if isinstance(raw, dict):
            turn_detection = dict(raw)
        else:
            turn_detection = {}
        turn_detection.setdefault("type", "semantic_vad")
        if turn_detection.get("type") == "server_vad":
            turn_detection.setdefault("threshold", 0.5)
            turn_detection.setdefault("prefix_padding_ms", 200)
            turn_detection.setdefault("silence_duration_ms", 350)
        elif turn_detection.get("type") == "semantic_vad":
            turn_detection.setdefault("eagerness", "high")
            turn_detection.setdefault("interrupt_response", True)
        turn_detection.setdefault("create_response", True)
        return turn_detection

    def _reasoning_config(self) -> dict:
        """Build low-latency reasoning settings for realtime voice."""
        raw = self.config.get("reasoning")
        if isinstance(raw, dict):
            reasoning = dict(raw)
        else:
            reasoning = {}
        reasoning.setdefault("effort", "low")
        return reasoning

    def _silence_frame_interval_seconds(self) -> float:
        """Return the cadence for synthetic post-speech silence frames."""
        try:
            frame_ms = float(self.config.get("silence_frame_ms", 100))
        except (TypeError, ValueError):
            frame_ms = 100.0
        return max(0.02, min(frame_ms / 1000.0, 0.5))

    def _trailing_silence_seconds(self) -> float:
        """How long to continue silence after Discord stops sending speech packets."""
        try:
            seconds = float(self.config.get("trailing_silence_seconds", 3.0))
        except (TypeError, ValueError):
            seconds = 3.0
        return max(0.0, min(seconds, 10.0))

    def _mark_real_pcm_appended(self, now: Optional[float] = None) -> None:
        """Record that real Discord audio was appended to Realtime."""
        ts = time.monotonic() if now is None else float(now)
        with self._audio_timing_lock:
            self._last_real_pcm_at = ts
            self._silence_until = ts + self._trailing_silence_seconds()

    def _should_stream_trailing_silence(self, now: Optional[float] = None) -> bool:
        """Return True when Realtime needs synthetic silence after last speech.

        Discord often stops sending RTP audio packets when the user becomes
        silent. OpenAI Realtime turn detection cannot observe that silence if
        we only forward decoded Discord packets, so a short synthetic tail is
        needed to let semantic/server VAD close the turn and create a response.
        """
        ts = time.monotonic() if now is None else float(now)
        with self._audio_timing_lock:
            last_real = self._last_real_pcm_at
            silence_until = self._silence_until
        if last_real <= 0 or ts > silence_until:
            return False
        return (ts - last_real) >= self._silence_frame_interval_seconds()

    def _trailing_silence_worker(self) -> None:
        interval = self._silence_frame_interval_seconds()
        samples = max(1, int(24000 * interval))
        frame = b"\x00\x00" * samples
        warned = False
        while not self._stop_event.wait(interval):
            session = self.session
            if session is None or not self._is_realtime_session_usable() or not self._should_stream_trailing_silence():
                continue
            try:
                session.append_audio(frame)
                warned = False
            except Exception:
                if not warned:
                    logger.warning("Failed to stream trailing silence to Realtime", exc_info=True)
                    warned = True

    def _on_discord_pcm(self, _ssrc: int, user_id: int, pcm_48k_stereo: bytes) -> None:
        session = self.session
        if self._stop_event.is_set() or session is None or not self._is_realtime_session_usable():
            return
        if self.config.get("controller_only", True) and str(user_id) != self.controller_user_id:
            return
        try:
            pcm_24k_mono = discord_pcm_to_realtime_pcm(pcm_48k_stereo)
            if pcm_24k_mono:
                session.append_audio(pcm_24k_mono)
                self._mark_real_pcm_appended()
        except Exception:
            logger.warning("Failed to stream Discord PCM to Realtime", exc_info=True)

    def _on_input_speech_started(self) -> None:
        """Clear queued model audio only after Realtime detects real speech.

        Do not cancel on raw Discord PCM packets: open microphones and ambient
        noise can produce PCM even when the user is not intentionally barging in.
        Let Realtime VAD/semantic VAD decide whether speech actually started.
        """
        if self.audio_source is not None:
            self.audio_source.clear()

    def _on_audio_delta(self, pcm_24k_mono: bytes) -> None:
        if self.audio_source is not None:
            self.audio_source.enqueue_realtime_pcm(pcm_24k_mono)

    def _read_loop_worker(self) -> None:
        error: Exception | None = None
        try:
            if self.session is None:
                return
            self.session.run_read_loop(
                on_audio_delta=self._on_audio_delta,
                on_tool_call=self._on_tool_call_from_thread,
                on_input_speech_started=self._on_input_speech_started,
                stop_fn=self._stop_event.is_set,
            )
        except Exception as exc:
            error = exc
            logger.warning("Discord realtime read loop ended with error: %s", exc, exc_info=True)
        finally:
            if self.session is not None and hasattr(self.session, "_closed"):
                try:
                    self.session._closed = True
                except Exception:
                    pass
            if error is not None and self._loop is not None and not self._stop_event.is_set():
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._send_text_notice("Realtime voice connection dropped. Hermes background jobs will continue and text results will still be sent."),
                        self._loop,
                    )
                except RuntimeError:
                    logger.debug("Failed to schedule realtime disconnect notice", exc_info=True)

    def _on_tool_call_from_thread(self, name: str, arguments: dict, call_id: str) -> None:
        if self._loop is None or self._stopped or self._stop_event.is_set():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._handle_tool_call(name, arguments, call_id),
                self._loop,
            )
        except RuntimeError:
            logger.debug("Failed to schedule realtime tool call; event loop is unavailable", exc_info=True)
            return

        def _log_failure(done_future) -> None:
            try:
                done_future.result()
            except Exception:
                logger.warning("Realtime tool call handler failed", exc_info=True)

        future.add_done_callback(_log_failure)

    def _next_job_id(self) -> str:
        self._job_counter += 1
        return f"rtjob_{self._job_counter}"

    @staticmethod
    def _compact_text(value: Any, *, limit: int = 900) -> str:
        clean = re.sub(r"MEDIA:\s*\S+", "", str(value or ""))
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) > limit:
            return clean[: max(0, limit - 3)].rstrip() + "..."
        return clean

    @staticmethod
    def _active_job(job: RealtimeHermesJob) -> bool:
        return job.status in {"queued", "running"}

    def _job_elapsed_seconds(self, job: RealtimeHermesJob) -> int:
        start = job.started_at or job.created_at
        end = job.completed_at or time.monotonic()
        return max(0, int(end - start))

    def _extract_response_text(self, result: Any) -> str:
        if isinstance(result, dict):
            text = result.get("final_response") or result.get("response") or ""
        elif isinstance(result, str):
            text = result
        else:
            text = ""
        return str(text or "").strip()

    def _voice_result_payload(self, job_id: str, prompt: str, response: str, *, failed: bool = False) -> str:
        clean = self._compact_text(response, limit=900)
        request = self._compact_text(prompt, limit=240)
        status = "failed" if failed else "completed"
        return (
            f"[Hermes task {job_id} {status}]\n"
            f"Original voice request: {request}\n"
            f"Hermes result: {clean or '(no visible text response)'}\n"
            "Tell the user the result in Korean polite speech. Keep it to 1-2 short sentences unless critical details are needed. "
            "If the result is long, say that details were sent to the text channel."
        )

    def _remember_voice_result(self, job: RealtimeHermesJob) -> None:
        if not job.result_text and not job.error_text:
            return
        summary = f"{job.job_id} {job.status}: {self._compact_text(job.result_text or job.error_text, limit=240)}"
        self._recent_voice_results.append(summary)
        del self._recent_voice_results[:-5]

    async def _send_text_result(self, adapter: Any, source: SessionSource, text: str) -> None:
        if not text or adapter is None or not hasattr(adapter, "send"):
            return
        try:
            metadata: dict[str, Any] = {"notify": True}
            if getattr(source, "thread_id", None):
                metadata["thread_id"] = source.thread_id
            result = adapter.send(source.chat_id, text, metadata=metadata)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Failed to send realtime Hermes job text result", exc_info=True)

    def _is_realtime_session_usable(self) -> bool:
        session = self.session
        if session is None:
            return False
        if bool(getattr(session, "closed", False)):
            return False
        connected = getattr(session, "is_connected", None)
        if isinstance(connected, bool):
            return connected
        return True

    def _queue_voice_notice(self, text: str) -> None:
        clean = str(text or "").strip()
        if not clean:
            return
        self._pending_voice_notices.append(clean)
        try:
            max_notices = int(self.config.get("max_pending_voice_notices", 10))
        except (TypeError, ValueError):
            max_notices = 10
        max_notices = max(1, min(max_notices, 50))
        del self._pending_voice_notices[:-max_notices]

    async def _send_voice_notice(self, text: str, *, create_response: bool = True) -> bool:
        if not str(text or "").strip():
            return False
        if not self._is_realtime_session_usable():
            self._queue_voice_notice(text)
            return False
        try:
            sender = getattr(self.session, "send_conversation_message", None)
            if not callable(sender):
                self._queue_voice_notice(text)
                return False
            result = await asyncio.to_thread(sender, text, create_response=create_response)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception:
            self._queue_voice_notice(text)
            logger.warning("Failed to inject notice into Realtime session; queued for replay", exc_info=True)
            return False

    async def _send_tool_result(self, call_id: str, output: dict) -> None:
        session = self.session
        if session is None:
            return
        sender = getattr(session, "send_tool_result", None)
        if not callable(sender):
            return
        try:
            result = await asyncio.to_thread(sender, call_id, output)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Failed to send realtime tool result", exc_info=True)

    async def _replay_pending_voice_notices(self) -> None:
        if not self._pending_voice_notices or not self._is_realtime_session_usable():
            return
        notices = list(self._pending_voice_notices)
        self._pending_voice_notices.clear()
        for index, notice in enumerate(notices):
            if await self._send_voice_notice(notice, create_response=True):
                continue
            remainder = notices[index + 1 :]
            self._pending_voice_notices.extend(remainder)
            break

    def _progress_notice_first_seconds(self) -> float:
        try:
            value = float(self.config.get("progress_notice_first_seconds", 8.0))
        except (TypeError, ValueError):
            value = 8.0
        return max(0.0, min(value, 300.0))

    def _progress_notice_interval_seconds(self) -> float:
        try:
            value = float(self.config.get("progress_notice_interval_seconds", 30.0))
        except (TypeError, ValueError):
            value = 30.0
        return max(0.0, min(value, 600.0))

    def _max_progress_notices_per_job(self) -> int:
        try:
            value = int(self.config.get("max_progress_notices_per_job", 3))
        except (TypeError, ValueError):
            value = 3
        return max(0, min(value, 10))

    async def _progress_notice_loop(self, job_id: str) -> None:
        max_notices = self._max_progress_notices_per_job()
        if max_notices <= 0:
            return
        try:
            await asyncio.sleep(self._progress_notice_first_seconds())
            while not self._stop_event.is_set():
                job = self._jobs.get(job_id)
                if job is None or not self._active_job(job):
                    return
                if not job.speak_summary:
                    return
                if job.progress_notices_sent >= max_notices:
                    return
                await self._send_voice_notice(
                    f"[Hermes task {job_id} progress]\n아직 작업 중이에요. 끝나면 바로 말씀드릴게요.",
                    create_response=True,
                )
                job.progress_notices_sent += 1
                job.last_progress_at = time.monotonic()
                interval = self._progress_notice_interval_seconds()
                if interval <= 0:
                    return
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return

    def _job_payload(self, job: RealtimeHermesJob) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": job.job_id,
            "status": job.status,
            "prompt_preview": self._compact_text(job.prompt, limit=180),
            "elapsed_seconds": self._job_elapsed_seconds(job),
            "progress_notices_sent": job.progress_notices_sent,
        }
        if job.result_text:
            payload["result_preview"] = self._compact_text(job.result_text, limit=300)
        if job.error_text:
            payload["error_preview"] = self._compact_text(job.error_text, limit=300)
        return payload

    def _resolve_job_for_tool(self, arguments: dict) -> tuple[RealtimeHermesJob | None, dict[str, Any] | None]:
        job_id = str((arguments or {}).get("job_id") or "").strip()
        if job_id:
            job = self._jobs.get(job_id)
            if job is None:
                return None, {"accepted": False, "message": f"{job_id} 작업을 찾지 못했습니다."}
            return job, None
        active = [job for job in self._jobs.values() if self._active_job(job)]
        if len(active) == 1:
            return active[0], None
        if not active and len(self._jobs) == 1:
            return next(iter(self._jobs.values())), None
        if not self._jobs:
            return None, {"accepted": False, "message": "현재 추적 중인 Hermes 작업이 없습니다."}
        return None, {
            "accepted": False,
            "message": "어떤 작업인지 job_id가 필요합니다.",
            "jobs": [self._job_payload(job) for job in self._jobs.values()],
        }

    def _job_status_message(self, job: RealtimeHermesJob) -> str:
        if job.status in {"queued", "running"}:
            return f"{job.job_id}는 아직 작업 중이에요. 시작한 지 약 {self._job_elapsed_seconds(job)}초 됐습니다."
        if job.status == "completed":
            return f"{job.job_id} 작업은 완료됐습니다."
        if job.status == "cancelled":
            return f"{job.job_id} 작업은 중단됐습니다."
        if job.status == "failed":
            return f"{job.job_id} 작업은 실패했습니다."
        return f"{job.job_id} 상태는 {job.status}입니다."

    async def _run_hermes_job(self, job_id: str, prompt: str, source: SessionSource, adapter: Any) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.status = "running"
            job.started_at = time.monotonic()
        event = MessageEvent(
            source=source,
            text=f"[Realtime voice task {job_id}]\n{prompt}",
            message_type=MessageType.TEXT,
            raw_message=SimpleNamespace(guild_id=self.guild_id, guild=None),
        )
        try:
            handler = getattr(self.runner, "_handle_message", None)
            if not callable(handler):
                raise RuntimeError("Gateway runner cannot run Hermes tasks directly.")
            result = handler(event)
            if inspect.isawaitable(result):
                result = await result
            response_text = self._extract_response_text(result)
            if not response_text:
                response_text = "Hermes 작업이 끝났지만 표시할 텍스트 응답이 없었습니다. 텍스트 채널을 확인해 주세요."
            if job is not None:
                job.status = "completed"
                job.completed_at = time.monotonic()
                job.result_text = response_text
            if response_text:
                await self._send_text_result(adapter, source, response_text)
                if job is not None:
                    job.text_notice_sent = True
            sent = False
            if job is None or job.speak_summary:
                sent = await self._send_voice_notice(self._voice_result_payload(job_id, prompt, response_text), create_response=True)
            if job is not None:
                job.voice_notice_sent = sent
                self._remember_voice_result(job)
        except asyncio.CancelledError:
            if job is not None:
                job.status = "cancelled"
                job.completed_at = time.monotonic()
                if not job.cancel_notice_sent and job.speak_summary:
                    await self._send_voice_notice(f"[Hermes task {job_id} cancel requested]\n작업 중단을 요청했습니다.", create_response=True)
                    job.cancel_notice_sent = True
            return
        except Exception as exc:
            message = f"Hermes 작업이 실패했습니다: {type(exc).__name__}: {str(exc)[:300]}"
            if job is not None:
                job.status = "failed"
                job.completed_at = time.monotonic()
                job.error_text = message
            if job is None or job.speak_summary:
                await self._send_voice_notice(self._voice_result_payload(job_id, prompt, message, failed=True), create_response=True)
            if job is not None:
                self._remember_voice_result(job)
            logger.warning("Realtime Hermes job %s failed: %s", job_id, exc, exc_info=True)
        finally:
            if job is not None and job.progress_task and not job.progress_task.done():
                job.progress_task.cancel()

    async def _handle_tool_call(self, name: str, arguments: dict, call_id: str) -> None:
        if self.session is None:
            return
        if self._stopped or self._stop_event.is_set():
            await self._send_tool_result(
                call_id,
                {"accepted": False, "message": "Realtime voice is stopping; start a new voice session before launching work."},
            )
            return
        if name == "get_hermes_task_status":
            job, error = self._resolve_job_for_tool(arguments or {})
            if error:
                await self._send_tool_result(call_id, error)
                return
            assert job is not None
            await self._send_tool_result(
                call_id,
                {"accepted": True, "message": self._job_status_message(job), "job": self._job_payload(job)},
            )
            return
        if name == "list_hermes_tasks":
            status_filter = str((arguments or {}).get("status") or "").strip()
            jobs = list(self._jobs.values())
            if status_filter:
                jobs = [job for job in jobs if job.status == status_filter]
            await self._send_tool_result(
                call_id,
                {
                    "accepted": True,
                    "message": f"추적 중인 Hermes 작업은 {len(jobs)}개입니다.",
                    "jobs": [self._job_payload(job) for job in jobs],
                },
            )
            return
        if name == "cancel_hermes_task":
            job, error = self._resolve_job_for_tool(arguments or {})
            if error:
                await self._send_tool_result(call_id, error)
                return
            assert job is not None
            if not self._active_job(job):
                await self._send_tool_result(
                    call_id,
                    {"accepted": False, "message": f"{job.job_id} 작업은 이미 {job.status} 상태입니다.", "job": self._job_payload(job)},
                )
                return
            job.status = "cancelled"
            job.completed_at = time.monotonic()
            job.cancel_notice_sent = True
            if job.progress_task and not job.progress_task.done():
                job.progress_task.cancel()
            if job.task and not job.task.done():
                job.task.cancel()
            await self._send_voice_notice(f"[Hermes task {job.job_id} cancel requested]\n작업 중단을 요청했습니다.", create_response=True)
            await self._send_tool_result(
                call_id,
                {"accepted": True, "message": f"{job.job_id} 작업 중단을 요청했습니다.", "job": self._job_payload(job)},
            )
            return
        if name != "run_hermes_task":
            await self._send_tool_result(call_id, {"accepted": False, "message": "Unknown tool."})
            return
        prompt = str((arguments or {}).get("prompt") or "").strip()
        if not prompt:
            await self._send_tool_result(call_id, {"accepted": False, "message": "prompt is required."})
            return
        adapter = getattr(self.runner, "adapters", {}).get(Platform.DISCORD)
        if not callable(getattr(self.runner, "_handle_message", None)):
            await self._send_tool_result(call_id, {"accepted": False, "message": "Gateway runner cannot run Hermes tasks."})
            return
        job_id = self._next_job_id()
        source = self._build_task_source(job_id=job_id)
        speak_summary = bool((arguments or {}).get("speak_summary", True))
        job = RealtimeHermesJob(job_id=job_id, prompt=prompt, source=source, speak_summary=speak_summary)
        self._jobs[job_id] = job
        await self._send_tool_result(
            call_id,
            {
                "accepted": True,
                "job_id": job_id,
                "message": "Hermes 작업을 백그라운드에서 시작했습니다. 완료되면 음성으로 알려드리겠습니다.",
            },
        )
        if self._stopped or self._stop_event.is_set():
            self._jobs.pop(job_id, None)
            return
        job.task = asyncio.create_task(self._run_hermes_job(job_id, prompt, source, adapter))
        job.progress_task = asyncio.create_task(self._progress_notice_loop(job_id))

    def _build_task_source(self, job_id: str | None = None) -> SessionSource:
        if self.source_data:
            source = SessionSource.from_dict(self.source_data)
        else:
            chat_id = str(getattr(self.text_channel, "id", ""))
            source = SessionSource(platform=Platform.DISCORD, chat_id=chat_id, user_id=self.controller_user_id, chat_type="channel")
        source.platform = Platform.DISCORD
        source.user_id = self.controller_user_id
        source.user_name = self.controller_user_id
        if job_id:
            # Give each background Hermes job its own participant key so
            # multiple voice-launched jobs do not trip the normal gateway
            # single-running-agent guard for the user's live text session.
            source.user_id_alt = f"{self.controller_user_id}:realtime:{job_id}"
        return source

    async def _send_text_notice(self, text: str) -> None:
        if self.text_channel is None or not hasattr(self.text_channel, "send"):
            return
        try:
            result = self.text_channel.send(text)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.debug("Failed to send realtime voice notice", exc_info=True)
