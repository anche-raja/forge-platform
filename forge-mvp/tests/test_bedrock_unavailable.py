"""A Bedrock refusal after retries is one unit for a human, not the end of the run.

On AMS a ServiceUnavailableException on the reviewer's call for the eighth
pom, after the client's two attempts, ended the run twice -- seven paid units
in, with no report written. The client now retries more patiently, and a unit
whose call still fails goes to manual review with the reason.
"""

from unittest.mock import MagicMock

from botocore.exceptions import ClientError, EndpointConnectionError

from forge.config import BEDROCK_MAX_ATTEMPTS, ForgeConfig, bedrock_client_config
from forge.service import run_file

UNAVAILABLE = ClientError({"Error": {"Code": "ServiceUnavailableException",
                                     "Message": "Bedrock is unable to process your request."}}, "Converse")


def test_the_client_retries_more_than_twice_with_backoff():
    cfg = bedrock_client_config(ForgeConfig(data={}))
    assert cfg.retries["max_attempts"] == BEDROCK_MAX_ATTEMPTS >= 5
    assert cfg.retries["mode"] == "adaptive"


def _run(app, tmp_path, config):
    events = []
    final = run_file(app, config, MagicMock(), MagicMock(), file_path=str(tmp_path / "pom.xml"), index=8, total=8,
                     phase="build-maven-modernize", dry_run=True, source_dir=str(tmp_path),
                     output_dir=str(tmp_path / "out"), on_event=events.append)
    return final, events


def test_a_refusal_after_retries_sends_the_unit_to_a_human_and_the_run_goes_on(tmp_path, config):
    app = MagicMock()
    app.invoke.side_effect = UNAVAILABLE
    final, events = _run(app, tmp_path, config)
    fs = final["current_file"]
    assert fs["status"] == "MANUAL_REVIEW"
    assert fs["error"].startswith("Bedrock call failed after retries: ") and "ServiceUnavailableException" in fs["error"]
    assert final["bedrock_calls"] == 0 and final["estimated_cost_usd"] == 0.0
    assert [e["type"] for e in events] == ["file"] and events[0]["status"] == "MANUAL_REVIEW"


def test_a_dropped_connection_is_treated_the_same(tmp_path, config):
    app = MagicMock()
    app.invoke.side_effect = EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com")
    final, _ = _run(app, tmp_path, config)
    assert final["current_file"]["status"] == "MANUAL_REVIEW"


def test_any_other_exception_still_ends_the_run(tmp_path, config):
    app = MagicMock()
    app.invoke.side_effect = KeyError("a bug, not the network")
    try:
        _run(app, tmp_path, config)
    except KeyError:
        return
    raise AssertionError("a programming error must not be hidden as a manual-review unit")
