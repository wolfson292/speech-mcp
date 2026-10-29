# speech-mcp

Exposes the local speech-swift install as MCP tools, so any MCP client can
transcribe audio with speaker separation without shell access or a folder grant.

Tools: `transcribe`, `transcription_status`, `diarize_only`, `speech_info`.

## Install

```bash
cd ~/speech-mcp
python3 -m venv .venv
.venv/bin/pip install fastmcp
```

Requires Python 3.10+, ffmpeg (`brew install ffmpeg`), and a working `speech`
binary.

Check it before wiring anything up:

```bash
.venv/bin/python server.py --http --port 8420 &
curl -s localhost:8420/mcp -H 'Accept: text/event-stream' | head -5
kill %1
```

## Claude Desktop and Claude Code (stdio)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`, and
for Claude Code use `claude mcp add` or the equivalent block in
`~/.claude.json`:

```json
{
  "mcpServers": {
    "speech": {
      "command": "~/speech-mcp/.venv/bin/python",
      "args": ["~/speech-mcp/server.py"],
      "env": {
        "SPEECH_BIN": "/opt/homebrew/bin/speech",
        "SPEECH_MCP_ROOTS": "~/Documents:~/Downloads:~/Movies"
      }
    }
  }
}
```

Correct `SPEECH_BIN` to whatever `command -v speech` actually reports.

## Always-on HTTP service

Save as `~/Library/LaunchAgents/com.denlair.speech-mcp.plist`, then
`launchctl load -w ~/Library/LaunchAgents/com.denlair.speech-mcp.plist`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.denlair.speech-mcp</string>
  <key>ProgramArguments</key>
  <array>
    <string>~/speech-mcp/.venv/bin/python</string>
    <string>~/speech-mcp/server.py</string>
    <string>--http</string>
    <string>--port</string><string>8420</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SPEECH_BIN</key><string>/opt/homebrew/bin/speech</string>
    <key>SPEECH_MCP_ROOTS</key><string>~/Documents:~/Movies</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/tmp/speech-mcp.err</string>
  <key>StandardOutPath</key><string>/tmp/speech-mcp.out</string>
</dict>
</plist>
```

## Security notes

The server binds loopback only by default, and refuses to bind a routable
address unless `SPEECH_MCP_TOKEN` is set. That check is a guard rail, not
authentication. FastMCP does not enforce the token for you.

`transcribe` takes an arbitrary path and reads it. `SPEECH_MCP_ROOTS` is the
only thing limiting that, and symlinks are resolved before the check so a link
cannot escape. Keep the roots narrow.

If you want web sessions (claude.ai) to reach this, it needs a public HTTPS
endpoint. Put it behind a Cloudflare Tunnel with Access in front rather than
opening a port. MCP's HTTP transport expects OAuth 2.1 for real remote auth,
which this server does not implement. Until then, stdio on the machine that
holds the files is the honest configuration.

## Notes

- Strategy selection is automatic. Stereo recordings split by channel, a known
  speaker count routes to diarize-then-transcribe, otherwise MOSS runs
  end-to-end.
- Files that take longer than `wait_seconds` return a job id rather than
  timing out. Poll with `transcription_status`.
- Model weights come from the shared cache at
  `~/Library/Caches/qwen3-speech/`. Do not override `QWEN3_CACHE_DIR` per
  client, since that defeats the point.
