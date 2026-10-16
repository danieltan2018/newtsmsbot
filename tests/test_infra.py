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
    assert template["Parameters"][parameter]["Default"] == "60"


@pytest.mark.parametrize("overrides,expected", [
    ([], "SentTagWindowSeconds=60"),
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
