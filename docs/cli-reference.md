# speech CLI reference (as installed on this Mac)

Captured 2026-10-29 from Homebrew `speech` 0.0.28 (0.0.29 is available but not
installed). Everything below is raw output from this machine unless marked as
a finding.

```
$ command -v speech
/opt/homebrew/bin/speech
$ command -v speech-server
/opt/homebrew/bin/speech-server
```

Both are bash shims that `exec` the real binaries in
`/opt/homebrew/Cellar/speech/0.0.28/libexec/`. Pointing `SPEECH_BIN` at the shim
is fine, since it survives `brew upgrade`.

## Findings

These are what the original `server.py` assumed, checked against the binary.

| Assumption in server.py | Verified? | Notes |
|---|---|---|
| `transcribe --engine` accepts cohere, moss, qwen3, parakeet, whisper | yes | Also voxtral, nemotron, omnilingual, qwen3-coreml, qwen3-coreml-full. Default is `qwen3`. |
| `--backend mlx --model int8 --kv-cache fp16` are real moss flags | yes | `--model int8` is a valid short name for moss mlx and resolves to `aufklarer/MOSS-Transcribe-Diarize-0.9B-MLX-INT8`, which is the one already in the shared cache. The moss mlx *default* is int5, which is not cached, so dropping `--model int8` would trigger a new download. |
| `transcribe` prints plain text on stdout | **no** | stdout carries progress logs, a `Result: <text>` line, and a `Time:` line. There is no JSON flag on `transcribe`. Only `transcribe-batch --jsonl` emits JSON. The original `cli_transcribe` returned the logs as part of the transcript. |
| `diarize --json` prints JSON on stdout | **partly** | The JSON *is* on stdout, but after progress lines (`Loading audio: ...`, `[61%] Downloading...`). `json.loads(stdout)` raises. stderr is empty. |
| diarize JSON keys | corrected | `{"num_speakers": int, "segments": [{"start", "end", "duration", "speaker": int}]}`. Speaker is a 0-based integer, not a string label. |
| `diarize --engine community1` | yes | Engines: pyannote (CLI default), community1, sortformer, nemotron3. |
| `--num-speakers/--min-speakers/--max-speakers` | yes | Help text says Community-1 only. The code already gated them on community1. |
| MOSS emits `[0.48][S01]text[1.66]` or `[S01] text` lines | **no** | Real form is `  [0.07s-3.86s][S01] text` under a `Segments:` header, with an `s` suffix on both times. Neither original regex matched, so every moss run fell through to "one unlabeled block", and that block contained the progress logs too. |
| `.m4a/.mp3/.caf` are read natively | yes for m4a, mp3 | Help says "WAV, any sample rate", but m4a and mp3 decoded fine in testing. caf not tested. |
| `speech-server` has an OpenAI-compatible `/v1/audio/transcriptions` | exists, **not usable** | See below. |

### speech-server

