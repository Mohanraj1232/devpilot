"""Amazon Bedrock client using the Converse API with tool use for structured output."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import boto3  # type: ignore[import-untyped]
from pydantic import ValidationError

from ai_hub.errors import FailureReason, LLMError

logger = logging.getLogger("ai_hub.llm")

_RETRYABLE_ERRORS = ("ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException")
_MAX_RETRIES = 3
_BASE_DELAY = 2.0


class BedrockClient:
    """Thin wrapper around Bedrock Converse API with retry and structured output."""

    def __init__(
        self,
        model_id: str,
        *,
        region: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> None:
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature
        session = boto3.Session(region_name=region)
        self.client = session.client("bedrock-runtime")

    def converse(
        self,
        messages: list[dict[str, Any]],
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Call the Converse API with retries for throttling/server errors."""
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }
        if system:
            kwargs["system"] = [{"text": system}]
        if tools:
            kwargs["toolConfig"] = {"tools": tools}

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response: dict[str, Any] = self.client.converse(**kwargs)
                return response
            except self.client.exceptions.AccessDeniedException as exc:
                raise LLMError(
                    FailureReason.AUTH_FAILURE,
                    f"Bedrock access denied: {exc}",
                ) from exc
            except self.client.exceptions.ResourceNotFoundException as exc:
                raise LLMError(
                    FailureReason.MODEL_UNAVAILABLE,
                    f"Model not found: {self.model_id}",
                ) from exc
            except self.client.exceptions.ValidationException as exc:
                raise LLMError(
                    FailureReason.INVALID_RESPONSE,
                    f"Bedrock validation error: {exc}",
                ) from exc
            except Exception as exc:
                error_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if error_code in _RETRYABLE_ERRORS and attempt < _MAX_RETRIES - 1:
                    delay = _BASE_DELAY * (2**attempt)
                    logger.warning(
                        "Bedrock %s (attempt %d/%d), retrying in %.1fs",
                        error_code,
                        attempt + 1,
                        _MAX_RETRIES,
                        delay,
                    )
                    time.sleep(delay)
                    last_error = exc
                    continue
                raise LLMError(
                    FailureReason.MODEL_UNAVAILABLE,
                    f"Bedrock call failed: {exc}",
                ) from exc

        raise LLMError(
            FailureReason.MODEL_UNAVAILABLE,
            f"Bedrock call failed after {_MAX_RETRIES} retries",
        ) from last_error

    def converse_with_tool(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tool_schema: dict[str, Any],
        response_model: Any,
    ) -> Any:
        """Call Converse with a tool and validate+parse the tool use response."""
        tools = [{"toolSpec": tool_schema}]
        response = self.converse(messages, system=system, tools=tools)

        output = response.get("output", {})
        content_blocks = output.get("message", {}).get("content", [])

        tool_use_block = None
        for block in content_blocks:
            if "toolUse" in block:
                tool_use_block = block["toolUse"]
                break

        if tool_use_block is None:
            raise LLMError(
                FailureReason.INVALID_RESPONSE,
                "Model did not return a tool use response",
                details={"content": content_blocks},
            )

        tool_input = tool_use_block.get("input", {})

        try:
            return response_model.model_validate(tool_input)
        except ValidationError as exc:
            raise LLMError(
                FailureReason.INVALID_RESPONSE,
                f"Tool response validation failed: {exc}",
                details={"raw_input": tool_input, "errors": exc.errors()},
            ) from exc

    def converse_with_tool_retry(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tool_schema: dict[str, Any],
        response_model: Any,
        *,
        max_validation_retries: int = 1,
    ) -> Any:
        """Call Converse with tool use. On validation failure, re-ask once with the error."""
        try:
            return self.converse_with_tool(messages, system, tool_schema, response_model)
        except LLMError as exc:
            if exc.reason != FailureReason.INVALID_RESPONSE or max_validation_retries < 1:
                raise

            logger.warning("Tool response validation failed, retrying with error context")
            error_details = json.dumps(exc.details.get("errors", []), default=str)
            retry_messages = messages + [
                {
                    "role": "assistant",
                    "content": [{"text": "I'll fix the output format."}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "text": (
                                f"Your previous response had validation errors: {error_details}. "
                                "Please call the tool again with corrected output."
                            )
                        }
                    ],
                },
            ]
            return self.converse_with_tool(retry_messages, system, tool_schema, response_model)
