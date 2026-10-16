"""End-to-end tests of the "Send to Community" button handler."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

import groups
import lambda_function as lf
import templates
from test_groups import SONG, active_group, db_user, make_ctx, register, search, user

REAL_SAVE_LOG = lf.saveLog


class FakeQuery:
    def __init__(self, reply_markup=None):
        self.answers = []
        self.message = SimpleNamespace(reply_markup=reply_markup)
        self.edits = []

    async def edit_message_reply_markup(self, reply_markup, **kwargs):
        self.message.reply_markup = reply_markup
        self.edits.append(reply_markup)

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


@pytest.fixture(autouse=True)
def enable_community(monkeypatch):
    monkeypatch.setattr(lf, "COMMUNITY_ENABLED", True)
    monkeypatch.setattr(lf, "saveLog", lambda *a: None)


def run_send(dynamo, ctx, sender_id, song=SONG):
    query = FakeQuery()
    update = SimpleNamespace(callback_query=query)
    asyncio.run(lf.group_send(update, ctx, user(sender_id), db_user(dynamo, sender_id), song))
    return query.answers[-1]


@pytest.fixture
def community(dynamo):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    ctx.bot.sent.clear()
    return ctx


def test_broadcast_goes_to_other_members_only(dynamo, community):
    text, alert = run_send(dynamo, community, 1)
    assert text == "Sent to 2 members" and not alert
    assert sorted(c for c, _ in community.bot.sent) == [2, 3]
    body = community.bot.sent[0][1]
    assert body.startswith("TSMS 28") and "Sent by User 1" in body


def test_second_send_of_same_song_is_blocked(dynamo, community):
    run_send(dynamo, community, 1)
    text, alert = run_send(dynamo, community, 2)
    assert text == templates.group_send_already and alert


def test_not_in_community(dynamo):
    register(dynamo, 7)
    text, alert = run_send(dynamo, make_ctx(), 7)
    assert text == templates.group_send_expired and alert


@pytest.mark.parametrize("already_sent", [False, True])
def test_lone_member_send_closes_community(dynamo, community, monkeypatch, already_sent):
    gid = active_group(dynamo, 1)
    if already_sent:
        run_send(dynamo, community, 1)
        community.bot.sent.clear()
    groups.leave_group(2, gid)
    groups.leave_group(3, gid)
    rows = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: rows.append((e, req, resp)))
    text, alert = run_send(dynamo, community, 1)
    assert text == templates.group_closed and alert
    assert active_group(dynamo, 1) is None
    assert "Item" not in dynamo.Table("test_groups").get_item(Key={"id": gid})
    assert community.bot.sent == []
    assert rows == [("COMMUNITY_SEND", SONG, {
        "community_id": gid, "status": "blocked", "reason": "NO_RECIPIENTS",
        "community_closed": True,
    })]


@pytest.mark.parametrize("already_sent", [False, True])
def test_join_wins_against_lone_member_closure(dynamo, community, monkeypatch, already_sent):
    gid = active_group(dynamo, 1)
    if already_sent:
        run_send(dynamo, community, 1)
        community.bot.sent.clear()
    groups.leave_group(2, gid)
    groups.leave_group(3, gid)
    real_close = groups.close_if_single_member

    def joined_before_close(uid, group_id):
        assert groups._claim_membership(2, gid)
        assert groups._add_to_group(2, gid)
        return real_close(uid, group_id)

    monkeypatch.setattr(groups, "close_if_single_member", joined_before_close)
    text, alert = run_send(dynamo, community, 1)
    assert text == templates.group_send_nobody and alert
    assert active_group(dynamo, 1) == active_group(dynamo, 2) == gid
    # The failed close must neither claim a new song nor unclaim an old one.
    text, alert = run_send(dynamo, community, 1)
    if already_sent:
        assert text == templates.group_send_already and alert
        assert community.bot.sent == []
    else:
        assert text == "Sent to 1 member" and not alert


def test_member_who_blocked_the_bot_is_dropped(dynamo, community):
    community.bot.fail_for = {3}
    text, _ = run_send(dynamo, community, 1)
    assert text == "Sent to 1 member"
    assert active_group(dynamo, 3) is None                          # removed from the group
    assert groups.try_mark_sent(active_group(dynamo, 1), "X 1", 1) == {1, 2}


def test_all_deliveries_failing_unmarks_the_song(dynamo, community):
    async def boom(chat_id, text, **kw):
        raise NetworkError("telegram down")
    community.bot.send_message = boom
    text, _ = run_send(dynamo, community, 1)
    assert text == "Sent to 0 members"
    assert groups.try_mark_sent(active_group(dynamo, 1), SONG, 1) is not None   # retry allowed


def test_name_is_html_escaped(dynamo, community):
    q = FakeQuery()
    sender = SimpleNamespace(id=1, full_name="Tom <3 & Co", username=None)
    asyncio.run(lf.group_send(SimpleNamespace(callback_query=q), community, sender, db_user(dynamo, 1), SONG))
    assert "Tom &lt;3 &amp; Co" in community.bot.sent[0][1]


def test_attempt_rows_use_existing_schema_and_no_recipient_lookups(dynamo, community, monkeypatch):
    monkeypatch.setattr(lf, "saveLog", REAL_SAVE_LOG)
    user_reads = []
    original_table = groups.table

    def tracked_table(name):
        table = original_table(name)
        if name == "users":
            get_item = table.get_item

            def read(**kwargs):
                user_reads.append(kwargs["Key"])
                return get_item(**kwargs)
            table.get_item = read
        return table

    monkeypatch.setattr(groups, "table", tracked_table)
    sender = SimpleNamespace(id=1, full_name="Sender Name", username="sender")
    update = SimpleNamespace(update_id=500, callback_query=FakeQuery())
    asyncio.run(lf.group_send(update, community, sender, db_user(dynamo, 1), SONG))
    rows = dynamo.Table("test_logs").scan()["Items"]
    assert len(rows) == 3  # one send + one receive attempt per other member
    assert user_reads == []
    expected_fields = {"user_id", "name", "username", "timestamp", "timestamp_iso", "event", "request", "response"}
    assert all(set(row) == expected_fields for row in rows)
    assert all(row["request"] == SONG for row in rows)
    assert all("broadcast_id" not in row["response"] for row in rows)
    assert {row["response"]["community_id"] for row in rows} == {active_group(dynamo, 1)}
    sent = [r for r in rows if r["event"] == "COMMUNITY_SEND"]
    assert len(sent) == 1 and sent[0]["user_id"] == 1
    assert sent[0]["name"] == "Sender Name" and sent[0]["username"] == "sender"
    assert sent[0]["response"]["status"] == "attempted"
    assert sent[0]["response"]["recipient_count"] == 2
    assert "member_count" not in sent[0]["response"]
    received = [r for r in rows if r["event"] == "COMMUNITY_RECEIVE"]
    assert {r["user_id"] for r in received} == {2, 3}
    assert all(r["name"] is None and r["username"] is None for r in received)
    assert all({k: r["response"][k] for k in ("sender_id", "sender_name", "sender_username")} == {"sender_id": 1, "sender_name": "Sender Name", "sender_username": "sender"} for r in received)


@pytest.mark.parametrize("error_type,error_arg,outcome", [
    (Forbidden, "blocked", "forbidden"),
    (NetworkError, "timeout", "unknown"),
    (RetryAfter, 5, "rate_limited"),
    (BadRequest, "rejected", "rejected"),
], ids=["blocked", "network", "rate-limit", "bad-request"])
def test_failed_attempt_keeps_receive_row_and_adds_generic_error(dynamo, community, monkeypatch, error_type, error_arg, outcome):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")
    error = error_type(error_arg)
    rows = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: rows.append((u.id, e, req, resp)))
    send_message = community.bot.send_message

    async def send(chat_id, text, **kwargs):
        if chat_id == 3:
            raise error
        await send_message(chat_id, text, **kwargs)

    community.bot.send_message = send
    text, _ = run_send(dynamo, community, 1)
    assert text == "Sent to 1 member"
    assert [r[1] for r in rows].count("COMMUNITY_SEND") == 1
    assert {r[0] for r in rows if r[1] == "COMMUNITY_RECEIVE"} == {2, 3}
    errors = [r for r in rows if r[1] == "ERROR"]
    assert len(errors) == 1 and errors[0][0] == 3 and errors[0][2] == SONG
    assert errors[0][3]["action"] == "COMMUNITY_RECEIVE"
    assert "outcome" not in errors[0][3]
    assert errors[0][3]["error"] == type(error).__name__
    assert errors[0][3]["sender_id"] == 1
    assert "sender_name" not in errors[0][3] and "sender_username" not in errors[0][3]
    assert all(r[1] in {"COMMUNITY_SEND", "COMMUNITY_RECEIVE", "COMMUNITY_LEAVE", "ERROR"} for r in rows)
    leaves = [r for r in rows if r[1] == "COMMUNITY_LEAVE"]
    assert len(leaves) == int(error_type is Forbidden)
    if leaves:
        assert leaves[0][0] == 3
        assert leaves[0][3] == {"community_id": active_group(dynamo, 1), "reason": "bot_unavailable"}


def test_logging_outage_does_not_abort_broadcast(dynamo, community, monkeypatch):
    def fail(*args):
        raise RuntimeError("log storage unavailable")
    monkeypatch.setattr(lf, "saveLog", fail)
    text, _ = run_send(dynamo, community, 1)
    assert text == "Sent to 2 members"
    assert {uid for uid, _ in community.bot.sent} == {2, 3}


def test_failed_member_cleanup_does_not_cancel_other_deliveries(dynamo, community, monkeypatch):
    rows = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: rows.append((e, resp)))
    community.bot.fail_for = {3}

    def fail(*args):
        raise RuntimeError("cleanup failed")
    monkeypatch.setattr(groups, "leave_group", fail)
    text, _ = run_send(dynamo, community, 1)
    assert text == "Sent to 1 member"
    assert any(e == "ERROR" and r["action"] == "COMMUNITY_REMOVE" for e, r in rows)


def test_expired_callback_does_not_prevent_invitation(dynamo, community, monkeypatch):
    query = FakeQuery()
    query.answer = AsyncMock(side_effect=BadRequest("query too old"))
    invite = AsyncMock(return_value=0)
    monkeypatch.setattr(groups, "invite_recent_searchers", invite)
    asyncio.run(lf.group_send(SimpleNamespace(callback_query=query), community, user(1), db_user(dynamo, 1), SONG))
    assert {uid for uid, _ in community.bot.sent} == {2, 3}
    invite.assert_awaited_once()


def test_simultaneous_broadcasts_have_one_winner(dynamo, community, monkeypatch):
    rows = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: rows.append((u.id, e, resp)))

    async def send_both():
        await asyncio.gather(*(
            lf.group_send(SimpleNamespace(update_id=uid, callback_query=FakeQuery()),
                          community, user(uid), db_user(dynamo, uid), SONG)
            for uid in (1, 2)
        ))
    asyncio.run(send_both())
    assert len([r for r in rows if r[1] == "COMMUNITY_SEND" and r[2]["status"] == "attempted"]) == 1
    assert len([r for r in rows if r[1] == "COMMUNITY_SEND" and r[2]["status"] == "blocked"]) == 1
    assert len([r for r in rows if r[1] == "COMMUNITY_RECEIVE"]) == 2
    assert len(community.bot.sent) == 2


def test_blocked_broadcast_does_not_emit_attempt_rows(dynamo, community, monkeypatch):
    rows = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: rows.append((e, resp)))
    run_send(dynamo, community, 1)
    rows.clear()
    run_send(dynamo, community, 2)
    assert len(rows) == 1 and rows[0][0] == "COMMUNITY_SEND"
    assert rows[0][1]["status"] == "blocked"
    assert rows[0][1]["reason"] == "ALREADY_SENT_OR_INACTIVE"
    assert "recipient_count" not in rows[0][1]
    assert "broadcast_id" not in rows[0][1]


def test_logging_outage_does_not_abort_leave(dynamo, community, monkeypatch):
    def fail(*args):
        raise RuntimeError("log storage unavailable")
    monkeypatch.setattr(lf, "saveLog", fail)
    monkeypatch.setattr(lf, "getDbUser", lambda u: db_user(dynamo, u.id))
    update = SimpleNamespace(effective_user=user(1), message=SimpleNamespace(reply_html=AsyncMock()))
    asyncio.run(lf.leave(update, community))
    assert active_group(dynamo, 1) is None
    update.message.reply_html.assert_awaited_once_with(templates.group_left)


def test_attempt_logged_before_any_delivery(dynamo, community, monkeypatch):
    rows = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: rows.append((u.id, e, resp)))
    send = community.bot.send_message

    async def checked_send(chat_id, text, **kwargs):
        sends = [r for r in rows if r[1] == "COMMUNITY_SEND"]
        assert len(sends) == 1
        assert sends[0][2]["recipient_count"] == 2
        assert sends[0][2]["status"] == "attempted"
        assert any(r[0] == chat_id and r[1] == "COMMUNITY_RECEIVE" for r in rows)
        await send(chat_id, text, **kwargs)

    community.bot.send_message = checked_send
    run_send(dynamo, community, 1)


def test_concurrent_leave_prevents_stale_sender_broadcast(dynamo, community, monkeypatch):
    stale = db_user(dynamo, 1)
    groups.leave_group(1, stale["Item"]["group"])
    rows = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: rows.append((e, resp)))
    query = FakeQuery()
    asyncio.run(lf.group_send(SimpleNamespace(callback_query=query), community, user(1), stale, SONG))
    assert community.bot.sent == []
    assert rows[0][0] == "COMMUNITY_SEND" and rows[0][1]["status"] == "blocked"
    # The rejected sender did not claim the song; another current member can send it.
    assert groups.try_mark_sent(stale["Item"]["group"], SONG, 2) == {2, 3}


def test_retry_after_total_failure_logs_each_attempt_without_broadcast_id(dynamo, community, monkeypatch):
    rows = []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: rows.append((e, resp)))
    community.bot.send_message = AsyncMock(side_effect=NetworkError("timeout"))
    run_send(dynamo, community, 1)
    run_send(dynamo, community, 1)
    sends = [r for e, r in rows if e == "COMMUNITY_SEND"]
    assert len(sends) == 2
    assert all(r["status"] == "attempted" and r["recipient_count"] == 2 for r in sends)
    assert sum(e == "COMMUNITY_RECEIVE" for e, _ in rows) == 4
    assert sum(e == "ERROR" for e, _ in rows) == 4
    assert len(rows) == 10
    assert all("broadcast_id" not in r for _, r in rows)
    assert {r["community_id"] for _, r in rows} == {active_group(dynamo, 1)}
    assert all(r["sender_id"] == 1 for e, r in rows if e != "COMMUNITY_SEND")


def test_one_event_query_returns_attempted_and_blocked_sends(dynamo, community, monkeypatch):
    from boto3.dynamodb.conditions import Key
    monkeypatch.setattr(lf, "saveLog", REAL_SAVE_LOG)
    run_send(dynamo, community, 1)
    run_send(dynamo, community, 2)
    rows = dynamo.Table("test_logs").query(
        IndexName="event-timestamp-index",
        KeyConditionExpression=Key("event").eq("COMMUNITY_SEND"),
    )["Items"]
    assert len(rows) == 2
    assert {r["response"]["status"] for r in rows} == {"attempted", "blocked"}
    assert all(r["request"] == SONG for r in rows)


@pytest.mark.parametrize("failed,expected", [
    (set(), "✅ Sent to 2 members"),
    ({3}, "✅ Sent to 1 member"),
    ({2, 3}, "📢 Send to Community"),
])
def test_send_button_shows_progress_and_final_result(dynamo, community, failed, expected):
    other = InlineKeyboardButton("Chords", callback_data=f"CHORDS {SONG}")
    query = FakeQuery(InlineKeyboardMarkup([
        [other], [InlineKeyboardButton(templates.group_send_button, callback_data=f"GROUP_SEND {SONG}")],
    ]))
    community.bot.fail_for = failed
    send = community.bot.send_message

    async def checked_send(chat_id, text, **kwargs):
        assert query.message.reply_markup.inline_keyboard[1][0].text == templates.group_send_sending
        await send(chat_id, text, **kwargs)

    community.bot.send_message = checked_send
    asyncio.run(lf.group_send(SimpleNamespace(callback_query=query), community, user(1), db_user(dynamo, 1), SONG))
    assert [m.inline_keyboard[1][0].text for m in query.edits] == [templates.group_send_sending, expected]
    assert all(m.inline_keyboard[0][0] == other for m in query.edits)
    assert all(m.inline_keyboard[1][0].callback_data == f"GROUP_SEND {SONG}" for m in query.edits)
    if len(failed) == 2:
        assert groups.try_mark_sent(active_group(dynamo, 1), SONG, 1) is not None


def test_button_edit_failure_does_not_prevent_sends_or_invitations(dynamo, community, monkeypatch):
    query = FakeQuery(InlineKeyboardMarkup([
        [InlineKeyboardButton(templates.group_send_button, callback_data=f"GROUP_SEND {SONG}")],
    ]))
    query.edit_message_reply_markup = AsyncMock(side_effect=BadRequest("message cannot be edited"))
    invite = AsyncMock(return_value=0)
    monkeypatch.setattr(groups, "invite_recent_searchers", invite)
    asyncio.run(lf.group_send(SimpleNamespace(callback_query=query), community, user(1), db_user(dynamo, 1), SONG))
    assert {uid for uid, _ in community.bot.sent} == {2, 3}
    assert query.answers[-1] == ("Sent to 2 members", False)
    invite.assert_awaited_once()
