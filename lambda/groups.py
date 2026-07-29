import time
from decimal import Decimal
from types import SimpleNamespace

import boto3
import templates
from botocore.exceptions import ClientError
from cache import TITLES
from telegram import constants

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
    await context.bot.send_message(
        chat_id=user_id,
        text=templates.community_join.format(title=title),
        parse_mode=constants.ParseMode.HTML,
    )
    dbUser = dynamodb.Table("tsms_users").get_item(Key={"id": user_id}).get("Item", {})
    log_user = SimpleNamespace(
        id=user_id, full_name=dbUser.get("name"), username=None
    )
    saveLog(log_user, "GROUP", group_id, "JOIN")


def try_mark_sent(group_id, song_number):
    now = int(time.time())
    group = dynamodb.Table("tsms_groups").get_item(Key={"id": group_id}).get("Item")
    if not group or int(group.get("ttl", 0)) <= now:
        return False
    try:
        # Atomic check-and-mark: only one of two simultaneous "Send to Community"
        # taps for the same song can win this write, closing the duplicate-
        # broadcast race a plain read-then-write would leave open.
        dynamodb.Table("tsms_groups").update_item(
            Key={"id": group_id},
            UpdateExpression="ADD #s :s SET #ttl = :ttl",
            ConditionExpression="attribute_not_exists(#s) OR NOT contains(#s, :song)",
            ExpressionAttributeNames={"#s": "sent", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":s": {song_number},
                ":ttl": Decimal(now + GROUP_TTL_SECONDS),
                ":song": song_number,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    return True


def get_group_members(group_id):
    group = dynamodb.Table("tsms_groups").get_item(Key={"id": group_id}).get("Item")
    if not group:
        return set()
    return {int(u) for u in group.get("users", set())}


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
            recents_table.update_item(
                Key={"song": song_number},
                UpdateExpression="SET #g = :g",
                ExpressionAttributeNames={"#g": "group"},
                ExpressionAttributeValues={":g": active_group_id},
            )
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
        for uid in updated_users:
            await community_join(context, saveLog, uid, song_number, group_id)
        return

    recents_table.update_item(
        Key={"song": song_number},
        UpdateExpression="ADD #u :u",
        ExpressionAttributeNames={"#u": "users"},
        ExpressionAttributeValues={":u": {user.id}},
    )
