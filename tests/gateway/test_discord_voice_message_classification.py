"""Discord native voice messages should enter the STT path.

Discord represents native voice messages as audio attachments plus a message
flag.  Regular uploaded audio files are also audio attachments, so the adapter
must check the voice-message flag before falling back to MessageType.AUDIO.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.discord.adapter import DiscordAdapter


def _make_dm_channel():
    import discord

    channel = MagicMock(spec=discord.DMChannel)
    channel.id = 111
    return channel


def _make_audio_attachment(*, filename="voice-message.ogg", content_type="audio/ogg"):
    return SimpleNamespace(
        content_type=content_type,
        filename=filename,
        url="https://cdn.discordapp.example/voice-message.ogg",
        size=1234,
    )


def _make_discord_message(*, flags_voice: object | None, attachment=None):
    message = SimpleNamespace(
        id=555,
        author=SimpleNamespace(
            id=12345,
            name="TestUser",
            display_name="TestUser",
            bot=False,
        ),
        channel=_make_dm_channel(),
        content="",
        attachments=[attachment or _make_audio_attachment()],
        message_snapshots=[],
        mentions=[],
        reference=None,
        created_at=None,
        type=None,
    )
    if flags_voice is not None:
        message.flags = SimpleNamespace(voice=flags_voice)
    return message


@pytest.mark.asyncio
async def test_discord_native_voice_message_is_classified_as_voice():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=99999))
    adapter._cache_discord_audio = AsyncMock(return_value="/tmp/voice-message.ogg")
    adapter.handle_message = AsyncMock()

    await adapter._handle_message(_make_discord_message(flags_voice=True))

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.message_type is MessageType.VOICE
    assert event.media_urls == ["/tmp/voice-message.ogg"]
    assert event.media_types == ["audio/ogg"]


@pytest.mark.asyncio
async def test_discord_uploaded_audio_without_flags_stays_audio():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=99999))
    adapter._cache_discord_audio = AsyncMock(return_value="/tmp/uploaded.ogg")
    adapter.handle_message = AsyncMock()

    await adapter._handle_message(_make_discord_message(flags_voice=None))

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.message_type is MessageType.AUDIO
    assert event.media_urls == ["/tmp/uploaded.ogg"]
    assert event.media_types == ["audio/ogg"]


@pytest.mark.asyncio
async def test_discord_truthy_non_bool_voice_flag_stays_audio():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=99999))
    adapter._cache_discord_audio = AsyncMock(return_value="/tmp/mock-flag.ogg")
    adapter.handle_message = AsyncMock()

    await adapter._handle_message(_make_discord_message(flags_voice=MagicMock()))

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.message_type is MessageType.AUDIO
    assert event.media_urls == ["/tmp/mock-flag.ogg"]
    assert event.media_types == ["audio/ogg"]


@pytest.mark.asyncio
async def test_discord_uploaded_audio_attachment_stays_audio():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="x"))
    adapter._client = SimpleNamespace(user=SimpleNamespace(id=99999))
    adapter._cache_discord_audio = AsyncMock(return_value="/tmp/song.mp3")
    adapter.handle_message = AsyncMock()

    attachment = _make_audio_attachment(filename="song.mp3", content_type="audio/mpeg")
    await adapter._handle_message(
        _make_discord_message(flags_voice=False, attachment=attachment)
    )

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.call_args.args[0]
    assert event.message_type is MessageType.AUDIO
    assert event.media_urls == ["/tmp/song.mp3"]
    assert event.media_types == ["audio/mpeg"]
