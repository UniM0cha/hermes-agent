# Codex Stream Progress Timeout Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Codex/GPT-5.x Responses calls should retry after a true provider-silent timeout, but keep waiting longer when the provider has emitted stream activity such as `response.created`, `response.in_progress`, reasoning deltas, text deltas, or tool-call deltas.

**Architecture:** Keep the current worker-thread API-call model, but add a small shared Codex stream activity tracker between `agent/chat_completion_helpers.py` and `agent/codex_runtime.py`. The outer watchdog should use the existing 300s-style stale timeout before the first provider event, then switch to a longer post-event inactivity timeout after any provider event proves the stream is alive. Codex runtime should report status/progress events, not only final text deltas.

**Tech Stack:** Python, OpenAI Responses API streaming, pytest.

---

## Current context from code inspection

- Current branch is `local/solstice`.
- Working tree already has unrelated modified files in `gateway/run.py`, locale files, and `tests/gateway/test_telegram_topic_mode.py`; implementation should avoid touching them unless needed.
- `agent/chat_completion_helpers.py:79-229` runs all non-chat streaming-compatible calls in a worker thread via `interruptible_api_call()`.
- For `agent.api_mode == "codex_responses"`, `interruptible_api_call()` calls `agent._run_codex_stream(...)`, but the outer stale detector still uses `_call_start` and the non-stream message `waiting for non-streaming response`.
- `run_agent.py:881-917` resolves provider/model `stale_timeout_seconds`, env `HERMES_API_CALL_STALE_TIMEOUT`, and default 300s. This is currently used as a wall-clock timeout for Codex.
- `agent/codex_runtime.py:178-331` streams through `active_client.responses.stream(**api_kwargs)` and handles:
  - `response.output_text.delta`
  - `function_call` events
  - `reasoning` + `delta` events
  - `response.output_item.done`
  - terminal `response.incomplete` / `response.failed`
- `agent/codex_runtime.py:335-440` fallback path uses `responses.create(stream=True)` and already sees `response.created` / `response.in_progress` in tests, but treats them only as ignored non-terminal events.
- Existing tests live mainly in:
  - `tests/run_agent/test_run_agent_codex_responses.py`
  - `tests/hermes_cli/test_timeouts.py`
  - `tests/run_agent/test_run_agent.py`

## Desired behavior

1. **No provider event before timeout**
   - If no Responses stream event arrives within the current stale timeout, close the request client and raise timeout so the main retry loop can retry.

2. **Provider is alive**
   - If any real Responses stream event arrives (`response.created`, `response.queued`, `response.in_progress`, `reasoning*delta`, `output_text.delta`, function-call delta, terminal event), mark provider activity.
   - After that, do not abort at the original wall-clock 300s boundary.
   - Instead, use a longer post-event inactivity timeout.

3. **True progress**
   - If reasoning/text/tool-call deltas keep arriving, keep extending the watchdog from the latest event.

4. **One early status event then silence**
   - Treat as “provider was alive, now inactive”; wait longer than 300s, but not forever.
   - Proposed default: `max(initial_stale_timeout * 3, 900.0)` for post-event inactivity.
   - Allow override with env var `HERMES_CODEX_ACTIVE_STALE_TIMEOUT` first; optional provider config can be a later refinement.

5. **User-facing diagnostics**
   - Existing final timeout notice can remain, but message/logs should distinguish:
     - no provider events before timeout
     - provider stream went inactive after last event
   - Activity detail should avoid the misleading `non-streaming` wording for Codex.

---

## Proposed approach

### Core design

Add a tiny activity-state dict in `interruptible_api_call()` only for `codex_responses`:

```python
codex_stream_state = {
    "first_event_at": None,
    "last_event_at": None,
    "last_event_type": None,
    "event_count": 0,
    "last_progress_at": None,
    "last_progress_type": None,
}
```

Pass a callback into `_run_codex_stream()`:

```python
def _on_codex_event(event_type: str, *, progress: bool = False) -> None:
    now = time.time()
    if codex_stream_state["first_event_at"] is None:
        codex_stream_state["first_event_at"] = now
    codex_stream_state["last_event_at"] = now
    codex_stream_state["last_event_type"] = event_type or "unknown"
    codex_stream_state["event_count"] += 1
    if progress:
        codex_stream_state["last_progress_at"] = now
        codex_stream_state["last_progress_type"] = event_type or "unknown"
```

Then the outer watchdog picks the deadline like this:

```python
initial_timeout = agent._compute_non_stream_stale_timeout(api_kwargs.get("messages", []))
active_timeout = max(initial_timeout * 3, 900.0)
# if HERMES_CODEX_ACTIVE_STALE_TIMEOUT is set and > 0, use that instead.

if codex_stream_state["last_event_at"] is None:
    elapsed = now - call_start
    threshold = initial_timeout
    mode = "no_provider_events"
else:
    elapsed = now - codex_stream_state["last_event_at"]
    threshold = active_timeout
    mode = "inactive_after_provider_event"
```

