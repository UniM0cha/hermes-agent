# Discord Progress Bubble Parity Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task. Follow the user's preferred self-verification loop: TDD → implementation → targeted tests/compile/diff checks → 3-subagent code review → triage/fix → focused re-review if needed. Do not restart the gateway; report that restart is required after code changes.

**Goal:** Make Discord gateway tool-progress/status bubbles behave closer to Telegram: edit the current progress bubble in place, route edits/deletes correctly inside Discord threads, and clean up temporary progress messages after the final response when `cleanup_progress` is enabled.

**Architecture:** This is not a Realtime voice change. The fix belongs in the messaging gateway progress pipeline and Discord platform adapter. Extend Discord's message lifecycle support to match the generic gateway contract: send/edit/delete must use the same destination channel or thread, and cleanup must remember the per-message routing metadata instead of only `message_id`.

**Tech Stack:** Python, `discord.py[voice]==2.7.1`, Hermes gateway adapters, pytest/pytest-asyncio.

---

## Pre-investigation Findings

### User-visible symptom

Telegram shows tool-progress as one message that gets edited/cleaned up. Discord often leaves multiple new progress/status messages.

### Current local config

From `~/.hermes/config.yaml`:

```yaml
display:
  streaming: true
  tool_progress: all
  cleanup_progress: true
  tool_preview_length: 0
  platforms: {}
```

Implications:

- Tool progress is enabled globally.
- Cleanup is enabled globally.
- No Discord-specific compacting override exists.
- Tool preview length is unlimited, which makes Discord hit its 2000-char message limit sooner than Telegram.

### Code findings

- `gateway/run.py` already has a generic editable progress-bubble pipeline:
  - It sends the first progress bubble.
  - Then it edits that bubble through `adapter.edit_message()`.
  - It passes `metadata` only when the adapter signature accepts `metadata`.
  - It tracks temporary progress/status message IDs and deletes them after successful final delivery when the adapter overrides `delete_message()`.

- `TelegramAdapter` is feature-complete for this pattern:
  - `gateway/platforms/telegram.py` has `MAX_MESSAGE_LENGTH = 4096`.
  - `edit_message(..., metadata=None)` exists.
  - Oversized edits split into continuation messages instead of failing or truncating.
  - `delete_message()` is implemented.
  - Telegram topic metadata is intentionally carried through send/edit paths.

- `DiscordAdapter` is only partially implemented:
  - `gateway/platforms/discord.py` has `MAX_MESSAGE_LENGTH = 2000`.
  - `send()` routes to `metadata.thread_id` correctly.
  - `edit_message()` does not accept `metadata`, fetches only `chat_id`, and cannot route edits to a thread channel.
  - `delete_message()` is not implemented, so `cleanup_progress` is silently disabled for Discord by `gateway/run.py`.
  - Discord `send()` returns `raw_response={"message_ids": [...]}` for split sends, but cleanup currently tracks only `result.message_id`.

### Official Discord API findings

From Discord official docs:

- Edit message endpoint:
  - `PATCH /channels/{channel.id}/messages/{message.id}`
  - Bot can edit content of messages it authored.
  - `content` limit is 2000 characters.

- Delete message endpoint:
  - `DELETE /channels/{channel.id}/messages/{message.id}`
  - Deleting a message not sent by the current user requires `MANAGE_MESSAGES`.
  - Deleting the bot's own progress messages should not require `MANAGE_MESSAGES`.

- Threads:
  - Threads inherit permissions from parent channels.
  - Forum/media channel threads use the same endpoints and gateway events as text-channel threads.
  - Messages in threads must be addressed through the thread channel id, not the parent channel id.

Conclusion: there is no fundamental Discord API blocker for editing/deleting Hermes' own progress messages. The missing part is Hermes adapter/routing implementation and careful handling of Discord's smaller 2000-character content limit.

### Why this likely was not implemented before

This appears more like incremental platform drift than an intentional impossibility.

