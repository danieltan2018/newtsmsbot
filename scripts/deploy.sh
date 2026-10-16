#!/usr/bin/env bash
# Packages and deploys infra/template.yaml with the AWS CLI (no SAM CLI needed).
#
#   scripts/deploy.sh                       # prod; uses parameter values already on the stack
#   ENV=staging scripts/deploy.sh BotToken=... WebhookSecret=...   # staging stack (tsms-bot-staging)
#   scripts/deploy.sh AlarmEmail=you@example.com
#
# Secrets are passed as CloudFormation parameters (NoEcho). On re-deploys you
# can omit them: every parameter not given is kept with UsePreviousValue.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${AWS_REGION:-eu-west-2}"
ENV="${ENV:-prod}"
case "$ENV" in prod|staging) ;; *) echo "ENV must be prod or staging" >&2; exit 1;; esac
if [ "$ENV" = staging ]; then DEFAULT_STACK=tsms-bot-staging; else DEFAULT_STACK=tsms-bot; fi
STACK="${STACK_NAME:-$DEFAULT_STACK}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="${ARTIFACT_BUCKET:-tsms-bot-artifacts-${ACCOUNT}-${REGION}}"

if [ ! -d build/function ]; then
  echo "build/ missing: run scripts/build.sh first" >&2; exit 1
fi

if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
  echo "==> Creating artifact bucket s3://$BUCKET"
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
    --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" --region "$REGION" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  aws s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" --region "$REGION" \
    --lifecycle-configuration '{"Rules":[{"ID":"expire-old-artifacts","Status":"Enabled","Filter":{"Prefix":""},"Expiration":{"Days":30}}]}'
fi

echo "==> Packaging (uploads build/layer and build/function to s3://$BUCKET)"
aws cloudformation package \
  --region "$REGION" \
  --template-file infra/template.yaml \
  --s3-bucket "$BUCKET" \
  --s3-prefix "$STACK" \
  --output-template-file build/packaged.yaml >/dev/null

# `aws cloudformation deploy` keeps the previous value of every parameter that
# is not overridden here (secrets survive routine redeploys).
OVERRIDES=("Environment=$ENV" "$@")

echo "==> Deploying stack $STACK ($ENV) in $REGION"
CHANGESET="deploy-$(date +%Y%m%d%H%M%S)"
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file build/packaged.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "${OVERRIDES[@]}"

echo "==> Outputs"
aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}' --output table
