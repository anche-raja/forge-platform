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


# ─── normalize_files ──────────────────────────────────────────────────────────

from forge.utils.llm_json import TransformShapeError, normalize_files  # noqa: E402


def test_normalize_files_passes_strings_through():
    assert normalize_files({"A.java": "class A {}"}) == {"A.java": "class A {}"}


def test_normalize_files_unwraps_the_shape_seen_live():
    """Sonnet 4.5 on java8-to-java21: the text under "code", beside metadata."""
    files = {"A.java": {"language": "java", "code": "class A {}", "changelog": ["x"]}}
    assert normalize_files(files) == {"A.java": "class A {}"}


def test_normalize_files_empty_is_empty():
    assert normalize_files(None) == {}
    assert normalize_files({}) == {}


@pytest.mark.parametrize("files", [
    ["A.java"],                                        # not a map
    {"A.java": 42},                                    # not text
    {"A.java": {"language": "java"}},                  # no text at all
    {"A.java": {"code": "class A {}", "content": "class B {}"}},  # two candidates: ambiguous
])
def test_normalize_files_rejects_what_it_cannot_read(files):
    with pytest.raises(TransformShapeError):
        normalize_files(files)
