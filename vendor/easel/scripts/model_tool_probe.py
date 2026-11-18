"""Opt-in, two-request Chat Completions tool-contract probe; no real tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easel.timeouts import TIMEOUT_MODEL_TOOL_PROBE


TOOL = {"type": "function", "function": {
    "name": "lookup_receipt", "description": "Read a receipt from the test fixture.",
    "parameters": {"type": "object", "properties": {"ticket": {"type": "string"}},
                   "required": ["ticket"], "additionalProperties": False},
}}
MAX_RESPONSE_BYTES = 1024 * 1024


def endpoint(base_url):
    url = urlsplit(base_url)
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or url.scheme not in {"http", "https"}
            or (url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("Use HTTPS or loopback HTTP without URL credentials/query/fragment")
    try:
        httpx.URL(base_url)
        url.port
    except (httpx.InvalidURL, ValueError) as exc:
        raise ValueError("Invalid endpoint") from exc
    return base_url.rstrip("/") + "/chat/completions"


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Non-finite JSON value")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


async def probe(base_url, api_key, model, *, max_tokens=256,
                timeout=TIMEOUT_MODEL_TOOL_PROBE, transport=None):
    """Return only aggregate evidence. Transport injection is for offline tests."""
    target = endpoint(base_url)
    if (not api_key or not model or type(max_tokens) is not int or not 1 <= max_tokens <= 1024
            or type(timeout) not in (int, float) or not 0 < timeout <= TIMEOUT_MODEL_TOOL_PROBE):
        raise ValueError("Invalid probe configuration")
    result = {"schema_version": 1, "scenario": "model-tool-contract-v1", "status": "not_run",
              "requests_started": 0, "responses_received": 0, "fixture_tool_calls": 0,
              "prompt_tokens": None, "completion_tokens": None, "cost": None,
              "max_requests": 2, "max_completion_tokens_per_request": max_tokens}
    usages = []
    receipt = secrets.token_hex(16)
    messages = [{"role": "user", "content":
                 "Call lookup_receipt exactly once with ticket='probe'. "
                 "Then reply with only the receipt value returned by that tool, without formatting."}]

    async def run():
        # No automatic retries, redirects or environment proxies carrying the credential elsewhere.
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                     follow_redirects=False, timeout=timeout) as client:
            for turn in range(2):
                result["requests_started"] += 1
                async with client.stream("POST", target, headers={"Authorization": f"Bearer {api_key}"},
                                         json={"model": model, "messages": messages, "tools": [TOOL],
                                               "tool_choice": "auto" if turn == 0 else "none",
                                               "stream": False, "max_tokens": max_tokens}) as response:
                    if response.status_code != 200:
                        result["status"] = "http_error"
                        return
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            result["status"] = "response_too_large"
                            return
                result["responses_received"] += 1
                body = decode(raw)
                usage = body.get("usage")
                if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                                  for k in ("prompt_tokens", "completion_tokens")):
                    usages.append(usage)
                choices = body["choices"]
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError("Expected one choice")
                choice = choices[0]
                message = choice["message"]
                if message["role"] != "assistant" or message.get("refusal"):
                    raise ValueError("Expected assistant response")
                calls = message.get("tool_calls")
                if (calls is not None and not isinstance(calls, list)) or message.get("function_call") is not None:
                    raise ValueError("Unexpected tool-call representation")
                if turn == 0:
                    if choice["finish_reason"] != "tool_calls" or not isinstance(calls, list) or len(calls) != 1:
                        result["status"] = "tool_contract_failed"
                        return
                    call = calls[0]
                    if (call["type"] != "function" or not isinstance(call["id"], str)
                            or not call["id"].strip() or len(call["id"]) > 512
                            or call["function"]["name"] != "lookup_receipt"
                            or decode(call["function"]["arguments"]) != {"ticket": "probe"}):
                        result["status"] = "tool_contract_failed"
                        return
                    result["fixture_tool_calls"] += 1
                    # Forward only protocol fields, never provider metadata or reasoning traces.
                    normalized = {"id": call["id"], "type": "function", "function": {
                        "name": "lookup_receipt", "arguments": '{"ticket":"probe"}'}}
                    messages.extend([{"role": "assistant", "content": None, "tool_calls": [normalized]},
                                     {"role": "tool", "tool_call_id": call["id"], "content": receipt}])
                else:
                    result["status"] = ("passed" if choice["finish_reason"] == "stop" and not calls
                                        and message.get("content") == receipt else "answer_contract_failed")

    started = time.monotonic()
    try:
        await asyncio.wait_for(run(), timeout=timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        result["status"] = "timeout"
    except httpx.HTTPError:
        result["status"] = "transport_error"
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        result["status"] = "invalid_response"
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    # Partial/missing accounting is unknown, not zero or an understated partial sum.
    if len(usages) == result["requests_started"]:
        for key in ("prompt_tokens", "completion_tokens"):
            result[key] = sum(usage[key] for usage in usages)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--live", action="store_true", help="Explicitly permit up to two model requests")
    parser.add_argument("--max-tokens", type=int, default=256)
    args = parser.parse_args()
    if not args.live:
        parser.error("Live requests require --live")
    try:
        result = asyncio.run(probe(os.environ["EASEL_PROBE_BASE_URL"], os.environ["EASEL_PROBE_API_KEY"],
                                   args.model, max_tokens=args.max_tokens))
    except (KeyError, ValueError):
        print("Invalid configuration; set EASEL_PROBE_BASE_URL and EASEL_PROBE_API_KEY.", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
