#!/usr/bin/env bash
# Build a two-speaker test conversation with macOS `say`.
#   tests/fixtures/meeting_mono.wav    16 kHz mono, both voices mixed
#   tests/fixtures/meeting_stereo.wav  16 kHz stereo, speaker A left, B right
# Generated audio is gitignored; rerun this script to rebuild it.
#
# Speaker A defaults to Daniel, not Alex: Alex is not installed on every Mac,
# and `say -v <missing voice>` silently falls back to the system voice, which
# can be Samantha. That produces a "two speaker" fixture with one voice.
set -euo pipefail

cd "$(dirname "$0")"
out="fixtures"
mkdir -p "$out"
tmp="$(mktemp -d)"
VOICE_A="${VOICE_A:-Daniel}"
VOICE_B="${VOICE_B:-Samantha}"
for v in "$VOICE_A" "$VOICE_B"; do
  if ! say -v '?' | grep -q "^$v "; then
    echo "voice '$v' is not installed (System Settings > Accessibility > Spoken Content)" >&2
    exit 1
  fi
done
trap 'rm -rf "$tmp"' EXIT

say -v "$VOICE_A" "Thanks for joining. I wanted to walk through the quarterly numbers." -o "$tmp/a1.aiff"
say -v "$VOICE_B" "Sure. I pulled the regional breakdown before the call." -o "$tmp/b1.aiff"
say -v "$VOICE_A" "Good. Start with the southeast, that is the one I am worried about." -o "$tmp/a2.aiff"
say -v "$VOICE_B" "Southeast is down eleven percent quarter over quarter." -o "$tmp/b2.aiff"

# Normalize each utterance to 16 kHz mono and append 0.6 s of silence so turns
# are separable.
for f in a1 b1 a2 b2; do
  ffmpeg -nostdin -loglevel error -y -i "$tmp/$f.aiff" \
    -af "apad=pad_dur=0.6" -ac 1 -ar 16000 -c:a pcm_s16le "$tmp/$f.wav"
done

# Mono: plain concatenation in conversation order.
ffmpeg -nostdin -loglevel error -y \
  -i "$tmp/a1.wav" -i "$tmp/b1.wav" -i "$tmp/a2.wav" -i "$tmp/b2.wav" \
  -filter_complex "[0][1][2][3]concat=n=4:v=0:a=1" \
  -ac 1 -ar 16000 -c:a pcm_s16le "$out/meeting_mono.wav"

# Stereo: each voice keeps its own timeline slot, silent on the other channel.
# Build per-speaker tracks by concatenating each utterance with silence of the
# same length as the other speaker's utterance.
for f in a1 b1 a2 b2; do
  ffmpeg -nostdin -loglevel error -y -i "$tmp/$f.wav" -af "volume=0" "$tmp/${f}_silent.wav"
done
ffmpeg -nostdin -loglevel error -y \
  -i "$tmp/a1.wav" -i "$tmp/b1_silent.wav" -i "$tmp/a2.wav" -i "$tmp/b2_silent.wav" \
  -filter_complex "[0][1][2][3]concat=n=4:v=0:a=1" "$tmp/left.wav"
ffmpeg -nostdin -loglevel error -y \
  -i "$tmp/a1_silent.wav" -i "$tmp/b1.wav" -i "$tmp/a2_silent.wav" -i "$tmp/b2.wav" \
  -filter_complex "[0][1][2][3]concat=n=4:v=0:a=1" "$tmp/right.wav"
ffmpeg -nostdin -loglevel error -y -i "$tmp/left.wav" -i "$tmp/right.wav" \
  -filter_complex "[0][1]join=inputs=2:channel_layout=stereo" \
  -ar 16000 -c:a pcm_s16le "$out/meeting_stereo.wav"

echo "wrote $out/meeting_mono.wav and $out/meeting_stereo.wav"
