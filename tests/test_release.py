"""Release gates: Community isolation, legacy action logs and PTB compatibility."""
import asyncio
import json
import runpy
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
from zipfile import ZipFile

import httpx
import pytest
from telegram.ext import Application, ExtBot
from telegram.request import HTTPXRequest

import ai
import lambda_function as lf


@pytest.fixture
def release(monkeypatch):
    user = NS(id=1, full_name="Test User", username="test")
    chat = NS(**{name: AsyncMock() for name in (
        "send_message", "send_action", "send_media_group", "send_audio",
        "send_video", "send_document",
    )})
    message = NS(text="28", reply_html=AsyncMock(), reply_chat_action=AsyncMock())
    query = NS(data="", answer=AsyncMock())
    update = NS(effective_user=user, effective_chat=chat, message=message, callback_query=query)
    logs = []
    monkeypatch.setattr(lf, "COMMUNITY_ENABLED", False)
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: logs.append((e, req, resp)))
    monkeypatch.setattr(lf, "getDbUser", lambda u: {"Item": {"id": 1, "phone": "65", "state": 3}})
    monkeypatch.setattr(lf, "updateState", AsyncMock())
    for name in ("get_active_group", "leave_group", "try_mark_sent"):
        monkeypatch.setattr(lf.groups, name, Mock(side_effect=AssertionError("Community accessed while off")))
    monkeypatch.setattr(lf.groups, "process_search_event", AsyncMock(side_effect=AssertionError("Community search while off")))
    return update, NS(), logs


def test_search_with_community_off_preserves_hit_log(release):
    update, ctx, logs = release
    asyncio.run(lf.search(update, ctx))
    assert ("SEARCH_HIT", "28", f"TSMS 28 {lf.TITLES['TSMS 28']}") in logs
    buttons = update.effective_chat.send_message.call_args_list[0].kwargs["reply_markup"]
    assert all(not b.callback_data.startswith("GROUP_SEND ") for row in buttons.inline_keyboard for b in row)
    lf.groups.get_active_group.assert_not_called()
    lf.groups.process_search_event.assert_not_called()


def test_old_community_button_is_rejected_without_group_access(release):
    update, ctx, logs = release
    update.callback_query.data = "GROUP_SEND TSMS 28"
    asyncio.run(lf.answer_callback(update, ctx))
    update.callback_query.answer.assert_awaited_once_with(text=lf.templates.community_disabled, show_alert=True)
    assert logs == []
    lf.groups.get_active_group.assert_not_called()


def test_leave_while_disabled_has_no_membership_side_effect(release):
    update, ctx, logs = release
    asyncio.run(lf.leave(update, ctx))
    assert logs == []
    lf.groups.leave_group.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_command_menu_matches_feature_flag(monkeypatch, enabled):
    monkeypatch.setenv("COMMUNITY_ENABLED", str(enabled).lower())
    script = Path(__file__).resolve().parents[1] / "utilities" / "set_commands.py"
    commands = runpy.run_path(str(script))["COMMANDS"]
    assert [c.command for c in commands] == ["start", "help"] + (["leave"] if enabled else [])


@pytest.mark.parametrize("action", ["SONG", "CHORDS", "SCORE", "MP3", "PIANO", "VIDEO", "PPT", "EXPLAIN"])
def test_existing_callback_log_payloads(release, monkeypatch, action):
    update, ctx, logs = release
    arg = next(iter(lf.CALLBACK_LOOKUP[action]))
    update.callback_query.data = f"{action} {arg}"
    monkeypatch.setattr(ai, "explainSong", lambda lyrics: "Explanation")
    monkeypatch.setattr(lf, "make_ppt", lambda number: BytesIO(b"pptx"))
    asyncio.run(lf.answer_callback(update, ctx))
    assert [row for row in logs if row[0] == "CALLBACK"] == [("CALLBACK", action, arg)]


def test_song_callback_is_logged_only_after_all_links_succeed(release):
    update, ctx, logs = release
    update.callback_query.data = "SONG CA 1"
    update.effective_chat.send_message.side_effect = [None, RuntimeError("link send failed")]
    with pytest.raises(RuntimeError, match="link send failed"):
        asyncio.run(lf.answer_callback(update, ctx))
    assert not any(e == "CALLBACK" for e, _, _ in logs)


