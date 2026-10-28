"""Cross-check the deployment workflow against the AWS trust policy."""
import os
from pathlib import Path
import subprocess

import pytest

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_environment_subjects_match_oidc_trust():
    # BaseLoader retains CloudFormation intrinsic values without evaluating them.
    template = yaml.load((ROOT / "infra/github-oidc.yaml").read_text(), Loader=yaml.BaseLoader)
    workflow = yaml.load((ROOT / ".github/workflows/deploy.yml").read_text(), Loader=yaml.BaseLoader)
    environments = workflow["on"]["workflow_dispatch"]["inputs"]["environment"]["options"]
    statement = template["Resources"]["DeployRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    condition = statement["Condition"]["StringEquals"]
    assert condition["token.actions.githubusercontent.com:aud"] == "sts.amazonaws.com"
    assert set(condition["token.actions.githubusercontent.com:sub"]) == {
        "repo:${GitHubRepo}:environment:" + env for env in environments
    }
    assert workflow["jobs"]["deploy"]["if"] == "github.ref == 'refs/heads/main'"


def test_formation_and_join_windows_share_one_setting():
    template = yaml.load((ROOT / "infra/template.yaml").read_text(), Loader=yaml.BaseLoader)
    settings = template["Resources"]["Function"]["Properties"]["Environment"]["Variables"]
    assert settings["RECENTS_TTL_SECONDS"] == settings["SENT_TAG_TTL_SECONDS"]
    parameter = settings["SENT_TAG_TTL_SECONDS"]
    assert template["Parameters"][parameter]["Default"] == "120"


@pytest.mark.parametrize("overrides,expected", [
    ([], "SentTagWindowSeconds=120"),
    (["SentTagWindowSeconds=90"], "SentTagWindowSeconds=90"),
])
def test_deploy_migrates_window_unless_explicitly_overridden(tmp_path, overrides, expected):
    script = tmp_path / "scripts/deploy.sh"
    script.parent.mkdir()
    script.write_text((ROOT / "scripts/deploy.sh").read_text())
    (tmp_path / "build/function").mkdir(parents=True)
    capture = tmp_path / "deploy-args"
    aws = tmp_path / "aws"
    aws.write_text('''#!/usr/bin/env bash
if [ "$1" = sts ]; then echo 123456789012; fi
if [ "$1" = cloudformation ] && [ "$2" = deploy ]; then
  printf '%s\n' "$@" > "$CAPTURE"
fi
''')
    aws.chmod(0o755)
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "ENV": "staging", "CAPTURE": str(capture)}
    subprocess.run(["bash", str(script), *overrides],
                   env=env, check=True, capture_output=True, text=True)
    parameters = [arg for arg in capture.read_text().splitlines()
                  if arg.startswith("SentTagWindowSeconds=")]
    assert parameters == [expected]


def test_alarm_notifier_is_independent_and_preserves_telegram_retry_policy():
    template = yaml.load((ROOT / "infra/template.yaml").read_text(), Loader=yaml.BaseLoader)
    resources = template["Resources"]
    notifier = resources["AlertNotifier"]["Properties"]
    assert notifier["Handler"] == "alert_notifications.handler"
    assert notifier["Role"] == "AlertRole.Arn"
    assert "Layers" not in notifier
    assert notifier["LoggingConfig"]["LogGroup"] != resources["Function"]["Properties"]["LoggingConfig"]["LogGroup"]
    assert resources["AlertSubscription"]["Properties"]["Endpoint"] == "AlertNotifier.Arn"
    assert resources["AlertPermission"]["Properties"]["FunctionName"] == "AlertNotifier"
    assert resources["FunctionInvokeConfig"]["Properties"]["MaximumRetryAttempts"] == "0"
    variables = notifier["Environment"]["Variables"]
    assert variables["EMAIL_TOPIC_ARN"] == "AlarmTopic"
    assert "BOT_TOKEN" not in variables
    assert "EMAIL_TOPIC_ARN" not in resources["Function"]["Properties"]["Environment"]["Variables"]
    assert resources["AlarmSubscription"]["Properties"]["TopicArn"] == "AlarmTopic"


def test_failed_notifications_retry_then_enter_a_retained_queue():
    template = yaml.load((ROOT / "infra/template.yaml").read_text(), Loader=yaml.BaseLoader)
    resources = template["Resources"]
    config = resources["AlertInvokeConfig"]["Properties"]
    assert config["FunctionName"] == "AlertNotifier"
    assert config["MaximumRetryAttempts"] == "2"
    assert config["MaximumEventAgeInSeconds"] == "3600"
    assert "AlertInvokeConfig" in resources["AlertSubscription"]["DependsOn"]
    assert resources["AlertNotifier"]["Properties"]["DeadLetterConfig"]["TargetArn"] == "AlertFailuresQueue.Arn"
    queue = resources["AlertFailuresQueue"]
    assert queue["DeletionPolicy"] == queue["UpdateReplacePolicy"] == "Retain"
    assert queue["Properties"]["MessageRetentionPeriod"] == "1209600"
    assert queue["Properties"]["SqsManagedSseEnabled"] == "true"
    statements = resources["AlertRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert any(s["Action"] == "sqs:SendMessage" and s["Resource"] == "AlertFailuresQueue.Arn" for s in statements)
    for alarm in ("ErrorsAlarm", "ThrottlesAlarm", "HandledErrorsAlarm"):
        assert resources[alarm]["Properties"]["AlarmActions"] == ["AlarmEventsTopic"]


def test_deploy_role_can_manage_the_notifier_and_failure_queue():
    template = yaml.load((ROOT / "infra/github-oidc.yaml").read_text(), Loader=yaml.BaseLoader)
    statements = template["Resources"]["DeployRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    roles = next(s["Resource"] for s in statements if s.get("Sid") == "FunctionRole")
    assert any("${StackName}-alert-role" in arn for arn in roles)
    assert any("${StackName}-staging-alert-role" in arn for arn in roles)
    actions = next(s["Action"] for s in statements if s.get("Sid") == "StackResources")
    assert "sqs:CreateQueue" in actions and "sqs:GetQueueAttributes" in actions
