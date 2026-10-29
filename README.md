# speech-mcp

Exposes the local speech-swift install (`speech` CLI, Homebrew 0.0.28) as MCP
tools, so any MCP client on this Mac can transcribe audio with speaker
separation without shell access or a folder grant, and without installing
another ASR package.

Tools: `transcribe`, `transcription_status`, `diarize_only`, `speech_info`.

What the server assumes about the CLI, and what turned out to be true, is in
[docs/cli-reference.md](docs/cli-reference.md).

## Install

```bash
cd ~/Downloads/GitHub/speech-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .      # optional: adds the speech-mcp entry point
```

Requires Python 3.10+, ffmpeg and ffprobe (`brew install ffmpeg`), and a
working `speech` binary (`command -v speech` reports `/opt/homebrew/bin/speech`
here).

## Test

```bash
tests/make_fixtures.sh                 # two-voice test meeting, gitignored
.venv/bin/pytest -q                    # unit + end-to-end on the real binary
.venv/bin/python tests/smoke_stdio.py  # real MCP over stdio
.venv/bin/python tests/bench.py        # strategy timings
```

The end-to-end tests skip when the binary, ffmpeg or the fixtures are missing.

## Claude Code and Claude Desktop (stdio)

Registered on this Mac in both places at user scope.

Claude Code:

```bash
claude mcp add --scope user speech \
  -e SPEECH_BIN=/opt/homebrew/bin/speech \
  -e SPEECH_MCP_ROOTS=$HOME/Documents:$HOME/Downloads:$HOME/Movies:$HOME/Music:$HOME/Desktop \
  -- $HOME/Downloads/GitHub/speech-mcp/.venv/bin/python $HOME/Downloads/GitHub/speech-mcp/server.py
```

Claude Desktop, in `~/Library/Application Support/Claude/claude_desktop_config.json`
(restart Desktop after editing). JSON does not expand `~` or `$HOME`, so replace
`/Users/YOU` with your home directory:

```json
{
  "mcpServers": {
    "speech": {
      "command": "/Users/YOU/Downloads/GitHub/speech-mcp/.venv/bin/python",
      "args": ["/Users/YOU/Downloads/GitHub/speech-mcp/server.py"],
      "env": {
        "SPEECH_BIN": "/opt/homebrew/bin/speech",
        "SPEECH_MCP_ROOTS": "/Users/YOU/Documents:/Users/YOU/Downloads:/Users/YOU/Movies:/Users/YOU/Music:/Users/YOU/Desktop"
      }
    }
  }
}
```

## Strategies

`mode="auto"` picks one:

| Picked when | Strategy | What runs |
|---|---|---|
| two channels that actually differ, speakers unset or 2 | `stereo` | each channel diarized as one speaker, turns transcribed |
| `speakers` given | `two_stage` | community1 diarization with that count, then each turn transcribed |
| otherwise | `moss` | MOSS-Transcribe-Diarize end to end, model picks the count |

`plain` (no speakers) is available explicitly. Dual-mono files (the same mix on
both channels, common from phones and room recorders) are not treated as
stereo, since splitting them would transcribe the call twice under two labels.

The `Method:` line of every result says what ran. It carries a `WARNING` if
MOSS output could not be parsed into turns (attribution lost, text returned
unlabeled) or if MOSS hit its token limit (transcript truncated).

### Warm path

`two_stage` and `stereo` transcribe each speaker turn separately. With
`engine="qwen3"` (or `parakeet`) all turns go through one
`speech transcribe-batch` process, so the model loads once. Other engines,
including the default `cohere`, run one `speech transcribe` per turn and
reload the weights each time. Set `SPEECH_MCP_BATCH=0` to force the per-turn
path.

`speech-server` was the intended warm path, but on 0.0.28 it always runs
parakeet regardless of `model` or `engine`, and parakeet returns `"Th"` for
the fixture on this install (the CLI's `--engine parakeet` does the same). It
is not used.

### Timings

Apple Silicon, weights already in the shared cache and page cache.
`tests/bench.py`, median of 3, on the 16.8 s two-speaker fixture (4 turns):

| Configuration | Wall time |
|---|---|
| moss end-to-end | 1.14 s |
| plain cohere (no speakers) | 0.90 s |
| two_stage, cohere, per turn (cold) | 3.57 s |
| two_stage, qwen3, per turn (cold) | 4.62 s |
| two_stage, qwen3, transcribe-batch (warm) | 1.90 s |
| stereo, cohere, per turn (cold) | 4.20 s |
| stereo, qwen3, per turn (cold) | 5.15 s |
| stereo, qwen3, transcribe-batch (warm) | 2.62 s |

The same fixture looped to 22.4 min (320 turns), single run:

| Configuration | Wall time |
|---|---|
| moss end-to-end | 98 s |
| plain cohere (no speakers) | 24 s |
| two_stage, cohere, per turn (cold) | 286 s |
| two_stage, qwen3, per turn (cold) | 388 s |
| two_stage, qwen3, transcribe-batch (warm) | 72 s |

On the long file the warm path is 5.4x faster than the same engine cold and
4x faster than cohere cold. Of the 72 s, community1 diarization and slicing
320 clips with ffmpeg are most of what remains.

Each per-turn process costs about 0.5 to 0.6 s of model load on top of
inference, so the per-turn path grows with the number of turns, not just with
audio length.

## Limits

- MOSS MLX stops at 5120 output tokens by default, which ended a 22 min
  transcript at 713 s. The server now passes `--max-tokens` at 10 per second
  of audio (about 7.2 per second is used), which covered the whole 22 min
  file. Files of an hour or more have not been tested with MOSS; the Method
  line warns if it truncates, and `mode="two_stage"` is the fallback.
- Diarization was only checked on synthetic `say` voices. Real meetings with
  similar voices, crosstalk or many speakers will do worse.
- Turn clips carry 0.2 s of padding on each side, so a word from the
  neighbouring turn can appear at a clip edge. On the 22 min file qwen3
  returned 3434 words against about 3280 spoken; cohere returned 3200.

## Always-on HTTP service (optional)

```bash
.venv/bin/python server.py --http --port 8420
```

HTTP binds loopback only. `--host` accepts only `127.0.0.1`, `::1` or
`localhost`, and there is no authentication, so do not proxy it to a routable
address. For a LaunchAgent, put the same command in `ProgramArguments` and the
two environment variables in `EnvironmentVariables`.

## Security notes

- `transcribe` reads whatever path it is given. `SPEECH_MCP_ROOTS` is the only
  limit, and symlinks are resolved before the check so a link inside a root
  cannot escape it. The default roots include `~/Downloads` and `~/Documents`;
  narrow them if those hold sensitive files.
- Every subprocess is an argv list with `shell=False`. Paths reach the CLI as
  resolved absolute paths, and engine names are restricted to an enum in the
  tool schema.
- Model weights come from the shared cache at `~/Library/Caches/qwen3-speech/`.
  Do not set `QWEN3_CACHE_DIR` per client, since that defeats the point.