Likely reasons:

1. Telegram progress UX received several targeted fixes first.
   - Git history has recent Telegram-specific progress edits: transient edit failures, flood control, overflow rollover.
   - Discord history is more focused on auth, slash commands, channel/thread backfill, and voice.

2. Discord thread routing is more subtle.
   - `send()` already supports `metadata.thread_id`, but `edit_message()` never grew the same parameter.
   - Since the generic gateway only passes `metadata` when the adapter signature advertises it, Discord silently misses thread-aware edit routing.

3. Cleanup had a reasonable safety default.
   - `BasePlatformAdapter.delete_message()` defaults to unsupported.
   - `gateway/run.py` disables cleanup when the adapter does not override deletion, preventing accidental deletes on platforms with unknown semantics.
   - Discord delete support likely was skipped because permission/thread behavior needed more care.

4. Discord's 2000-character limit makes naive parity worse.
   - Even with edit/delete fixed, unlimited previews can still cause rollover/new messages.
   - Telegram can tolerate larger progress text and has better overflow handling.

---

## Desired behavior

For Discord, when Hermes runs a tool-heavy task:

1. The first tool-progress update sends one progress bubble.
2. Later tool-progress updates edit that same bubble whenever possible.
3. If the conversation is in a Discord thread, edits target the thread channel, not the parent channel.
4. Temporary progress/status bubbles are deleted after the final answer when `cleanup_progress` resolves true.
5. Cleanup deletes all progress chunks Hermes sent, including split/continuation message IDs.
6. Failed runs keep progress breadcrumbs instead of deleting them.
7. If Discord lacks permission or the message disappeared, cleanup fails quietly and the final answer still sends.
8. Discord progress text is compact enough to avoid frequent 2000-char overflow.

---

## Task 1: Add Discord adapter regression tests for thread-aware edit/delete

**Objective:** Prove Discord can edit/delete messages in a thread when `metadata.thread_id` is supplied.

**Files:**

- Create or extend: `tests/gateway/test_discord_message_lifecycle.py`
- Modify later: `gateway/platforms/discord.py`

**Step 1: Create fakes**

Build lightweight fake Discord client/channel/message objects:

```python
class FakeDiscordMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edited_content = None
        self.deleted = False

    async def edit(self, *, content):
        self.edited_content = content

    async def delete(self):
        self.deleted = True

class FakeDiscordChannel:
    def __init__(self, channel_id):
        self.id = int(channel_id)
        self.messages = {}

    async def fetch_message(self, message_id):
        return self.messages[str(message_id)]

class FakeDiscordClient:
    def __init__(self):
        self.channels = {}
        self.get_channel_calls = []
        self.fetch_channel_calls = []

    def get_channel(self, channel_id):
        self.get_channel_calls.append(channel_id)
        return self.channels.get(str(channel_id))

    async def fetch_channel(self, channel_id):
        self.fetch_channel_calls.append(channel_id)
        return self.channels.get(str(channel_id))
```

**Step 2: Write failing test for thread-aware edit**

```python
@pytest.mark.asyncio
async def test_discord_edit_message_uses_metadata_thread_id():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    client = FakeDiscordClient()
    parent = FakeDiscordChannel("100")
    thread = FakeDiscordChannel("200")
    msg = FakeDiscordMessage("300")
    thread.messages["300"] = msg
    client.channels = {"100": parent, "200": thread}
    adapter._client = client

    result = await adapter.edit_message(
        chat_id="100",
        message_id="300",
        content="progress update",
        metadata={"thread_id": "200"},
    )

    assert result.success is True
    assert msg.edited_content == "progress update"
    assert client.get_channel_calls[0] == 200
```

Expected before implementation: `TypeError` because `DiscordAdapter.edit_message()` does not accept `metadata`, or failure because it fetches parent channel `100`.

**Step 3: Write failing test for thread-aware delete**

