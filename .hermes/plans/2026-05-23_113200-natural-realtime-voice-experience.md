# Natural Realtime Voice Experience Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Make Discord `/voice rt` feel like one continuous, natural “니코” call: quick voice replies stay fast, real work runs through Hermes/gpt-5.5, and the user hears progress/results without the session feeling split.

**Architecture:** Keep the current hybrid A안. OpenAI Realtime remains the low-latency voice controller, Hermes/gpt-5.5 remains the high-quality worker, and `DiscordRealtimeBridge` becomes a small orchestration layer with job state, progress notices, reconnect/replay, status/cancel tools, and compact context synchronization. Do not replace Hermes with Realtime as the main agent for complex work yet; make the bridge feel unified first.

**Tech Stack:** Python gateway code, OpenAI Realtime WebSocket client, Discord voice bridge, existing Hermes Gateway runner/session system, pytest.

---

## Experience Target

The implementation should optimize for these user-facing behaviors:

1. **Fast casual call feel**
   - Short conversational turns stay on Realtime and answer quickly.
   - Background noise does not trigger unnecessary job starts.

2. **Natural handoff to real work**
   - When work requires tools/files/web/calendar/memory/current facts, Realtime calls `run_hermes_task`.
   - Realtime immediately tells the user the work started and keeps listening.

3. **Progress without spam**
   - For jobs over a short threshold, the user hears compact progress such as “아직 작업 중이에요.”
   - Progress should not interrupt every tool call or flood the voice channel.

4. **Completion comes back into the call**
   - Hermes final result is injected into the Realtime conversation.
   - Realtime speaks a short, natural summary.
   - Text channel still receives the fuller result.

5. **The call survives common failures**
   - If the Realtime WebSocket closes while Hermes work is running, the text result still sends.
   - When Realtime reconnects, pending/finished job notices are replayed compactly.

6. **The user can ask about work**
   - “아까 시킨 거 어디까지 됐어?” → status tool.
   - “그 작업 취소해줘” → cancel tool.

---

## Current Context / Assumptions

- Existing A안 code already does the first non-blocking job handoff:
  - `gateway/voice/discord_realtime_bridge.py`
    - `run_hermes_task` returns `job_id` immediately.
    - Hermes work runs in a background asyncio task.
    - final result is injected back into Realtime via `send_conversation_message(..., create_response=True)`.
  - `gateway/voice/openai_realtime.py`
    - supports `send_conversation_message`.
  - `hermes_cli/config.py`
    - Realtime prompt already says name is `니코`, Korean polite speech, no Hermes self-introduction, job result injection.
- User preference: do **not** restart Hermes Gateway automatically. After code/config changes, report that restart is needed and let the user restart manually.
- Keep `/voice channel` existing local STT/TTS path intact. Realtime remains opt-in through `/voice realtime` / `/voice rt`.
- Do not expose secrets or print `.env` values.

---

## Proposed Approach

Implement a “natural voice orchestration” layer inside the bridge, not a full B안 rewrite.

Key additions:

1. **Structured job state**
   - Track job status, prompt, timestamps, result/error, cancellation, and whether a voice/text notice has been sent.

2. **Small Realtime tool set**
   - Keep `run_hermes_task`.
   - Add `get_hermes_task_status`.
   - Add `cancel_hermes_task`.
   - Optionally add `list_hermes_tasks` if status by vague reference is unreliable.

3. **Voice notice queue**
   - One helper handles all Realtime injections.
   - If Realtime is disconnected, queue the notice and replay on reconnect.

4. **Progress heartbeat**
   - Minimal timed progress updates, not every low-level tool event.
   - Example thresholds: 8s first “작업 중”, then 30s intervals while still running.

5. **Context synchronization**
   - Startup context includes recent Hermes text context, active jobs, and recent voice job results.
   - Job completion updates a compact in-memory voice context tail.

6. **Reconnect/replay skeleton**
   - Detect closed Realtime session.
   - Recreate session using the same instructions builder.
   - Replay compact pending notices and active-job summary.

7. **Prompt/router tuning**
   - Realtime should know what it can answer directly and what must go to Hermes.
   - Avoid pretending the Realtime model has completed Hermes work before the job result arrives.

