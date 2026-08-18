param(
    [Parameter(Mandatory=$true)][string]$ExePath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$requiredNames = @('Nika Core', 'Що має зробити Nika?', 'Створити завдання', 'Клавіатура')
$process = Start-Process -FilePath $ExePath -PassThru
try {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $window = $null
    for ($attempt = 0; $attempt -lt 40 -and $null -eq $window; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            throw "Nika Core exited before the top-level UI Automation window appeared. Exit code: $($process.ExitCode)"
        }
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            'Nika Core M5 Proof'
        )
        $window = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $condition)
    }
    if ($null -eq $window) { throw 'Nika Core top-level window not found in UI Automation tree.' }

    $names = @()
    $missing = $requiredNames
    for ($attempt = 0; $attempt -lt 40 -and $missing.Count -gt 0; $attempt++) {
        Start-Sleep -Milliseconds 500
        if ($process.HasExited) {
            throw "Nika Core exited while waiting for WebView2 accessibility descendants. Exit code: $($process.ExitCode)"
        }
        $descendants = $window.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            [System.Windows.Automation.Condition]::TrueCondition
        )
        $names = @()
        foreach ($element in $descendants) {
            try {
                if ($element.Current.Name) { $names += $element.Current.Name }
            } catch { }
        }
        $missing = @($requiredNames | Where-Object { $names -notcontains $_ })
    }

    if ($missing.Count -gt 0) {
        $preview = ($names | Select-Object -Unique | Select-Object -First 80) -join ' | '
        throw "WebView2 UIA descendants were not discoverable. Missing: $($missing -join ', '). Seen: $preview"
    }

    Write-Host 'WebView2 UI Automation descendants discovered successfully.'
    Write-Host (($names | Select-Object -Unique | Select-Object -First 40) -join ' | ')
}
finally {
    if (!$process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
