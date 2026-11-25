param(
    [Parameter(Mandatory=$true)][string]$Bundle,
    [Parameter(Mandatory=$true)][string]$ResultDirectory
)
$ErrorActionPreference = 'Stop'
$bundlePath = (Resolve-Path -LiteralPath $Bundle).Path
$agent = Join-Path $bundlePath 'XRelay-Agent.exe'
if (-not (Test-Path -LiteralPath $agent -PathType Leaf)) { throw 'Agent executable missing.' }
if (Test-Path -LiteralPath $ResultDirectory) { throw 'Choose a new results directory.' }
$work = (New-Item -ItemType Directory -Path $ResultDirectory).FullName
$source = (New-Item -ItemType Directory -Path (Join-Path $work '样品')).FullName
$output = Join-Path $work '结果'
$sample = Join-Path $source '探测器.tif'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'fixtures/radial-smoke.tif') -Destination $sample
$before = (Get-FileHash -LiteralPath $sample).Hash

$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = $agent
$start.Arguments = '--input-root "' + $source + '" --output-root "' + $output + '"'
$start.WorkingDirectory = $work
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardInput = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$start.StandardErrorEncoding = [System.Text.Encoding]::UTF8
$start.EnvironmentVariables.Remove('PYTHONPATH')
$start.EnvironmentVariables.Remove('PYTHONHOME')
$start.EnvironmentVariables['PATH'] = "$env:SystemRoot\System32;$env:SystemRoot"
$process = [System.Diagnostics.Process]::Start($start)
$stderr = $process.StandardError.ReadToEndAsync()
$script:requestId = 0
function Send-Request($Method, $Parameters) {
    $operation = $Method
    if ($Method -eq 'tools/call') { $operation = $Parameters.name }
    $script:requestId++
    $message = @{jsonrpc='2.0'; id=$script:requestId; method=$Method; params=$Parameters}
    $json = $message | ConvertTo-Json -Compress -Depth 10
    $wire = [regex]::Replace($json, '[^\u0000-\u007f]', {
        param($match)
        '\u{0:x4}' -f [int][char]$match.Value
    })
    $process.StandardInput.WriteLine($wire)
    $process.StandardInput.Flush()
    $response = $process.StandardOutput.ReadLineAsync()
    if (-not $response.Wait(30000)) { throw "Timed out after 30 seconds waiting for $operation." }
    if ($null -eq $response.Result) { throw "Agent closed its output while waiting for $operation." }
    $parsed = $response.Result | ConvertFrom-Json
    if ($parsed.id -ne $script:requestId -or $parsed.error) { throw "Invalid MCP reply: $($response.Result)" }
    $parsed.result
}
function Invoke-Tool($Name, $Arguments) {
    $reply = Send-Request 'tools/call' @{name=$Name; arguments=$Arguments}
    if ($reply.isError) { throw $reply.content[0].text }
    $reply.content[0].text | ConvertFrom-Json
}
try {
    $protocol = Send-Request 'initialize' @{protocolVersion='2025-06-18'}
    if ($protocol.protocolVersion -ne '2025-06-18') { throw 'Protocol negotiation failed.' }
    $image = Invoke-Tool 'inspect_image' @{path='探测器.tif'}
    if (($image.shape -join 'x') -ne '64x64') { throw 'Unexpected detector shape.' }
    $job = Invoke-Tool 'start_integration' @{
        paths=@('探测器.tif'); kind='radial'; settings=@{npt=100; radial_range=@(.05,1.0)}
        geometry=@{wavelength_m=1e-10; detector_distance_m=.2; pixel_size_x_m=1e-4;
                   pixel_size_y_m=1e-4; beam_center_x_px=32; beam_center_y_px=32}
    }
    $deadline = (Get-Date).AddSeconds(45)
    do {
        $state = Invoke-Tool 'get_job' @{job_id=$job.job_id}
        if ($state.state -notin @('queued','running')) { break }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    if ($state.state -ne 'completed' -or $state.errors.Count) {
        throw ($state | ConvertTo-Json -Compress -Depth 8)
    }
    $workbook = (Resolve-Path -LiteralPath $state.output).Path
    if (-not $workbook.StartsWith($output + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Workbook is outside the selected output directory.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($workbook)
    try {
        $sheet = $archive.GetEntry('xl/worksheets/sheet1.xml')
        if (-not $sheet) { throw 'Missing curve worksheet.' }
        $reader = New-Object System.IO.StreamReader($sheet.Open())
        try { [xml]$xml = $reader.ReadToEnd() } finally { $reader.Dispose() }
        $cell = $xml.SelectSingleNode("//*[local-name()='c' and @r='A5']/*[local-name()='v']")
        if (-not $cell -or -not $cell.InnerText) { throw 'Missing exported curve value.' }
        $curveX = [double]::Parse($cell.InnerText,[Globalization.CultureInfo]::InvariantCulture)
        if ($curveX -lt .05 -or $curveX -gt 1.0) { throw 'Unexpected exported q value.' }
    } finally { $archive.Dispose() }
    $fitJob = Invoke-Tool 'start_fit' @{
        integration_job=$job.job_id
        settings=@{model='gaussian_height'; x_range=@(.1,.8)}
    }
    $deadline = (Get-Date).AddSeconds(45)
    do {
        $fitState = Invoke-Tool 'get_job' @{job_id=$fitJob.job_id}
        if ($fitState.state -notin @('queued','running')) { break }
        Start-Sleep -Milliseconds 200
    } while ((Get-Date) -lt $deadline)
    if ($fitState.state -ne 'completed' -or $fitState.errors.Count) {
        throw ($fitState | ConvertTo-Json -Compress -Depth 8)
    }
    $fitWorkbook = (Resolve-Path -LiteralPath $fitState.output).Path
    if (-not $fitWorkbook.StartsWith($output + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Fit workbook is outside the selected output directory.'
    }
    $archive = [System.IO.Compression.ZipFile]::OpenRead($fitWorkbook)
    try {
        $reader = New-Object System.IO.StreamReader($archive.GetEntry('xl/worksheets/sheet1.xml').Open())
        try { [xml]$xml = $reader.ReadToEnd() } finally { $reader.Dispose() }
        $raw = @{}
        $residualChecks = 0
        foreach ($row in $xml.SelectNodes("//*[local-name()='row']")) {
            if ([int]$row.r -lt 5) { continue }
            $cells = @{}
            foreach ($cell in $row.SelectNodes("*[local-name()='c']")) {
                $value = $cell.SelectSingleNode("*[local-name()='v']")
                if ($value) { $cells[($cell.r -replace '[0-9]', '')] = $value.InnerText }
            }
            if ($cells.ContainsKey('A') -and $cells.ContainsKey('B')) {
                $raw[$cells['A']] = [double]::Parse($cells['B'], [Globalization.CultureInfo]::InvariantCulture)
            }
        }
        foreach ($row in $xml.SelectNodes("//*[local-name()='row']")) {
            if ([int]$row.r -lt 5) { continue }
            $cells = @{}
            foreach ($cell in $row.SelectNodes("*[local-name()='c']")) {
                $value = $cell.SelectSingleNode("*[local-name()='v']")
                if ($value) { $cells[($cell.r -replace '[0-9]', '')] = $value.InnerText }
            }
            if (-not $cells.ContainsKey('E')) { continue }
            if ($cells['C'] -ne $cells['E'] -or -not $raw.ContainsKey($cells['E'])) {
                throw 'Fit and residual coordinates are misaligned.'
            }
            $predicted = [double]::Parse($cells['D'], [Globalization.CultureInfo]::InvariantCulture)
            $residual = [double]::Parse($cells['F'], [Globalization.CultureInfo]::InvariantCulture)
            if ([math]::Abs($residual - ($raw[$cells['E']] - $predicted)) -gt 1e-8) {
                throw 'Exported residual differs from observed minus fit.'
            }
            $residualChecks++
        }
        if ($residualChecks -lt 5) { throw 'Too few exported residual points.' }
    } finally { $archive.Dispose() }
    if ((Get-FileHash -LiteralPath $sample).Hash -ne $before) { throw 'Source checksum changed.' }
    $process.StandardInput.Close()
    if (-not $process.WaitForExit(10000) -or $process.ExitCode -ne 0) { throw 'Agent did not exit cleanly.' }
    [ordered]@{result='passed'; os=[Environment]::OSVersion.VersionString; protocol=$protocol.protocolVersion;
        source_sha256=$before; workbook=$workbook; first_q=$curveX;
        fit_workbook=$fitWorkbook; residual_points_checked=$residualChecks; agent_exit=$process.ExitCode} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $work 'acceptance.json') -Encoding UTF8
    Write-Output "PASS: $work"
} catch {
    if ($process.WaitForExit(1000)) {
        Write-Warning "Agent exit code: $($process.ExitCode)"
        if ($stderr.Wait(1000) -and $stderr.Result) { Write-Warning $stderr.Result }
    }
    throw
} finally {
    if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
    $process.Dispose()
}
