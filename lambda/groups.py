"""Communities: people who look up the same song at the same time get a
temporary group they can broadcast songs to.

Design (see docs/COMMUNITY_LOGGING.md and tests/test_groups.py):
  * Every search by a user who is not in a group is recorded in `recents`
    (one row per song, RECENTS_TTL_SECONDS window). Members' own searches
    have no effect on anyone else.
  * GROUP_FORMATION_THRESHOLD different users on an untagged row form a new
    group together.
  * Broadcasting a song to a group tags that song's row with the group for
    SENT_TAG_TTL_SECONDS. Anyone without a group who looks the song up while
    the tag is live joins that group at once; people who searched it in the
    minute before the broadcast are joined too. Only broadcasts create
    invitations, never browsing.
  * A user is in at most one group; joins are skipped for users who already
    have one. Groups live GROUP_TTL_SECONDS, refreshed by every broadcast.
  * A song can be broadcast to a group once.
"""

import asyncio
import html
import logging
import os
import re
import time
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import templates
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeDeserializer
from cache import TITLES
from db import community_log_write, table
from telegram import constants
from telegram.error import TelegramError

logger = logging.getLogger()

GROUP_FORMATION_THRESHOLD = int(os.getenv("GROUP_FORMATION_THRESHOLD", "3"))
RECENTS_TTL_SECONDS = int(os.getenv("RECENTS_TTL_SECONDS", "60"))
GROUP_TTL_SECONDS = int(os.getenv("GROUP_TTL_SECONDS", "3600"))
SENT_TAG_TTL_SECONDS = int(os.getenv("SENT_TAG_TTL_SECONDS", str(RECENTS_TTL_SECONDS)))
_log_retry_at = 0.0


def _is_condition_failure(e):
    return e.response["Error"]["Code"] == "ConditionalCheckFailedException"


def _transact(items):
    """False only for a failed condition; service failures must remain visible."""
    for attempt in range(3):
        try:
            table("users").meta.client.transact_write_items(TransactItems=items)
            return True
        except ClientError as error:
            if error.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            codes = {r["Code"] for r in error.response.get("CancellationReasons", [])}
            if codes <= {"None", "ConditionalCheckFailed"} and "ConditionalCheckFailed" in codes:
                return False
            if codes <= {"None", "TransactionConflict"} and attempt < 2:
                continue
            raise


def _membership_update(user_id, group_id):
    return {"Update": {
        "TableName": table("users").name, "Key": {"id": user_id},
        "UpdateExpression": "SET #g = :g",
        "ConditionExpression": "attribute_exists(id) AND attribute_not_exists(#g)",
        "ExpressionAttributeNames": {"#g": "group"},
        "ExpressionAttributeValues": {":g": group_id},
    }}


def log_user(user_id):
    """An affected user's identity without a database lookup for display names."""
    return SimpleNamespace(id=user_id, full_name=None, username=None)


def safe_log(save_log, user, event, request, response):
    """Best-effort writes with short SDK timeouts and a brief outage cooldown."""
    global _log_retry_at
    if time.monotonic() < _log_retry_at:
        return
    token = community_log_write.set(True)
    try:
        save_log(user, event, request, response)
    except Exception:
        _log_retry_at = time.monotonic() + 10
        logger.exception("Community log write failed event=%s user=%s; pausing logs for 10s",
                         event, user.id)
    finally:
        community_log_write.reset(token)


# --------------------------------------------------------------------------- #
# Membership
# --------------------------------------------------------------------------- #
def get_active_group(user_id, dbUser):
    """The user's live group id, clearing a pointer to an expired/deleted group."""
    group_id = dbUser["Item"].get("group")
    if not group_id:
        return None
    group = table("groups").get_item(Key={"id": group_id}, ConsistentRead=True).get("Item")
    now = int(time.time())
    if group and int(group.get("ttl", 0)) > now:
        return group_id
    cleared = _transact([
        {"ConditionCheck": {
            "TableName": table("groups").name, "Key": {"id": group_id},
            "ConditionExpression": "attribute_not_exists(id) OR attribute_not_exists(#ttl) OR #ttl <= :now",
            "ExpressionAttributeNames": {"#ttl": "ttl"},
            "ExpressionAttributeValues": {":now": now},
        }},
        {"Update": {
            "TableName": table("users").name, "Key": {"id": user_id},
            "UpdateExpression": "REMOVE #g", "ConditionExpression": "#g = :g",
            "ExpressionAttributeNames": {"#g": "group"},
            "ExpressionAttributeValues": {":g": group_id},
        }},
    ])
    if cleared:
        logger.info("Community membership cleared user=%s reason=expired_or_missing", user_id)
        return None
    # A broadcast may have refreshed the group between the read and cleanup.
    current = table("users").get_item(Key={"id": user_id}, ConsistentRead=True).get("Item", {})
    group = table("groups").get_item(Key={"id": group_id}, ConsistentRead=True).get("Item", {})
    return group_id if current.get("group") == group_id and group.get("ttl", 0) > int(time.time()) else None


