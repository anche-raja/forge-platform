from langchain_aws import ChatBedrockConverse
from langchain_core.messages import HumanMessage, SystemMessage

from forge.agents.base import BaseAgent
from forge.config import ForgeConfig, bedrock_client_config, model_max_tokens
from forge.context.inject import context_block_for, declared_context
from forge.phases import get_phase
from forge.state import ForgeState
from forge.utils.cost import accrue
from forge.utils.llm_json import TransformShapeError, extract_json, normalize_files
from forge.utils.telemetry import get_logger

_log = get_logger(__name__)



class JavaUpgradeAgent(BaseAgent):
    def __init__(self, config: ForgeConfig):
        super().__init__(config)
        self.llm = ChatBedrockConverse(
            model=config.transform_model,
            region_name=config.aws_region,
            max_tokens=model_max_tokens(config, config.transform_model),
            config=bedrock_client_config(config),
        )

    def run(self, state: ForgeState) -> ForgeState:
        file_status = dict(state["current_file"])
        file_path = file_status["file_path"]
        retry_count = file_status.get("retry_count", 0)

        if file_status.get("generate"):
            # The pack creates this file; the descriptors in the context block
            # are its only input.
            source_section = f"No existing file — generate it.\nTarget path: {file_path}"
        else:
            try:
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    source_code = f.read()
            except Exception as e:
                file_status["status"] = "MANUAL_REVIEW"
                file_status["error"] = f"Cannot read file: {e}"
                return {**state, "current_file": file_status}
            source_section = f"```\n{source_code}\n```"

        spec = get_phase(state.get("phase") or file_status.get("phase") or "java21")
        # The "File path:" header is a contract — the CLI tests split on it.
        user_content = f"Transform this file:\nFile path: {file_path}\n\n{source_section}"

        block, digest = context_block_for(state, self.config)
        # `context_name` is recorded whether or not a block arrived. Setting it
        # only on the success path made "the extractor is not built" identical
        # to "this pack wants no context" — both left it null — so a pack
        # transforming without the cross-file facts its author declared looked
        # exactly like one that never needed them.
        declared = declared_context(state)
        file_status["context_name"] = None if declared == "none" else declared
        if block:
            user_content += "\n\n" + block
            file_status["context_digest"] = digest
        elif declared != "none":
            file_status["context_missing"] = True

        # A human's note is its own block, not a value in review_feedback: that
        # field is only rendered on retries and a build failure overwrites it.
        # The note must survive both and outrank automated feedback.
        human_note = file_status.get("human_note")
        if human_note:
            user_content += (
                f"\n\nHUMAN REVIEW FEEDBACK:\n{human_note}\n"
                "Address every point above; it takes precedence over automated feedback."
            )

        if retry_count > 0:
            feedback = file_status.get("review_feedback", "")
            user_content += (
                f"\n\nPREVIOUS REVIEW FEEDBACK (retry {retry_count}):\n{feedback}\n"
                "Address all feedback points in this retry."
            )

        messages = [SystemMessage(content=spec.transform_prompt), HumanMessage(content=user_content)]
        response = self.llm.invoke(messages)
        bedrock_calls = state.get("bedrock_calls", 0) + 1
        cost = accrue(state, response, self.config.transform_model, self.config.get("model_pricing", {}))

        try:
            result = extract_json(response.content)
        except Exception as e:
            _log.warning("Transform output for %s was not valid JSON: %s", file_path, e)
            return self._malformed(state, file_status, f"Failed to parse transform output as JSON: {e}",
                                   bedrock_calls, cost)

        # An empty `files` map is the model saying "nothing here needs changing"
        # (issue #17): the unit is DONE, nothing is written and no reviewer is
        # paid to grade nothing -- it used to score 0 and reach a human. That
        # reading holds only where no change was demanded: a generated unit
        # must produce its file, and a retry was sent back *because* the file
        # needs changes (a reviewer's, the compiler's or a human's), so an
        # empty answer there is a non-answer and goes round the retry loop.
        # A retry after an unreadable reply is a first answer in all but name.
        changes_demanded = bool(human_note) or (retry_count > 0 and not file_status.get("transform_malformed"))
        try:
            if not isinstance(result, dict):
                raise TransformShapeError(f"output is a {type(result).__name__}, expected an object")
            if "files" not in result:
                # Absent is not empty: only an explicit {} is read as "unchanged".
                raise TransformShapeError("no 'files' key; a file that needs no change is \"files\": {}")
            result = {**result, "files": normalize_files(result.get("files"))}
            unchanged = not result["files"] and not result.get("deleted_files")
            if unchanged and file_status.get("generate"):
                raise TransformShapeError("'files' is empty, but this unit is generated and must be written")
            if unchanged and changes_demanded:
                raise TransformShapeError("'files' is empty, but this retry was asked to change the file")
        except TransformShapeError as e:
            # One malformed answer is one file retried, never the end of the run.
            _log.warning("Transform output for %s has the wrong shape: %s", file_path, e)
            return self._malformed(state, file_status, f"Transform output has the wrong shape: {e}",
                                   bedrock_calls, cost)

        # A usable answer clears the last attempt's malformed verdict, or a unit
        # that recovers on retry would reach DONE still carrying its error.
        if file_status.get("transform_malformed"):
            file_status["transform_malformed"] = False
            file_status["error"] = None

        file_status["transform_output"] = result
        file_status["unchanged"] = unchanged
        # struts-spring6 reports XML configs it replaced with Java @Configuration.
        deleted = result.get("deleted_files") or []
        if isinstance(deleted, list) and deleted:
            file_status["deleted_files"] = [str(d) for d in deleted]
        file_status["transform_model"] = self.config.transform_model
        file_status["status"] = "REVIEWING"

        return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}

    def _malformed(self, state, file_status, error: str, bedrock_calls: int, cost: float) -> ForgeState:
        """An answer the pipeline cannot read goes round the retry loop, like a low score.

        It used to go straight to MANUAL_REVIEW, and a broken JSON envelope -- an
        unescaped quote 2,000 characters into a string -- is a formatting slip
        the next call rarely repeats (issue #20). ``route_syntax`` sends the
        unit to ``increment_retry`` within ``max_retries``, with this feedback,
        and to ``manual_queue`` with ``error`` once the budget is spent. The
        previous attempt's output is dropped so nothing downstream can mistake
        it for this attempt's.
        """
        file_status["transform_malformed"] = True
        file_status["transform_output"] = None
        file_status["error"] = error
        file_status["review_feedback"] = (
            f"Your previous reply could not be used. {error}\n"
            "Reply with exactly one valid JSON object in the shape the instructions give: no markdown "
            "fences and no text before or after it. Each file's content is a single JSON string, so "
            "escape every double quote, backslash and newline inside it."
        )
        return {**state, "current_file": file_status, "bedrock_calls": bedrock_calls, "estimated_cost_usd": cost}
