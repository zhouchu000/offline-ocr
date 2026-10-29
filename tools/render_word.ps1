param([Parameter(Mandatory=$true)][string]$InputDocx, [Parameter(Mandatory=$true)][string]$OutputPdf)
$ErrorActionPreference = 'Stop'
$sourcePath = (Resolve-Path -LiteralPath $InputDocx).Path
$pdfPath = [IO.Path]::GetFullPath($OutputPdf)
[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($pdfPath)) | Out-Null
$priorIds = @(Get-Process WINWORD -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public static class OwnedWordWindow { [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid); }'
$word = New-Object -ComObject Word.Application
$document = $null
$probe = $null
$owned = $false
[object]$noSave = 0
[object]$firstWindow = 1
try {
    # Word exposes HWND on document windows, unlike Excel's Application.Hwnd.
    # Create only our own empty document to identify the COM instance first.
    $probe = $word.Documents.Add()
    [uint32]$wordProcessId = 0
    [OwnedWordWindow]::GetWindowThreadProcessId([IntPtr]$probe.Windows.Item([ref]$firstWindow).Hwnd, [ref]$wordProcessId) | Out-Null
    if ($wordProcessId -eq 0 -or $priorIds -contains $wordProcessId) { throw 'A separate Word instance could not be verified.' }
    if ((Get-Process -Id $wordProcessId).ProcessName -ne 'WINWORD') { throw 'The COM provider is not Microsoft Word.' }
    $owned = $true
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $document = $word.Documents.Open($sourcePath, $false, $true, $false)
    $document.Repaginate()
    $count = $document.ComputeStatistics(2)
    $document.ExportAsFixedFormat($pdfPath, 17)
    [pscustomobject]@{pages=$count; pdf_created=[IO.File]::Exists($pdfPath); owned_instance=$owned} | ConvertTo-Json
} finally {
    try {
        if ($null -ne $document) { $document.Close([ref]$noSave); [Runtime.InteropServices.Marshal]::FinalReleaseComObject($document) | Out-Null }
        if ($null -ne $probe) {
            try { $probe.Close([ref]$noSave) } catch {
                if ($_.Exception.GetBaseException().HResult -ne -2147417848) { throw }
            }
            [Runtime.InteropServices.Marshal]::FinalReleaseComObject($probe) | Out-Null
        }
    } finally {
        if ($owned) {
            try { $word.Quit([ref]$noSave) } catch {
                if ($_.Exception.GetBaseException().HResult -ne -2147417848) { throw }
            }
        }
        [Runtime.InteropServices.Marshal]::FinalReleaseComObject($word) | Out-Null
    }
}
