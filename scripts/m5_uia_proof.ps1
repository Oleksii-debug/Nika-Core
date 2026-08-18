param(
    [Parameter(Mandatory=$true)][string]$ExePath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$process = Start-Process -FilePath $ExePath -PassThru
try {
    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $window = $null
    for ($attempt = 0; $attempt -lt 40 -and $null -eq $window; $attempt++) {
        Start-Sleep -Milliseconds 500
        $condition = New-Object System.Windows.Automation.PropertyCondition(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            'Nika Core M5 Proof'
        )
        $window = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, $condition)
    }
    if ($null -eq $window) { throw 'Nika Core top-level window not found in UI Automation tree.' }

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

    foreach ($required in @('Nika Core', 'Що має зробити Nika?', 'Створити завдання', 'Клавіатура')) {
        if ($names -notcontains $required) {
            $preview = ($names | Select-Object -Unique | Select-Object -First 80) -join ' | '
            throw "WebView2 UIA descendant '$required' was not discoverable. Seen: $preview"
        }
    }

    Write-Host 'WebView2 UI Automation descendants discovered successfully.'
    Write-Host (($names | Select-Object -Unique | Select-Object -First 40) -join ' | ')
}
finally {
    if (!$process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
}
