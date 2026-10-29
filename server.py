#!/usr/bin/env python3
"""
speech-mcp: an MCP server exposing the local speech-swift install as tools.

Wraps the `speech` CLI so any MCP client can transcribe audio with speaker
separation without needing shell access or a folder grant.

Transports:
    python server.py                      # stdio (Claude Desktop, Claude Code)
    python server.py --http --port 8420   # streamable HTTP (remote clients)

Environment:
    SPEECH_BIN          path to the speech binary (default: search PATH)
    SPEECH_MCP_ROOTS    colon-separated directories that may be read
                        (default: ~/Documents:~/Downloads:~/Movies:~/Music:~/Desktop)
    SPEECH_MCP_TOKEN    bearer token required in HTTP mode (strongly recommended)
    QWEN3_CACHE_DIR     model weight cache (leave unset to use the shared cache)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

DEFAULT_ROOTS = ["~/Documents", "~/Downloads", "~/Movies", "~/Music", "~/Desktop"]

CANDIDATE_BINARIES = [
    os.environ.get("SPEECH_BIN", ""),
    shutil.which("speech") or "",
    str(Path.home() / "speech-swift/.build/release/speech"),
    "/opt/homebrew/bin/speech",
    "/usr/local/bin/speech",
]

# Extensions speech accepts without conversion.
NATIVE_EXTS = {".wav", ".m4a", ".mp3", ".caf"}


def speech_bin() -> str:
    for candidate in CANDIDATE_BINARIES:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError(
        "The speech binary was not found. Set SPEECH_BIN to its full path. "
        "This server wraps a local speech-swift install and cannot transcribe without it."
    )


def allowed_roots() -> list[Path]:
    raw = os.environ.get("SPEECH_MCP_ROOTS")
    entries = raw.split(":") if raw else DEFAULT_ROOTS
    roots = []
    for e in entries:
        if not e.strip():
            continue
        p = Path(e).expanduser()
        try:
            roots.append(p.resolve())
        except OSError:
            continue
    return roots


def resolve_media(path_str: str) -> Path:
    """Resolve a user-supplied path and confirm it sits inside an allowed root.

    Symlinks are resolved before the check so a link cannot escape the
    allowlist.
    """
    p = Path(path_str).expanduser()
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot resolve path: {path_str} ({exc})") from exc
    if not p.is_file():
        raise ValueError(f"Not a file: {p}")

    roots = allowed_roots()
    if not any(p == r or r in p.parents for r in roots):
        raise ValueError(
            f"{p} is outside the allowed roots. Allowed: "
            + ", ".join(str(r) for r in roots)
            + ". Set SPEECH_MCP_ROOTS to widen this."
        )
    return p


# --------------------------------------------------------------------------
# Subprocess helpers
# --------------------------------------------------------------------------


def run_cmd(cmd: list[str], timeout: int = 3600) -> str:
    """Run a command with no shell. Raises on non-zero exit."""
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, shell=False
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({proc.returncode}): {tail}")
    return proc.stdout


def require_ffmpeg() -> tuple[str, str]:
    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if not ff or not fp:
        raise RuntimeError(
            "ffmpeg and ffprobe are required for format conversion and channel "
            "inspection. Install with: brew install ffmpeg"
        )
    return ff, fp


def probe_channels(path: Path) -> int:
    _, fp = require_ffmpeg()
    out = run_cmd(
        [fp, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
        timeout=60,
    )
    try:
        return int(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def probe_duration(path: Path) -> float:
    _, fp = require_ffmpeg()
    out = run_cmd(
        [fp, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        timeout=60,
    )
    try:
        return float(out.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0.0


def to_wav(src: Path, dst: Path, channel: int | None = None) -> Path:
    """Downmix to 16 kHz mono WAV, optionally extracting a single channel."""
    ff, _ = require_ffmpeg()
    cmd = [ff, "-nostdin", "-loglevel", "error", "-y", "-i", str(src)]
    if channel is not None:
        cmd += ["-filter_complex", f"pan=mono|c0=c{channel}"]
    else:
        cmd += ["-ac", "1"]
    cmd += ["-vn", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)]
    run_cmd(cmd)
    return dst


def slice_wav(src: Path, dst: Path, start: float, end: float, pad: float = 0.2) -> Path:
    ff, _ = require_ffmpeg()
    s = max(0.0, start - pad)
    d = (end - start) + 2 * pad
    run_cmd([
        ff, "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{s:.3f}", "-t", f"{d:.3f}", "-i", str(src),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst),
    ])
    return dst


# --------------------------------------------------------------------------
# speech CLI wrappers
# --------------------------------------------------------------------------


# `speech transcribe` has no JSON mode. stdout interleaves progress logs with a
# "Result: <text>" line, then a "  Time: ..." line (and for moss a "Segments:"
# block). See docs/cli-reference.md.
RESULT_END = re.compile(r"^\s*(Time:|Segments:)")


def parse_result(stdout: str) -> str:
    """Pull the transcript out of `speech transcribe` stdout."""
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("Result:"):
            parts = [line[len("Result:"):]]
            for nxt in lines[i + 1:]:
                if RESULT_END.match(nxt):
                    break
                parts.append(nxt)
            return " ".join(" ".join(parts).split())
    tail = stdout.strip()[-400:]
    raise RuntimeError(f"speech transcribe printed no 'Result:' line. Output tail: {tail}")


def extract_json(stdout: str) -> Any:
    """`speech diarize --json` prints progress lines before the JSON on stdout."""
    for m in re.finditer(r"^[\[{]", stdout, re.M):
        try:
            obj, _ = json.JSONDecoder().raw_decode(stdout, m.start())
        except json.JSONDecodeError:
            continue  # a progress line such as "[61%] Downloading..."
        return obj
    raise RuntimeError(f"No JSON found in speech output: {stdout.strip()[-400:]}")


def cli_transcribe(audio: Path, engine: str, extra: list[str] | None = None) -> str:
    cmd = [speech_bin(), "transcribe", str(audio), "--engine", engine]
    cmd += extra or []
    return parse_result(run_cmd(cmd))


def cli_diarize(audio: Path, engine: str = "community1",
                num_speakers: int | None = None,
                min_speakers: int | None = None,
                max_speakers: int | None = None) -> list[dict]:
    cmd = [speech_bin(), "diarize", str(audio), "--engine", engine, "--json"]
    if engine == "community1":
        if num_speakers:
            cmd += ["--num-speakers", str(num_speakers)]
        if min_speakers:
            cmd += ["--min-speakers", str(min_speakers)]
        if max_speakers:
            cmd += ["--max-speakers", str(max_speakers)]
    return normalize_segments(extract_json(run_cmd(cmd)))


def _pick(d: dict, *keys):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def speaker_label(spk: Any) -> str:
    """Diarize emits 0-based ints. Render them like MOSS does (S01, S02, ...)."""
    if isinstance(spk, bool) or spk is None:
        return "S?"
    if isinstance(spk, int):
        return f"S{spk + 1:02d}"
    if isinstance(spk, str) and spk.isdigit():
        return f"S{int(spk) + 1:02d}"
    return str(spk)


def normalize_segments(raw: Any) -> list[dict]:
    """Normalize `speech diarize --json` output.

    Real schema (speech 0.0.28, every engine):
        {"num_speakers": 2,
         "segments": [{"start": 0.03, "end": 3.79, "duration": 3.76, "speaker": 0}]}
    Other spellings are tolerated in case a release changes them.
    """
    if isinstance(raw, dict):
        for key in ("segments", "results", "diarization", "speakers", "turns"):
            if key in raw:
                raw = raw[key]
                break
    segs: list[dict] = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        start = _pick(s, "start", "startTime", "start_time", "begin", "from")
        end = _pick(s, "end", "endTime", "end_time", "stop", "to")
        if end is None and start is not None and s.get("duration") is not None:
            end = float(start) + float(s["duration"])
        spk = _pick(s, "speaker", "speakerId", "speaker_id", "label", "id")
        if start is None or end is None:
            continue
        segs.append({
            "start": float(start),
            "end": float(end),
            "speaker": speaker_label(spk),
        })
    segs.sort(key=lambda x: x["start"])
    return segs


# MOSS prints a "Segments:" block after the Result line, one turn per line:
#   "  [0.07s-3.86s][S01] Thanks for joining."
# The stream form [0.48][S01]text[1.66] and bare "[S01] text" lines are kept
# as fallbacks in case a release changes the format.
MOSS_SEGMENT = re.compile(
    r"^\s*\[(\d+(?:\.\d+)?)s?\s*-\s*(\d+(?:\.\d+)?)s?\]\s*\[(S\d+)\]\s*(.*?)\s*$", re.M
)
MOSS_STREAM = re.compile(r"\[(\d+(?:\.\d+)?)\]\[(S\d+)\](.*?)\[(\d+(?:\.\d+)?)\]", re.S)
MOSS_LINE = re.compile(r"^\s*(?:\[(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\]\s*)?\[?(S\d+)\]?[:\s]\s*(.+?)\s*$")


def parse_moss(text: str) -> list[dict]:
    turns = [
        {"start": float(a), "end": float(b), "speaker": spk, "text": " ".join(t.split())}
        for a, b, spk, t in MOSS_SEGMENT.findall(text)
        if t.strip()
    ]
    if turns:
        return turns
    turns = [
        {"start": float(a), "end": float(d), "speaker": b, "text": " ".join(c.split())}
        for a, b, c, d in MOSS_STREAM.findall(text)
        if c.strip()
    ]
    if turns:
        return turns
    for line in text.splitlines():
        m = MOSS_LINE.match(line)
        if m:
            turns.append({
                "start": float(m.group(1)) if m.group(1) else 0.0,
                "end": float(m.group(2)) if m.group(2) else 0.0,
                "speaker": m.group(3),
                "text": m.group(4),
            })
    return turns


# --------------------------------------------------------------------------
# Transcription strategies
# --------------------------------------------------------------------------


def coalesce(segments: list[dict], gap: float = 0.75) -> list[dict]:
    merged: list[dict] = []
    for s in segments:
        if (merged and s["speaker"] == merged[-1]["speaker"]
                and s["start"] - merged[-1]["end"] <= gap):
            merged[-1]["end"] = max(merged[-1]["end"], s["end"])
        else:
            merged.append(dict(s))
    return merged


def transcribe_turns(audio: Path, segments: list[dict], engine: str,
                     workdir: Path, min_dur: float = 0.4,
                     offset: float = 0.0) -> list[dict]:
    """Slice at speaker turns and transcribe each. No alignment, no length cap."""
    out = []
    for i, t in enumerate(coalesce(segments)):
        if t["end"] - t["start"] < min_dur:
            continue
        clip = workdir / f"turn_{i:05d}.wav"
        slice_wav(audio, clip, t["start"], t["end"])
        try:
            text = cli_transcribe(clip, engine)
        finally:
            clip.unlink(missing_ok=True)
        if text:
            out.append({
                "speaker": t["speaker"],
                "start": t["start"] + offset,
                "end": t["end"] + offset,
                "text": text,
            })
    return out


def strategy_moss(audio: Path, workdir: Path, **_) -> tuple[list[dict], str]:
    wav = audio if audio.suffix.lower() in NATIVE_EXTS else to_wav(audio, workdir / "in.wav")
    raw = run_cmd([
        speech_bin(), "transcribe", str(wav),
        "--engine", "moss", "--backend", "mlx",
        "--model", "int8", "--kv-cache", "fp16",
    ])
    turns = parse_moss(raw)
    if not turns:
        return ([{"speaker": "S01", "start": 0.0, "end": 0.0,
                  "text": " ".join(raw.split())}],
                "moss (speaker labels could not be parsed, returned as one block)")
    return turns, "moss end-to-end, model-chosen speaker count"


def strategy_two_stage(audio: Path, workdir: Path, engine: str = "cohere",
                       num_speakers: int | None = None,
                       min_speakers: int | None = None,
                       max_speakers: int | None = None, **_) -> tuple[list[dict], str]:
    wav = to_wav(audio, workdir / "in.wav")
    segs = cli_diarize(wav, "community1", num_speakers, min_speakers, max_speakers)
    if not segs:
        raise RuntimeError("Diarization returned no segments.")
    turns = transcribe_turns(wav, segs, engine, workdir)
    n = len({s["speaker"] for s in segs})
    return turns, f"community1 diarization ({n} speakers) + {engine} per turn"


def strategy_stereo(audio: Path, workdir: Path, engine: str = "cohere",
                    **_) -> tuple[list[dict], str]:
    """Each channel is one party. Best possible attribution when it applies."""
    turns: list[dict] = []
    for ch, label in ((0, "S01"), (1, "S02")):
        mono = to_wav(audio, workdir / f"ch{ch}.wav", channel=ch)
        segs = cli_diarize(mono, "community1", num_speakers=1)
        for s in segs:
            s["speaker"] = label
        turns += transcribe_turns(mono, segs, engine, workdir)
    turns.sort(key=lambda t: t["start"])
    return turns, "stereo channel split, one speaker per channel"


def strategy_plain(audio: Path, workdir: Path, engine: str = "cohere",
                   **_) -> tuple[list[dict], str]:
    wav = audio if audio.suffix.lower() in NATIVE_EXTS else to_wav(audio, workdir / "in.wav")
    text = cli_transcribe(wav, engine)
    dur = probe_duration(wav)
    return ([{"speaker": "", "start": 0.0, "end": dur, "text": text}],
            f"{engine}, no speaker separation")


def choose_strategy(audio: Path, mode: str, speakers: int | None) -> str:
    if mode != "auto":
        return mode
    try:
        if probe_channels(audio) == 2 and (speakers is None or speakers == 2):
            return "stereo"
    except RuntimeError:
        pass
    if speakers:
        return "two_stage"
    return "moss"


STRATEGIES = {
    "moss": strategy_moss,
    "two_stage": strategy_two_stage,
    "stereo": strategy_stereo,
    "plain": strategy_plain,
}


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def ts(sec: float, srt: bool = False) -> str:
    h, rem = divmod(max(0.0, sec), 3600)
    m, s = divmod(rem, 60)
    if srt:
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
    return f"{int(h):02d}:{int(m):02d}:{s:05.2f}"


def format_output(turns: list[dict], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(turns, indent=2)
    if fmt == "srt":
        return "\n".join(
            f"{i}\n{ts(t['start'], True)} --> {ts(t['end'], True)}\n"
            + (f"[{t['speaker']}] " if t["speaker"] else "") + t["text"] + "\n"
            for i, t in enumerate(turns, 1)
        )
    if fmt == "plain":
        return "\n".join(t["text"] for t in turns)
    return "\n".join(
        f"[{ts(t['start'])}] " + (f"{t['speaker']}: " if t["speaker"] else "") + t["text"]
        for t in turns
    )


# --------------------------------------------------------------------------
# Job tracking (long files outlive a client's patience)
# --------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    path: str
    state: str = "running"      # running | done | error
    method: str = ""
    result: str = ""
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None

    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="speech")


def do_transcribe(job: Job, audio: Path, mode: str, speakers: int | None,
                  min_speakers: int | None, max_speakers: int | None,
                  engine: str, fmt: str) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix="speech-mcp-") as tmp:
            workdir = Path(tmp)
            strategy = choose_strategy(audio, mode, speakers)
            fn = STRATEGIES[strategy]
            turns, method = fn(
                audio, workdir, engine=engine, num_speakers=speakers,
                min_speakers=min_speakers, max_speakers=max_speakers,
            )
        with JOBS_LOCK:
            job.method = method
            job.result = format_output(turns, fmt)
            job.state = "done"
            job.finished = time.time()
    except Exception as exc:  # surfaced to the caller, never swallowed
        with JOBS_LOCK:
            job.error = f"{type(exc).__name__}: {exc}"
            job.state = "error"
            job.finished = time.time()


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------

mcp = FastMCP(
    "speech",
    instructions=(
        "Local speech-to-text with speaker separation, running on this Mac via "
        "the shared speech-swift install. Use the `transcribe` tool for any "
        "audio or video transcription, captioning, subtitling, or meeting-notes "
        "task. Do not install whisper, whisper-fast, faster-whisper, whisperx, "
        "or any other speech-to-text package: this server already covers those "
        "cases with better accuracy and real speaker diarization."
    ),
)


@mcp.tool()
def transcribe(
    path: str,
    speakers: int | None = None,
    mode: Literal["auto", "moss", "two_stage", "stereo", "plain"] = "auto",
    output: Literal["text", "srt", "json", "plain"] = "text",
    engine: str = "cohere",
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    wait_seconds: int = 150,
) -> str:
    """Transcribe an audio or video file with speaker separation.

    Args:
        path: Absolute path to the media file on this Mac. Any format ffmpeg
            can read, including video, is converted automatically.
        speakers: Exact number of speakers, if known. Improves attribution a
            lot. Leave unset when unknown.
        mode: Strategy. "auto" picks for you and is almost always right.
            "stereo" splits a two-channel recording by channel, which is the
            most accurate option when each party is on its own channel.
            "moss" is end-to-end and chooses the speaker count itself.
            "two_stage" diarizes then transcribes each turn, and is the only
            mode that honors an exact speaker count. "plain" skips speakers.
        output: "text" for timestamped speaker lines, "srt" for subtitles,
            "json" for structured turns, "plain" for text with no labels.
        engine: ASR engine for the two_stage, stereo, and plain modes.
        wait_seconds: How long to wait inline. Past this a job id is returned
            and you poll with transcription_status.

    Returns the transcript, or a job id for long files.
    """
    audio = resolve_media(path)
    job = Job(id=uuid.uuid4().hex[:12], path=str(audio))
    with JOBS_LOCK:
        JOBS[job.id] = job

    future = POOL.submit(
        do_transcribe, job, audio, mode, speakers,
        min_speakers, max_speakers, engine, output,
    )
    try:
        future.result(timeout=max(5, wait_seconds))
    except Exception:
        dur = probe_duration(audio)
        return (
            f"Still running after {wait_seconds}s (file is {dur / 60:.1f} min). "
            f"Job id: {job.id}\n"
            f"Call transcription_status(\"{job.id}\") to collect the result."
        )

    if job.state == "error":
        raise RuntimeError(job.error)
    return f"Method: {job.method}\nElapsed: {job.elapsed():.1f}s\n\n{job.result}"


@mcp.tool()
def transcription_status(job_id: str) -> str:
    """Check or collect the result of a transcription job started earlier."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise ValueError(f"No such job: {job_id}")
    if job.state == "running":
        return f"Still running, {job.elapsed():.0f}s elapsed."
    if job.state == "error":
        raise RuntimeError(job.error)
    return f"Method: {job.method}\nElapsed: {job.elapsed():.1f}s\n\n{job.result}"


