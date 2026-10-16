"""Behavioural tests for the Community (groups) feature against a DynamoDB mock.

They encode the intended design:
  * 3 different users searching the same song within 60 s form a group
  * a song broadcast to a group is an invitation: anyone without a group who
    looks it up within the invitation window joins immediately, and people
    who looked it up in the minute before the broadcast are joined too
  * members' own searches never affect anyone else
  * a user is in at most one group at a time
  * a song can be broadcast to a group once; the sender is not a recipient
  * /leave removes the user from the group
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

import groups

SONG = "TSMS 28"


class FakeBot:
    def __init__(self, fail_for=()):
        self.sent = []          # (chat_id, text)
        self.fail_for = set(fail_for)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail_for:
            from telegram.error import Forbidden
            raise Forbidden("bot was blocked by the user")
        self.sent.append((chat_id, text))


def make_ctx(fail_for=()):
    return SimpleNamespace(bot=FakeBot(fail_for))


LOGS = []


def save_log(user, event, request, response):
    LOGS.append((user.id, event, request, response))


def user(uid):
    return SimpleNamespace(id=uid, full_name=f"User {uid}", username=None)


def register(dynamo, *uids):
    for uid in uids:
        dynamo.Table("test_users").put_item(Item={"id": uid, "name": f"User {uid}", "phone": "65", "state": 3})


def db_user(dynamo, uid):
    return dynamo.Table("test_users").get_item(Key={"id": uid})


def active_group(dynamo, uid):
    row = db_user(dynamo, uid)
    return groups.get_active_group(uid, row) if "Item" in row else None


def search(dynamo, ctx, uid, song=SONG):
    """What send_song does: resolve the user's live group, then record the search."""
    asyncio.run(groups.process_search_event(ctx, save_log, user(uid), song, active_group(dynamo, uid)))
    return active_group(dynamo, uid)


def advance_clock(monkeypatch, seconds):
    later = time.time() + seconds
    monkeypatch.setattr(groups.time, "time", lambda: later)


@pytest.fixture(autouse=True)
def _reset_logs():
    LOGS.clear()


def test_three_searchers_form_a_group(dynamo):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    assert search(dynamo, ctx, 1) is None
    assert search(dynamo, ctx, 2) is None
    g = search(dynamo, ctx, 3)
    assert g and g.startswith(SONG)
    assert groups.get_active_group(1, db_user(dynamo, 1)) == g
    assert groups.get_active_group(2, db_user(dynamo, 2)) == g
    members = {int(u) for u in dynamo.Table("test_groups").get_item(Key={"id": g})["Item"]["users"]}
    assert members == {1, 2, 3}
    assert sorted(c for c, _ in ctx.bot.sent) == [1, 2, 3]          # everyone got the join DM
    joins = [l for l in LOGS if l[1] == "COMMUNITY_JOIN"]
    assert len(joins) == 3
    assert all(l[2] == SONG and l[3]["reason"] == "formation" for l in joins)
    assert all(l[3] == {"community_id": g, "reason": "formation"} for l in joins)
    created = [l for l in LOGS if l[1] == "COMMUNITY_CREATE"]
    assert len(created) == 1 and created[0][0] == 3
    assert created[0][3] == {"community_id": g}


def test_same_user_searching_three_times_does_not_form(dynamo):
    register(dynamo, 1)
    ctx = make_ctx()
    for _ in range(3):
        assert search(dynamo, ctx, 1) is None


def test_logging_outage_does_not_abort_membership_changes(dynamo):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()

    def fail(*args):
        raise RuntimeError("log storage unavailable")

    for uid in (1, 2, 3):
        asyncio.run(groups.process_search_event(ctx, fail, user(uid), SONG, active_group(dynamo, uid)))
    assert len({active_group(dynamo, uid) for uid in (1, 2, 3)}) == 1
    assert active_group(dynamo, 1)
    assert {uid for uid, _ in ctx.bot.sent} == {1, 2, 3}


