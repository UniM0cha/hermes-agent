# Discord Voice + OpenAI Realtime Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Discord 서버 음성채널에서 Hermes 봇이 `gpt-realtime-2`와 저지연 음성 대화를 하고, 필요하면 안전한 단일 Hermes 작업 브리지를 통해 실제 작업을 텍스트 채널에 실행하도록 만든다.

**Architecture:** 기존 Discord voice 기능은 유지한다. 새 `/voice realtime` 모드는 기존 STT→Hermes→TTS 경로를 우회하고, Discord PCM 스트림을 OpenAI Realtime WebSocket으로 보내며, Realtime 오디오 델타를 Discord `AudioSource`로 재생한다. 작업 실행은 Realtime 모델에게 모든 Hermes 도구를 열지 않고 `run_hermes_task` 단일 함수만 제공한다.

**Tech Stack:** `discord.py` voice, PyNaCl/Opus, `websockets`, OpenAI Realtime WebSocket, PCM16 audio conversion, pytest fake WebSocket tests.

**Current verified code facts:**
- Discord voice receive/playback exists in `gateway/platforms/discord.py`.
- Existing voice join command path is `GatewayRunner._handle_voice_command()` → `_handle_voice_channel_join()` in `gateway/run.py`.
- Current Discord voice input path is RTP/Opus decode → utterance buffer → WAV → STT → synthetic `MessageEvent`.
- Existing OpenAI Realtime client lives at `plugins/google_meet/realtime/openai_client.py` and currently supports text-in/audio-out.
- Existing Realtime tests use fake WebSocket in `tests/plugins/test_google_meet_realtime.py`.
- API key must come from env, not config or chat logs. Preferred lookup order for this feature: `HERMES_DISCORD_REALTIME_KEY`, then `OPENAI_API_KEY`, then `HERMES_MEET_REALTIME_KEY`.

**Non-goals for MVP:**
- Discord DM calls.
- Multi-speaker diarized conversation.
- Exposing all Hermes tools directly to Realtime.
- Perfect barge-in cancellation in the first pass; add it after stable stream playback.

---

## Milestone 0: Baseline and safety setup

### Task 0.1: Add explicit `/voice realtime` command metadata

**Objective:** Make `/voice realtime` visible in command hints without changing runtime behavior yet.

**Files:**
- Modify: `hermes_cli/commands.py:154-155`
- Test: existing slash/commands tests if any command hint snapshots fail

**Steps:**
1. Write a failing test or update an existing command registry test to assert `voice` supports `realtime` and `rt` subcommands.
2. Run the focused test:
   ```bash
   cd /Users/solstice/.hermes/hermes-agent
   venv/bin/python3 -m pytest tests/hermes_cli -q -o 'addopts='
   ```
3. Change:
   ```python
   CommandDef("voice", "Toggle voice mode", "Configuration",
              args_hint="[on|off|tts|status|channel|leave|realtime]",
              subcommands=("on", "off", "tts", "status", "channel", "join", "leave", "realtime", "rt")),
   ```
4. Re-run the focused test.
5. Do not commit unless the user explicitly asks.

### Task 0.2: Add Discord realtime config defaults

**Objective:** Add non-secret runtime knobs under `discord.realtime`.

**Files:**
- Modify: `hermes_cli/config.py:1349-1385`
- Test: `tests/hermes_cli/test_config.py` or nearest config-default test

**Config shape:**
```python
"realtime": {
    "enabled": True,
    "model": "gpt-realtime-2",
    "voice": "alloy",
    "instructions": "You are Hermes Agent in a Discord voice channel. Be concise, confirm before risky actions, and use run_hermes_task for real work.",
    "input_sample_rate": 24000,
    "output_sample_rate": 24000,
    "controller_only": True,
    "tool_bridge_enabled": True,
    "max_session_seconds": 1800,
}
```

**Verification:** Config loading preserves existing `discord` settings and adds defaults only when absent.

---

## Milestone 1: Audio primitives with tests

### Task 1.1: Create shared audio conversion helpers

**Objective:** Convert between Discord PCM and OpenAI Realtime PCM in one isolated module.

**Files:**
- Create: `gateway/voice/realtime_audio.py`
- Test: `tests/gateway/test_discord_realtime_audio.py`

