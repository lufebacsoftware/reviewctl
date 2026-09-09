#!/usr/bin/env python3
"""Run a provider-free end-to-end receipt canary for CI and local diagnostics."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def write_fake_llm(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sqlite3
import sys
from pathlib import Path

arguments = sys.argv[1:]
database = Path(arguments[arguments.index('-d') + 1])
model = arguments[arguments.index('-m') + 1]
database.parent.mkdir(parents=True, exist_ok=True)
connection = sqlite3.connect(database)
connection.execute(
    'CREATE TABLE responses ('
    'id INTEGER PRIMARY KEY, response TEXT, conversation_id TEXT, model TEXT, '
    'input_tokens INTEGER, output_tokens INTEGER)'
)
connection.execute(
    'INSERT INTO responses VALUES (1, ?, ?, ?, ?, ?)',
    (json.dumps({'verdict': 'approved', 'findings': []}), 'synthetic-canary', model, 1, 1),
)
connection.commit()
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run(command: list[str], *, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=False, env=environment)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="reviewctl-synthetic-canary-") as temporary_directory:
        root = Path(temporary_directory)
        prompt = root / "prompt.md"
        source = root / "source.py"
        fake_llm = root / "llm"
        artifact_root = root / "artifacts"
        prompt.write_text("Return the requested findings JSON.\n")
        source.write_text("def add(left: int, right: int) -> int:\n    return left + right\n")
        write_fake_llm(fake_llm)
        environment = {**os.environ, "LLM_BIN": str(fake_llm)}
        result = run(
            [
                sys.executable,
                "-m",
                "reviewctl",
                "run",
                "--review-id",
                "synthetic-canary",
                "--prompt-file",
                str(prompt),
                "--file",
                str(source),
                "--model",
                "synthetic-canary",
                "--transport",
                "llm",
                "--source-class",
                "synthetic",
                "--response-contract",
                "findings-json",
                "--artifact-root",
                str(artifact_root),
                "--timeout-seconds",
                "5",
            ],
            environment=environment,
        )
        if result.returncode:
            sys.stderr.write(result.stderr)
            return result.returncode
        receipts = list(artifact_root.glob("synthetic-canary/*/receipt.json"))
        if len(receipts) != 1:
            sys.stderr.write(f"expected one canary receipt, found {len(receipts)}\n")
            return 1
        verification = run(
            [sys.executable, "-m", "reviewctl", "verify", str(receipts[0])],
            environment=environment,
        )
        if verification.returncode:
            sys.stderr.write(verification.stderr)
            return verification.returncode
        print(verification.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