```python
@pytest.mark.asyncio
async def test_discord_delete_message_uses_metadata_thread_id():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    client = FakeDiscordClient()
    parent = FakeDiscordChannel("100")
    thread = FakeDiscordChannel("200")
    msg = FakeDiscordMessage("300")
    thread.messages["300"] = msg
    client.channels = {"100": parent, "200": thread}
    adapter._client = client

    ok = await adapter.delete_message(
        chat_id="100",
        message_id="300",
        metadata={"thread_id": "200"},
    )

    assert ok is True
    assert msg.deleted is True
    assert client.get_channel_calls[0] == 200
```

Expected before implementation: fail because DiscordAdapter has no `delete_message()` override and Base returns `False`.

---

## Task 2: Implement Discord thread-aware message-channel resolution

**Objective:** Use one helper for send/edit/delete destination resolution so message lifecycle operations target the same channel/thread.

**Files:**

- Modify: `gateway/platforms/discord.py`

**Step 1: Add helper**

Add near `send()` / `edit_message()`:

```python
def _message_channel_id(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> str:
    if metadata and metadata.get("thread_id"):
        return str(metadata["thread_id"])
    return str(chat_id)

async def _resolve_channel_for_message_lifecycle(
    self,
    chat_id: str,
    metadata: Optional[Dict[str, Any]] = None,
):
    channel_id = self._message_channel_id(chat_id, metadata)
    channel = self._client.get_channel(int(channel_id)) if self._client else None
    if not channel and self._client:
        channel = await self._client.fetch_channel(int(channel_id))
    return channel
```

**Step 2: Update `edit_message()` signature and implementation**

Change signature:

```python
async def edit_message(
    self,
    chat_id: str,
    message_id: str,
    content: str,
    *,
    finalize: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> SendResult:
```

Use the helper:

```python
channel = await self._resolve_channel_for_message_lifecycle(chat_id, metadata)
if not channel:
    return SendResult(success=False, error=f"Channel {self._message_channel_id(chat_id, metadata)} not found")
msg = await channel.fetch_message(int(message_id))
```

Keep the current 2000-char truncation for now; Task 6 handles compacting/overflow policy.

**Step 3: Implement `delete_message()`**

```python
async def delete_message(
    self,
    chat_id: str,
    message_id: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    if not self._client:
        return False
    try:
        channel = await self._resolve_channel_for_message_lifecycle(chat_id, metadata)
        if not channel:
            return False
        msg = await channel.fetch_message(int(message_id))
        await msg.delete()
        return True
    except Exception as e:
        logger.debug("[%s] Failed to delete Discord message %s: %s", self.name, message_id, e)
        return False
```

Use debug-level logging for delete failure. Cleanup is best-effort and should not look like an agent failure.

**Step 4: Run tests**

```bash
python -m pytest tests/gateway/test_discord_message_lifecycle.py -q -o 'addopts='
```

Expected: new tests pass.

---

## Task 3: Extend cleanup tracking to preserve per-message metadata

**Objective:** Allow cleanup to delete Discord thread messages by remembering the metadata used when each temporary progress/status message was sent.

**Files:**

- Modify: `gateway/run.py`
- Modify if needed: `gateway/platforms/base.py`
- Test: `tests/gateway/test_run_cleanup_progress.py`

**Current problem:** `_cleanup_msg_ids` is `List[str]`, and final cleanup calls:

```python
await _adapter_snapshot.delete_message(_chat_id_snapshot, _mid)
```

This loses `metadata.thread_id`, so Discord thread cleanup cannot find the progress messages.

**Step 1: Add a small cleanup target structure**

Near the cleanup setup in `_run_agent()`:

```python
@dataclass
class _ProgressCleanupTarget:
    chat_id: str
    message_id: str
    metadata: Optional[Dict[str, Any]] = None
```

If local function dataclasses are undesirable in `gateway/run.py`, use simple dicts:

```python
_cleanup_targets: List[Dict[str, Any]] = []
```

