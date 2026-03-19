"""
Upstream commit evaluator CLI.

Fetches new commits from the hermes-function-calling upstream,
shows diffs, and evaluates each under AGP TECHNICAL domain rules.
"""

import json
import subprocess
import sys

from inference import get_architect


UPSTREAM_REMOTE = "origin"
PINNED_COMMIT = "ea3c4723"
HERMES_DIR = "hermes-function-calling"


def run_git(args: list[str], cwd: str = HERMES_DIR) -> str:
    result = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True
    )
    return result.stdout.strip()


def fetch_upstream() -> None:
    print("Fetching upstream commits...")
    subprocess.run(
        ["git", "fetch", UPSTREAM_REMOTE],
        cwd=HERMES_DIR,
        capture_output=True,
    )


def get_new_commits() -> list[dict]:
    """Get commits between pinned and upstream HEAD."""
    log_output = run_git([
        "log", f"{PINNED_COMMIT}..{UPSTREAM_REMOTE}/main",
        "--format=%H|%s|%an|%ai", "--reverse",
    ])
    if not log_output:
        return []

    commits = []
    for line in log_output.strip().split("\n"):
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "hash": parts[0],
                "subject": parts[1],
                "author": parts[2],
                "date": parts[3],
            })
    return commits


def get_diff(commit_hash: str) -> str:
    return run_git(["diff", f"{commit_hash}~1..{commit_hash}"])


def evaluate_commit(commit: dict, diff: str) -> dict:
    """Have the Architect evaluate a commit under AGP TECHNICAL rules."""
    architect = get_architect()
    messages = [
        {"role": "system", "content": architect.config.system_prompt},
        {"role": "user", "content": (
            f"Evaluate this upstream commit for the Callisto project under AGP TECHNICAL domain rules.\n\n"
            f"Commit: {commit['hash'][:12]}\n"
            f"Subject: {commit['subject']}\n"
            f"Author: {commit['author']}\n"
            f"Date: {commit['date']}\n\n"
            f"Diff:\n```\n{diff[:4000]}\n```\n\n"
            f"We import from hermes-function-calling: functions.py, prompter.py, schema.py, utils.py, validator.py.\n"
            f"We do NOT use functioncall.py or jsonmode.py (they need torch).\n\n"
            f"Evaluate and respond with JSON:\n"
            f'{{"decision": "PULL|SKIP|MODIFY_THEN_PULL", '
            f'"reasoning": "...", '
            f'"risk_level": "LOW|MEDIUM|HIGH", '
            f'"affected_files": ["..."]}}'
        )},
    ]
    response = architect.chat(messages)
    parsed = response.get("parsed_json")
    if parsed:
        return parsed
    return {"decision": "SKIP", "reasoning": "Could not parse evaluation", "raw": response["content"]}


def main():
    fetch_upstream()
    commits = get_new_commits()

    if not commits:
        print("No new upstream commits since pinned commit.")
        return

    print(f"\nFound {len(commits)} new commit(s) since {PINNED_COMMIT[:8]}:\n")

    for commit in commits:
        print(f"{'='*60}")
        print(f"  {commit['hash'][:12]} — {commit['subject']}")
        print(f"  {commit['author']} | {commit['date']}")

        diff = get_diff(commit["hash"])
        if not diff:
            print("  (no diff available)")
            continue

        print(f"  Diff size: {len(diff)} chars")
        print("  Evaluating with Architect...")

        evaluation = evaluate_commit(commit, diff)
        decision = evaluation.get("decision", "UNKNOWN")
        reasoning = evaluation.get("reasoning", "")
        risk = evaluation.get("risk_level", "UNKNOWN")

        print(f"  Decision: {decision}")
        print(f"  Risk: {risk}")
        print(f"  Reasoning: {reasoning}")
        print()


if __name__ == "__main__":
    main()