def test_two_searchers_then_window_expires(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    search(dynamo, ctx, 1); search(dynamo, ctx, 2)
    advance_clock(monkeypatch, groups.RECENTS_TTL_SECONDS + 1)
    assert search(dynamo, ctx, 3) is None                              # fresh window, count restarts


def test_fourth_late_searcher_joins_existing_group(dynamo):
    register(dynamo, 1, 2, 3, 4)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    g = groups.get_active_group(1, db_user(dynamo, 1))
    assert search(dynamo, ctx, 4) == g


def broadcast(dynamo, ctx, sender, song):
    """The tail of group_send: mark sent, then open the invitation."""
    g = active_group(dynamo, sender)
    members = groups.try_mark_sent(g, song, sender)
    assert members is not None
    return asyncio.run(groups.invite_recent_searchers(ctx, save_log, g, song, exclude=members))


def test_member_searches_do_not_invite_anyone(dynamo):
    register(dynamo, 1, 2, 3, 9)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    search(dynamo, ctx, 1, song="HGG 12")                              # member just browsing
    assert search(dynamo, ctx, 9, song="HGG 12") is None
    row = dynamo.Table("test_recents").get_item(Key={"song": "HGG 12"})["Item"]
    assert "group" not in row and {int(u) for u in row["users"]} == {9}   # member not even counted


def test_broadcast_song_invites_later_searchers(dynamo):
    register(dynamo, 1, 2, 3, 9)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    g = active_group(dynamo, 1)
    assert broadcast(dynamo, ctx, 1, "HGG 12") == 0                    # nobody had searched it yet
    assert search(dynamo, ctx, 9, song="HGG 12") == g                  # latecomer joins at once
    assert any(c == 9 and "joined a new Community" in t for c, t in ctx.bot.sent)
    assert len(dynamo.Table("test_groups").scan()["Items"]) == 1


def test_broadcast_joins_people_who_searched_just_before(dynamo):
    register(dynamo, 1, 2, 3, 8, 9)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    g = active_group(dynamo, 1)
    search(dynamo, ctx, 8, song="HGG 12"); search(dynamo, ctx, 9, song="HGG 12")
    assert broadcast(dynamo, ctx, 1, "HGG 12") == 2
    assert active_group(dynamo, 8) == g and active_group(dynamo, 9) == g


def test_invitation_window_expires(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3, 9)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    broadcast(dynamo, ctx, 1, "HGG 12")
    advance_clock(monkeypatch, groups.SENT_TAG_TTL_SECONDS + 1)
    assert search(dynamo, ctx, 9, song="HGG 12") is None


def test_expired_row_with_old_tag_is_replaced_on_broadcast(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3, 4, 5, 6, 8)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid, song="TSMS 1")
    search(dynamo, ctx, 8, song="HGG 12")                              # 8 searched long ago
    broadcast(dynamo, ctx, 1, "HGG 12")                                # g1 invitation, now stale
    advance_clock(monkeypatch, groups.SENT_TAG_TTL_SECONDS + 1)
    for uid in (4, 5, 6): search(dynamo, ctx, uid, song="TSMS 2")
    g2 = active_group(dynamo, 4)
    assert broadcast(dynamo, ctx, 4, "HGG 12") == 0                    # stale searcher 8 not invited
    row = dynamo.Table("test_recents").get_item(Key={"song": "HGG 12"})["Item"]
    assert row["group"] == g2 and "users" not in row


def test_first_live_invitation_wins(dynamo):
    register(dynamo, 1, 2, 3, 4, 5, 6, 9)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid, song="TSMS 1")
    for uid in (4, 5, 6): search(dynamo, ctx, uid, song="TSMS 2")
    g1 = active_group(dynamo, 1)
    broadcast(dynamo, ctx, 1, "HGG 12")
    broadcast(dynamo, ctx, 4, "HGG 12")
    assert search(dynamo, ctx, 9, song="HGG 12") == g1


def test_user_is_never_in_two_groups(dynamo):
    register(dynamo, 1, 2, 3, 4, 5, 6)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid, song="TSMS 1")
    for uid in (4, 5, 6): search(dynamo, ctx, uid, song="TSMS 2")
    g1, g2 = active_group(dynamo, 1), active_group(dynamo, 4)
    broadcast(dynamo, ctx, 1, "HGG 12")                                # g1 invites on HGG 12
    assert search(dynamo, ctx, 4, song="HGG 12") == g2                 # g2 member stays in g2
    for item in dynamo.Table("test_groups").scan()["Items"]:
        members = {int(u) for u in item["users"]}
        assert not (4 in members and item["id"] == g1)


