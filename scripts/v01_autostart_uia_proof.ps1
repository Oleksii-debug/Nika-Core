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

    # Prove the generic keyboard/source/model journey without granting autostart
    # mutation authority. The proof-owned Ollama-compatible loopback remains live
    # through this phase so the selected model transport is verified exactly once.
    # The generic M5 harness owns a private per-process DB fixture, so a known hosted-
    # WebView2 focus transient can be retried once in a fresh process without replaying
    # any HKCU Run mutation.
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -VerifySourceSetup
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Packaged generic keyboard/source-setup proof made no autostart mutation; retrying once in a fresh process.'
        # The retry owns a fresh M5 database/process generation. Discard only transport
        # evidence from the failed proof generation so exact-three counts describe the
        # successful retry rather than accumulating stale QA observations.
        Remove-Item -LiteralPath $requestLog -Force -ErrorAction SilentlyContinue
        & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -VerifySourceSetup
        if ($LASTEXITCODE -ne 0) {
            throw 'Packaged generic keyboard/source-setup proof failed after the single non-mutating retry.'
        }
    }
    Assert-SelectedModelRequests

    # Each autostart invocation cold-starts the same real EXE with a new PID/window
    # generation and exercises only the exact autostart semantic controls.
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Enable
    if ($LASTEXITCODE -ne 0) {
        # Hosted WebView2 can occasionally keep focus on the exact Save button while
        # dropping its keyboard activation. Never replay an unknown write: retry the
        # fresh-process Enable mutation at most once only when the OS proves the
        # failed attempt left the test-owned registration completely absent.
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath)
        try {
            $afterFailedEnable = if ($null -eq $key) {
                $null
            } else {
                $key.GetValue('NikaCore', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            }
        } finally { if ($null -ne $key) { $key.Dispose() } }

        if ($null -eq $afterFailedEnable) {
            Write-Host 'Autostart enable attempt made no OS registration mutation; retrying once in a fresh process.'
            & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Enable
            if ($LASTEXITCODE -ne 0) {
                throw 'Packaged autostart enable phase failed after the single zero-mutation retry.'
            }
        } elseif ($afterFailedEnable -ceq $expectedCommand) {
            throw 'Packaged autostart enable phase failed after changing OS registration; refusing to replay the mutation.'
        } else {
            throw 'Packaged autostart enable phase failed with unexpected OS registration; refusing to replay the mutation.'
        }
    }
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Observe
    if ($LASTEXITCODE -ne 0) { throw 'Packaged autostart persistence after process restart failed.' }
    & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Disable
    if ($LASTEXITCODE -ne 0) {
        # A hosted WebView2 provider can occasionally drop the keyboard activation
        # while leaving focus on the exact Save button. Replaying an unknown write is
        # forbidden. Retry the fresh-process Disable mutation at most once only when
        # the OS proves that the failed attempt made no registration mutation at all.
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($keyPath)
        try {
            $afterFailedDisable = if ($null -eq $key) {
                $null
            } else {
                $key.GetValue('NikaCore', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            }
        } finally { if ($null -ne $key) { $key.Dispose() } }

        if ($afterFailedDisable -ceq $expectedCommand) {
            Write-Host 'Autostart disable attempt made no OS registration mutation; retrying once in a fresh process.'
            & $pwsh -NoProfile -File $proof -ExePath $ExePath -WindowTitle $WindowTitle -AutostartPhase Disable
            if ($LASTEXITCODE -ne 0) {
                throw 'Packaged autostart disable phase failed after the single zero-mutation retry.'
            }
        } elseif ($null -eq $afterFailedDisable) {
            throw 'Packaged autostart disable phase failed after changing OS registration; refusing to replay the mutation.'
        } else {
            throw 'Packaged autostart disable phase failed with unexpected OS registration; refusing to replay the mutation.'
        }
    }
    Write-Host 'Generic keyboard/source proof plus autostart UI -> registration -> fresh process -> persisted UI -> disable verified. Windows login execution and human NVDA remain unverified.'
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
