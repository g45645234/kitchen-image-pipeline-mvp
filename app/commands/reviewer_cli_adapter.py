from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
from typing import Any


OUTPUT_CONTRACT = {
    "reviewer_name": "codex | antigravity | claude_cli",
    "reviewer_version": "short backend/model/version string",
    "score": "number from 0.0 to 1.0",
    "verdict": "pass | maybe | fail",
    "reason": "brief explanation in Russian or English",
    "flags": {
        "kitchen_detected": "boolean if assessable",
        "watermark": "boolean if visible",
        "text_overlay": "boolean if visible",
        "low_quality": "boolean if visible",
        "rights_comment": "AI must not confirm rights",
    },
}


def build_prompt(reviewer_name: str, payload: dict[str, Any]) -> str:
    return f"""You are the {reviewer_name} visual suitability reviewer for a kitchen image pipeline.

Task:
Evaluate the image candidate for visual relevance and quality against the given mistake, side, prompts, and rubric.
If candidate.image_file_path is present, open and inspect that local image file; base visual judgments on the actual image, not only URL text or metadata.
Do not confirm copyright, licensing, ownership, or commercial-use rights.
Do not select a final asset.

Compute the assessment from the review payload and respond as a single valid JSON object.
Do not copy placeholder values; choose score, verdict, reason, and flags from the evidence in the payload.
Do not wrap the JSON in markdown or add prose outside the JSON.
Required JSON response schema:
{json.dumps(OUTPUT_CONTRACT, ensure_ascii=False, indent=2)}

Review payload:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def run_backend(command: str, prompt: str, prompt_as_arg: bool, timeout: int) -> str:
    args = shlex.split(command)
    if not args:
        raise ValueError("backend command is empty")
    input_data = None if prompt_as_arg else prompt.encode("utf-8")
    if prompt_as_arg:
        args.append(prompt)
    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        stdout, stderr = process.communicate()
        raise TimeoutError("backend command timed out") from e
    if process.returncode != 0:
        error_text = stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"backend command failed with code {process.returncode}: {error_text[:1000]}")
    return stdout.decode("utf-8", errors="replace").strip()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adapt a generic AI CLI into the candidate reviewer JSON contract")
    parser.add_argument("--reviewer", required=True, choices=["codex", "antigravity", "claude_cli"])
    parser.add_argument("--backend-command", required=True)
    parser.add_argument("--prompt-as-arg", action="store_true")
    parser.add_argument("--timeout", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("stdin payload must be a JSON object")
        prompt = build_prompt(args.reviewer, payload)
        print(run_backend(args.backend_command, prompt, args.prompt_as_arg, args.timeout))
        return 0
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
