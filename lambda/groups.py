import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace

import boto3
import templates
from botocore.exceptions import ClientError
from cache import TITLES
from telegram import constants
from telegram.error import TelegramError

dynamodb = boto3.resource("dynamodb")

GROUP_FORMATION_THRESHOLD = 3
RECENTS_TTL_SECONDS = 60
GROUP_TTL_SECONDS = 3600


def get_active_group(user_id, dbUser):
    group_id = dbUser["Item"].get("group")
    if not group_id:
        return None
    group = dynamodb.Table("tsms_groups").get_item(Key={"id": group_id}).get("Item")
    if not group or int(group.get("ttl", 0)) <= int(time.time()):
        dynamodb.Table("tsms_users").update_item(
            Key={"id": user_id},
            UpdateExpression="REMOVE #g",
            ExpressionAttributeNames={"#g": "group"},
        )
        return None
    return group_id


async def community_join(context, saveLog, user_id, song_number, group_id):
    try:
        dynamodb.Table("tsms_groups").update_item(
            Key={"id": group_id},
            UpdateExpression="ADD #u :u",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeNames={"#u": "users"},
            ExpressionAttributeValues={":u": {user_id}},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # group already expired/gone, don't resurrect it
        raise
    dynamodb.Table("tsms_users").update_item(
        Key={"id": user_id},
        UpdateExpression="SET #g = :g",
        ExpressionAttributeNames={"#g": "group"},
        ExpressionAttributeValues={":g": group_id},
    )
    title = TITLES[song_number].title()
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=templates.community_join.format(title=title),
            parse_mode=constants.ParseMode.HTML,
        )
        dm_sent = True
    except TelegramError:
        dm_sent = False  # can't notify this member, but they're still joined
    saveLog(get_log_user(user_id), "GROUP", group_id, "JOIN" if dm_sent else "JOIN_NO_DM")


def get_log_user(user_id):
    dbUser = dynamodb.Table("tsms_users").get_item(Key={"id": user_id}).get("Item", {})
    return SimpleNamespace(id=user_id, full_name=dbUser.get("name"), username=None)


def try_mark_sent(group_id, song_number):
    """Marks song as sent; returns the member id set, or None if the group is
    gone/expired or the song was already sent."""
    now = int(time.time())
    try:
        # Atomic check-and-mark: only one of two simultaneous "Send to Community"
        # taps for the same song can win this write, closing the duplicate-
        # broadcast race a plain read-then-write would leave open. Folding the
        # existence check into the same condition (rather than a separate
        # pre-read) also avoids a redundant get_item, since get_active_group
        # has already confirmed liveness immediately before this is called.
        response = dynamodb.Table("tsms_groups").update_item(
            Key={"id": group_id},
            UpdateExpression="ADD #s :s SET #ttl = :ttl",
            ConditionExpression="attribute_exists(id) AND (attribute_not_exists(#s) OR NOT contains(#s, :song))",
            ExpressionAttributeNames={"#s": "sent", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": {song_number},
                ":ttl": Decimal(now + GROUP_TTL_SECONDS),
                ":song": song_number,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return {int(u) for u in response["Attributes"].get("users", set())}


def leave_group(user_id, group_id):
    try:
        dynamodb.Table("tsms_groups").update_item(
            Key={"id": group_id},
            UpdateExpression="DELETE #u :u",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeNames={"#u": "users"},
            ExpressionAttributeValues={":u": {user_id}},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise  # group already expired/gone, nothing to remove them from
    dynamodb.Table("tsms_users").update_item(
        Key={"id": user_id},
        UpdateExpression="REMOVE #g",
        ExpressionAttributeNames={"#g": "group"},
    )


async def process_search_event(context, saveLog, user, song_number, active_group_id):
    now = int(time.time())
    recents_table = dynamodb.Table("tsms_recents")
    item = recents_table.get_item(Key={"song": song_number}).get("Item")
    if item and int(item.get("ttl", 0)) <= now:
        item = None

    if item is None:
        new_item = {
            "song": song_number,
            "timestamp": Decimal(now),
            "users": {user.id},
            "ttl": Decimal(now + RECENTS_TTL_SECONDS),
        }
        if active_group_id:
            new_item["group"] = active_group_id
        recents_table.put_item(Item=new_item)
        return

    recents_group_id = item.get("group")

    if active_group_id:
        if not recents_group_id:
            try:
                # CAS guard: get_item above is eventually consistent, so without
                # this a stale read could clobber a group tag another concurrent
                # request already wrote moments ago.
                recents_table.update_item(
                    Key={"song": song_number},
                    UpdateExpression="SET #g = :g",
                    ConditionExpression="attribute_not_exists(#g)",
                    ExpressionAttributeNames={"#g": "group"},
                    ExpressionAttributeValues={":g": active_group_id},
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                # someone else already tagged this recents row first — fine,
                # this user already has their own active_group_id regardless
        return

    if recents_group_id:
        await community_join(context, saveLog, user.id, song_number, recents_group_id)
        return

    updated_users = {int(u) for u in item.get("users", set())} | {user.id}
    if len(updated_users) >= GROUP_FORMATION_THRESHOLD:
        group_id = f"{song_number}-{now}"
        try:
            # Atomically claim group-formation for this song: only one concurrent
            # request can win this write. ConditionExpression + a shared table
            # provide the compare-and-swap that a plain read-then-write can't.
            recents_table.update_item(
                Key={"song": song_number},
                UpdateExpression="SET #g = :g",
                ConditionExpression="attribute_not_exists(#g)",
                ExpressionAttributeNames={"#g": "group"},
                ExpressionAttributeValues={":g": group_id},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Lost the race - another request already formed a group for this
            # song. Join that one instead of creating a duplicate.
            winner = recents_table.get_item(
                Key={"song": song_number}, ConsistentRead=True
            ).get("Item", {})
            winning_group_id = winner.get("group")
            if winning_group_id:
                await community_join(context, saveLog, user.id, song_number, winning_group_id)
            return

        dynamodb.Table("tsms_groups").put_item(
            Item={
                "id": group_id,
                "ttl": Decimal(now + GROUP_TTL_SECONDS),
                "users": updated_users,
            }
        )
        await asyncio.gather(
            *(community_join(context, saveLog, uid, song_number, group_id) for uid in updated_users)
        )
        return

    recents_table.update_item(
        Key={"song": song_number},
        UpdateExpression="ADD #u :u",
        ExpressionAttributeNames={"#u": "users"},
        ExpressionAttributeValues={":u": {user.id}},
    )