---

## Files Likely to Change

- Modify: `gateway/voice/discord_realtime_bridge.py`
  - job state dataclasses
  - tool handling for run/status/cancel/list
  - progress heartbeat
  - notice queue
  - reconnect/replay hooks
  - startup context expansion

- Modify: `gateway/voice/openai_realtime.py`
  - expose connection/session state if not already available
  - add optional close/error callback hooks
  - make conversation injection failures observable to bridge

- Modify: `hermes_cli/config.py`
  - Realtime default instructions for natural routing, status/cancel, and progress behavior
  - Realtime tool schema defaults if stored there

- Modify: `tests/gateway/test_discord_realtime_bridge.py`
  - job status/cancel/list tests
  - progress heartbeat tests
  - disconnected notice replay tests
  - startup context active-job tests

- Modify: `tests/gateway/test_discord_realtime_client.py`
  - connection closed/error callback tests if client changes

- Modify: `tests/hermes_cli/test_config.py`
  - prompt assertions for natural routing and status/cancel instructions

- Optional new file: `gateway/voice/realtime_jobs.py`
  - only create if `discord_realtime_bridge.py` becomes too large
  - contains `RealtimeHermesJob`, `RealtimeJobStatus`, and result/notice helpers

---

## Step-by-Step Plan

### Task 1: Define a voice UX contract in tests

**Objective:** Lock the expected call experience before changing behavior.

**Files:**
- Modify: `tests/gateway/test_discord_realtime_bridge.py`

**Step 1: Add test names first**

Add or sketch tests for:

```python
def test_run_hermes_task_returns_job_id_and_keeps_voice_available(): ...
def test_long_running_job_sends_single_progress_notice_after_threshold(): ...
def test_completed_job_injects_voice_summary_and_text_notice(): ...
def test_task_status_tool_reports_running_and_completed_jobs(): ...
def test_cancel_task_tool_cancels_running_job_and_injects_notice(): ...
def test_pending_voice_notice_replays_after_realtime_reconnect(): ...
def test_startup_context_includes_active_jobs_and_recent_voice_results(): ...
```

**Step 2: Run the current focused bridge tests**

Run:

```bash
python -m pytest tests/gateway/test_discord_realtime_bridge.py -q -o 'addopts='
```

Expected:
- Existing tests pass.
- New tests fail until implementation.

---

### Task 2: Introduce structured job state

**Objective:** Replace ad-hoc `_jobs: dict[str, asyncio.Task]` with state that can support status, cancel, progress, reconnect, and replay.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Optional create: `gateway/voice/realtime_jobs.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Implementation shape:**

```python
@dataclass
class RealtimeHermesJob:
    job_id: str
    prompt: str
    task: asyncio.Task | None = None
    status: str = "queued"  # queued|running|completed|failed|cancelled
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    completed_at: float | None = None
    last_progress_at: float | None = None
    result_text: str | None = None
    error_text: str | None = None
    voice_notice_sent: bool = False
    text_notice_sent: bool = False
```

**Verification:**

Run:

```bash
python -m pytest tests/gateway/test_discord_realtime_bridge.py -q -o 'addopts='
python -m py_compile gateway/voice/discord_realtime_bridge.py
```

Expected:
- Existing async job behavior still passes.
- `run_hermes_task` still returns `job_id` immediately.

---

### Task 3: Add a single voice notice helper and pending queue

**Objective:** Make all Realtime injections consistent and safe if the WebSocket is unavailable.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`

**Implementation shape:**

```python
async def _send_voice_notice(self, text: str, *, create_response: bool = True) -> bool:
    if not text.strip():
        return False
    session = self._session
    if session is None or not self._is_realtime_session_usable(session):
        self._pending_voice_notices.append(text)
        return False
    try:
        await session.send_conversation_message(text, create_response=create_response)
        return True
    except Exception:
        self._pending_voice_notices.append(text)
        return False
```

Add a compact replay helper:

```python
async def _replay_pending_voice_notices(self) -> None:
    # replay at most N notices, combine older ones into one summary if needed
```

**Tests:**