@pytest.mark.parametrize("offset", [0, 1])
def test_concurrent_formation_race_yields_one_group(dynamo, offset):
    """Two requests both see the threshold reached and both try to form a group:
    exactly one group survives and everyone lands in it."""
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    now = int(time.time())
    for uid in (1, 2, 3):
        groups._record_search(SONG, uid, now)
    first, claimed = groups._form_group(SONG, now)
    second, other_claimed = groups._form_group(SONG, now + offset)  # same-second contenders must also yield safely
    assert claimed == {1, 2, 3} and other_claimed == set()
    assert first == second
    assert [g["id"] for g in dynamo.Table("test_groups").scan()["Items"]] == [first]
    # the winner's request then joins everyone in the window; the loser's joins itself
    for uid in (1, 2, 3):
        asyncio.run(groups.community_join(ctx, save_log, uid, SONG, second))
    assert {active_group(dynamo, u) for u in (1, 2, 3)} == {first}


def test_redelivered_update_is_idempotent(dynamo):
    """Telegram may deliver the same update twice: no duplicate DMs or groups."""
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    sent_before = len(ctx.bot.sent)
    search(dynamo, ctx, 3)
    assert len(ctx.bot.sent) == sent_before
    assert len(dynamo.Table("test_groups").scan()["Items"]) == 1


def test_expired_window_is_replaced_not_resurrected(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3, 4)
    ctx = make_ctx()
    search(dynamo, ctx, 1); search(dynamo, ctx, 2)
    advance_clock(monkeypatch, groups.RECENTS_TTL_SECONDS + 1)
    search(dynamo, ctx, 3)
    row = dynamo.Table("test_recents").get_item(Key={"song": SONG})["Item"]
    assert {int(u) for u in row["users"]} == {3}                       # stale users 1, 2 dropped
    assert search(dynamo, ctx, 4) is None


def test_unmark_sent_allows_retry(dynamo):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    g = active_group(dynamo, 1)
    assert groups.try_mark_sent(g, SONG, 1) is not None
    groups.unmark_sent(g, SONG)
    assert groups.try_mark_sent(g, SONG, 1) is not None


def test_broadcast_once_and_not_to_sender(dynamo):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    g = groups.get_active_group(1, db_user(dynamo, 1))
    members = groups.try_mark_sent(g, SONG, 1)
    assert members == {1, 2, 3}
    assert groups.try_mark_sent(g, SONG, 1) is None                        # second send of same song blocked
    assert groups.try_mark_sent(g, "TSMS 2", 1) == {1, 2, 3}


def test_leave_removes_membership(dynamo):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    g = groups.get_active_group(2, db_user(dynamo, 2))
    groups.leave_group(2, g)
    assert groups.get_active_group(2, db_user(dynamo, 2)) is None
    assert groups.try_mark_sent(g, "X 1", 1) == {1, 3}


