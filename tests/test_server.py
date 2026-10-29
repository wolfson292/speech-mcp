"""Tests for speech-mcp.

Unit tests run anywhere. Tests marked `speech` need the real speech binary,
ffmpeg, and the fixtures from tests/make_fixtures.sh; they skip otherwise.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402

DATA = ROOT / "tests" / "data"
FIX = ROOT / "tests" / "fixtures"
MONO = FIX / "meeting_mono.wav"
STEREO = FIX / "meeting_stereo.wav"
DUALMONO = FIX / "meeting_dualmono.wav"


def have_speech() -> bool:
    try:
        server.speech_bin()
    except RuntimeError:
        return False
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe") and MONO.exists()),
    reason="needs ffmpeg and tests/make_fixtures.sh output",
)
needs_speech = pytest.mark.skipif(
    not (have_speech() and MONO.exists()),
    reason="needs the speech binary and tests/make_fixtures.sh output",
)


# --------------------------------------------------------------------------
# resolve_media: the allowlist is what keeps transcribe from being an
# arbitrary file-read primitive.
# --------------------------------------------------------------------------


@pytest.fixture
def roots(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("SPEECH_MCP_ROOTS", str(allowed))
    return allowed, outside


def test_resolve_media_accepts_file_inside_root(roots):
    allowed, _ = roots
    f = allowed / "ok.wav"
    f.write_bytes(b"x")
    assert server.resolve_media(str(f)) == f.resolve()


def test_resolve_media_rejects_file_outside_root(roots):
    _, outside = roots
    f = outside / "secret.wav"
    f.write_bytes(b"x")
    with pytest.raises(ValueError, match="outside the allowed roots"):
        server.resolve_media(str(f))


def test_resolve_media_rejects_symlink_escaping_root(roots):
    allowed, outside = roots
    target = outside / "secret.txt"
    target.write_text("ssh key")
    link = allowed / "innocent.wav"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="outside the allowed roots"):
        server.resolve_media(str(link))


def test_resolve_media_rejects_symlinked_directory_escaping_root(roots):
    allowed, outside = roots
    (outside / "secret.wav").write_bytes(b"x")
    (allowed / "dir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside the allowed roots"):
        server.resolve_media(str(allowed / "dir" / "secret.wav"))


def test_resolve_media_rejects_dotdot_traversal(roots):
    allowed, outside = roots
    (outside / "secret.wav").write_bytes(b"x")
    with pytest.raises(ValueError, match="outside the allowed roots"):
        server.resolve_media(str(allowed / ".." / "outside" / "secret.wav"))


def test_resolve_media_rejects_sibling_with_shared_prefix(tmp_path, monkeypatch):
    # /x/allowed must not admit /x/allowed-evil/file.
    allowed = tmp_path / "allowed"
    evil = tmp_path / "allowed-evil"
    allowed.mkdir()
    evil.mkdir()
    (evil / "f.wav").write_bytes(b"x")
    monkeypatch.setenv("SPEECH_MCP_ROOTS", str(allowed))
    with pytest.raises(ValueError, match="outside the allowed roots"):
        server.resolve_media(str(evil / "f.wav"))


def test_resolve_media_rejects_dangling_symlink_and_directory(roots):
    allowed, outside = roots
    (allowed / "dangling.wav").symlink_to(outside / "missing.wav")
    with pytest.raises(ValueError, match="Cannot resolve"):
        server.resolve_media(str(allowed / "dangling.wav"))
    with pytest.raises(ValueError, match="Not a file"):
        server.resolve_media(str(allowed))


def test_transcribe_tool_enforces_allowlist(roots):
    _, outside = roots
    f = outside / "secret.wav"
    f.write_bytes(b"x")
    with pytest.raises(ValueError, match="outside the allowed roots"):
        server.transcribe(str(f))


# --------------------------------------------------------------------------
# Parsers, against stdout captured from the real CLI (tests/data/).
# --------------------------------------------------------------------------


def test_extract_json_skips_progress_lines():
    raw = (DATA / "diarize_community1_stdout.txt").read_text()
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)  # what the original code did
    obj = server.extract_json(raw)
    assert obj["num_speakers"] == 2
    assert isinstance(obj["segments"][0]["speaker"], int)


def test_normalize_segments_real_diarize_json():
    segs = server.normalize_segments(
        server.extract_json((DATA / "diarize_community1_stdout.txt").read_text())
    )
    assert [s["speaker"] for s in segs] == ["S01", "S02", "S02", "S01", "S02"]
    assert segs == sorted(segs, key=lambda s: s["start"])
    assert segs[0]["start"] == pytest.approx(0.031)
    assert segs[0]["end"] == pytest.approx(3.794)
    # Coalescing the split S02 segment gives the four real turns.
    assert [t["speaker"] for t in server.coalesce(segs)] == ["S01", "S02", "S01", "S02"]


def test_normalize_segments_fallback_spellings():
    raw = {"turns": [
        {"startTime": 1, "endTime": 2, "speakerId": "A"},
        {"begin": 0, "duration": 0.5, "label": 3},
        {"speaker": 0},  # no times: dropped
    ]}
    assert server.normalize_segments(raw) == [
        {"start": 0.0, "end": 0.5, "speaker": "S04"},
        {"start": 1.0, "end": 2.0, "speaker": "A"},
    ]


def test_parse_result_real_cohere_stdout():
    text = server.parse_result((DATA / "cohere_stdout.txt").read_text())
    assert text.startswith("Thanks for joining.")
    assert text.endswith("quarter over quarter.")
    assert "Loading" not in text and "Time:" not in text


def test_parse_result_without_result_line_raises():
    with pytest.raises(RuntimeError, match="no 'Result:' line"):
        server.parse_result("Loading audio: x.wav\n  [0%] Downloading\n")


def test_parse_moss_real_stdout():
    turns = server.parse_moss((DATA / "moss_mlx_stdout.txt").read_text())
    assert [t["speaker"] for t in turns] == ["S01", "S02", "S01", "S02"]
    assert turns[0] == {
        "start": 0.07, "end": 3.86, "speaker": "S01",
        "text": "Thanks for joining. I wanted to walk through the quarterly numbers.",
    }
    assert "southeast" in turns[3]["text"].lower()


def test_parse_moss_fallback_forms():
    stream = "[0.48][S01]hello there[1.66][1.9][S02]hi[2.4]"
    assert [(t["speaker"], t["text"]) for t in server.parse_moss(stream)] == [
        ("S01", "hello there"), ("S02", "hi"),
    ]
    assert server.parse_moss("S01: one\nS02: two") [1]["speaker"] == "S02"


def test_moss_unparseable_output_warns(monkeypatch, tmp_path):
    out = "Loading audio: x.wav\nResult: some words here\n  Time: 0.1s\n"
    monkeypatch.setattr(server, "run_cmd", lambda cmd, timeout=3600: out)
    monkeypatch.setattr(server, "speech_bin", lambda: "/bin/speech")
    monkeypatch.setattr(server, "probe_duration", lambda p: 12.0)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"")
    turns, method = server.strategy_moss(wav, tmp_path)
    assert "WARNING" in method and "attribution was lost" in method
    assert turns == [{"speaker": "", "start": 0.0, "end": 12.0, "text": "some words here"}]


def test_moss_truncation_warns_and_scales_tokens(monkeypatch, tmp_path):
    seen = {}
    out = ("Result: a\nSegments:\n  [0.0s-1.0s][S01] a\n"
           "  Time: 1s\n  Warning: generation stopped at maximumTokens\n")

    def fake_run(cmd, timeout=3600):
        seen["cmd"] = cmd
        return out

    monkeypatch.setattr(server, "run_cmd", fake_run)
    monkeypatch.setattr(server, "speech_bin", lambda: "/bin/speech")
    monkeypatch.setattr(server, "probe_duration", lambda p: 3600.0)
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"")
    turns, method = server.strategy_moss(wav, tmp_path)
    assert seen["cmd"][seen["cmd"].index("--max-tokens") + 1] == "36000"
    assert "truncated" in method and len(turns) == 1


def test_cli_transcribe_batch_parses_jsonl(monkeypatch, tmp_path):
    out = ("Found 2 audio files\n"
           '{"file":"turn_00000","text":"Hello  there.","time":0.04}\n'
           '{"file":"turn_00001","text":"Hi.","time":0.03}\n'
           "Batch complete: 2 files\n")
    monkeypatch.setattr(server, "run_cmd", lambda cmd, timeout=3600: out)
    monkeypatch.setattr(server, "speech_bin", lambda: "/bin/speech")
    assert server.cli_transcribe_batch(tmp_path, "qwen3") == {
        "turn_00000": "Hello there.", "turn_00001": "Hi.",
    }


def test_subprocess_calls_never_use_a_shell(monkeypatch):
    calls = []
    real = server.subprocess.run

    def spy(cmd, *a, **kw):
        calls.append((cmd, kw.get("shell")))
        return real(["/usr/bin/true"], *a, **kw)

    monkeypatch.setattr(server.subprocess, "run", spy)
    server.run_cmd(["echo", "$(rm -rf ~)"])
    assert calls == [(["echo", "$(rm -rf ~)"], False)]


# --------------------------------------------------------------------------
# Strategy selection
# --------------------------------------------------------------------------


@needs_ffmpeg
def test_choose_strategy():
    assert server.choose_strategy(STEREO, "auto", None) == "stereo"
    assert server.choose_strategy(STEREO, "auto", 2) == "stereo"
    assert server.choose_strategy(MONO, "auto", 2) == "two_stage"
    assert server.choose_strategy(MONO, "auto", None) == "moss"
    assert server.choose_strategy(MONO, "plain", None) == "plain"
    # Same mix on both channels must not be split into two "speakers".
    assert server.choose_strategy(DUALMONO, "auto", None) == "moss"
    assert server.choose_strategy(DUALMONO, "auto", 2) == "two_stage"


# --------------------------------------------------------------------------
# End to end against the real binary
# --------------------------------------------------------------------------


def _speakers(result: str) -> set[str]:
    body = result.split("\n\n", 1)[1]
    return {t["speaker"] for t in json.loads(body)}


@pytest.fixture
def fixture_roots(monkeypatch):
    monkeypatch.setenv("SPEECH_MCP_ROOTS", str(FIX))


@needs_speech
def test_transcribe_end_to_end_mono(fixture_roots):
    result = server.transcribe(str(MONO), output="json", wait_seconds=600)
    assert result.startswith("Method: moss")
    assert "WARNING" not in result
    assert len(_speakers(result)) == 2
    assert "southeast" in result.lower()


@needs_speech
@pytest.mark.parametrize("engine,batch", [("cohere", "1"), ("qwen3", "1"), ("qwen3", "0")])
def test_transcribe_end_to_end_two_stage(fixture_roots, monkeypatch, engine, batch):
    monkeypatch.setenv("SPEECH_MCP_BATCH", batch)
    result = server.transcribe(str(MONO), speakers=2, engine=engine,
                               output="json", wait_seconds=600)
    assert "community1 diarization (2 speakers)" in result
    assert ("transcribe-batch" in result) == (engine == "qwen3" and batch == "1")
    assert len(_speakers(result)) == 2
    assert "southeast" in result.lower().replace("south east", "southeast")


@needs_speech
def test_transcribe_end_to_end_stereo(fixture_roots):
    result = server.transcribe(str(STEREO), engine="qwen3", output="json", wait_seconds=600)
    assert result.startswith("Method: stereo")
    turns = json.loads(result.split("\n\n", 1)[1])
    assert [t["speaker"] for t in turns] == ["S01", "S02", "S01", "S02"]
    assert "quarterly" in turns[0]["text"].lower()


@needs_speech
def test_diarize_only(fixture_roots):
    out = json.loads(server.diarize_only(str(MONO)))
    assert out["speakers"] == ["S01", "S02"]


@needs_speech
def test_speech_info_reports_binary(fixture_roots):
    info = json.loads(server.speech_info())
    assert info["speech_binary"]
    assert "transcribe" in info["help"]
    assert info["cache_dir"].endswith("Library/Caches/qwen3-speech")