@pytest.mark.parametrize("action", ["VIDEO", "EXPLAIN"])
def test_slow_callback_attempt_is_logged_before_toast_failure(release, action):
    update, ctx, logs = release
    arg = next(iter(lf.CALLBACK_LOOKUP[action]))
    update.callback_query.data = f"{action} {arg}"
    update.callback_query.answer.side_effect = RuntimeError("expired callback")
    with pytest.raises(RuntimeError, match="expired callback"):
        asyncio.run(lf.answer_callback(update, ctx))
    assert logs == [("CALLBACK", action, arg)]


@pytest.mark.parametrize("allowed", [False, True])
def test_registration_logs_keep_existing_payload(release, dynamo, allowed):
    update, ctx, logs = release
    update.message.contact = NS(phone_number="+6512345678", user_id=1 if allowed else 2)
    asyncio.run(lf.contact(update, ctx))
    assert logs == [("CONTACT", "6512345678", "Allowed" if allowed else "Blocked")]


def test_help_log_is_unchanged(release):
    update, ctx, logs = release
    asyncio.run(lf.help_command(update, ctx))
    assert logs == [("HELP", None, None)]


@pytest.mark.parametrize("text,event,response", [
    ("Z" * 201, "SEARCH_TOO_LONG", None),
    ("QZXWQZXWQZXWQZXW", "SEARCH_NONE", None),
    ("grace amazing", "SEARCH_RESULTS", "9 results"),
])
def test_search_logs_keep_existing_payload(release, text, event, response):
    update, ctx, logs = release
    update.message.text = text
    asyncio.run(lf.search(update, ctx))
    assert (event, text, response) in logs


def test_log_row_schema_is_unchanged(dynamo):
    user = NS(id=42, full_name="Test", username=None)
    lf.saveLog(user, "CALLBACK", "SONG", "TSMS 28")
    row = dynamo.Table("test_logs").scan()["Items"][0]
    assert set(row) == {"user_id", "name", "username", "timestamp", "timestamp_iso", "event", "request", "response"}
    assert (row["event"], row["request"], row["response"]) == ("CALLBACK", "SONG", "TSMS 28")
    assert row["timestamp_iso"].endswith("+08:00")


def test_powerpoint_generation_with_current_dependencies():
    result = lf.make_ppt("TSMS 28")
    with ZipFile(result) as archive:
        assert "ppt/presentation.xml" in archive.namelist()
        assert "ppt/slides/slide1.xml" in archive.namelist()


def test_gemini_rest_and_html_output(monkeypatch):
    def respond(request):
        assert request.headers["x-goog-api-key"] == "test"
        assert json.loads(request.content)["contents"][0]["parts"][1]["text"] == "Lyrics"
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Love <3 & hope"}]}}]})
    monkeypatch.setattr(ai, "API_KEY", "test")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(ai, "_client", client)
        assert ai.explainSong("Lyrics") == "Love &lt;3 &amp; hope" + ai.disclaimer


def test_ptb_processes_consecutive_warm_invocations(monkeypatch):
    """Use real PTB initialization/dispatch/HTTPX, with Telegram HTTP mocked."""
    calls = []

    def respond(request):
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "getMe":
            result = {"id": 123, "is_bot": True, "first_name": "Test", "username": "test_bot"}
        else:
            result = {"message_id": 10, "date": 0, "chat": {"id": 1, "type": "private"}, "text": "Welcome"}
        return httpx.Response(200, json={"ok": True, "result": result})

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(respond)})
    bot = ExtBot("1:test", request=request)
    application = Application.builder().bot(bot).build()
    application.add_handler(lf.CommandHandler("start", lf.start))
    logs = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: logs.append((e, req, resp)))
    event = {"update_id": 1, "message": {"message_id": 1, "date": 0,
        "chat": {"id": 1, "type": "private"},
        "from": {"id": 1, "is_bot": False, "first_name": "Test"},
        "text": "/start", "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}
    for update_id in (1, 2):
        event["update_id"] = update_id
        asyncio.run(lf.tg_bot_main(application, event))
    assert calls.count("sendMessage") == 2
    assert logs == [("START", None, None)] * 2
