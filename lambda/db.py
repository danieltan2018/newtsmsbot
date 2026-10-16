"""Single shared DynamoDB resource and environment-aware table names.

TABLE_PREFIX is "tsms_" in production and "tsms_staging_" in staging, so both
stacks can live side by side in one account.
"""

import os
from contextvars import ContextVar

import boto3
from botocore.config import Config

TABLE_PREFIX = os.getenv("TABLE_PREFIX", "tsms_")

dynamodb = boto3.resource("dynamodb")

# Only best-effort Community telemetry gets the short, single-attempt policy.
# Membership operations and existing action logs retain their normal policy.
community_log_write = ContextVar("community_log_write", default=False)
COMMUNITY_LOG_CONFIG = Config(
    connect_timeout=0.5, read_timeout=0.5, retries={"total_max_attempts": 1},
)
community_dynamodb = boto3.resource("dynamodb", config=COMMUNITY_LOG_CONFIG)


def table(name):
    """Returns the DynamoDB Table for a logical name such as "users"."""
    resource = community_dynamodb if name == "logs" and community_log_write.get() else dynamodb
    return resource.Table(TABLE_PREFIX + name)