def _clear_membership(user_id, group_id):
    """Clear only this membership; a concurrent new membership must survive."""
    try:
        table("users").update_item(
            Key={"id": user_id}, UpdateExpression="REMOVE #g",
            ConditionExpression="#g = :g",
            ExpressionAttributeNames={"#g": "group"},
            ExpressionAttributeValues={":g": group_id},
        )
        return True
    except ClientError as error:
        if _is_condition_failure(error):
            return False
        raise


def _add_to_group(user_id, group_id):
    """Claim both sides of membership together, or neither."""
    return _transact([
        _membership_update(user_id, group_id),
        {"Update": {
            "TableName": table("groups").name, "Key": {"id": group_id},
            "UpdateExpression": "ADD #u :u",
            "ConditionExpression": "attribute_exists(id) AND #ttl > :now",
            "ExpressionAttributeNames": {"#u": "users", "#ttl": "ttl"},
            "ExpressionAttributeValues": {":u": {user_id}, ":now": int(time.time())},
        }},
    ])


async def community_join(context, saveLog, user_id, song_number, group_id,
                         reason="formation", user=None):
    """Joins one user to a group and tells them. Idempotent: existing members
    and users in another group are left untouched and get no message."""
    if not _add_to_group(user_id, group_id):
        return False
    await _notify_join(context, saveLog, user_id, song_number, group_id, reason, user)
    return True


async def _notify_join(context, saveLog, user_id, song_number, group_id,
                       reason="formation", user=None):
    safe_log(saveLog, user or log_user(user_id), "COMMUNITY_JOIN", song_number,
             {"community_id": group_id, "reason": reason})
    # Keep apostrophes inside words so DON'T becomes Don't, not Don'T.
    title = re.sub(
        r"[^\W\d_]+(?:['’][^\W\d_]+)*",
        lambda match: match.group().capitalize(), TITLES[song_number],
    )
    title = html.escape(title)
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=templates.community_join.format(title=title),
            parse_mode=constants.ParseMode.HTML,
        )
    except TelegramError as error:
        safe_log(saveLog, user or log_user(user_id), "ERROR", song_number,
                 {"community_id": group_id, "action": "COMMUNITY_JOIN", "error": type(error).__name__})
    return True


def leave_group(user_id, group_id, save_log=None, user=None, reason="user_request"):
    removed = _transact([
        {"Update": {
            "TableName": table("groups").name, "Key": {"id": group_id},
            "UpdateExpression": "DELETE #u :u",
            "ConditionExpression": "attribute_exists(id)",
            "ExpressionAttributeNames": {"#u": "users"},
            "ExpressionAttributeValues": {":u": {user_id}},
        }},
        {"Update": {
            "TableName": table("users").name, "Key": {"id": user_id},
            "UpdateExpression": "REMOVE #g", "ConditionExpression": "#g = :g",
            "ExpressionAttributeNames": {"#g": "group"},
            "ExpressionAttributeValues": {":g": group_id},
        }},
    ])
    if not removed:
        # Deleted group IDs are never reused. Avoid recreating an empty group.
        group = table("groups").get_item(Key={"id": group_id}, ConsistentRead=True).get("Item")
        if not group:
            removed = _clear_membership(user_id, group_id)
    if save_log and removed:
        safe_log(save_log, user or log_user(user_id), "COMMUNITY_LEAVE", None,
                 {"community_id": group_id, "reason": reason})


def close_if_single_member(user_id, group_id):
    """Close only the singleton snapshot; a completed concurrent join wins."""
    try:
        table("groups").delete_item(
            Key={"id": group_id},
            ConditionExpression="#u = :only",
            ExpressionAttributeNames={"#u": "users"},
            ExpressionAttributeValues={":only": {user_id}},
        )
    except ClientError as error:
        if _is_condition_failure(error):
            return False
        raise
    _clear_membership(user_id, group_id)
    return True


