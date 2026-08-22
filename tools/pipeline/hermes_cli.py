"""PipelineModel backed by the Hermes CLI (Nous Portal / stealth-ox-alpha).

Why shell out instead of using ProviderRouter: Hermes holds its Nous OAuth
token in the macOS keychain under its own service entry. Driving the CLI uses
that auth without any process reading, copying, or storing the credential.

This is a real-model backend for end-to-end runs. It is slower than an HTTP
client (each call is a fresh CLI session) and is not meant for hot paths.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from typing import Optional

from tools.pipeline.model import PipelineModel

_HERMES = os.path.expanduser("~/.hermes/bin/hermes")


def hermes_available() -> bool:
    return os.path.exists(_HERMES) or bool(shutil.which("hermes"))


class HermesCliModel(PipelineModel):
    """Each complete() is one `hermes -z` invocation returning final text."""

    name = "hermes-cli"

    def __init__(self, binary: Optional[str] = None, timeout_s: float = 180.0,
                 cwd: Optional[str] = None):
        self.binary = binary or (_HERMES if os.path.exists(_HERMES)
                                 else shutil.which("hermes") or "hermes")
        self.timeout_s = timeout_s
        self.cwd = cwd or "/tmp"
        self.calls: list[dict] = []

    @staticmethod
    def _flatten(role: str, messages: list[dict]) -> str:
        parts = []
        for m in messages:
            who = m.get("role", "user")
            body = m.get("content", "")
            parts.append(body if who == "user" else f"[{who}]\n{body}")
        parts.append(
            "\nRespond with ONLY the JSON object requested. No prose, no code "
            "fences, no commentary before or after."
        )
        return "\n\n".join(p for p in parts if p)

    async def complete(self, role: str, messages: list[dict],
                       schema: Optional[dict] = None, **_ignored) -> dict:
        # Callers differ: the pipeline passes (role, messages); the
        # Adversary passes task_class/messages/schema by keyword. Accept
        # both — a signature mismatch here made the adversary backend
        # crash, and because it correctly fails closed that surfaced as a
        # veto rather than an error, which is much harder to diagnose.
        prompt = self._flatten(role, messages)
        proc = await asyncio.create_subprocess_exec(
            self.binary, "-z", prompt, "--in", self.cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(),
                                              timeout=self.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"hermes timed out after {self.timeout_s}s (role={role})")
        text = (out or b"").decode("utf-8", "replace").strip()
        self.calls.append({"role": role, "chars_in": len(prompt),
                           "chars_out": len(text),
                           "stderr": (err or b"").decode("utf-8", "replace")[-200:]})
        if proc.returncode != 0 and not text:
            raise RuntimeError(f"hermes failed (rc={proc.returncode}): "
                               f"{(err or b'').decode('utf-8','replace')[:300]}")
        return {"content": text}
