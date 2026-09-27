"""A transform reply that gives the file as a fenced block instead of JSON is still an answer.

On AMS a Java class with regexes reached a human as "Expecting ',' delimiter"
after every retry: a model that fails to JSON-escape a file once tends to fail
again. The retry now offers the easier answer and the agent reads it.
"""

from unittest.mock import MagicMock, patch

from forge.agents.java_upgrade import JavaUpgradeAgent
from forge.utils.llm_json import salvage_file_reply
from tests.conftest import CLEAN_JAVA, make_state, write_config

PATH = "/src/com/corp/user/UserAction.java"


def _reply(text: str) -> MagicMock:
    m = MagicMock()
    m.content = text
    m.usage_metadata = {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}
    return m


def test_the_last_fenced_file_is_taken_as_the_files_content():
    reply = "Here is the migrated class:\n\n```java\n" + CLEAN_JAVA.rstrip("\n") + "\n```\n"
    out = salvage_file_reply(reply, PATH)
    assert out["files"] == {PATH: CLEAN_JAVA} and out["deleted_files"] == [] and out["salvaged"] == "fenced file"


def test_a_fenced_xml_file_and_a_bedrock_block_list_are_read_too():
    pom = '<?xml version="1.0"?>\n<project>\n  <modelVersion>4.0.0</modelVersion>\n</project>'
    reply = [{"type": "text", "text": "```xml\n" + pom + "\n```"}]
    assert salvage_file_reply(reply, "/p/pom.xml")["files"] == {"/p/pom.xml": pom + "\n"}


def test_a_fenced_json_envelope_or_prose_is_not_a_file():
    assert salvage_file_reply('```json\n{"files": {"a": "b"}\n```', PATH) is None, "that is the caller's JSON failure"
    assert salvage_file_reply("I could not migrate this file.\n```\nsorry\n```", PATH) is None
    assert salvage_file_reply("no fence at all", PATH) is None
    assert salvage_file_reply(None, PATH) is None


def test_the_agent_accepts_a_fenced_reply_instead_of_sending_the_file_to_a_human(tmp_path, java_file):
    cfg = write_config(tmp_path)
    state = make_state(java_file, tmp_path)
    with patch("forge.agents.java_upgrade.ChatBedrockConverse") as converse:
        converse.return_value.invoke.return_value = _reply("```java\n" + CLEAN_JAVA + "```")
        result = JavaUpgradeAgent(cfg).run(state)
    fs = result["current_file"]
    assert fs["status"] == "REVIEWING" and not fs.get("transform_malformed")
    assert fs["transform_output"]["files"] == {java_file: CLEAN_JAVA}


def test_prose_with_no_file_still_goes_round_the_retry_loop_with_the_easier_ask(tmp_path, java_file):
    cfg = write_config(tmp_path)
    state = make_state(java_file, tmp_path)
    with patch("forge.agents.java_upgrade.ChatBedrockConverse") as converse:
        converse.return_value.invoke.return_value = _reply("I'm not able to do this.")
        result = JavaUpgradeAgent(cfg).run(state)
    fs = result["current_file"]
    assert fs["transform_malformed"] and fs["error"].startswith("Failed to parse transform output as JSON")
    assert "fenced code block" in fs["review_feedback"]