**Public API:**
```python
DISCORD_RATE = 48000
DISCORD_CHANNELS = 2
OPENAI_RATE = 24000
OPENAI_CHANNELS = 1
DISCORD_FRAME_BYTES = 3840  # 20ms * 48000 * 2 channels * 2 bytes

def discord_pcm_to_realtime_pcm(pcm_48k_stereo: bytes) -> bytes: ...
def realtime_pcm_to_discord_pcm(pcm_24k_mono: bytes) -> bytes: ...
def split_discord_frames(pcm_48k_stereo: bytes) -> tuple[list[bytes], bytes]: ...
```

**Implementation note:** Use stdlib `audioop` for MVP behind this module only. It is available in the current Python 3.11 runtime. If Hermes later moves to Python 3.13, replace this module internally without touching callers.

**Tests:**
- Empty bytes return empty bytes.
- 20ms of 48k stereo input converts to about 20ms of 24k mono.
- 20ms of 24k mono output converts to one 3840-byte Discord frame.
- `split_discord_frames()` returns full 3840-byte frames plus a remainder.

**Commands:**
```bash
cd /Users/solstice/.hermes/hermes-agent
venv/bin/python3 -m pytest tests/gateway/test_discord_realtime_audio.py -q -o 'addopts='
```

### Task 1.2: Create a streaming Discord AudioSource

**Objective:** Provide a non-file audio source that Discord can keep playing while Realtime audio chunks arrive.

**Files:**
- Modify: `gateway/voice/realtime_audio.py`
- Test: `tests/gateway/test_discord_realtime_audio.py`

**Public API:**
```python
class RealtimeDiscordAudioSource(discord.AudioSource):
    def enqueue_realtime_pcm(self, pcm_24k_mono: bytes) -> None: ...
    def read(self) -> bytes: ...  # always returns 20ms Discord PCM, silence if no audio queued
    def is_opus(self) -> bool: return False
    def stop(self) -> None: ...
```

**Tests:**
- `read()` returns exactly 3840 bytes.
- With no queued audio, `read()` returns silence.
- Queued OpenAI PCM is converted and emitted in order.
- After `stop()`, `read()` returns `b""` so Discord playback can end cleanly.

---

## Milestone 2: Realtime WebSocket client for streaming audio

### Task 2.1: Extract or create a gateway-safe Realtime client

**Objective:** Avoid coupling Discord gateway code to the Google Meet plugin path.

**Files:**
- Create: `gateway/voice/openai_realtime.py`
- Test: `tests/gateway/test_discord_realtime_client.py`
- Optionally keep: `plugins/google_meet/realtime/openai_client.py` unchanged for now to reduce regression risk

**Public API:**
```python
class OpenAIRealtimeVoiceSession:
    def __init__(self, api_key: str, model: str, voice: str, instructions: str, tools: list[dict] | None = None): ...
    def connect(self) -> None: ...
    def append_audio(self, pcm_24k_mono: bytes) -> None: ...
    def commit_audio(self) -> None: ...  # fallback/manual turn commit
    def cancel_response(self) -> bool: ...
    def close(self) -> None: ...
    def run_read_loop(self, on_audio_delta, on_text_delta=None, on_tool_call=None, stop_fn=None) -> None: ...
```

**Session update frame:**
```json
{
  "type": "session.update",
  "session": {
    "modalities": ["audio", "text"],
    "voice": "alloy",
    "instructions": "...",
    "input_audio_format": "pcm16",
    "output_audio_format": "pcm16",
    "turn_detection": {
      "type": "server_vad",
      "threshold": 0.5,
      "prefix_padding_ms": 300,
      "silence_duration_ms": 700,
      "create_response": true
    },
    "tools": [...]
  }
}
```

**Tests with fake WebSocket:**
- `connect()` sends Authorization and Realtime beta headers without logging the key.
- `append_audio()` sends `input_audio_buffer.append` with base64 audio.
- `run_read_loop()` emits decoded bytes for `response.audio.delta`.
- `run_read_loop()` detects `response.function_call_arguments.done` and calls `on_tool_call(name, arguments, call_id)`.
- Error frames raise or call a controlled error callback.

### Task 2.2: Implement safe Realtime tool result sending

**Objective:** Let the bridge send function-call results back to Realtime without granting broad tool access.

**Files:**
- Modify: `gateway/voice/openai_realtime.py`
- Test: `tests/gateway/test_discord_realtime_client.py`

**Public API:**
```python
def send_tool_result(self, call_id: str, output: dict) -> None: ...
```