- When `send_conversation_message` raises, notice is queued.
- When session becomes usable, queued notice is injected.
- Queue is bounded to avoid unbounded memory.

**Verification:**

Run:

```bash
python -m pytest tests/gateway/test_discord_realtime_bridge.py::test_pending_voice_notice_replays_after_realtime_reconnect -q -o 'addopts='
```

Expected:
- Fails before implementation, passes after.

---

### Task 4: Add status/list/cancel Realtime tools

**Objective:** Let the user naturally ask about running work.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Modify: `hermes_cli/config.py` if default instructions/tool descriptions live there
- Test: `tests/gateway/test_discord_realtime_bridge.py`
- Test: `tests/hermes_cli/test_config.py`

**Tool schemas:**

```python
get_hermes_task_status(job_id: str | None = None)
cancel_hermes_task(job_id: str | None = None)
list_hermes_tasks(status: str | None = None)
```

**Behavior:**

- If `job_id` is provided, operate on that job.
- If no `job_id` and exactly one running job exists, use it.
- If no `job_id` and multiple jobs exist, return a short list and ask which one.
- Cancel should call `job.task.cancel()` and set status to `cancelled`.

**Voice style:**

- Status response should be short:
  - “rtjob_2는 아직 작업 중이에요. 시작한 지 약 25초 됐습니다.”
  - “방금 작업은 완료됐고, 결과를 다시 말씀드릴게요.”
- Avoid exposing internal stack traces by voice. Put detailed errors in logs/text notice if needed.

**Verification:**

Run:

```bash
python -m pytest tests/gateway/test_discord_realtime_bridge.py -q -o 'addopts='
python -m pytest tests/hermes_cli/test_config.py -q -o 'addopts='
```

Expected:
- Tool schemas are present.
- Ambiguous status/cancel cases are handled gracefully.

---

### Task 5: Add minimal progress heartbeat

**Objective:** Avoid the “silent wait” feeling during longer Hermes work without flooding.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Implementation shape:**

- Configurable constants or config values:
  - `first_progress_notice_seconds`: default `8`
  - `progress_notice_interval_seconds`: default `30`
  - `max_progress_notices_per_job`: default `3`
- When a job starts, create a heartbeat task tied to the job.
- Heartbeat stops when job completes/fails/cancels.
- Notice content should be generic until richer progress events exist:
  - “아직 작업 중이에요. 끝나면 바로 말씀드릴게요.”

**Important guardrails:**

- Do not send heartbeat if the job already completed.
- Do not create a Realtime `response.create` while another model response is actively streaming if the client exposes that state.
- If unsure, queue the notice and let the next safe moment speak it.

**Verification:**

Use monkeypatched clock/sleeps in tests; do not make tests wait real 8–30 seconds.

Run:

```bash
python -m pytest tests/gateway/test_discord_realtime_bridge.py::test_long_running_job_sends_single_progress_notice_after_threshold -q -o 'addopts='
```

---

### Task 6: Improve completion result shaping

**Objective:** Make spoken completion sound natural while keeping full detail in text.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Behavior:**

- Text channel gets the fuller Hermes result, as today.
- Realtime gets a compact completion message:

```text
Hermes 작업 rtjob_3이 완료됐습니다. 사용자가 음성으로 듣기 좋게 1~2문장으로 요약해서 말하세요. 원문 결과: ...
```

- Keep raw result length bounded before injecting into Realtime.
- If result is very long, include a short deterministic preview and say details were sent to text channel.

**Implementation helpers:**

```python
def _compact_for_voice(text: str, *, limit: int = 900) -> str: ...
def _build_completion_voice_message(job: RealtimeHermesJob) -> str: ...
def _build_completion_text_notice(job: RealtimeHermesJob) -> str: ...
```

**Verification:**

Run:

```bash
python -m pytest tests/gateway/test_discord_realtime_bridge.py::test_completed_job_injects_voice_summary_and_text_notice -q -o 'addopts='
```

Expected:
- Realtime injection is compact.
- Text notice preserves useful detail.

---

### Task 7: Expand startup context into a bounded “call context packet”

**Objective:** Make new `/voice rt` sessions less amnesic without overloading Realtime prompt size.

**Files:**
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Context packet should include:**

