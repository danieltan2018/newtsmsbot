"""Cross-check the deployment workflow against the AWS trust policy."""
from pathlib import Path

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