**Frame shape:**
```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "function_call_output",
    "call_id": "...",
    "output": "{...json...}"
  }
}
```

Then send:
```json
{"type": "response.create", "response": {"modalities": ["audio", "text"]}}
```

**Tests:** verify both frames are sent in order.

---

## Milestone 3: Tap Discord decoded PCM before STT

### Task 3.1: Add optional PCM streaming callback to VoiceReceiver

**Objective:** Reuse existing RTP/Opus decode path but stream PCM chunks before utterance buffering/STT.

**Files:**
- Modify: `gateway/platforms/discord.py:152-502`
- Test: `tests/gateway/test_voice_command.py` or `tests/gateway/test_discord_realtime_bridge.py`

**API:**
```python
class VoiceReceiver:
    def set_pcm_stream_callback(self, callback: Callable[[int, int, bytes], None] | None, *, buffer_utterances: bool = True) -> None: ...
```

**Behavior:**
- Inside `_on_packet()`, after Opus decode and user inference:
  - call callback with `(ssrc, user_id, pcm)` when user is known or inferable.
  - if `buffer_utterances=False`, do not append to `_buffers`; this prevents duplicate STT processing and unbounded buffer growth in realtime mode.
- Existing default behavior remains unchanged.

**Tests:**
- With no callback, existing buffering behavior is unchanged.
- With callback and `buffer_utterances=False`, PCM reaches callback and `_buffers` does not grow.
- Callback exceptions are logged and do not kill packet processing.

### Task 3.2: Teach Discord listen loop to stay alive without STT in realtime mode

**Objective:** Keep UDP keepalive and receiver lifecycle working while realtime mode bypasses local STT.

**Files:**
- Modify: `gateway/platforms/discord.py:2115-2150`
- Test: `tests/gateway/test_voice_command.py`

**Behavior:**
- If guild has an active realtime bridge, `_voice_listen_loop()` still sends keepalive.
- It does not call `_process_voice_input()` for buffered utterances while realtime streaming is active.
- On realtime stop, normal STT/TTS mode can resume if `/voice channel` is still active.

---

## Milestone 4: Discord realtime bridge lifecycle

### Task 4.1: Create DiscordRealtimeBridge

**Objective:** Own one realtime session per guild and connect Discord input/output to OpenAI.

**Files:**
- Create: `gateway/voice/discord_realtime_bridge.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Constructor:**
```python
class DiscordRealtimeBridge:
    def __init__(self, *, guild_id: int, controller_user_id: str, voice_client, receiver: VoiceReceiver, text_channel, source: SessionSource, runner, config: dict): ...
