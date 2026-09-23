import json

import pytest

from forge.utils.llm_json import extract_json


def test_plain_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_leading_fence():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_content_blocks():
    assert extract_json([{"type": "text", "text": '{"a": 1}'}]) == {"a": 1}


def test_reasoning_before_fenced_json():
    """The live Opus reply shape on build-maven-modernize: prose, then the fence."""
    reply = (
        "This module is a `jar` with only two dependencies. Let me check each rule.\n\n"
        "- Rule 1: no compiler properties here (they'd be in the parent `${java.version}`).\n\n"
        '```json\n{"files": {"pom.xml": "<project>\\n</project>\\n"}, '
        '"deleted_files": [], "manual_flags": []}\n```'
    )
    assert extract_json(reply)["files"] == {"pom.xml": "<project>\n</project>\n"}


def test_last_fenced_block_wins():
    reply = 'Example:\n```json\n{"draft": true}\n```\nFinal:\n```json\n{"draft": false}\n```'
    assert extract_json(reply) == {"draft": False}


def test_reasoning_before_unfenced_json():
    assert extract_json('Checked every rule.\n{"score": 90, "feedback": "ok"}') == {
        "score": 90, "feedback": "ok"}


@pytest.mark.parametrize("reply", ["", "No JSON here at all.", "```json\n{broken\n```"])
def test_no_object_still_fails(reply):
    with pytest.raises(json.JSONDecodeError):
        extract_json(reply)