def test_expired_group_is_cleared_lazily(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    g = groups.get_active_group(1, db_user(dynamo, 1))
    advance_clock(monkeypatch, groups.GROUP_TTL_SECONDS + 1)
    assert groups.get_active_group(1, db_user(dynamo, 1)) is None
    assert groups.try_mark_sent(g, "X 1", 1) is None
    assert "group" not in db_user(dynamo, 1)["Item"]


def test_join_dm_failure_still_joins_and_logs(dynamo):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx(fail_for={2})
    for uid in (1, 2, 3): search(dynamo, ctx, uid)
    assert groups.get_active_group(2, db_user(dynamo, 2)) is not None
    assert any(l[0] == 2 and l[1] == "COMMUNITY_JOIN" and l[3]["reason"] == "formation" for l in LOGS)
    assert any(l[0] == 2 and l[1] == "ERROR" and l[3]["action"] == "COMMUNITY_JOIN" for l in LOGS)


def test_deleted_user_row_is_not_resurrected(dynamo):
    register(dynamo, 1, 2)          # user 3 searched earlier but their row was deleted by an admin
    ctx = make_ctx()
    search(dynamo, ctx, 1); search(dynamo, ctx, 2); search(dynamo, ctx, 3)
    assert "Item" not in dynamo.Table("test_users").get_item(Key={"id": 3})


def test_invitation_join_records_song_and_reason(dynamo):
    register(dynamo, 1, 2, 3, 4)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    broadcast(dynamo, ctx, 1, "TSMS 29")
    LOGS.clear()
    search(dynamo, ctx, 4, "TSMS 29")
    assert LOGS == [(4, "COMMUNITY_JOIN", "TSMS 29",
                     {"community_id": active_group(dynamo, 4), "reason": "invitation"})]


def test_leave_logs_once_with_reason(dynamo):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    gid = active_group(dynamo, 1)
    LOGS.clear()
    groups.leave_group(1, gid, save_log, user(1))
    groups.leave_group(1, gid, save_log, user(1))
    assert LOGS == [(1, "COMMUNITY_LEAVE", None,
                     {"community_id": gid, "reason": "user_request"})]


@pytest.mark.parametrize("deleted", [False, True])
def test_expiry_clears_pointer_without_dynamo_log(dynamo, monkeypatch, deleted):
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    stale = db_user(dynamo, 1)
    if deleted:
        dynamo.Table("test_groups").delete_item(Key={"id": stale["Item"]["group"]})
    else:
        advance_clock(monkeypatch, groups.GROUP_TTL_SECONDS + 1)
    LOGS.clear()
    assert groups.get_active_group(1, stale) is None
    assert groups.get_active_group(1, stale) is None
    assert LOGS == []
    assert "group" not in db_user(dynamo, 1)["Item"]


def test_stale_expiry_preserves_new_membership(dynamo):
    register(dynamo, 1)
    dynamo.Table("test_users").update_item(
        Key={"id": 1}, UpdateExpression="SET #g = :g",
        ExpressionAttributeNames={"#g": "group"}, ExpressionAttributeValues={":g": "new"})
    stale = {"Item": {"id": 1, "group": "old"}}
    assert groups.get_active_group(1, stale) is None
    assert db_user(dynamo, 1)["Item"]["group"] == "new"
    assert LOGS == []


def test_formation_loser_does_not_log_creation(dynamo):
    now = int(time.time())
    register(dynamo, 1, 2, 3)
    for uid in (1, 2, 3):
        groups._record_search(SONG, uid, now)
    winner, claimed = groups._form_group(SONG, now, save_log, user(1))
    assert claimed == {1, 2, 3}
    assert groups._form_group(SONG, now, save_log, user(2)) == (winner, set())
    assert len(LOGS) == 1 and LOGS[0][1] == "COMMUNITY_CREATE"


def test_lifecycle_rows_retain_eight_field_schema(dynamo, monkeypatch):
    import lambda_function as lf
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        asyncio.run(groups.process_search_event(ctx, lf.saveLog, user(uid), SONG, None))
    gid = active_group(dynamo, 1)
    groups.leave_group(1, gid, lf.saveLog, user(1))
    stale = db_user(dynamo, 2)
    advance_clock(monkeypatch, groups.GROUP_TTL_SECONDS + 1)
    groups.get_active_group(2, stale)
    rows = dynamo.Table("test_logs").scan()["Items"]
    assert len(rows) == 5
    assert {r["event"] for r in rows} == {
        "COMMUNITY_CREATE", "COMMUNITY_JOIN", "COMMUNITY_LEAVE"}
    fields = {"user_id", "name", "username", "timestamp", "timestamp_iso", "event", "request", "response"}
    assert all(set(r) == fields for r in rows)


def test_join_logged_before_notification_starts(dynamo):
    register(dynamo, 1)
    now = int(time.time())
    groups._record_search(SONG, 1, now)
    gid = "notification-test"
    dynamo.Table("test_groups").put_item(Item={"id": gid, "ttl": now + 3600})
    ctx = make_ctx()

    async def interrupted_send(**kwargs):
        assert LOGS == [(1, "COMMUNITY_JOIN", SONG,
                         {"community_id": gid, "reason": "formation"})]
        raise asyncio.CancelledError()

    ctx.bot.send_message = interrupted_send
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(groups.community_join(ctx, save_log, 1, SONG, gid))
    assert active_group(dynamo, 1) == gid
    assert len(LOGS) == 1


def test_community_log_failure_cooldown_and_context_reset(monkeypatch):
    from db import community_log_write
    calls = []

    def fail(*args):
        assert community_log_write.get()
        calls.append(args)
        raise RuntimeError("database unavailable")

    groups.safe_log(fail, user(1), "COMMUNITY_SEND", SONG, {})
    assert not community_log_write.get()
    groups.safe_log(fail, user(2), "COMMUNITY_RECEIVE", SONG, {})
    assert len(calls) == 1  # don't stall every recipient during an outage
    monkeypatch.setattr(groups, "_log_retry_at", 0.0)
    groups.safe_log(save_log, user(1), "COMMUNITY_SEND", SONG, {})
    assert len(LOGS) == 1 and not community_log_write.get()


def test_only_community_logs_use_bounded_client(dynamo):
    import db
    config = db.community_dynamodb.meta.client.meta.config
    assert config.connect_timeout == config.read_timeout == 0.5
    assert config.retries["total_max_attempts"] == 1
    assert db.table("logs").meta.client is db.dynamodb.meta.client
    observed = []

    def inspect(*args):
        observed.append(db.table("logs").meta.client)
        # State operations must retain their ordinary client even inside a log call.
        assert db.table("users").meta.client is db.dynamodb.meta.client

    groups.safe_log(inspect, user(1), "COMMUNITY_SEND", SONG, {})
    assert observed == [db.community_dynamodb.meta.client]
    assert db.table("logs").meta.client is db.dynamodb.meta.client


def test_competing_broadcast_preserves_invitation_without_retry_writes(dynamo, monkeypatch):
    from unittest.mock import Mock
    groups.tag_sent_song("community-a", SONG, 1000)
    recents = dynamo.Table("test_recents")
    before = recents.get_item(Key={"song": SONG})["Item"]
    spy = Mock(wraps=recents)
    original = groups.table
    monkeypatch.setattr(groups, "table", lambda name: spy if name == "recents" else original(name))
    assert groups.tag_sent_song("community-b", SONG, 1000 + groups.SENT_TAG_TTL_SECONDS - 10) == set()
    assert recents.get_item(Key={"song": SONG})["Item"] == before
    assert before["ttl"] == 1000 + groups.SENT_TAG_TTL_SECONDS
    assert spy.update_item.call_count == 1
    spy.put_item.assert_not_called()


@pytest.mark.parametrize("seconds,joins", [(59, True), (60, False), (61, False)])
def test_one_minute_broadcast_join_boundary(dynamo, monkeypatch, seconds, joins):
    register(dynamo, 1, 2, 3, 9)
    now = int(time.time())
    monkeypatch.setattr(groups.time, "time", lambda: now)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    gid = active_group(dynamo, 1)
    broadcast(dynamo, ctx, 1, "HGG 12")
    monkeypatch.setattr(groups.time, "time", lambda: now + seconds)
    assert search(dynamo, ctx, 9, song="HGG 12") == (gid if joins else None)


def test_closed_invitation_starts_fresh_without_readding_old_members(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3)
    now = int(time.time())
    monkeypatch.setattr(groups.time, "time", lambda: now)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    old = active_group(dynamo, 1)
    groups.leave_group(2, old)
    groups.leave_group(3, old)
    assert groups.close_if_single_member(1, old)
    ctx.bot.sent.clear()
    assert search(dynamo, ctx, 1) is None
    assert ctx.bot.sent == []  # old searchers must not be re-added
    assert search(dynamo, ctx, 2) is None
    new = search(dynamo, ctx, 3)
    assert new and new != old  # distinct lifecycles even for same song/second
    assert {active_group(dynamo, uid) for uid in (1, 2, 3)} == {new}


def test_pending_join_rolls_back_if_singleton_closes_first(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3, 4)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    gid = active_group(dynamo, 1)
    groups.leave_group(2, gid)
    groups.leave_group(3, gid)
    real_add = groups._add_to_group

    def close_before_add(uid, group_id):
        assert groups.close_if_single_member(1, gid)
        return real_add(uid, group_id)

    monkeypatch.setattr(groups, "_add_to_group", close_before_add)
    assert not asyncio.run(groups.community_join(ctx, save_log, 4, SONG, gid))
    assert active_group(dynamo, 4) is None
    assert active_group(dynamo, 1) is None


def test_closing_old_group_preserves_newer_membership_pointer(dynamo):
    register(dynamo, 1)
    dynamo.Table("test_groups").put_item(Item={"id": "old", "users": {1}})
    dynamo.Table("test_groups").put_item(Item={"id": "new", "users": {1}, "ttl": int(time.time()) + 60})
    dynamo.Table("test_users").update_item(
        Key={"id": 1}, UpdateExpression="SET #g = :g",
        ExpressionAttributeNames={"#g": "group"}, ExpressionAttributeValues={":g": "new"})
    assert groups.close_if_single_member(1, "old")
    assert active_group(dynamo, 1) == "new"


def test_stale_invitation_recovery_preserves_a_new_live_invitation(dynamo, monkeypatch):
    from unittest.mock import Mock
    register(dynamo, 1, 2)
    now = int(time.time())
    recents = dynamo.Table("test_recents")
    recents.put_item(Item={"song": SONG, "group": "deleted", "users": {2}, "ttl": now + 60})
    dynamo.Table("test_groups").put_item(Item={"id": "new", "users": {1}, "ttl": now + 3600})
    assert groups._add_to_group(1, "new")
    spy = Mock(wraps=recents)

    def replacement(**kwargs):
        # Another broadcast wins ownership before this request clears the old tag.
        recents.put_item(Item={"song": SONG, "group": "new", "users": {1}, "ttl": now + 60})
        return recents.put_item(**kwargs)

    spy.put_item.side_effect = replacement
    original = groups.table
    monkeypatch.setattr(groups, "table", lambda name: spy if name == "recents" else original(name))
    ctx = make_ctx()
    search(dynamo, ctx, 2)
    assert active_group(dynamo, 2) == "new"
    assert recents.get_item(Key={"song": SONG})["Item"]["group"] == "new"


@pytest.mark.parametrize("title,expected", [
    ("DON'T BE AFRAID", "Don&#x27;t Be Afraid"),
    ("DON’T BE AFRAID", "Don’t Be Afraid"),
    ("GOD'S LOVE & MERCY", "God&#x27;s Love &amp; Mercy"),
    ("THE EVER-LASTING WORD", "The Ever-Lasting Word"),
])
def test_join_message_title_preserves_apostrophe_casing(dynamo, monkeypatch, title, expected):
    monkeypatch.setitem(groups.TITLES, SONG, title)
    register(dynamo, 1, 2, 3)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    assert len(ctx.bot.sent) == 3
    assert all(f"<b>{expected}</b>" in text for _, text in ctx.bot.sent)


def test_recent_searchers_in_other_communities_do_not_meet_threshold(dynamo):
    register(dynamo, 1, 2, 3, 4, 5, 6)
    ctx = make_ctx()
    for uid in (1, 2):
        search(dynamo, ctx, uid, SONG)
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid, 'TSMS 29')
    ctx.bot.sent.clear()
    LOGS.clear()
    assert search(dynamo, ctx, 4, SONG) is None
    assert ctx.bot.sent == [] and LOGS == []
    assert len(dynamo.Table('test_groups').scan()['Items']) == 1
    assert search(dynamo, ctx, 5, SONG) is None
    gid = search(dynamo, ctx, 6, SONG)
    assert {active_group(dynamo, u) for u in (4, 5, 6)} == {gid}
    assert {uid for uid, _ in ctx.bot.sent} == {4, 5, 6}