```

**Responsibilities:**
- Resolve API key from `HERMES_DISCORD_REALTIME_KEY`, `OPENAI_API_KEY`, `HERMES_MEET_REALTIME_KEY`.
- Create `RealtimeDiscordAudioSource` and start `voice_client.play(source)` if not already playing.
- Create `OpenAIRealtimeVoiceSession`.
- Set `receiver.set_pcm_stream_callback(..., buffer_utterances=False)`.
- Convert Discord PCM chunks to Realtime PCM and call `session.append_audio()`.
- Convert Realtime `response.audio.delta` bytes into Discord source queue.
- Send short lifecycle notices to the bound text channel.
- Stop cleanly on `/voice leave`, timeout, or gateway shutdown.

**Tests:**
- Missing API key returns a user-actionable error and does not start playback.
- Start wires receiver callback with `buffer_utterances=False`.
- Incoming PCM calls `append_audio()` with converted bytes.
- Outgoing audio delta enqueues into `RealtimeDiscordAudioSource`.
- Stop clears receiver callback, stops audio source, closes session.

### Task 4.2: Add bridge dictionaries and adapter methods

**Objective:** Let `GatewayRunner` start/stop realtime mode through the Discord adapter.

**Files:**
- Modify: `gateway/platforms/discord.py:553-575`
- Modify: `gateway/platforms/discord.py:1888-2038`
- Test: `tests/gateway/test_voice_command.py`

**New adapter state:**
```python
self._realtime_bridges: Dict[int, DiscordRealtimeBridge] = {}
```

**New adapter methods:**
```python
async def start_realtime_voice(self, guild_id: int, controller_user_id: str, text_channel_id: int, source_data: dict, runner) -> tuple[bool, str]: ...
async def stop_realtime_voice(self, guild_id: int) -> None: ...
def is_realtime_voice(self, guild_id: int) -> bool: ...
```

**Lifecycle integration:**
- `leave_voice_channel()` calls `stop_realtime_voice()` before disconnecting.
- Inactivity timeout stops realtime bridge too.
- Starting realtime twice in the same guild returns an “already active” message or restarts cleanly.

---

## Milestone 5: Gateway command UX

### Task 5.1: Implement `/voice realtime`

**Objective:** Start Realtime mode from a Discord text channel bound to the user’s current voice channel.

**Files:**
- Modify: `gateway/run.py:10686-10828`
- Test: `tests/gateway/test_voice_command.py`

**Behavior:**
- `/voice realtime` and `/voice rt`:
  - Only works in Discord server context.
  - If bot is not in user’s voice channel, join it using existing `join_voice_channel()` path.
  - Save text-channel binding in `adapter._voice_text_channels[guild_id]`.
  - Save source metadata in `adapter._voice_sources[guild_id]`.
  - Start realtime bridge.
  - Set `runner._voice_mode["discord:<chat_id>"] = "realtime"`.
  - Disable normal auto-TTS for that chat.
- `/voice realtime off`:
  - Stop bridge and set mode back to `all` if still connected, or `off` if not connected.
- `/voice leave`:
  - Stops bridge and disconnects.
- `/voice status`:
  - Shows current mode as `realtime` and current voice channel participants.

**Tests:**
- Realtime unsupported on non-Discord adapter returns clean message.
- Missing guild returns Discord server message.
- User not in VC returns existing “need to be in a voice channel” message.
- Successful start calls `start_realtime_voice()` and sets mode `realtime`.
- Missing API key from adapter start bubbles a safe message; no key value is shown.
- `/voice leave` stops realtime.

### Task 5.2: Prevent normal TTS/STT duplicate responses in realtime mode

**Objective:** Ensure Realtime mode does not also run the normal Hermes voice pipeline for every utterance.

**Files:**
- Modify: `gateway/run.py:10949-11001`
- Modify: `gateway/platforms/discord.py:2115-2150`
- Test: `tests/gateway/test_voice_command.py`

**Behavior:**
- `_should_send_voice_reply()` returns `False` when mode is `realtime`.
- Realtime voice input does not call `_handle_voice_channel_input()` unless invoked by the explicit `run_hermes_task` tool bridge.

---

## Milestone 6: Safe Hermes task bridge

### Task 6.1: Define a single Realtime tool schema

**Objective:** Allow voice conversation to request real Hermes work without exposing arbitrary tools to Realtime.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Tool schema:**
```json
{
  "type": "function",
  "name": "run_hermes_task",
  "description": "Ask the full Hermes Agent to perform a concrete task. Use this for calendar, files, web, code, messages, or long-running work. The result will be posted in the bound Discord text channel.",
  "parameters": {
    "type": "object",
    "properties": {
      "prompt": {"type": "string", "description": "Self-contained Korean task instruction for Hermes."},
      "speak_summary": {"type": "boolean", "description": "Whether to speak a short acknowledgement in the voice channel."}
    },
    "required": ["prompt"]
  }
}
```

**Policy in instructions:**
- Ask for confirmation before destructive or external side-effect actions.
- For ambiguous tasks, ask a short clarifying question by voice.
- For actual Hermes work, call `run_hermes_task` with a self-contained Korean prompt.

### Task 6.2: Route `run_hermes_task` to normal Hermes gateway pipeline

**Objective:** Make Realtime tool calls create normal Hermes tasks in the bound Discord text channel.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Add helper in: `gateway/run.py` if needed, e.g. `_create_realtime_task_event(...)`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Behavior:**
- On tool call, build a synthetic `MessageEvent`:
  - `platform=Platform.DISCORD`
  - `chat_id=<bound text channel>`
  - `user_id=<controller_user_id>`
  - `message_type=MessageType.TEXT`
  - `text=f"[Realtime voice task]\n{prompt}"`
- Schedule `adapter.handle_message(event)` with `asyncio.create_task()` so the audio loop does not block.
- Return tool output to Realtime immediately:
  ```json
  {"accepted": true, "message": "Hermes 작업을 텍스트 채널에 시작했습니다."}
  ```
- If the chat is already busy, respect the existing gateway busy/queue behavior instead of bypassing it.

**Tests:**
- Tool call creates a synthetic event with the bound source metadata.
- Tool output is sent back to Realtime.
- Unauthorized/non-controller tool calls are rejected.

---

## Milestone 7: Barge-in and turn quality

### Task 7.1: Cancel model speech when the controller starts speaking

**Objective:** Reduce “AI keeps talking over me” behavior.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Modify: `gateway/platforms/discord.py` if speaking state events are needed
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Behavior:**
- When PCM arrives from the controller while output audio queue is non-empty:
  - call `session.cancel_response()`.
  - clear pending audio frames in `RealtimeDiscordAudioSource`.
- Debounce to avoid repeated cancel calls on every packet.

**Tests:**
- Incoming controller PCM during playback calls cancel once.
- Non-controller PCM does not cancel when `controller_only=True`.

### Task 7.2: Add text-channel transcript breadcrumbs

**Objective:** Keep auditability without flooding Discord.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Behavior:**
- Post lifecycle messages:
  - Realtime started.
  - Hermes task accepted.
  - Realtime stopped with reason.
- Do not post every audio transcript by default. If OpenAI emits final transcripts, keep them debug-log-only unless `discord.realtime.post_transcripts` is later added.

---

## Milestone 8: Integration and manual verification

### Task 8.1: Focused automated test run

**Objective:** Verify new code and adjacent voice tests.

**Command:**
```bash
cd /Users/solstice/.hermes/hermes-agent
venv/bin/python3 -m pytest \
  tests/gateway/test_discord_realtime_audio.py \
  tests/gateway/test_discord_realtime_client.py \
  tests/gateway/test_discord_realtime_bridge.py \
  tests/gateway/test_voice_command.py \
  tests/plugins/test_google_meet_realtime.py \
  -q -o 'addopts='
