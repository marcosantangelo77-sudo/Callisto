"""Learning-value/key sanitization for Hermes memory (audit C-4).

Extracted verbatim from tools/hermes_memory.py during the tools.hmem split.
"""

import re


# SECURITY (audit C-4): sanitize values before storing so prompt-injection
# markers can't survive the round-trip through hermes_learnings → context
# → next Claude prompt. We neutralize tag-like metacharacters, code-fence
# markers, and common system-prompt sentinels, and cap the length.
def sanitize_learning_value(value: str) -> str:
    if not isinstance(value, str):
        value = str(value)
    # Cap before substitution so attacker-supplied massive payload doesn't
    # consume CPU re-stringifying. 4KB is plenty for a learning summary.
    if len(value) > 4096:
        value = value[:4096] + " …[truncated]"
    # Neutralize HTML/XML-ish tags and code fences. We escape rather than
    # strip so the original signal stays human-readable in audits.
    value = (
        value.replace("\u200b", "")  # zero-width space sometimes used to bypass filters
             .replace("<", "‹")
             .replace(">", "›")
             .replace("```", "ʼʼʼ")
             .replace("\x00", "")
    )
    # Strip common LLM jailbreak sentinels by escaping the leading bracket.
    for sentinel in (
        "[INST]", "[/INST]",
        "[SYSTEM]", "[/SYSTEM]",
        "{{system}}", "{{/system}}",
        "<|im_start|>", "<|im_end|>",
    ):
        value = value.replace(sentinel, sentinel.replace("[", "(").replace("]", ")")
                              .replace("<", "‹").replace(">", "›")
                              .replace("{", "(").replace("}", ")"))
    return value


def sanitize_learning_key(key: str) -> str:
    """Keys must be short, ASCII-ish identifiers — they appear verbatim in prompts."""
    if not isinstance(key, str):
        key = str(key)
    key = key.strip()
    if not key:
        raise ValueError("learning key must be non-empty")
    if len(key) > 128:
        key = key[:128]
    # Permissive but no markup: letters, digits, underscore, dash, dot, colon, slash.
    return re.sub(r"[^A-Za-z0-9_\-\.:/]+", "_", key)