def test_join_then_concurrent_leave_never_leaves_ghost_recipient(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3, 4)
    ctx = make_ctx()
    for uid in (1, 2, 3):
        search(dynamo, ctx, uid)
    gid = active_group(dynamo, 1)
    real_transact = groups._transact

    def leave_after_commit(items):
        result = real_transact(items)
        monkeypatch.setattr(groups, '_transact', real_transact)
        # This is the earliest moment another request can observe membership.
        assert active_group(dynamo, 4) == gid
        groups.leave_group(4, gid)
        return result

    monkeypatch.setattr(groups, '_transact', leave_after_commit)
    assert asyncio.run(groups.community_join(ctx, save_log, 4, SONG, gid))
    assert active_group(dynamo, 4) is None
    assert groups.try_mark_sent(gid, 'TSMS 29', 1) == {1, 2, 3}


def test_failed_join_has_no_user_pointer(dynamo):
    register(dynamo, 1)
    assert not groups._add_to_group(1, 'missing')
    assert 'group' not in db_user(dynamo, 1)['Item']
    assert 'Item' not in dynamo.Table('test_groups').get_item(Key={'id': 'missing'})


def test_expiry_cleanup_loses_to_concurrent_ttl_refresh(dynamo, monkeypatch):
    register(dynamo, 1)
    now = int(time.time())
    users = dynamo.Table('test_users')
    gt = dynamo.Table('test_groups')
    users.put_item(Item={'id': 1, 'group': 'g'})
    gt.put_item(Item={'id': 'g', 'ttl': now - 1, 'users': {1, 2}})
    real_transact = groups._transact

    def refreshed_before_cleanup(items):
        gt.update_item(Key={'id': 'g'}, UpdateExpression='SET #t = :t',
                       ExpressionAttributeNames={'#t': 'ttl'},
                       ExpressionAttributeValues={':t': now + 3600})
        return real_transact(items)

    monkeypatch.setattr(groups, '_transact', refreshed_before_cleanup)
    assert active_group(dynamo, 1) == 'g'
    assert db_user(dynamo, 1)['Item']['group'] == 'g'