**Step 2: Replace `_cleanup_msg_ids` with metadata-aware targets**

Add helper inside `_run_agent()`:

```python
def _track_cleanup_result(result, *, metadata=None, chat_id=None) -> None:
    if not (_cleanup_progress and getattr(result, "success", False)):
        return
    ids = []
    raw = getattr(result, "raw_response", None)
    if isinstance(raw, dict) and isinstance(raw.get("message_ids"), list):
        ids.extend(str(mid) for mid in raw["message_ids"] if mid)
    mid = getattr(result, "message_id", None)
    if mid:
        ids.append(str(mid))
    for cont in getattr(result, "continuation_message_ids", ()) or ():
        ids.append(str(cont))

    seen = set()
    for mid in ids:
        if mid in seen:
            continue
        seen.add(mid)
        _cleanup_targets.append({
            "chat_id": str(chat_id or source.chat_id),
            "message_id": mid,
            "metadata": dict(metadata) if isinstance(metadata, dict) else metadata,
        })
```

Use this instead of direct `_cleanup_msg_ids.append(...)` in:

- progress send result tracking
- flood-control fallback send
- first progress message send
- status callback tracking
- long-running notification tracking

**Step 3: Update final cleanup callback**

When deleting, pass metadata if the adapter supports it:

```python
_delete_accepts_metadata = False
try:
    _delete_params = inspect.signature(_adapter_snapshot.delete_message).parameters
    _delete_accepts_metadata = (
        "metadata" in _delete_params
        or any(param.kind is inspect.Parameter.VAR_KEYWORD for param in _delete_params.values())
    )
except (TypeError, ValueError):
    _delete_accepts_metadata = False

for target in _targets_snapshot:
    kwargs = {
        "chat_id": target["chat_id"],
        "message_id": target["message_id"],
    }
    if _delete_accepts_metadata:
        kwargs["metadata"] = target.get("metadata")
    await _adapter_snapshot.delete_message(**kwargs)
```

**Step 4: Keep failed-run behavior unchanged**

Final cleanup must still only register when:

```python
not response.get("failed")
```

**Step 5: Run focused cleanup tests**

```bash
python -m pytest tests/gateway/test_run_cleanup_progress.py -q -o 'addopts='
```

---

## Task 4: Add Discord cleanup integration tests

**Objective:** Prove progress/status cleanup works for Discord and preserves thread metadata.

**Files:**

- Modify: `tests/gateway/test_run_cleanup_progress.py`
- Possibly share fake adapter helpers from `tests/gateway/test_run_progress_topics.py`

**Step 1: Add a Discord cleanup fake**

```python
class DiscordCleanupCaptureAdapter(CleanupCaptureAdapter):
    def __init__(self):
        super().__init__(platform=Platform.DISCORD)

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "content": content,
            "metadata": metadata,
        })
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id, message_id, metadata=None):
        self.deleted.append({
            "chat_id": chat_id,
            "message_id": str(message_id),
            "metadata": metadata,
        })
        return True
```

**Step 2: Add test: Discord cleanup enabled by global config**

Because the user's config currently has global `display.cleanup_progress: true`, ensure global config works, not only `display.platforms.telegram.cleanup_progress`.

```python
@pytest.mark.asyncio
async def test_discord_cleanup_progress_deletes_temp_bubbles_when_global_cleanup_enabled(...):
    # _load_gateway_config returns {"display": {"cleanup_progress": True}}
    # Run ProgressAgent through _run_agent()
    # Pop and execute post-delivery callback
    # Assert adapter.deleted contains progress/status ids
```

**Step 3: Add test: Discord thread metadata survives cleanup**

Use source:

```python
source = SessionSource(
    platform=Platform.DISCORD,
    chat_id="100",
    thread_id="200",
)
```

Assert every deleted target has:

```python
assert item["metadata"] == {"thread_id": "200"}
```

or includes `thread_id: "200"` if extra metadata keys are added.

