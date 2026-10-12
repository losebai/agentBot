"""Bounded JSON Schema subset without remote retrieval or regular expressions."""

import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from agent_bot.agent.errors import AgentError

_KEYWORDS = {
    "type",
    "title",
    "description",
    "default",
    "enum",
    "const",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minProperties",
    "maxProperties",
    "anyOf",
    "oneOf",
    "allOf",
    "not",
    "$defs",
    "$ref",
    "$schema",
    "uniqueItems",
}


def bounded_json(value, *, maximum=65536, depth=16):
    def walk(node, remaining):
        if remaining < 0:
            raise AgentError("AGENT_TOOL_SCHEMA_INVALID", status_code=422)
        if isinstance(node, dict):
            for item in node.values():
                walk(item, remaining - 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, remaining - 1)

    walk(value, depth)
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise AgentError("AGENT_TOOL_ARGUMENTS_INVALID", status_code=422) from error
    if len(result.encode()) > maximum:
        raise AgentError("AGENT_TOOL_RESULT_TOO_LARGE", status_code=422)
    return result


class ToolSchema:
    @staticmethod
    def validate_definition(tool):
        root = tool.input_schema
        bounded_json(root)
        if root.get("type") != "object":
            raise AgentError("AGENT_TOOL_SCHEMA_INVALID", status_code=422)
        visits = 0

        def visit(schema, refs=()):
            nonlocal visits
            visits += 1
            if visits > 512 or len(refs) > 16:
                raise AgentError("AGENT_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
            if isinstance(schema, bool):
                return
            if not isinstance(schema, dict) or set(schema) - _KEYWORDS:
                raise AgentError("AGENT_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
            ref = schema.get("$ref")
            if ref is not None:
                if not isinstance(ref, str) or not ref.startswith("#/$defs/") or ref in refs:
                    raise AgentError("AGENT_TOOL_SCHEMA_UNSUPPORTED", status_code=422)
                target = root
                try:
                    for segment in ref[2:].split("/"):
                        target = target[segment.replace("~1", "/").replace("~0", "~")]
                except (KeyError, TypeError) as error:
                    raise AgentError("AGENT_TOOL_SCHEMA_INVALID", status_code=422) from error
                visit(target, (*refs, ref))
            for key in ("properties", "$defs"):
                values = schema.get(key, {})
                if not isinstance(values, dict):
                    raise AgentError("AGENT_TOOL_SCHEMA_INVALID", status_code=422)
                for item in values.values():
                    visit(item, refs)
            for key in ("items", "additionalProperties", "not"):
                if key in schema:
                    visit(schema[key], refs)
            for key in ("allOf", "anyOf", "oneOf"):
                for item in schema.get(key, []):
                    visit(item, refs)

        try:
            Draft202012Validator.check_schema(root)
            visit(root)
        except (SchemaError, TypeError, RecursionError) as error:
            raise AgentError("AGENT_TOOL_SCHEMA_INVALID", status_code=422) from error

    @staticmethod
    def validate_arguments(tool, arguments):
        ToolSchema.validate_definition(tool)
        bounded_json(arguments)
        try:
            Draft202012Validator(tool.input_schema).validate(arguments)
        except ValidationError as error:
            raise AgentError("AGENT_TOOL_ARGUMENTS_INVALID", status_code=422) from error
        return arguments
