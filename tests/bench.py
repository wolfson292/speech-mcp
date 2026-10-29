#!/usr/bin/env python3
"""Time each transcription strategy on the fixtures.

    .venv/bin/python tests/bench.py [runs]

Runs each configuration once untimed to page model weights into memory, then
reports the median wall time of `runs` timed runs (default 3). "cold" means one
`speech transcribe` process per speaker turn; "warm" means one
`speech transcribe-batch` process for all turns.
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402

FIX = ROOT / "tests" / "fixtures"

CONFIGS = [
    # label, fixture, strategy, engine, batch
    ("moss end-to-end", "meeting_mono.wav", "moss", "cohere", True),
    ("plain cohere", "meeting_mono.wav", "plain", "cohere", True),
    ("two_stage cohere (cold)", "meeting_mono.wav", "two_stage", "cohere", True),
    ("two_stage qwen3 cold", "meeting_mono.wav", "two_stage", "qwen3", False),
    ("two_stage qwen3 warm", "meeting_mono.wav", "two_stage", "qwen3", True),
    ("stereo cohere (cold)", "meeting_stereo.wav", "stereo", "cohere", True),
    ("stereo qwen3 cold", "meeting_stereo.wav", "stereo", "qwen3", False),
    ("stereo qwen3 warm", "meeting_stereo.wav", "stereo", "qwen3", True),
]


def run_once(path: Path, strategy: str, engine: str, batch: bool) -> tuple[float, int, str]:
    os.environ["SPEECH_MCP_BATCH"] = "1" if batch else "0"
    with tempfile.TemporaryDirectory(prefix="speech-bench-") as tmp:
        t0 = time.perf_counter()
        turns, method = server.STRATEGIES[strategy](
            path, Path(tmp), engine=engine, num_speakers=2 if strategy == "two_stage" else None,
        )
        return time.perf_counter() - t0, len(turns), method


def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    if not (FIX / "meeting_mono.wav").exists():
        sys.exit("Run tests/make_fixtures.sh first.")
    print(f"{'configuration':28} {'median s':>9} {'turns':>6}  method")
    for label, fixture, strategy, engine, batch in CONFIGS:
        path = FIX / fixture
        run_once(path, strategy, engine, batch)  # warm the page cache
        samples = [run_once(path, strategy, engine, batch) for _ in range(runs)]
        med = statistics.median(s[0] for s in samples)
        print(f"{label:28} {med:9.2f} {samples[-1][1]:6d}  {samples[-1][2]}")


if __name__ == "__main__":
    main()