Do not change non-Codex providers in the first implementation.

---

## Step-by-step plan

### Task 1: Add tests for Codex pre-event timeout vs post-event extended wait

**Objective:** Lock down the user-facing behavior before changing code.

**Files:**
- Modify: `tests/run_agent/test_run_agent_codex_responses.py`
- Possibly import: `agent.chat_completion_helpers.interruptible_api_call`

**Tests to add:**

1. `test_codex_interruptible_times_out_when_no_stream_events`
   - Build a minimal fake agent-like object or reuse `_build_agent(monkeypatch)`.
   - Set `agent.api_mode = "codex_responses"`.
   - Monkeypatch `agent._compute_non_stream_stale_timeout` to return a tiny timeout such as `0.05`.
   - Implement `_run_codex_stream` fake that sleeps/blocks without calling any event callback.
   - Assert `TimeoutError` mentions no response / no provider events.
   - Assert request client close was called with `stale_call_kill` or equivalent.

2. `test_codex_interruptible_uses_active_timeout_after_stream_event`
   - Same setup, but fake `_run_codex_stream` calls the new callback with `response.in_progress` before blocking briefly.
   - Set initial timeout tiny (`0.05`) and active timeout bigger via env `HERMES_CODEX_ACTIVE_STALE_TIMEOUT=0.5`.
   - Assert it does **not** time out at the initial threshold.
   - Then let it exceed active threshold and assert timeout happens after active threshold, with last event info in the error/status.

3. `test_codex_interruptible_resets_active_timeout_on_repeated_events`
   - Fake stream calls callback multiple times before active timeout expires.
   - Assert watchdog does not kill until after the last event + active timeout.

**Verification command:**

```bash
venv/bin/python3 -m pytest tests/run_agent/test_run_agent_codex_responses.py -q -o 'addopts='
```

Expected initially: new tests fail because no event callback exists and Codex uses wall-clock stale timeout.

### Task 2: Add Codex stream event callback support

**Objective:** Let runtime report Responses stream activity to the outer watchdog.

**Files:**
- Modify: `agent/codex_runtime.py:178-440`
- Modify: `run_agent.py:2555-2561` thin forwarder signature

**Implementation details:**

1. Change forwarder signature:

```python
def _run_codex_stream(
    self,
    api_kwargs: dict,
    client: Any = None,
    on_first_delta: callable = None,
    on_stream_event: callable = None,
):
```

2. Change `run_codex_stream(...)` signature similarly.

3. Add local helper in `run_codex_stream()`:

```python
def _notify_event(event_type: str, *, progress: bool = False) -> None:
    if on_stream_event is None:
        return
    try:
        on_stream_event(event_type or "unknown", progress=progress)
    except Exception:
        pass
```

4. In the main `for event in stream:` loop:
   - Extract `event_type` before handling.
   - Call `_notify_event(event_type, progress=_is_progress_event(event_type))`.
   - Progress events should include at least:
     - `response.output_text.delta` with non-empty delta
     - any event containing `reasoning` and `delta` with non-empty delta
     - any event containing `function_call`
   - Liveness-only events should include:
     - `response.created`
     - `response.queued`
     - `response.in_progress`
     - `codex.rate_limits`
     - terminal events

5. Pass `on_stream_event` down to `run_codex_create_stream_fallback(...)` as well, and call it in the fallback `for event in stream_or_response:` loop.

**Verification command:**

```bash
venv/bin/python3 -m pytest tests/run_agent/test_run_agent_codex_responses.py::test_run_codex_stream_fallback_parses_create_stream_events -q -o 'addopts='
```

### Task 3: Replace Codex wall-clock stale watchdog with event-aware stale watchdog

**Objective:** Use initial timeout before first event, active timeout after the provider proves the stream is alive.

**Files:**
- Modify: `agent/chat_completion_helpers.py:79-229`

**Implementation details:**

1. In `interruptible_api_call()`, initialize Codex state only when `agent.api_mode == "codex_responses"`.
2. Pass `on_stream_event=_on_codex_stream_event` to `agent._run_codex_stream()`.
3. Add a helper to compute active timeout:

```python
def _codex_active_stale_timeout(initial_timeout: float) -> float:
    raw = os.getenv("HERMES_CODEX_ACTIVE_STALE_TIMEOUT")
    if raw is not None:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    if initial_timeout == float("inf"):
        return float("inf")
    return max(initial_timeout * 3, 900.0)
```

4. In the polling loop:
   - For non-Codex, preserve current behavior exactly.
   - For Codex before first event, use current `_elapsed = now - _call_start` and `_stale_timeout`.
   - For Codex after first event, use `_elapsed = now - last_event_at` and `_active_timeout`.
