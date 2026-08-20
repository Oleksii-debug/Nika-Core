Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Nika DEV04 UIA Proof'
$form.Width = 620
$form.Height = 430
$form.StartPosition = 'CenterScreen'

$heading = New-Object System.Windows.Forms.Label
$heading.Text = 'Computer Interaction accessibility proof'
$heading.AccessibleName = 'Computer Interaction accessibility proof'
$heading.AutoSize = $true
$heading.Left = 20
$heading.Top = 20
$form.Controls.Add($heading)

$inputLabel = New-Object System.Windows.Forms.Label
$inputLabel.Text = 'Problem description'
$inputLabel.AutoSize = $true
$inputLabel.Left = 20
$inputLabel.Top = 65
$form.Controls.Add($inputLabel)

$input = New-Object System.Windows.Forms.TextBox
$input.Name = 'ProblemInput'
$input.AccessibleName = 'Problem description'
$input.Left = 20
$input.Top = 90
$input.Width = 360
$form.Controls.Add($input)

$check = New-Object System.Windows.Forms.CheckBox
$check.Name = 'VerifySemantics'
$check.Text = 'Verify semantic target'
$check.AccessibleName = 'Verify semantic target'
$check.Left = 20
$check.Top = 135
$check.Width = 260
$form.Controls.Add($check)

$comboLabel = New-Object System.Windows.Forms.Label
$comboLabel.Text = 'Action mode'
$comboLabel.AutoSize = $true
$comboLabel.Left = 20
$comboLabel.Top = 180
$form.Controls.Add($comboLabel)

$combo = New-Object System.Windows.Forms.ComboBox
$combo.Name = 'ActionMode'
$combo.AccessibleName = 'Action mode'
$combo.Left = 20
$combo.Top = 205
$combo.Width = 220
$combo.DropDownStyle = 'DropDownList'
[void]$combo.Items.Add('Observe')
[void]$combo.Items.Add('Repair')
$combo.SelectedIndex = 0
$form.Controls.Add($combo)

$apply = New-Object System.Windows.Forms.Button
$apply.Name = 'ApplyButton'
$apply.Text = 'Apply semantic action'
$apply.AccessibleName = 'Apply semantic action'
$apply.Left = 20
$apply.Top = 260
$apply.Width = 190
$form.Controls.Add($apply)

$status = New-Object System.Windows.Forms.Label
$status.Name = 'StatusLabel'
$status.Text = 'Ready'
$status.AccessibleName = 'Status'
$status.AutoSize = $true
$status.Left = 20
$status.Top = 315
$form.Controls.Add($status)

$apply.Add_Click({
    $status.Text = 'Applied: ' + $input.Text
    $status.AccessibleName = $status.Text
}.GetNewClosure())

$form.Add_Shown({
    param($sender, $eventArgs)
    $target = $sender.Controls.Find('ProblemInput', $true)
    if ($target.Count -ne 1) {
        throw "Expected exactly one ProblemInput control, found $($target.Count)"
    }
    [void]$target[0].Focus()
})
[void]$form.ShowDialog()
