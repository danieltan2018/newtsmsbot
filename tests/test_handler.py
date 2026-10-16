"""lambda_handler: envelope parsing, secret check, and the async handoff."""
import json
from types import SimpleNamespace

import pytest

import lambda_function as lf

UPDATE = {"update_id": 5, "message": {"message_id": 1, "date": 0, "chat": {"id": 1, "type": "private"},
                                       "from": {"id": 1, "is_bot": False, "first_name": "A"}, "text": "hi"}}
CTX = SimpleNamespace(invoked_function_arn="arn:aws:lambda:eu-west-2:1:function:tsms-bot")


class FakeLambda:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def invoke(self, **kw):
        if self.fail:
            raise RuntimeError("no permission")
        self.calls.append(kw)
        return {"StatusCode": 202}


@pytest.fixture
def harness(monkeypatch):
    processed = []

    async def fake_main(app, update):
        processed.append(update["update_id"])
    monkeypatch.setattr(lf, "tg_bot_main", fake_main)
    monkeypatch.setattr(lf, "WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr(lf, "ASYNC_HANDOFF", True)
    fake = FakeLambda()
    monkeypatch.setattr(lf, "_lambda", lambda: fake)
    return processed, fake


def envelope(update=UPDATE, secret="s3cret"):
    headers = {"x-telegram-bot-api-secret-token": secret} if secret else {}
    return {"body": json.dumps(update), "headers": headers}


def test_function_url_request_is_acknowledged_and_handed_off(harness):
    processed, fake = harness
    assert lf.lambda_handler(envelope(), CTX) == {"statusCode": 200}
    assert processed == []                                   # nothing done inline
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["InvocationType"] == "Event" and call["FunctionName"] == CTX.invoked_function_arn
    assert json.loads(call["Payload"]) == {lf.ASYNC_KEY: UPDATE}


def test_second_hop_processes_the_update(harness):
    processed, fake = harness
    assert lf.lambda_handler({lf.ASYNC_KEY: UPDATE}, CTX) == {"statusCode": 200}
    assert processed == [5] and fake.calls == []


def test_handoff_failure_falls_back_to_inline(harness, monkeypatch):
    processed, _ = harness
    monkeypatch.setattr(lf, "_lambda", lambda: FakeLambda(fail=True))
    assert lf.lambda_handler(envelope(), CTX) == {"statusCode": 200}
    assert processed == [5]


def test_legacy_raw_update_is_processed_inline(harness, monkeypatch):
    processed, fake = harness
    monkeypatch.setattr(lf, "WEBHOOK_SECRET", "")               # legacy API Gateway path has no headers
    assert lf.lambda_handler(dict(UPDATE), CTX) == {"statusCode": 200}
    assert processed == [5] and fake.calls == []


def test_secret_and_body_checks_happen_before_handoff(harness):
    processed, fake = harness
    assert lf.lambda_handler(envelope(secret=None), CTX)["statusCode"] == 401
    assert lf.lambda_handler({"body": "{not json", "headers": {"x-telegram-bot-api-secret-token": "s3cret"}}, CTX)["statusCode"] == 400
    assert lf.lambda_handler(envelope(update={"foo": 1}), CTX)["statusCode"] == 400
    assert processed == [] and fake.calls == []


def test_health_check(harness):
    assert lf.lambda_handler({"healthCheck": 1}, CTX) == {"statusCode": 200}


def test_handoff_can_be_disabled(harness, monkeypatch):
    processed, fake = harness
    monkeypatch.setattr(lf, "ASYNC_HANDOFF", False)
    lf.lambda_handler(envelope(), CTX)
    assert processed == [5] and fake.calls == []


def test_error_handler_notifies_and_logs(monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS
    logged, answered = [], []
    monkeypatch.setattr(lf, "saveLog", lambda u, e, req, resp: logged.append((u.id, e, req, resp)))

    class Q:
        data = "SCORE TSMS 1"
        async def answer(self, text=None, show_alert=False): answered.append(text)
    from telegram import Update
    upd = NS(callback_query=Q(), effective_chat=None, effective_user=NS(id=9), effective_message=None)
    monkeypatch.setattr(lf, "Update", NS)          # let the isinstance check pass for the stub
    asyncio.run(lf.on_error(upd, NS(error=KeyError("TSMS 1"))))
    assert answered == [lf.templates.generic_error]
    assert logged == [(9, "ERROR", "SCORE TSMS 1", "KeyError: 'TSMS 1' -> ALERT")]
