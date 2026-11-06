"""Thin wrapper around the Anthropic API used by every agent.

- `call_json` asks for a JSON answer, parses it robustly and retries once.
- Long shared context (glossary + style guide) goes in a cached system block,
  so every segment of a book reuses it at a fraction of the cost.
- With MOCK_LLM=1 each agent supplies its own fake answer, so the whole
  pipeline can be tested end to end without an API key.
"""
import json
import logging
import re
import time

from . import config

log = logging.getLogger("llm")
def _extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # fall back to the outermost {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("model did not return valid JSON")


def call_json(*, agent: str, model: str, instructions: str, user: str,
              cached_context: str = "", images: list[bytes] | None = None,
              max_tokens: int = 8000, mock=None):
    """Run one agent call and return parsed JSON."""
    if config.MOCK_LLM:
        if mock is None:
            raise RuntimeError(f"agent {agent} has no mock answer")
        return mock()

    from .providers import KeyChain, current_chain
    chain = current_chain.get() or KeyChain.for_user(None)
    parts = [("image", img) for img in images or []] + [("text", user)]

    last_err = None
    for attempt in range(2):
        turn = list(parts)
        if attempt == 1:
            turn.append(("text", "Your previous answer was not valid JSON. Reply with the JSON object only."))
        started = time.time()
        text = chain.call(agent, instructions, cached_context, turn, max_tokens)
        log.info("%s %.1fs", agent, time.time() - started)
        try:
            return _extract_json(text)
        except ValueError as e:
            last_err = e
            log.warning("%s returned non-JSON, retrying", agent)
    raise last_err