@mcp.tool()
def diarize_only(
    path: str,
    speakers: int | None = None,
    engine: str = "community1",
) -> str:
    """Return speaker segments with no transcription, as JSON.

    Useful for checking how many speakers a recording actually contains before
    committing to a full transcription run.
    """
    audio = resolve_media(path)
    with tempfile.TemporaryDirectory(prefix="speech-mcp-") as tmp:
        wav = to_wav(audio, Path(tmp) / "in.wav")
        segs = cli_diarize(wav, engine, num_speakers=speakers)
    names = sorted({s["speaker"] for s in segs})
    talk = {n: round(sum(s["end"] - s["start"] for s in segs if s["speaker"] == n), 1)
            for n in names}
    return json.dumps(
        {"speakers": names, "speaking_seconds": talk, "segments": segs}, indent=2
    )


@mcp.tool()
def speech_info() -> str:
    """Report what this server can do: binary location, engines, allowed paths.

    Call this first if a transcription fails, or to confirm the install is
    reachable before promising the user a transcript.
    """
    info: dict[str, Any] = {"allowed_roots": [str(r) for r in allowed_roots()]}
    try:
        binary = speech_bin()
        info["speech_binary"] = binary
        try:
            info["help"] = run_cmd([binary, "--help"], timeout=30)[:4000]
        except Exception as exc:
            info["help_error"] = str(exc)
    except RuntimeError as exc:
        info["speech_binary"] = None
        info["error"] = str(exc)
    info["ffmpeg"] = shutil.which("ffmpeg")
    info["ffprobe"] = shutil.which("ffprobe")
    info["cache_dir"] = os.environ.get(
        "QWEN3_CACHE_DIR", str(Path.home() / "Library/Caches/qwen3-speech")
    )
    with JOBS_LOCK:
        info["jobs"] = [
            {"id": j.id, "state": j.state, "path": j.path,
             "elapsed": round(j.elapsed(), 1)}
            for j in JOBS.values()
        ]
    return json.dumps(info, indent=2)


# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="MCP server for local speech-to-text")
    ap.add_argument("--http", action="store_true",
                    help="serve streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1",
                    help="HTTP bind address (default: loopback only)")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--path", default="/mcp")
    args = ap.parse_args()

    if args.http:
        if args.host not in ("127.0.0.1", "::1", "localhost") and not os.environ.get("SPEECH_MCP_TOKEN"):
            sys.exit(
                "Refusing to bind a non-loopback address without SPEECH_MCP_TOKEN set. "
                "This server reads files from disk, so an unauthenticated public "
                "bind is a file-disclosure endpoint."
            )
        mcp.run(transport="http", host=args.host, port=args.port, path=args.path)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
