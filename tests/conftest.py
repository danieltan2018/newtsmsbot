import os
import sys
from pathlib import Path

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda"))

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ["AWS_CONFIG_FILE"] = "/dev/null"
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
os.environ.pop("AWS_PROFILE", None)
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ["TABLE_PREFIX"] = "test_"

from moto import mock_aws  # noqa: E402


@pytest.fixture
def dynamo():
    """Fresh in-memory DynamoDB with the four tables as infra/template.yaml defines them."""
    with mock_aws():
        ddb = boto3.resource("dynamodb")
        ddb.create_table(TableName="test_users", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "N"}],
                         KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])
        ddb.create_table(TableName="test_logs", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "N"},
                                               {"AttributeName": "timestamp", "AttributeType": "N"},
                                               {"AttributeName": "event", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"},
                                    {"AttributeName": "timestamp", "KeyType": "RANGE"}],
                         GlobalSecondaryIndexes=[{
                             "IndexName": "event-timestamp-index",
                             "KeySchema": [{"AttributeName": "event", "KeyType": "HASH"},
                                           {"AttributeName": "timestamp", "KeyType": "RANGE"}],
                             "Projection": {"ProjectionType": "ALL"},
                         }])
        ddb.create_table(TableName="test_groups", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}])
        ddb.create_table(TableName="test_recents", BillingMode="PAY_PER_REQUEST",
                         AttributeDefinitions=[{"AttributeName": "song", "AttributeType": "S"}],
                         KeySchema=[{"AttributeName": "song", "KeyType": "HASH"}])
        # db.py caches a boto3 resource at import; rebind it inside the mock.
        import db
        db.dynamodb = ddb
        db.community_dynamodb = boto3.resource("dynamodb", config=db.COMMUNITY_LOG_CONFIG)
        yield ddb


@pytest.fixture(autouse=True)
def reset_community_logging_cooldown(monkeypatch):
    import groups
    monkeypatch.setattr(groups, "_log_retry_at", 0.0)
