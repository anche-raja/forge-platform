"""Bedrock's "not now" is waited out; a refusal that outlasts the waiting is one unit for a human.

On AMS a ServiceUnavailableException on the reviewer's call for the eighth
pom, after the client's two attempts, ended a run twice -- seven paid units
in, with no report written. Then a Spring pack of 59 files, eight at a time
beside another run, saw 24 of them throttled and sent to a human with nothing
wrong in them. The client now retries more patiently, a throttled unit waits
and runs again, and only a unit that still cannot reach the model is held.
"""

from unittest.mock import MagicMock

from botocore.exceptions import ClientError, EndpointConnectionError

from forge.config import BEDROCK_MAX_ATTEMPTS, ForgeConfig, bedrock_client_config
from forge.service import UNIT_BACKOFF_SECONDS, _invoke_with_patience, run_file

UNAVAILABLE = ClientError({"Error": {"Code": "ServiceUnavailableException",
                                     "Message": "Bedrock is unable to process your request."}}, "Converse")
THROTTLED = ClientError({"Error": {"Code": "ThrottlingException", "Message": "Too many requests"}}, "Converse")
DENIED = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no model access"}}, "Converse")


def test_the_client_retries_more_than_twice_with_backoff():
    cfg = bedrock_client_config(ForgeConfig(data={}))
    assert cfg.retries["max_attempts"] == BEDROCK_MAX_ATTEMPTS >= 5
    assert cfg.retries["mode"] == "adaptive"


def _initial(tmp_path):
    return {"current_file": {"file_path": str(tmp_path / "A.java"), "status": "PENDING"},
            "bedrock_calls": 0, "estimated_cost_usd": 0.0}


def test_a_throttle_is_waited_out_and_the_unit_runs_again(tmp_path):
    app = MagicMock()
    app.invoke.side_effect = [THROTTLED, THROTTLED, {"current_file": {"status": "DONE"}}]
    slept = []
    final = _invoke_with_patience(app, _initial(tmp_path), {}, "A.java", sleep=slept.append)
    assert final["current_file"]["status"] == "DONE"
    assert app.invoke.call_count == 3 and slept == list(UNIT_BACKOFF_SECONDS[:2])


def test_a_refusal_that_outlasts_the_waiting_sends_the_unit_to_a_human(tmp_path):
    app = MagicMock()
    app.invoke.side_effect = UNAVAILABLE
    slept = []
    final = _invoke_with_patience(app, _initial(tmp_path), {}, "A.java", sleep=slept.append)
    fs = final["current_file"]
    assert fs["status"] == "MANUAL_REVIEW"
    assert fs["error"].startswith("Bedrock call failed after retries: ") and "ServiceUnavailableException" in fs["error"]
    assert app.invoke.call_count == len(UNIT_BACKOFF_SECONDS) + 1 and slept == list(UNIT_BACKOFF_SECONDS)


def test_a_dropped_connection_is_transient_and_a_denial_is_not(tmp_path):
    app = MagicMock()
    app.invoke.side_effect = [EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"),
                              {"current_file": {"status": "DONE"}}]
    assert _invoke_with_patience(app, _initial(tmp_path), {}, "A.java", sleep=lambda s: None)["current_file"]["status"] == "DONE"

    app = MagicMock()
    app.invoke.side_effect = DENIED
    slept = []
    final = _invoke_with_patience(app, _initial(tmp_path), {}, "A.java", sleep=slept.append)
    assert final["current_file"]["status"] == "MANUAL_REVIEW" and "AccessDeniedException" in final["current_file"]["error"]
    assert app.invoke.call_count == 1 and slept == [], "waiting does not change an access denial"


def test_run_file_reports_the_held_unit_and_the_run_goes_on(tmp_path, config, monkeypatch):
    monkeypatch.setattr("forge.service.time.sleep", lambda s: None)
    app = MagicMock()
    app.invoke.side_effect = UNAVAILABLE
    events = []
    final = run_file(app, config, MagicMock(), MagicMock(), file_path=str(tmp_path / "pom.xml"), index=8, total=8,
                     phase="build-maven-modernize", dry_run=True, source_dir=str(tmp_path),
                     output_dir=str(tmp_path / "out"), on_event=events.append)
    assert final["current_file"]["status"] == "MANUAL_REVIEW"
    assert final["bedrock_calls"] == 0 and final["estimated_cost_usd"] == 0.0
    assert [e["type"] for e in events] == ["file"] and events[0]["status"] == "MANUAL_REVIEW"


def test_a_spent_daily_quota_ends_the_run_at_once_with_the_message(tmp_path):
    from forge.service import BedrockQuotaExhausted

    app = MagicMock()
    app.invoke.side_effect = ClientError({"Error": {"Code": "ThrottlingException",
                                                    "Message": "Too many tokens per day, please wait before trying again."}},
                                         "ConverseStream")
    slept = []
    try:
        _invoke_with_patience(app, _initial(tmp_path), {}, "A.java", sleep=slept.append)
    except BedrockQuotaExhausted as e:
        assert "daily token quota" in str(e) and "transform_model" in str(e)
        assert app.invoke.call_count == 1 and slept == [], "waiting minutes cannot clear a daily quota"
        return
    raise AssertionError("a spent daily quota must end the run, not hold every unit")


def test_any_other_exception_still_ends_the_run(tmp_path, config):
    app = MagicMock()
    app.invoke.side_effect = KeyError("a bug, not the network")
    try:
        run_file(app, config, MagicMock(), MagicMock(), file_path=str(tmp_path / "pom.xml"), index=1, total=1,
                 phase="build-maven-modernize", dry_run=True, source_dir=str(tmp_path), output_dir=str(tmp_path / "out"))
    except KeyError:
        return
    raise AssertionError("a programming error must not be hidden as a manual-review unit")