def test_membership_lookup_ignores_eventually_consistent_missing_snapshot(dynamo, monkeypatch):
    from unittest.mock import Mock
    register(dynamo, 1)
    dynamo.Table('test_users').put_item(Item={'id': 1, 'group': 'g'})
    gt = dynamo.Table('test_groups')
    gt.put_item(Item={'id': 'g', 'users': {1}, 'ttl': int(time.time()) + 3600})
    spy = Mock(wraps=gt)
    spy.get_item.side_effect = lambda **kwargs: gt.get_item(**kwargs) if kwargs.get('ConsistentRead') else {}
    original = groups.table
    monkeypatch.setattr(groups, 'table', lambda name: spy if name == 'groups' else original(name))
    assert active_group(dynamo, 1) == 'g'
    assert db_user(dynamo, 1)['Item']['group'] == 'g'


def test_formation_claim_failure_rolls_back_group_tag_and_other_users(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3)
    now = int(time.time())
    for uid in (1, 2, 3):
        groups._record_search(SONG, uid, now)
    dynamo.Table('test_groups').put_item(Item={'id': 'other', 'users': {9}, 'ttl': now + 3600})
    real_transact = groups._transact

    def competing_join(items):
        monkeypatch.setattr(groups, '_transact', real_transact)
        assert groups._add_to_group(1, 'other')
        return real_transact(items)

    monkeypatch.setattr(groups, '_transact', competing_join)
    assert groups._form_group(SONG, now, save_log, user(3)) == (None, set())
    assert LOGS == []
    assert 'group' not in db_user(dynamo, 2)['Item']
    assert 'group' not in db_user(dynamo, 3)['Item']
    assert 'group' not in dynamo.Table('test_recents').get_item(Key={'song': SONG})['Item']
    assert [r['id'] for r in dynamo.Table('test_groups').scan()['Items']] == ['other']


