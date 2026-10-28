"""Exercise notification contents, filtering, pagination and failure paths."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location(
    "notifier", Path(__file__).resolve().parents[1] / "lambda/alert_notifications.py")
notifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notifier)


@pytest.fixture
def settings(monkeypatch):
    for key, value in dict(AWS_REGION="eu-west-2", BOT_LOG_GROUP="/aws/lambda/tsms-bot",
                           BOT_FUNCTION="tsms-bot", ENVIRONMENT="prod",
                           EMAIL_TOPIC_ARN="arn:aws:sns:eu-west-2:123:email").items():
        monkeypatch.setenv(key, value)


def alarm():
    return dict(AlarmName="tsms-bot-handled-errors", NewStateValue="ALARM", OldStateValue="OK",
                StateChangeTime="2026-10-22T14:42:47.073+0000", NewStateReason="1 error in 5 minutes",
                Trigger={"Period": 300, "EvaluationPeriods": 1})


def error():
    return dict(timestamp=1792680081726, message=json.dumps(dict(
        level="ERROR", message="explainSong failed", errorType="ReadTimeout",
        errorMessage="The read operation timed out", requestId="request-123",
        location="/var/task/ai.py:explainSong:75", rawUpdate="private message text",
        stackTrace=['  File "/var/task/ai.py", line 73, in explainSong\n    text = secret_source\n'])))


def test_email_contains_incident_details_without_payload_or_source(settings):
    logs = Mock()
    logs.filter_log_events.return_value = {"events": [error()]}
    message = notifier.compose(alarm(), logs)
    for expected in ("PROD", "22 Oct 2026 22:41:21 SGT", "ReadTimeout", "explainSong failed",
                     "request-123", "/var/task/ai.py:73", "cloudwatch/home", "Matching error entries: 1"):
        assert expected in message
    assert "private message text" not in message
    assert "secret_source" not in message
    args = logs.filter_log_events.call_args.kwargs
    assert args["startTime"] < error()["timestamp"] < args["endTime"]
    assert args["logGroupName"] == "/aws/lambda/tsms-bot"


def test_empty_page_with_token_does_not_hide_errors():
    logs = Mock()
    logs.filter_log_events.side_effect = [{"events": [], "nextToken": "next"}, {"events": [error()]}]
    events, truncated = notifier.error_events(logs, "group", 0, 1)
    assert events == [error()]
    assert not truncated
    assert logs.filter_log_events.call_args.kwargs["nextToken"] == "next"


def test_repeated_token_terminates():
    logs = Mock()
    logs.filter_log_events.return_value = {"events": [], "nextToken": "same"}
    assert notifier.error_events(logs, "group", 0, 1) == ([], True)
    assert logs.filter_log_events.call_count == 2


def test_error_volume_is_bounded():
    logs = Mock()
    logs.filter_log_events.return_value = {"events": [error()] * 10, "nextToken": "more"}
    events, truncated = notifier.error_events(logs, "group", 0, 1)
    assert len(events) == 10 and truncated
    assert logs.filter_log_events.call_count == 1


def test_log_failure_still_produces_alarm_email(settings):
    logs = Mock()
    logs.filter_log_events.side_effect = RuntimeError("unavailable")
    message = notifier.compose(alarm(), logs)
    assert "Log enrichment unavailable" in message
    assert "1 error in 5 minutes" in message
    assert "Logs: https://" in message


def test_no_logs_does_not_imply_recovery(settings):
    logs = Mock()
    logs.filter_log_events.return_value = {"events": []}
    assert "Throttles may have no invocation logs" in notifier.compose(alarm(), logs)


def test_runtime_error_does_not_copy_raw_log():
    event = dict(timestamp=1792680081726, message="Task timed out after 30.00 seconds private data")
    message = notifier.describe_error(event)
    assert "Task timed out after 30.00 seconds" in message
    assert "private data" not in message


def test_common_credentials_redacted():
    value = ('https://api.telegram.org/bot123456:abcdefghijklmnopqrstuvwxyz/sendMessage '
             'AIzaabcdefghijklmnopqrstuvwxy user=123456 api_key=private user@example.com')
    result = notifier.clean(value)
    for private in ("abcdefghijklmnopqrstuvwxyz", "AIza", "123456", "private", "user@example.com"):
        assert private not in result


def test_publish_failure_propagates_for_async_retry_and_dead_letter_retention(settings, monkeypatch):
    logs, sns = Mock(), Mock()
    logs.filter_log_events.return_value = {"events": [error()]}
    monkeypatch.setattr(notifier.boto3, "client", lambda name, **kw: {"logs": logs, "sns": sns}[name])
    event = {"Records": [{"Sns": {"Message": json.dumps(alarm())}}]}
    notifier.handler(event, None)
    assert sns.publish.call_args.kwargs["TopicArn"].endswith(":email")
    assert "ReadTimeout" in sns.publish.call_args.kwargs["Message"]
    sns.publish.side_effect = RuntimeError("SNS unavailable")
    with pytest.raises(RuntimeError, match="SNS unavailable"):
        notifier.handler(event, None)


def test_notifier_initializes_and_sends_when_bot_imports_are_broken(settings, monkeypatch):
    import builtins
    original_import = builtins.__import__

    def isolated_import(name, *args, **kwargs):
        if name.split(".")[0] in {"lambda_function", "telegram", "groups", "cache", "ai"}:
            raise ImportError("Bot dependency is broken")
        return original_import(name, *args, **kwargs)

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setattr(builtins, "__import__", isolated_import)
    standalone = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(standalone)
    logs, sns = Mock(), Mock()
    logs.filter_log_events.return_value = {"events": []}
    monkeypatch.setattr(standalone.boto3, "client", lambda name, **kw: {"logs": logs, "sns": sns}[name])
    event = {"Records": [{"Sns": {"Message": json.dumps(alarm())}}]}
    assert standalone.handler(event, None) == {"statusCode": 200}
    sns.publish.assert_called_once()


def test_non_alarm_message_does_not_send_email(settings, monkeypatch):
    client = Mock()
    monkeypatch.setattr(notifier.boto3, "client", lambda *a, **kw: client)
    notifier.handler({"Records": [{"Sns": {"Message": json.dumps({"NewStateValue": "OK"})}}]}, None)
    client.publish.assert_not_called()


def test_preview_reads_real_path_without_sending(settings, monkeypatch):
    logs, sns = Mock(), Mock()
    logs.filter_log_events.return_value = {"events": [error()]}
    monkeypatch.setattr(notifier.boto3, "client", lambda name, **kw: {"logs": logs, "sns": sns}[name])
    response = notifier.handler({"preview": True, "Records": [{"Sns": {"Message": json.dumps(alarm())}}]}, None)
    assert "ReadTimeout" in response["previews"][0]
    logs.filter_log_events.assert_called_once()
    sns.publish.assert_not_called()
