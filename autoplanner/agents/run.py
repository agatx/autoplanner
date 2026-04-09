from __future__ import annotations

import subprocess
import sys


def stream_command(cmd: list[str], *, label: str) -> str:
    """Run a subprocess, streaming its output line-by-line with a label prefix.

    Returns the full captured stdout.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        print(f"  [{label}] {line}", end="", flush=True)
        lines.append(line)

    # Drain stderr after stdout is done
    assert proc.stderr is not None
    stderr = proc.stderr.read()
    proc.wait()

    if proc.returncode != 0:
        if stderr:
            print(f"  [{label}] stderr: {stderr}", file=sys.stderr)
        raise RuntimeError(f"{label} exited with code {proc.returncode}")

    return "".join(lines).strip()