def test_transaction_service_failure_is_not_treated_as_contention(dynamo, monkeypatch):
    from botocore.exceptions import ClientError
    client = dynamo.meta.client
    def unavailable(**kwargs):
        raise ClientError({'Error': {'Code': 'TransactionCanceledException'},
                           'CancellationReasons': [{'Code': 'ProvisionedThroughputExceeded'}]},
                          'TransactWriteItems')
    monkeypatch.setattr(client, 'transact_write_items', unavailable)
    with pytest.raises(ClientError):
        groups._add_to_group(1, 'g')


def test_replaced_formation_window_does_not_join_old_searchers(dynamo, monkeypatch):
    register(dynamo, 1, 2, 3, 4, 5, 6)
    now = int(time.time())
    for uid in (1, 2, 3):
        groups._record_search(SONG, uid, now)
    real_transact = groups._transact
    def replace_before_commit(items):
        dynamo.Table('test_recents').put_item(Item={
            'song': SONG, 'users': {4, 5, 6}, 'timestamp': now + 1, 'ttl': now + 61})
        return real_transact(items)
    monkeypatch.setattr(groups, '_transact', replace_before_commit)
    assert groups._form_group(SONG, now) == (None, set())
    assert dynamo.Table('test_groups').scan()['Items'] == []
    assert all('group' not in db_user(dynamo, uid)['Item'] for uid in range(1, 7))