5. Update heartbeat text:
   - before event: `waiting for Codex stream response ({elapsed}s elapsed, no provider events yet)`
   - after event: `Codex stream active; last event {last_event_type} {elapsed}s ago`
6. Update warning/status text to mention:
   - `No provider events for ...` before first event
   - `Codex stream inactive for ... after last event ...` after first event

**Verification command:**

```bash
venv/bin/python3 -m pytest tests/run_agent/test_run_agent_codex_responses.py -q -o 'addopts='
```

### Task 4: Add focused timeout-helper tests if needed

**Objective:** Keep timeout math and env override stable.

**Files:**
- Modify: `tests/hermes_cli/test_timeouts.py` or `tests/run_agent/test_run_agent_codex_responses.py`

**Tests to add if helper is exposed on `AIAgent`:**

- Default active timeout is `max(initial * 3, 900)`.
- `HERMES_CODEX_ACTIVE_STALE_TIMEOUT` overrides when positive.
- Bad/zero env values fall back to default.
- `float("inf")` remains `inf`.

If helper stays private inside `chat_completion_helpers.py`, cover behavior through `interruptible_api_call()` tests instead of testing private math directly.

### Task 5: Verify no regressions in existing streaming paths

**Objective:** Confirm the change is Codex-scoped and does not alter chat-completions/Anthropic/Bedrock streaming behavior.

**Commands:**

```bash
venv/bin/python3 -m pytest tests/run_agent/test_run_agent_codex_responses.py tests/hermes_cli/test_timeouts.py -q -o 'addopts='
venv/bin/python3 -m pytest tests/run_agent/test_run_agent.py -q -o 'addopts='
```

If `tests/run_agent/test_run_agent.py` is too slow locally, run the focused classes/functions around interruptible API calls first, then optionally full file.

### Task 6: Manual smoke check with debug logging

**Objective:** Confirm the actual local gateway/CLI would show the right diagnostic shape.

**Manual command after implementation:**

```bash
HERMES_CODEX_ACTIVE_STALE_TIMEOUT=30 venv/bin/python3 -m pytest \
  tests/run_agent/test_run_agent_codex_responses.py \
  -q -o 'addopts='
```

Optional runtime smoke test if safe:

```bash
HERMES_CODEX_ACTIVE_STALE_TIMEOUT=30 hermes chat -q "간단히 한 문장만 답해줘" --provider openai-codex -m gpt-5.5
```

Do not restart gateway automatically; 정윤님 prefers to restart Gateway 직접.

---

## Files likely to change

- `agent/chat_completion_helpers.py`
  - Add Codex stream activity state and event-aware watchdog.
  - Update misleading non-streaming activity/status text for Codex.

- `agent/codex_runtime.py`
  - Add `on_stream_event` callback support to main Responses stream and create-stream fallback.
  - Mark status events vs progress deltas.

- `run_agent.py`
  - Update `_run_codex_stream()` forwarder signature.

- `tests/run_agent/test_run_agent_codex_responses.py`
  - Add event-aware timeout tests and callback tests.

- Optional: `tests/hermes_cli/test_timeouts.py`
  - Only if timeout math is exposed through a helper.

## Risks and tradeoffs

- **Hidden thinking is still not observable.** This change only tracks provider stream events. If GPT internally thinks without emitting any SSE event, Hermes still cannot prove progress.
- **Longer post-event waits can delay failure.** A single `response.in_progress` followed by a dead backend would wait up to the active timeout. That is intentional but should be bounded.
- **Thread coordination must stay simple.** Shared state is updated in the stream worker and read in the watchdog thread; dict assignment is enough for this diagnostic state, but avoid complex mutable structures.
- **Avoid duplicate retry after partial text.** If text deltas have already been delivered, existing streaming duplicate-prevention behavior must not be broken.
- **OpenAI SDK event variations.** Use string matching defensively (`"reasoning" in event_type`, `"function_call" in event_type`) and support dict events in fallback.

## Open questions

- Whether to expose `codex_active_stale_timeout_seconds` in `config.yaml` provider/model config now, or keep only `HERMES_CODEX_ACTIVE_STALE_TIMEOUT` for the first patch. Recommendation: env var first to minimize config surface, then add provider config later if needed.
- Exact active default: recommendation is 900s. If that feels too long for Discord, use 600s.

## Definition of done

- A Codex request with zero provider events still times out/retries at the existing stale threshold.
- A Codex request that emits `response.in_progress` before 300s is not killed at 300s.
- A Codex request with repeated reasoning/text/tool events extends the watchdog from the latest event.
- Timeout diagnostics identify whether there were no provider events or an inactive stream after a last event.
- Existing Codex stream fallback tests still pass.
