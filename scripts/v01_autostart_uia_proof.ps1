param(
    [Parameter(Mandatory=$true)][string]$ExePath,
    [Parameter(Mandatory=$true)][string]$WindowTitle
)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_ENVIRONMENT -ne 'github-hosted' -or -not $IsWindows) {
    throw 'Autostart restart proof requires an isolated GitHub-hosted Windows runner; never run on a user desktop.'
}
$ExePath = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $ExePath).Path)
$expectedCommand = if ($ExePath -match '[ \t]') { '"' + $ExePath + '"' } else { $ExePath }
$keyPath = 'Software\Microsoft\Windows\CurrentVersion\Run'
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath)
try {
    if ($null -ne $key -and $key.GetValueNames() -contains 'NikaCore') {
        throw 'Proof refuses to overwrite an existing NikaCore registration.'
    }
} finally { if ($null -ne $key) { $key.Dispose() } }

$proof = Join-Path $PSScriptRoot 'm5_uia_proof.ps1'
$pwsh = (Get-Process -Id $PID).Path
$qaRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('nika-ollama-loopback-' + [guid]::NewGuid().ToString('N'))
$serverScript = Join-Path $qaRoot 'ollama_loopback.py'
$readyPath = Join-Path $qaRoot 'ready.txt'
$requestLog = Join-Path $qaRoot 'requests.jsonl'
$qaServer = $null

function Assert-SelectedModelRequests {
    if (-not (Test-Path -LiteralPath $requestLog)) {
        throw 'Packaged selected-model proof produced no Ollama-compatible loopback request log.'
    }
    $lines = @(
        Get-Content -LiteralPath $requestLog |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($lines.Count -ne 3) {
        throw "Expected exactly three packaged selected-model requests (two workers and checker), got $($lines.Count)."
    }
    $requests = @($lines | ForEach-Object { $_ | ConvertFrom-Json })
    foreach ($request in $requests) {
        if ($request.path -cne '/api/chat') {
            throw "Packaged model request used unexpected path '$($request.path)'."
        }
        if ($request.model -cne 'uia-proof-model') {
            throw "Packaged model request used unexpected model '$($request.model)'."
        }
        if ($request.stream -ne $false) {
            throw 'Packaged Ollama-compatible request did not set stream=false.'
        }
        if ($request.think -ne $false) {
            throw 'Packaged Ollama-compatible request did not set think=false.'
        }
        if ([int]$request.message_count -lt 1) {
            throw 'Packaged Ollama-compatible request did not contain model messages.'
        }
        if ($request.authorization_present -ne $false) {
            throw 'Local packaged Ollama-compatible request unexpectedly carried Authorization.'
        }
    }
    Write-Host 'Packaged selected Ollama route issued exactly three canonical loopback /api/chat requests with model uia-proof-model, stream=false, think=false. Physical Ollama/model inference remains unverified.'
}

New-Item -ItemType Directory -Path $qaRoot -Force | Out-Null
$serverSource = @'
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

READY = Path(sys.argv[1])
REQUESTS = Path(sys.argv[2])
LOCK = threading.Lock()
MAX_BODY_BYTES = 2_000_000


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        if self.path != "/api/chat":
            self.send_error(404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
            if content_length <= 0 or content_length > MAX_BODY_BYTES:
                raise ValueError("invalid content length")
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("request body must be an object")
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                raise ValueError("messages must be non-empty")
            record = {
                "path": self.path,
                "model": payload.get("model"),
                "stream": payload.get("stream"),
                "think": payload.get("think"),
                "message_count": len(messages),
                "authorization_present": bool(self.headers.get("Authorization")),
            }
            with LOCK:
                with REQUESTS.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            response = {
                "model": payload.get("model"),
                "message": {
                    "role": "assistant",
                    "content": "controlled loopback response",
                },
                "done": True,
                "prompt_eval_count": 1,
                "eval_count": 1,
            }
            encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except Exception:
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


server = ThreadingHTTPServer(("127.0.0.1", 11434), Handler)
READY.write_text("11434\n", encoding="ascii")
server.serve_forever(poll_interval=0.1)
'@
$serverSource | Set-Content -LiteralPath $serverScript -Encoding utf8

try {
    # Own the exact loopback port before launching Nika. If anything else (including
    # physical Ollama) already owns it, the QA server exits and this proof fails
    # closed rather than sending controlled source text to a foreign process.
    $python = (Get-Command python -ErrorAction Stop).Source
    $qaServer = Start-Process -FilePath $python -ArgumentList @(
        "`"$serverScript`"",
        "`"$readyPath`"",
        "`"$requestLog`""
    ) -PassThru -WindowStyle Hidden
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $readyPath)) {
        $qaServer.Refresh()
        if ($qaServer.HasExited) {
            throw 'Bounded Ollama-compatible loopback QA server could not bind 127.0.0.1:11434.'
        }
        if ([DateTime]::UtcNow -ge $readyDeadline) {
            throw 'Bounded Ollama-compatible loopback QA server did not become ready within 10 seconds.'
        }
        Start-Sleep -Milliseconds 100
    }
    if ((Get-Content -LiteralPath $readyPath -Raw).Trim() -cne '11434') {
        throw 'Bounded Ollama-compatible loopback QA server reported an unexpected port.'
    }

    # Each invocation cold-starts the same real EXE with a new PID/window generation.
    # The existing M5 harness owns identity/focus, process cleanup and private DB fixtures.
    # The Enable/source phase additionally drives the selected model through the
    # canonical packaged ModelGateway into the proof-owned loopback Ollama endpoint.
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Enable -VerifySourceSetup
    if ($LASTEXITCODE -ne 0) { throw 'Packaged autostart enable/source-setup phase failed.' }
    Assert-SelectedModelRequests
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Observe
    if ($LASTEXITCODE -ne 0) { throw 'Packaged autostart persistence after process restart failed.' }
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Disable
    if ($LASTEXITCODE -ne 0) { throw 'Packaged autostart disable phase failed.' }
    Write-Host 'Autostart UI -> registration -> fresh process -> persisted UI -> disable verified. Windows login execution and human NVDA remain unverified.'
} finally {
    if ($null -ne $qaServer) {
        try {
            $qaServer.Refresh()
            if (-not $qaServer.HasExited) {
                Stop-Process -Id $qaServer.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {
            Write-Warning 'Could not inspect the bounded loopback QA server during cleanup.'
        }
    }
    if (Test-Path -LiteralPath $qaRoot) {
        Remove-Item -LiteralPath $qaRoot -Recurse -Force -ErrorAction SilentlyContinue
    }

    # Remove only our exact test-owned value if a later phase failed. Preserve any
    # concurrently replaced registration; never delete the Run key or other values.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath, $true)
    try {
        if ($null -ne $key) {
            $current = $key.GetValue('NikaCore', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ($current -ceq $expectedCommand) { $key.DeleteValue('NikaCore', $false) }
            elseif ($null -ne $current) { Write-Warning 'A foreign registration appeared; left untouched.' }
        }
    } finally { if ($null -ne $key) { $key.Dispose() } }
}