# --------------------------------------------------------------------------- #
# Broadcast
# --------------------------------------------------------------------------- #
def try_mark_sent(group_id, song_number, sender_id):
    """Reserve a song and return members; None means inactive or already sent.
    A singleton snapshot is returned for closure without reserving the song.
    The atomic claim checks membership and prevents duplicate broadcasts."""
    now = int(time.time())
    try:
        response = table("groups").update_item(
            Key={"id": group_id},
            UpdateExpression="ADD #s :s SET #ttl = :ttl",
            ConditionExpression=(
                "attribute_exists(id) AND #ttl > :now AND contains(#u, :sender) "
                "AND size(#u) > :one "
                "AND (attribute_not_exists(#s) OR NOT contains(#s, :song))"
            ),
            ExpressionAttributeNames={"#s": "sent", "#ttl": "ttl", "#u": "users"},
            ExpressionAttributeValues={
                ":s": {song_number},
                ":ttl": Decimal(now + GROUP_TTL_SECONDS),
                ":now": Decimal(now),
                ":song": song_number,
                ":sender": sender_id,
                ":one": 1,
            },
            ReturnValues="ALL_NEW",
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as e:
        if _is_condition_failure(e):
            old = {k: TypeDeserializer().deserialize(v)
                   for k, v in e.response.get("Item", {}).items()}
            if old.get("users") == {sender_id} and old.get("ttl", 0) > now:
                return {sender_id}
            return None
        raise
    return {int(u) for u in response["Attributes"].get("users", set())}


def unmark_sent(group_id, song_number):
    """Undo try_mark_sent when the broadcast reached nobody, so it can be retried."""
    try:
        table("groups").update_item(
            Key={"id": group_id},
            UpdateExpression="DELETE #s :s",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeNames={"#s": "sent"},
            ExpressionAttributeValues={":s": {song_number}},
        )
    except ClientError as e:
        if not _is_condition_failure(e):
            raise


# --------------------------------------------------------------------------- #
# Search events and group formation
# --------------------------------------------------------------------------- #
def _record_search(song_number, user_id, now):
    """Atomically adds the user to the song's current window and returns the
    row as it is after the write. A window that has expired is replaced."""
    names = {"#u": "users", "#ttl": "ttl", "#ts": "timestamp"}
    values = {":u": {user_id}, ":now": Decimal(now), ":ttl": Decimal(now + RECENTS_TTL_SECONDS)}
    expr = "ADD #u :u SET #ts = if_not_exists(#ts, :now), #ttl = if_not_exists(#ttl, :ttl)"
    for _ in range(2):
        try:
            return table("recents").update_item(
                Key={"song": song_number},
                UpdateExpression=expr,
                ConditionExpression="attribute_not_exists(song) OR #ttl > :now",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )["Attributes"]
        except ClientError as e:
            if not _is_condition_failure(e):
                raise
        # The row exists but its window has expired: start a fresh one. If
        # someone else replaces it first, loop once and add ourselves to theirs.
        fresh = {"song": song_number, "timestamp": Decimal(now), "users": {user_id},
                 "ttl": Decimal(now + RECENTS_TTL_SECONDS)}
        try:
            table("recents").put_item(
                Item=fresh,
                ConditionExpression="attribute_not_exists(song) OR #ttl <= :now",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": Decimal(now)},
            )
            return fresh
        except ClientError as e:
            if not _is_condition_failure(e):
                raise
    return table("recents").get_item(Key={"song": song_number}, ConsistentRead=True)["Item"]


def tag_sent_song(group_id, song_number, now):
    """After a broadcast: marks the song's row as an invitation into group_id
    for SENT_TAG_TTL_SECONDS (first live tag wins) and returns the ids of
    people who looked the song up in the current window, so they can be
    joined. An expired row is replaced so stale searchers are never invited."""
    recents = table("recents")
    for _ in range(2):
        try:
            row = recents.update_item(
                Key={"song": song_number},
                UpdateExpression=(
                    "SET #g = if_not_exists(#g, :g), #ttl = :ttl, #ts = if_not_exists(#ts, :now)"
                ),
                ConditionExpression=(
                    "(attribute_not_exists(song) OR #ttl > :now) "
                    "AND (attribute_not_exists(#g) OR #g = :g)"
                ),
                ExpressionAttributeNames={"#g": "group", "#ttl": "ttl", "#ts": "timestamp"},
                ExpressionAttributeValues={
                    ":g": group_id, ":now": Decimal(now),
                    ":ttl": Decimal(now + SENT_TAG_TTL_SECONDS),
                },
                ReturnValues="ALL_NEW",
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )["Attributes"]
            if row.get("group") != group_id:
                return set()  # another group's invitation is already open for this song
            return {int(u) for u in row.get("users", set())}
        except ClientError as e:
            if not _is_condition_failure(e):
                raise
            # A live row can only fail the ownership check. Reuse the failed
            # write's snapshot instead of reading or attempting more writes.
            old = {k: TypeDeserializer().deserialize(v)
                   for k, v in e.response.get("Item", {}).items()}
            if old.get("ttl", 0) > now:
                return set()
        # Row exists but its window expired: replace it outright.
        try:
            recents.put_item(
                Item={"song": song_number, "timestamp": Decimal(now), "group": group_id,
                      "ttl": Decimal(now + SENT_TAG_TTL_SECONDS)},
                ConditionExpression="attribute_not_exists(song) OR #ttl <= :now",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": Decimal(now)},
            )
            return set()
        except ClientError as e:
            if not _is_condition_failure(e):
                raise
    return set()


def _form_group(song_number, now, save_log=None, user=None, window=None):
    """Publish the invitation and its minimum membership in one transaction.

    Re-read on contention: candidates may have joined another Community or
    the search window may have been replaced. No partial group is published.
    """
    for _ in range(3):
        row = table("recents").get_item(Key={"song": song_number}, ConsistentRead=True).get("Item", {})
        if row.get("ttl", 0) <= int(time.time()):
            return None, set()
        if window is None:
            window = row
        if any(row.get(key) != window.get(key) for key in ("timestamp", "ttl")):
            return None, set()
        if row.get("group"):
            return row["group"], set()
        eligible = []
        for uid in sorted(row.get("users", set())):
            candidate = table("users").get_item(Key={"id": uid}, ConsistentRead=True).get("Item")
            if candidate and not candidate.get("group"):
                eligible.append(int(uid))
                if len(eligible) == GROUP_FORMATION_THRESHOLD:
                    break
        if len(eligible) < GROUP_FORMATION_THRESHOLD:
            return None, set()
        group_id = f"{song_number}-{now}-{uuid4().hex}"
        claimed = set(eligible)
        items = [
            {"Put": {
                "TableName": table("groups").name,
                "Item": {"id": group_id, "song": song_number, "created": now,
                         "ttl": now + GROUP_TTL_SECONDS, "users": claimed},
                "ConditionExpression": "attribute_not_exists(id)",
            }},
            {"Update": {
                "TableName": table("recents").name, "Key": {"song": song_number},
                "UpdateExpression": "SET #g = :g",
                "ConditionExpression": "attribute_not_exists(#g) AND #ts = :ts AND #ttl = :ttl AND #ttl > :now",
                "ExpressionAttributeNames": {"#g": "group", "#ts": "timestamp", "#ttl": "ttl"},
                "ExpressionAttributeValues": {":g": group_id, ":ts": row["timestamp"],
                                              ":ttl": row["ttl"], ":now": now},
            }},
            *(_membership_update(uid, group_id) for uid in eligible),
        ]
        if _transact(items):
            if save_log:
                safe_log(save_log, user, "COMMUNITY_CREATE", song_number, {"community_id": group_id})
            return group_id, claimed
    return None, set()


async def process_search_event(context, saveLog, user, song_number, active_group_id):
    if active_group_id:
        return  # members' own searches never affect anyone else
    now = int(time.time())
    for _ in range(2):
        row = _record_search(song_number, user.id, now)
        group_id = row.get("group")
        if not group_id:
            break
        if await community_join(context, saveLog, user.id, song_number, group_id,
                                reason="invitation", user=user):
            return
        # Only a failed join needs this read. Replace dead invitations lazily;
        # searches from a closed Community must not re-invite its old members.
        group = table("groups").get_item(Key={"id": group_id}, ConsistentRead=True).get("Item")
        if group and group.get("ttl", 0) > now:
            return
        try:
            table("recents").put_item(
                Item={"song": song_number, "timestamp": Decimal(now),
                      "ttl": Decimal(now + RECENTS_TTL_SECONDS), "users": {user.id}},
                ConditionExpression="#g = :g",
                ExpressionAttributeNames={"#g": "group"},
                ExpressionAttributeValues={":g": group_id},
            )
        except ClientError as error:
            if not _is_condition_failure(error):
                raise
    else:
        return
    users = {int(u) for u in row.get("users", set())}
    if len(users) < GROUP_FORMATION_THRESHOLD:
        return
    group_id, claimed = _form_group(song_number, now, saveLog, user, window=row)
    if not group_id:
        return
    # Everyone in the window joins; users already in another group are skipped
    # inside community_join.
    results = await asyncio.gather(
        *(_notify_join(context, saveLog, uid, song_number, group_id,
                       user=user if uid == user.id else None) for uid in sorted(claimed)),
        *(community_join(context, saveLog, uid, song_number, group_id,
                         user=user if uid == user.id else None) for uid in sorted(users - claimed)),
    )
    logger.info("community %s: %d joined for %s", group_id, sum(results), song_number)


async def invite_recent_searchers(context, saveLog, group_id, song_number, exclude=frozenset()):
    """Called after a broadcast: opens the invitation and joins anyone who
    looked the song up in the last window but is not in a group yet.
    `exclude` is the membership at broadcast time (including anyone dropped
    during delivery), who must not be (re)invited."""
    now = int(time.time())
    candidates = tag_sent_song(group_id, song_number, now) - set(exclude)
    if not candidates:
        return 0
    results = await asyncio.gather(
        *(community_join(context, saveLog, uid, song_number, group_id,
                         reason="invitation") for uid in sorted(candidates))
    )
    return sum(results)
