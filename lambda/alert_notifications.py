"""Standalone alarm notifier: initialization does not depend on the bot."""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
SGT = timezone(timedelta(hours=8))
MAX_EVENTS = 10


def clean(value, limit=600):
    """Bound email content and strip common credentials/contact data from errors."""
    text = str(value or "")
    text = re.sub(r"https?://[^\s\"'<>]+", "[URL omitted]", text)
    text = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[token omitted]", text)
    text = re.sub(r"\bAIza[A-Za-z0-9_-]+", "[key omitted]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email omitted]", text)
    text = re.sub(r"(?i)(api[_-]?key|authorization|token|secret)([\"']?\s*[:=]\s*)[^\s,}]+",
                  r"\1\2[redacted]", text)
    text = re.sub(r"(?i)\b(user|chat)([_ -]?id)?[=: ]+\d+", r"\1=[omitted]", text)
    return text[:limit]


def log_link(region, group):
    # The log-group link is stable; the email supplies the exact search window.
    return (f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
            f"#logsV2:log-groups/log-group/{quote(group, safe='')}")


def error_events(client, group, start, end):
    # Restrict the search to errors, including unstructured Lambda runtime errors.
    args = dict(logGroupName=group, startTime=start, endTime=end,
                filterPattern='?ERROR ?"Task timed out" ?"Status: timeout" ?"Runtime."',
                limit=MAX_EVENTS)
    events = []
    token = None
    # Empty pages may carry a nextToken. Bound both time and result volume.
    for _ in range(2):
        page = client.filter_log_events(**args)
        events.extend(page.get("events", []))
        next_token = page.get("nextToken")
        if len(events) >= MAX_EVENTS or not next_token or next_token == token:
            return events[:MAX_EVENTS], bool(next_token)
        token = next_token
        args["nextToken"] = token
    return events[:MAX_EVENTS], True


def describe_error(event):
    when = datetime.fromtimestamp(event["timestamp"] / 1000, SGT)
    try:
        data = json.loads(event["message"])
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not data:
        # Native runtime logs only: never include arbitrary raw application payloads.
        raw = event.get("message", "")
        kind = "Lambda runtime error"
        for pattern in (r"Task timed out after [\d.]+ seconds", r"Status: timeout",
                        r"Runtime\.[A-Za-z]+"):
            match = re.search(pattern, raw)
            if match:
                kind = match.group(0)
                break
        return f"{when:%d %b %Y %H:%M:%S SGT} | {kind}\nSee logs for details."
    lines = [f"{when:%d %b %Y %H:%M:%S SGT} | {clean(data.get('errorType') or data.get('type') or 'ERROR', 100)}",
             clean(data.get("message")), clean(data.get("errorMessage"))]
    for key in ("requestId", "location"):
        if data.get(key):
            lines.append(f"{key}: {clean(data[key], 200)}")
    # Source locations only; omit source text, locals, prompts and raw updates.
    frames = re.findall(r'File "([^"\n]+)", line (\d+), in ([^\n]+)',
                        "\n".join(map(str, data.get("stackTrace", []))))
    if frames:
        lines.append("Trace: " + " -> ".join(clean(f"{p}:{n} ({f})", 180)
                                             for p, n, f in frames[:4]))
    return "\n".join(line for line in lines if line)


def compose(alarm, logs):
    region = os.environ["AWS_REGION"]
    group = os.environ["BOT_LOG_GROUP"]
    name = alarm["AlarmName"]
    changed = datetime.fromisoformat(alarm["StateChangeTime"].replace("Z", "+00:00"))
    period = int(alarm.get("Trigger", {}).get("Period", 300))
    periods = int(alarm.get("Trigger", {}).get("EvaluationPeriods", 1))
    # Include evaluation lag and the complete bucket that triggered the alarm.
    end = int(changed.timestamp() * 1000)
    start = end - (period * (periods + 1) + 120) * 1000
    lines = [f"{os.environ['ENVIRONMENT'].upper()} | {clean(name, 150)}",
             f"Alarm time: {changed.astimezone(SGT):%d %b %Y %H:%M:%S SGT}",
             f"State: {clean(alarm.get('OldStateValue'))} -> {clean(alarm.get('NewStateValue'))}",
             f"Reason: {clean(alarm.get('NewStateReason'), 1000)}",
             f"Function: {os.environ['BOT_FUNCTION']}",
             f"Log search: {datetime.fromtimestamp(start / 1000, SGT):%H:%M:%S}–{changed.astimezone(SGT):%H:%M:%S SGT}",
             "Search includes evaluation lag; matching logs are context, not proof of causation."]
    try:
        events, truncated = error_events(logs, group, start, end)
        lines.append(f"\nMatching error entries: {len(events)}" + (" (sample; more may exist)" if truncated else ""))
        lines.extend("\n" + describe_error(event) for event in events)
        if not events:
            lines.append("No matching error logs found yet. Throttles may have no invocation logs; inspect the alarm metric and logs.")
    except Exception:
        logger.warning("Could not enrich alarm with logs", exc_info=True)
        lines.append("\nLog enrichment unavailable. The alarm is still valid; use the links below.")
    lines.extend(["\nLogs: " + log_link(region, group),
                  f"Alarm: https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#alarmsV2:alarm/{quote(name, safe='')}",
                  "\nOnly selected diagnostic fields are included. Raw updates, prompts and source-code lines are omitted."])
    return "\n".join(lines)


def handler(event, context):
    # This function has its own async retry policy and dead-letter queue.
    # Retrying alarm notifications never replays Telegram updates.
    logs = boto3.client("logs", config=Config(
        connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 1}))
    sns = boto3.client("sns", config=Config(
        connect_timeout=1, read_timeout=3, retries={"total_max_attempts": 2}))
    previews = []
    for record in event["Records"]:
        alarm = json.loads(record["Sns"]["Message"])
        # Topic is dedicated to alarms; reject malformed/unrelated messages.
        if alarm.get("NewStateValue") != "ALARM" or not alarm.get("AlarmName"):
            continue
        try:
            body = compose(alarm, logs)
        except Exception:
            logger.warning("Could not format alarm details", exc_info=True)
            body = "Alarm details unavailable. Original alarm:\n" + clean(json.dumps(alarm), 4000)
        if event.get("preview") is True:
            previews.append(body)
            continue
        # Propagate publication failures so Lambda retries, then retains the
        # original SNS event in the dead-letter queue if retries are exhausted.
        # Notifier failures are in a separate function/log group from bot alarms.
        sns.publish(TopicArn=os.environ["EMAIL_TOPIC_ARN"],
                    Subject=clean(f"[{os.environ['ENVIRONMENT']}] {alarm['AlarmName']} — error details", 100).replace("\n", " "),
                    Message=body)
    return {"statusCode": 200, "previews": previews} if event.get("preview") else {"statusCode": 200}