- Recent Hermes text session tail already implemented.
- Active Realtime-launched jobs:
  - job_id
  - status
  - brief prompt preview
  - elapsed time
- Recent completed voice job summaries from this bridge instance.
- Explicit instruction:
  - “This context is background. Do not recite it unless relevant.”

**Bounds:**

- Total call context target: 1,500–2,500 characters.
- Per job prompt preview: 120–200 characters.
- Recent completed jobs: last 3 only.

**Verification:**

Run:

```bash
python -m pytest tests/gateway/test_discord_realtime_bridge.py::test_startup_context_includes_active_jobs_and_recent_voice_results -q -o 'addopts='
```

---

### Task 8: Add Realtime session close/error observation and reconnect skeleton

**Objective:** Keep job completion delivery robust when the Realtime WebSocket drops.

**Files:**
- Modify: `gateway/voice/openai_realtime.py`
- Modify: `gateway/voice/discord_realtime_bridge.py`
- Test: `tests/gateway/test_discord_realtime_client.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Client changes:**

- Expose closed/error state.
- Add optional callbacks:

```python
on_closed: Callable[[Exception | None], Awaitable[None] | None]
on_error: Callable[[Exception], Awaitable[None] | None]
```

**Bridge behavior:**

- On unexpected close:
  - mark session unusable
  - keep Discord audio receiver/source lifecycle intact if possible
  - queue voice notices
  - attempt reconnect with conservative backoff while bridge is active
- On reconnect:
  - rebuild instructions using `_build_realtime_instructions()`
  - replay pending notices
  - inject active job summary

**Guardrails:**

- Do not create infinite tight reconnect loops.
- Do not restart the whole Hermes Gateway.
- If reconnect fails repeatedly, send text-channel notice and continue Hermes jobs.

**Verification:**

Run:

```bash
python -m pytest tests/gateway/test_discord_realtime_client.py -q -o 'addopts='
python -m pytest tests/gateway/test_discord_realtime_bridge.py -q -o 'addopts='
```

---

### Task 9: Tune Realtime prompt for “one 니코” behavior

**Objective:** Make the voice model route naturally and avoid overexplaining architecture.

**Files:**
- Modify: `hermes_cli/config.py`
- Modify: fallback instructions in `gateway/voice/discord_realtime_bridge.py`
- Test: `tests/hermes_cli/test_config.py`
- Test: `tests/gateway/test_discord_realtime_bridge.py`

**Prompt additions:**

Include rules like:

```text
For quick conversational replies, answer directly in 1-2 Korean polite sentences.
For tasks requiring tools, current facts, files, calendar, memory, web, code, or long reasoning, call run_hermes_task.
When a job_id is returned, briefly say the work started and keep listening.
If the user asks about a running job, use get_hermes_task_status or list_hermes_tasks.
If the user asks to stop a running job, use cancel_hermes_task.
Do not claim a Hermes job is complete until its result is injected back into the conversation.
```

**Keep existing persona rules:**

- Name: `니코`
- Korean polite speech
- no 반말
- do not introduce as Hermes/Hermes Agent
- ignore background noise unless addressed

**Verification:**

Run:

```bash
python -m pytest tests/hermes_cli/test_config.py tests/gateway/test_discord_realtime_bridge.py -q -o 'addopts='
```

---

### Task 10: Add a manual E2E QA checklist

**Objective:** Make real Discord testing repeatable after the user restarts Gateway manually.

**Files:**
- Optional create: `docs/dev/realtime-voice-e2e-checklist.md`
- Or add a short checklist comment/reference in the plan only if docs should not be touched.

**Manual scenarios:**

1. Start `/voice mode: rt`.
2. Say “안녕”.
   - Expected: response within a few seconds.
3. Ask “지금 내 최근 대화 기준으로 내가 뭘 작업 중이었지?”
   - Expected: either direct context answer or Hermes job start.
4. Ask for a real task: “방금 말한 내용 텔레그램에 보낼 문장으로 정리해줘.”
   - Expected: job_id start notice, voice remains usable, completion spoken.
5. While job runs, ask “지금 어디까지 됐어?”
   - Expected: status tool response.
6. Start a longer task, then say “취소해줘.”
   - Expected: cancel notice.
7. Simulate Realtime WebSocket drop if practical.
   - Expected: final Hermes text still sends; voice notice replays after reconnect.

**Important:** Do not have the agent run `hermes gateway restart`. Tell the user restart is required after implementation.

---

### Task 11: Run targeted validation bundle

**Objective:** Verify no regression in Realtime, slash commands, and config.

**Files:**
- No code changes expected.

**Commands:**

```bash
python -m py_compile gateway/voice/openai_realtime.py gateway/voice/discord_realtime_bridge.py hermes_cli/config.py
python -m pytest tests/gateway/test_discord_realtime_client.py tests/gateway/test_discord_realtime_bridge.py tests/hermes_cli/test_config.py -q -o 'addopts='
python -m pytest tests/gateway/test_discord_voice_commands.py tests/gateway/test_discord_slash_commands.py -q -o 'addopts='
git diff --check -- gateway/voice/openai_realtime.py gateway/voice/discord_realtime_bridge.py hermes_cli/config.py tests/gateway/test_discord_realtime_client.py tests/gateway/test_discord_realtime_bridge.py tests/hermes_cli/test_config.py
```

**Expected:**

- Targeted tests pass.
- No whitespace errors.
- Existing `/voice channel` behavior remains intact.

---

## Tests / Validation Summary

Minimum before reporting completion:

```bash
python -m py_compile gateway/voice/openai_realtime.py gateway/voice/discord_realtime_bridge.py hermes_cli/config.py
python -m pytest tests/gateway/test_discord_realtime_client.py tests/gateway/test_discord_realtime_bridge.py tests/hermes_cli/test_config.py -q -o 'addopts='
python -m pytest tests/gateway/test_discord_voice_commands.py tests/gateway/test_discord_slash_commands.py -q -o 'addopts='
git diff --check
```

Recommended if time allows:

```bash
python -m pytest tests/gateway -q -o 'addopts='
```

Manual Discord validation requires user-initiated Gateway restart after implementation.

---

## Risks and Tradeoffs

1. **Realtime as full main model is not the immediate target**
   - Full replacement could reduce tool/reasoning quality and complicate safety.
   - Hybrid orchestration gives the best near-term user experience.

2. **Progress can become annoying**
   - Keep progress notices sparse and generic.
   - Prefer user-initiated status checks for details.

3. **Reconnect is best-effort**
   - OpenAI Realtime conversation state may not be fully recoverable.
   - Rebuild from compact Hermes/bridge context instead of pretending the old session survived.

4. **Job cancellation may be cooperative**
   - Cancelling the asyncio task may not stop every underlying tool/process immediately.
   - Report cancellation honestly: “중단 요청을 보냈습니다” if hard cancellation is uncertain.

5. **Prompt size vs context quality**
   - More context improves continuity but can slow startup and confuse the voice model.
   - Keep a strict character budget.

6. **Audio interruptions remain separate**
   - This plan does not change `interrupt_response: true`.
   - If noise still interrupts too often, do a separate barge-in-gate task later.

---

## Open Questions to Decide Before Implementation

These are product choices, not blockers:

1. Progress notice timing:
   - Proposed default: first notice after 8 seconds, then every 30 seconds, max 3 per job.

2. Status tool naming:
   - Keep explicit names (`get_hermes_task_status`, `cancel_hermes_task`) or shorter names (`get_task_status`, `cancel_task`).
   - Explicit names reduce ambiguity inside Realtime tool schemas.

3. Reconnect aggressiveness:
   - Proposed default: 1s, 2s, 5s, 10s backoff, then text notice if still failed.

4. Voice completion length:
   - Proposed default: Realtime speaks 1–2 sentences; full result goes to text channel.

---

## Recommended Implementation Order

1. Job state dataclass and tests.
2. Status/cancel/list tools.
3. Voice notice queue and replay.
4. Progress heartbeat.
5. Completion result shaping.
6. Startup context expansion.
7. Reconnect skeleton.
8. Prompt/router tuning.
9. Targeted tests.
10. User restarts Gateway and runs Discord E2E.

This order keeps each step independently testable and avoids a risky full rewrite.