Routes (from the binary's startup banner):

```
  POST /transcribe     - Speech-to-text (WAV body or JSON with audio_base64)
  POST /v1/audio/transcriptions - OpenAI-compatible STT (multipart/form-data)
  POST /v1/audio/speech - OpenAI-compatible TTS (JSON body)
  POST /speak          - Text-to-speech (JSON: {text, engine?, language?})
  POST /respond        - Speech-to-speech (WAV body, voice/max_steps via query)
  POST /enhance        - Speech enhancement (WAV body)
  GET  /health         - Health check
  WS   /v1/realtime    - OpenAI Realtime API (JSON events, base64 PCM16 audio)
```

Both STT routes always use `parakeet-tdt-v3-coreml-int8-30s`. `model=cohere`,
`engine=cohere`, `?engine=qwen3` and `?model=cohere` were all ignored. On this
install parakeet is also broken: on the 16.8 s fixture, a 3.8 s clip, and an
m4a copy, both the server and `speech transcribe --engine parakeet` return the
text `"Th"`. So the server cannot run the requested engine, and the engine it
does run produces garbage. It is not wired into speech-mcp.

`speech transcribe-batch` loads the model once for a whole directory and emits
JSON lines. It only supports `qwen3` and `parakeet`, and is what speech-mcp uses
as its warm path.

### Fixture voices

`say -v Alex` is not installed here and silently falls back to the system
voice. The first fixture build therefore had one voice saying both parts, and
every diarize engine (correctly) reported one speaker. The fixture script now
uses Daniel and Samantha and fails if a voice is missing.

## Captured output

Real stdout samples used by the tests live in `tests/data/`:

- `diarize_community1_stdout.txt`: `speech diarize meeting_mono.wav --engine community1 --json`
- `moss_mlx_stdout.txt`: `speech transcribe meeting_mono.wav --engine moss --backend mlx --model int8 --kv-cache fp16`
- `cohere_stdout.txt`: `speech transcribe meeting_mono.wav --engine cohere`

`transcribe-batch --jsonl` line:

```
{"file":"turn_0","text":"Thanks for joining. I wanted to walk through the quarterly numbers.","time":0.040,"rtf":0.0103,"duration":3.90,"batch_time":0.040,"batch_rtf":0.0103,"batch_size":1}
```

## Raw help output

```
### speech  --help
OVERVIEW: AI speech models for Apple Silicon

USAGE: speech <subcommand>

OPTIONS:
  -h, --help              Show help information.

SUBCOMMANDS:
  transcribe              Transcribe speech to text (Qwen3-ASR, Cohere,
                          Voxtral, MOSS, and other ASR backends)
  transcribe-batch        Transcribe a directory of audio files (model loaded
                          once)
  align                   Forced alignment: align text to audio with word-level
                          timestamps
  speak                   Text-to-speech synthesis (Qwen3-TTS, CosyVoice,
                          VoxCPM2, IndexTTS2, F5-TTS, Higgs, Indic-Mio, or
                          Magpie). For CoreML, use the `qwen3-tts-coreml`
                          subcommand.
  csm                     Conversational Speech Model (CSM-1B) — text→audio
                          with voice cloning
  respond                 Full-duplex speech-to-speech inference using
                          PersonaPlex 7B
  voice-chat              Talk with Soniqo using the full-duplex Nemotron
                          VoiceChat 11B model.
  audio-translate         Translate speech with Hibiki Zero-3B (FR/ES/PT/DE →
                          EN, on-device, MLX)
  vad                     Detect speech segments using Voice Activity Detection
  vad-stream              Detect speech segments using streaming Silero VAD
                          (32ms chunks)
  turn                    Estimate whether a recorded utterance is a finished
                          turn (Smart Turn v3.2)
  diarize                 Identify speakers and their speech segments in audio
  embed-speaker           Extract a speaker embedding from audio
  language-id             Identify the spoken language in an audio file
  denoise                 Remove background noise from speech audio using
                          DeepFilterNet3
  restore                 Restore (denoise + dereverb) speech with Sidon —
                          CoreML, 48 kHz output
  separate                Separate a music track into stems (vocals, drums,
                          bass, other)
  compose                 Generate music from a text prompt (Stable Audio 3 by
                          default; MAGNeT also available, MLX, on-device)
  sa3-parity              Compare Swift SA3 outputs to a Python reference dump
                          (per stage).
  upsample                Audio super-resolution with FlashSR (one-step
                          distilled AudioSR, MLX, 48 kHz)
  kokoro                  Text-to-speech synthesis using Kokoro-82M (CoreML,
                          Neural Engine)
  qwen3-tts-coreml        Text-to-speech using Qwen3-TTS (CoreML)
  vibevoice               Text-to-speech synthesis using Microsoft VibeVoice
                          (MLX).
  vibevoice-encode-voice  Mint a VibeVoice voice cache from a reference audio
                          recording + transcript (Realtime-0.5B path; English
                          only).
  translate               Translate text into a target language using
                          MADLAD-400 (MLX, Apple Silicon)
  wake                    Detect wake words / keyword phrases (KWS Zipformer)
  avatar-motion           Generate NVIDIA Audio2Face-3D coefficient frames from
                          speech audio

  See 'speech help <subcommand>' for detailed help.
exit=0
### speech transcribe --help
OVERVIEW: Transcribe speech to text (Qwen3-ASR, Cohere, Voxtral, MOSS, and
other ASR backends)

USAGE: speech transcribe [<options>] <audio-file>

ARGUMENTS:
  <audio-file>            Audio file to transcribe (WAV, any sample rate)

OPTIONS:
  --engine <engine>       ASR engine: qwen3 (default), cohere, voxtral, moss,
                          parakeet, nemotron, omnilingual, whisper,
                          qwen3-coreml, or qwen3-coreml-full (default: qwen3)
  --window <window>       [omnilingual] Window size in seconds: 5 or 10
                          (default 10) — CoreML backend only (default: 10)
  --backend <backend>     [omnilingual/moss] Backend: coreml (default) or mlx
                          (default: coreml)
  --variant <variant>     [omnilingual mlx] Variant: 300M (default), 1B, 3B, or
                          7B (default: 300M)
  --bits <bits>           [omnilingual mlx] Quantization bits: 4 (default) or 8
                          (default: 4)
  -m, --model <model>     [qwen3] 0.6B (default), 0.6B-8bit, 1.7B, or
                          1.7B-4bit; [cohere/voxtral] int5 (default), int8, or
                          fp16; [moss coreml] int8 (default) or fp16; [moss
                          mlx] int5 (default) or int8; [whisper] default or
                          turbo; alternatively a model ID or local directory
                          (default: 0.6B)
  --max-tokens <max-tokens>
                          [moss] Maximum generated transcript tokens (default:
                          Core ML 512, MLX 5120)
  --kv-cache <kv-cache>   [moss mlx] KV-cache precision: fp16 (default) or int8
                          (default: fp16)
  --language <language>   Language hint (optional)
  --context <context>     Context string to bias recognition (e.g., 'Project:
                          Foo, participants: Alice, Bob'). Improves proper-noun
                          accuracy.
  --stream                Enable streaming transcription with VAD
  --max-segment <max-segment>
                          Maximum segment duration in seconds (default 10)
                          (default: 10.0)
  --partial               Emit partial results during speech
  -h, --help              Show help information.

exit=0
### speech diarize --help
OVERVIEW: Identify speakers and their speech segments in audio

USAGE: speech diarize [<options>] <audio-file>

ARGUMENTS:
  <audio-file>            Audio file to analyze (WAV, any sample rate)

OPTIONS:
  --target-speaker <target-speaker>
                          Enrollment audio for target speaker extraction
  --engine <engine>       Diarization engine: pyannote (default), community1,
                          sortformer, or nemotron3 (default: pyannote)
  --nemotron3-backend <nemotron3-backend>
                          Nemotron 3 backend: coreml (default) or mlx (default:
                          coreml)
  --nemotron3-directory <nemotron3-directory>
                          Optional local Nemotron 3 bundle directory
  --nemotron3-compute-units <nemotron3-compute-units>
                          Nemotron 3 Core ML compute units: ane (default), cpu,
                          gpu, or all (default: ane)
  --sortformer-variant <sortformer-variant>
                          Sortformer variant: default (offline, ~125x RTF),
                          balanced (faster first-load, ~hundreds-x RTF), or
                          streaming (low-latency) (default: default)
  --sortformer-compute-units <sortformer-compute-units>
                          Sortformer CoreML compute units: ane (default,
                          ANE+CPU), cpu (instant first-load, ~20x RTF), gpu
                          (cpu+gpu), all (every backend; rarely needed)
                          (default: ane)
  --community1-compute-units <community1-compute-units>
                          Community-1 CoreML compute units: ane (default), cpu,
                          gpu, or all (default: ane)
  --num-speakers <num-speakers>
                          Known exact speaker count for Community-1
  --min-speakers <min-speakers>
                          Minimum speaker count for Community-1 (default 1)
                          (default: 1)
  --max-speakers <max-speakers>
                          Maximum speaker count for Community-1
  --embedding-engine <embedding-engine>
                          Speaker embedding engine: mlx (default) or coreml
                          (default: mlx)
  --json                  Output as JSON
  --rttm                  Output as RTTM (standard diarization evaluation
                          format)
  --vad-filter            Pre-filter with Silero VAD to reduce false alarms
  --score-against <score-against>
                          Reference RTTM file to score against (computes DER)
  --min-silence <min-silence>
                          Minimum silence between segments in seconds (default
                          0.15) (default: 0.15)
  --min-speech <min-speech>
                          Minimum speech segment duration in seconds (default
                          0.3) (default: 0.3)
  --onset <onset>         Speaker activity onset threshold (default 0.5)
                          (default: 0.5)
  --offset <offset>       Speaker activity offset threshold (default: 0.5 for
                          Sortformer and Nemotron 3; 0.3 for pyannote)
  --cluster-threshold <cluster-threshold>
                          Cosine distance threshold for speaker clustering
                          (default 0.715, lower = fewer speakers) (default:
                          0.715)
  -h, --help              Show help information.

exit=0
### speech align --help
OVERVIEW: Forced alignment: align text to audio with word-level timestamps

USAGE: speech align <audio-file> [--text <text>] [--model <model>] [--engine <engine>] [--aligner-model <aligner-model>] [--language <language>] [--max-tokens <max-tokens>]

ARGUMENTS:
  <audio-file>            Audio file (WAV, any sample rate)

OPTIONS:
  -t, --text <text>       Text to align (if omitted, transcribes first)
  -m, --model <model>     ASR model for transcription: 0.6B (default), 1.7B, or
                          full ID (default: 0.6B)
  --engine <engine>       Forced aligner backend: mlx (default) or coreml
                          (default: mlx)
  --aligner-model <aligner-model>
                          Forced aligner model ID (defaults depend on --engine)
  --language <language>   Language hint (optional)
  --max-tokens <max-tokens>
                          Max ASR decoder tokens when transcribing (≈ 250
                          tokens/min) (default: 4096)
  -h, --help              Show help information.

exit=0
### speech transcribe-batch --help
OVERVIEW: Transcribe a directory of audio files (model loaded once)

USAGE: speech transcribe-batch <input-dir> [--output-dir <output-dir>] [--engine <engine>] [--model <model>] [--language <language>] [--batch-size <batch-size>] [--extensions <extensions>] [--jsonl]

ARGUMENTS:
  <input-dir>             Directory containing audio files (WAV, FLAC, etc.)

OPTIONS:
  --output-dir <output-dir>
                          Output directory for transcriptions (one .txt per
                          file)
  --engine <engine>       ASR engine: qwen3 (default), parakeet (default: qwen3)
  -m, --model <model>     [qwen3] Model: 0.6B (default), 0.6B-8bit, 1.7B,
                          1.7B-4bit (default: 0.6B)
  --language <language>   Language hint (optional)
  --batch-size <batch-size>
                          [qwen3] Max files per group (default: 1).
                          Experimental MLX batched decode requires
                          QWEN3_ASR_EXPERIMENTAL_BATCH_DECODE=1. (default: 1)
  --extensions <extensions>
                          Audio file extensions to process (default:
                          wav,flac,mp3) (default: wav,flac,mp3)
  --jsonl                 Output results as JSON lines (one per file)
  -h, --help              Show help information.

### speech-server --help
OVERVIEW: HTTP API server for speech models on Apple Silicon

USAGE: speech-server [--host <host>] [--port <port>] [--preload]

OPTIONS:
  --host <host>           Host to bind (default: 127.0.0.1) (default: 127.0.0.1)
  --port <port>           Port to bind (default: 8080) (default: 8080)
  --preload               Load all models on startup (slower start, faster
                          first request)
  -h, --help              Show help information.

exit=0
```