**Step 4: Add test: split sends cleanup all message IDs**

For a fake Discord adapter `send()` returning:

```python
SendResult(
    success=True,
    message_id="m1",
    raw_response={"message_ids": ["m1", "m2", "m3"]},
)
```

Assert cleanup deletes all three unique IDs.

**Step 5: Add test: failed run keeps breadcrumbs**

Existing failed-run cleanup tests should remain. Add a Discord variant if the current tests are platform-specific.

---

## Task 5: Add metadata-aware edit tests for progress pipeline

**Objective:** Ensure `gateway/run.py` actually passes metadata into Discord `edit_message()` now that Discord advertises it.

**Files:**

- Modify: `tests/gateway/test_run_progress_topics.py`

**Step 1: Add Discord-specific progress edit test**

Use a `MetadataEditProgressCaptureAdapter(platform=Platform.DISCORD)` and source with `thread_id="thread-1"`.

Expected:

- First `send()` receives `metadata={"thread_id": "thread-1"}`.
- Later `edit_message()` receives the same metadata.
- `send_typing()` receives the same metadata.

**Step 2: Run focused progress tests**

```bash
python -m pytest tests/gateway/test_run_progress_topics.py -q -o 'addopts='
```

---

## Task 6: Reduce Discord progress overflow/noise

**Objective:** Reduce Discord's tendency to create additional bubbles due to the 2000-char limit.

**Files:**

- Modify: `gateway/run.py`
- Modify: `hermes_cli/config.py` only if adding a durable default/option
- Test: `tests/gateway/test_run_progress_topics.py`
- Test: `tests/hermes_cli/test_config.py` if config default changes

**Recommended minimal approach:** Add platform-aware effective preview caps for tool-progress lines without changing the user's global `tool_preview_length: 0` meaning everywhere.

Implementation idea:

```python
def _effective_progress_preview_cap(platform_key: str, configured: int) -> int:
    if configured and configured > 0:
        return configured
    if platform_key == "discord":
        return 120
    return 40 if configured == 0 and progress_mode in {"all", "new"} else configured
```

But be careful: current code treats `tool_preview_length: 0` as unlimited in `agent.display.get_tool_preview_max_len()`. Do not silently change global behavior for CLI/Telegram. Prefer a gateway-local cap only when building persistent progress bubbles for Discord.

Simpler alternative:

- Add config default:

```yaml
display:
  platforms:
    discord:
      tool_preview_length: 120
```

However, changing `DEFAULT_CONFIG` to include platform-specific Discord defaults could surprise users. Prefer code-level fallback only for the persistent gateway progress bubble, or document it as a new explicit config option.

**Acceptance criteria:**

- Discord progress lines show enough information to be useful.
- Long terminal commands / delegate prompts are shortened.
- Telegram behavior remains unchanged.
- `/verbose` behavior remains understandable; verbose mode may still show more detail, but should respect Discord's hard limit.

**Tests:**

Add a Discord variant of long-preview progress test:

```python
assert all(len(send["content"]) <= adapter.MAX_MESSAGE_LENGTH for send in adapter.sent)
assert not adapter.oversized_sends
assert not adapter.oversized_edits
```

---

## Task 7: Decide whether to implement Discord edit overflow split now or defer

**Objective:** Avoid accidentally making progress behavior worse by overengineering overflow handling.

**Recommendation:** Defer full Discord edit-overflow split unless tests show it is necessary after Task 6.

Reasons:

- Discord's 2000-char limit is real and low.
- Splitting progress bubbles means multiple messages by design, which is the opposite of the desired UX.
- For progress status, compacting is better than preserving every character.
- Final assistant answers already go through normal send/truncate logic; this plan targets temporary progress bubbles.

If implemented later, it should mirror Telegram's `_edit_overflow_split()` but must ensure cleanup tracks every chunk.

---

## Task 8: Verification commands

Run after implementation:

```bash
python -m pytest \
  tests/gateway/test_discord_message_lifecycle.py \
  tests/gateway/test_run_cleanup_progress.py \
  tests/gateway/test_run_progress_topics.py \
  tests/gateway/test_telegram_progress_edit_transient.py \
  -q -o 'addopts='
```

Run adjacent Discord/gateway tests:

```bash
python -m pytest \
  tests/gateway/test_discord_slash_commands.py \
  tests/gateway/test_discord_model_picker.py \
  tests/gateway/test_voice_command.py \
  -q -o 'addopts='
```

Run compile/diff checks:

```bash
python -m py_compile \
  gateway/platforms/discord.py \
  gateway/platforms/base.py \
  gateway/run.py \
  hermes_cli/config.py

git diff --check -- \
  gateway/platforms/discord.py \
  gateway/platforms/base.py \
  gateway/run.py \
  hermes_cli/config.py \
  tests/gateway/test_discord_message_lifecycle.py \
  tests/gateway/test_run_cleanup_progress.py \
  tests/gateway/test_run_progress_topics.py \
  tests/hermes_cli/test_config.py
```

Then run the user's preferred review loop:

1. Spawn 3 read-only subagent reviewers:
   - Reviewer A: Discord adapter API/thread/permission review.
   - Reviewer B: Gateway cleanup/progress lifecycle review.
   - Reviewer C: Test coverage/regression risk review.
2. Triage findings.
3. Fix only findings that are real correctness/maintainability issues.
4. Re-run focused tests.
5. If any substantial fix is made, run focused re-review.

---

## Files likely to change

- `gateway/platforms/discord.py`
  - Add metadata-aware edit routing.
  - Add `delete_message()`.
  - Add helper for message lifecycle channel resolution.

- `gateway/platforms/base.py`
  - Optionally update `edit_message()`/`delete_message()` docs/signatures to mention `metadata` support.
  - Keep backwards compatibility for adapters that do not accept metadata.

- `gateway/run.py`
  - Track cleanup targets with chat_id/message_id/metadata, not only message_id.
  - Delete with metadata when supported.
  - Track all IDs from `raw_response.message_ids` and `continuation_message_ids`.
  - Possibly add Discord-only progress preview cap.

- `tests/gateway/test_discord_message_lifecycle.py`
  - New concrete Discord adapter unit tests.

- `tests/gateway/test_run_cleanup_progress.py`
  - Add Discord cleanup and metadata tests.

- `tests/gateway/test_run_progress_topics.py`
  - Add Discord metadata-aware edit and compact progress tests.

- `tests/hermes_cli/test_config.py`
  - Only if adding a new config default or explicit config key.

---

## Risks and tradeoffs

### Permission failures

Discord may reject deletion if the bot is not deleting its own message or lacks permissions. Hermes should log at debug level and continue. Do not make final response delivery depend on cleanup success.

### Thread/channel mismatch

The most important bug to prevent is sending progress into a thread but editing/deleting in the parent channel. Tests must assert the resolved channel id.

### Cleanup too aggressive

Only clean up messages Hermes created and tracked from `SendResult`. Do not delete user messages, reply anchors, or final answers.

### Message length

Discord progress should favor compactness. Do not attempt to preserve full tool arguments inside progress bubbles when it causes clutter. Detailed tool output still exists in the session/tool transcript and final summary.

### Behavior change across platforms

Avoid changing Telegram/Slack/Matrix behavior while fixing Discord. Use platform-specific tests and keep generic changes metadata-compatible.

---

## Rollout / user-facing result

After implementation and gateway restart, Discord should feel closer to Telegram:

- Progress appears as one evolving bubble more often.
- Temporary progress/status bubbles disappear after the final answer when cleanup is enabled.
- Discord thread sessions no longer leave parent-channel fallback progress artifacts due to edit/delete routing mismatch.

Gateway restart is required for code changes to take effect. Do not restart automatically; ask/notify the user to restart manually.
