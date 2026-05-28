import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class FakeDiscordMessage:
    def __init__(self, message_id):
        self.id = int(message_id)
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


def _adapter_with_parent_and_thread_message():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    client = FakeDiscordClient()
    parent = FakeDiscordChannel("100")
    thread = FakeDiscordChannel("200")
    msg = FakeDiscordMessage("300")
    thread.messages["300"] = msg
    client.channels = {"100": parent, "200": thread}
    adapter._client = client
    return adapter, client, msg


@pytest.mark.asyncio
async def test_discord_edit_message_uses_metadata_thread_id():
    adapter, client, msg = _adapter_with_parent_and_thread_message()

    result = await adapter.edit_message(
        chat_id="100",
        message_id="300",
        content="progress update",
        metadata={"thread_id": "200"},
    )

    assert result.success is True
    assert msg.edited_content == "progress update"
    assert client.get_channel_calls[0] == 200


@pytest.mark.asyncio
async def test_discord_delete_message_uses_metadata_thread_id():
    adapter, client, msg = _adapter_with_parent_and_thread_message()

    ok = await adapter.delete_message(
        chat_id="100",
        message_id="300",
        metadata={"thread_id": "200"},
    )

    assert ok is True
    assert msg.deleted is True
    assert client.get_channel_calls[0] == 200


@pytest.mark.asyncio
async def test_discord_delete_message_fails_quietly_when_message_missing():
    adapter, client, _msg = _adapter_with_parent_and_thread_message()

    ok = await adapter.delete_message(
        chat_id="100",
        message_id="999",
        metadata={"thread_id": "200"},
    )

    assert ok is False
    assert client.get_channel_calls[0] == 200


@pytest.mark.asyncio
async def test_discord_edit_message_falls_back_to_parent_channel_without_thread_metadata():
    adapter, client, _msg = _adapter_with_parent_and_thread_message()
    parent_msg = FakeDiscordMessage("301")
    client.channels["100"].messages["301"] = parent_msg

    result = await adapter.edit_message(
        chat_id="100",
        message_id="301",
        content="parent progress update",
    )

    assert result.success is True
    assert parent_msg.edited_content == "parent progress update"
    assert client.get_channel_calls[0] == 100


@pytest.mark.asyncio
async def test_discord_delete_message_fetches_uncached_thread_channel():
    adapter, client, msg = _adapter_with_parent_and_thread_message()
    thread = client.channels.pop("200")

    async def fetch_channel(channel_id):
        client.fetch_channel_calls.append(channel_id)
        return thread

    client.fetch_channel = fetch_channel

    ok = await adapter.delete_message(
        chat_id="100",
        message_id="300",
        metadata={"thread_id": "200"},
    )

    assert ok is True
    assert msg.deleted is True
    assert client.get_channel_calls[0] == 200
    assert client.fetch_channel_calls == [200]