```

**Expected:** all pass.

### Task 8.2: Gateway restart and live Discord test

**Objective:** Prove the feature works in the user’s real Discord server.

**Prerequisites:**
- API key stored in `~/.hermes/.env`, not pasted into chat:
  ```bash
  HERMES_DISCORD_REALTIME_KEY=sk-...
  ```
- Gateway restarted after env change:
  ```bash
  hermes gateway restart
  ```

**Manual test script:**
1. Join a Discord server voice channel.
2. In the linked text channel, send:
   ```text
   /voice realtime
   ```
3. Say: “니코, 들리면 짧게 대답해줘.”
4. Confirm spoken response latency is acceptable.
5. Say: “지금 이 텍스트 채널에 테스트 메시지 하나 남기는 작업을 Hermes한테 시켜줘.”
6. Confirm Realtime acknowledges by voice and Hermes posts the result in text channel.
7. Send:
   ```text
   /voice leave
   ```
8. Confirm bridge closes, bot leaves, and no background process keeps playing silence.

### Task 8.3: Failure-mode manual checks

**Objective:** Make sure common failures are safe and understandable.

**Cases:**
- No API key: `/voice realtime` returns an install/config hint and does not crash.
- OpenAI connection error: bridge stops and text channel gets a short error.
- User leaves voice channel: inactivity timeout or leave cleanup stops Realtime.
- Normal `/voice channel` still works after stopping realtime.
- No API key value appears in logs or user-facing messages.

---

## Suggested implementation order

1. Milestone 1 audio primitives.
2. Milestone 2 Realtime client.
3. Milestone 3 VoiceReceiver PCM stream tap.
4. Milestone 4 bridge lifecycle.
5. Milestone 5 `/voice realtime` UX.
6. Milestone 6 safe `run_hermes_task` bridge.
7. Milestone 7 barge-in polish.
8. Milestone 8 live test.

## Rollback strategy

- Existing `/voice channel` path remains intact.
- Realtime code is isolated under `gateway/voice/` and only activates for mode `realtime`.
- If live testing fails, disable by not setting `HERMES_DISCORD_REALTIME_KEY` or by setting `discord.realtime.enabled: false`.
- `/voice leave` must always stop both standard voice and realtime bridge.

## Final acceptance criteria

- `/voice channel` existing STT/TTS mode still works.
- `/voice realtime` starts a Realtime session from a Discord text channel while the user is in a voice channel.
- User speech reaches `gpt-realtime-2` without local Whisper STT.
- Realtime audio response is heard in the same Discord voice channel.
- `run_hermes_task` can start a normal Hermes job in the bound text channel.
- No broad Hermes tool access is exposed directly to Realtime.
- API key is loaded only from env and is never logged.
- Focused tests pass.
