"""Tests for Discord thread auto-rename after session auto-title generation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _ensure_discord_mock():
    """Install a mock discord module when discord.py isn't available."""
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
    discord_mod.MessageType = SimpleNamespace(default=0, reply=1)
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

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class FakeThread:
    def __init__(self, channel_id: int = 1, name: str = "thread"):
        self.id = channel_id
        self.name = name
        self.edit = AsyncMock()


@pytest.fixture(autouse=True)
def _patch_discord_thread(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)


def _make_discord_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        user_id="42",
        chat_id="777",
        user_name="tester",
        chat_type="thread",
        thread_id="777",
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.rename_thread = AsyncMock()
    runner.adapters = {Platform.DISCORD: adapter}
    runner.session_store = MagicMock()
    runner._session_db = None
    runner._gateway_loop = None
    return runner


def test_auto_title_callback_for_discord_thread_routes_to_scheduler():
    runner = _make_runner()
    source = _make_discord_source()
    runner._schedule_discord_thread_title_rename = MagicMock()

    callback = runner._auto_title_callback_for_source(source, "sess-discord")

    assert callable(callback)
    callback("Build Discord Auto Title")

    runner._schedule_discord_thread_title_rename.assert_called_once_with(
        source,
        "sess-discord",
        "Build Discord Auto Title",
    )


@pytest.mark.asyncio
async def test_rename_discord_thread_for_session_title_uses_adapter():
    runner = _make_runner()
    source = _make_discord_source()

    await runner._rename_discord_thread_for_session_title(
        source,
        "sess-discord",
        "  Build   Discord   Thread Title  ",
    )

    runner.adapters[Platform.DISCORD].rename_thread.assert_awaited_once_with(
        thread_id="777",
        name="Build Discord Thread Title",
        reason="Hermes auto-generated session title",
    )


@pytest.mark.asyncio
async def test_discord_adapter_rename_thread_edits_thread():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    fake_thread = FakeThread(channel_id=777, name="old title")
    adapter._client = SimpleNamespace(
        get_channel=MagicMock(return_value=fake_thread),
        fetch_channel=AsyncMock(return_value=None),
    )

    await adapter.rename_thread(
        thread_id="777",
        name="New Discord Thread Title",
        reason="Hermes auto-generated session title",
    )

    fake_thread.edit.assert_awaited_once_with(
        name="New Discord Thread Title",
        reason="Hermes auto-generated session title",
    )
