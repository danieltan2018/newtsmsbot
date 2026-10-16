#!/usr/bin/env bash
# Points the Telegram webhook at the stack's Function URL with the secret token.
# Both the bot token and the secret are read from the deployed function's
# environment, so nothing has to be kept on disk.
# Usage: scripts/set-webhook.sh            (prod)
#        ENV=staging scripts/set-webhook.sh
set -euo pipefail
cd "$(dirname "$0")/.."
REGION="${AWS_REGION:-eu-west-2}"
ENV="${ENV:-prod}"
if [ "$ENV" = staging ]; then DEFAULT_STACK=tsms-bot-staging; else DEFAULT_STACK=tsms-bot; fi
STACK="${STACK_NAME:-$DEFAULT_STACK}"

URL="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --query 'Stacks[0].Outputs[?OutputKey==`WebhookUrl`].OutputValue' --output text)"
FUNCTION="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --query 'Stacks[0].Outputs[?OutputKey==`FunctionName`].OutputValue' --output text)"
BOT_TOKEN="$(aws lambda get-function-configuration --region "$REGION" --function-name "$FUNCTION" --query 'Environment.Variables.BOT_TOKEN' --output text)"
SECRET="${WEBHOOK_SECRET:-$(aws lambda get-function-configuration --region "$REGION" --function-name "$FUNCTION" --query 'Environment.Variables.WEBHOOK_SECRET' --output text)}"
if [ -z "$SECRET" ] || [ "$SECRET" = "None" ]; then echo "WEBHOOK_SECRET is not set on $FUNCTION; deploy with WebhookSecret=... first" >&2; exit 1; fi

echo "==> setWebhook -> $URL"
curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${URL}" \
  --data-urlencode "secret_token=${SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]' \
  --data-urlencode "max_connections=10" \
  --data-urlencode "drop_pending_updates=false"
echo
echo "==> getWebhookInfo"
curl -sS "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo" | python3 -m json.tool
