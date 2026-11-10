# Reports this PC's scheduled jobs to the lab (lab.machaka.net/pc) every 10 minutes (scheduled task "lab-report").
# Counts and results only: no email subjects, senders or bodies ever leave the PC.
# Reads: Task Scheduler (last run, result, next run) and the jobs' own logs. Changes nothing.
# Sends to the lab's intake on the home network (NodePort on the Acer) with the token in %USERPROFILE%\.lab-token.
param([switch]$DryRun)          # -DryRun: print what would be sent, send nothing
$ErrorActionPreference = 'Stop'
$Intake = 'http://192.168.1.93:30881/report/pc'
$Coding = Join-Path $env:USERPROFILE 'Documents\Coding'
$TokenFile = Join-Path $env:USERPROFILE '.lab-token'
$LogFile = Join-Path $env:LOCALAPPDATA 'lab-report.log'

function Log($msg) {
    $line = '{0} {1}' -f (Get-Date -Format 's'), $msg
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    if ((Get-Item $LogFile).Length -gt 200KB) { (Get-Content $LogFile -Tail 500) | Set-Content $LogFile -Encoding utf8 }
}
# The jobs keep their logs open: read with sharing, and only the end of a big file.
function ReadShared($path, $tailBytes = 1MB) {
    $fs = [IO.File]::Open($path, 'Open', 'Read', [IO.FileShare]'ReadWrite, Delete')
    try {
        if ($fs.Length -gt $tailBytes) { $fs.Seek(-$tailBytes, 'End') | Out-Null }
        $text = (New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8)).ReadToEnd()
    } finally { $fs.Dispose() }
    return $text -split "`r?`n"
}
function Iso($d) { if ($d -and $d.Year -gt 2000) { $d.ToString('o') } else { $null } }

# ---- Scheduled tasks -------------------------------------------------------------------------------------------
# Results that aren't failures: 0x41301 still running, 0x800710E0 skipped because the last run was still going,
# 0x41303 never run yet.
$tasks = @()
$wanted = Get-ScheduledTask | Where-Object {
    ($_.TaskPath -eq '\' -and $_.TaskName -in @('GmailRejectionArchiver', 'GmailDailyReview', 'job-scrapers upwork round')) -or
    ($_.TaskPath -eq '\career-ops\' -and $_.State -ne 'Disabled')
}
foreach ($t in $wanted) {
    $i = $t | Get-ScheduledTaskInfo
    $code = [int64]$i.LastTaskResult
    if ($code -lt 0) { $code += 4294967296 }
    $status = 'failed'; $note = ''
    switch ($code) {
        0          { $status = 'ok' }
        267009     { $status = 'running' }
        267011     { $status = 'unknown'; $note = 'not run yet' }
        2147946720 { $status = 'ok'; $note = 'last start skipped: the run before was still going' }
        default    { $note = 'exit code {0} (0x{1:X})' -f $code, $code }
    }
    if ($t.State -eq 'Running') { $status = 'running' }
    $tasks += [ordered]@{ name = ($t.TaskPath.Trim('\') + ' ' + $t.TaskName).Trim(); status = $status; note = $note;
                          last_run = (Iso $i.LastRunTime); next_run = (Iso $i.NextRunTime); code = $code }
}

$jobs = [ordered]@{}
$headline = [ordered]@{}
$today = Get-Date -Format 'yyyy-MM-dd'

# ---- Gmail rejection archiver: today's archived emails by kind ---------------------------------------------------
$log = Join-Path $Coding 'Email-remove\Email-remove\archiver.log'
if (Test-Path $log) {
    $kinds = @{}; $errors = 0; $last = $null; $classify = 0
    foreach ($line in (ReadShared $log 2MB)) {
        if (-not $line.StartsWith($today)) { continue }
        $last = $line.Substring(0, 19)
        if ($line -match '\]\s+ARCHIVE \(([a-z_ -]+)\)') { $kinds[$Matches[1]] = 1 + [int]$kinds[$Matches[1]] }
        elseif ($line -match '\[ERROR\]') { $errors++ }
        elseif ($line -match 'new to classify: (\d+)') { $classify += [int]$Matches[1] }
    }
    $total = ($kinds.Values | Measure-Object -Sum).Sum
    $numbers = [ordered]@{ 'Archived today' = [int]$total }
    foreach ($k in ($kinds.Keys | Sort-Object)) { $numbers["  $k"] = $kinds[$k] }
    $numbers['Errors today'] = $errors
    if ($last) { $numbers['Last check'] = $last.Substring(11, 5) }
    $jobs['archiver'] = [ordered]@{ title = 'Gmail rejection archiver'; summary = 'Every 3 minutes, 3 accounts';
                                    numbers = $numbers; status = 'ok' }
    # A few errors a day are network blips; failing means it stopped checking (no cycle in 15 minutes).
    if (-not $last -or ((Get-Date) - [datetime]$last).TotalMinutes -gt 15) {
        $jobs['archiver'].status = 'failed'; $jobs['archiver'].note = 'No check in the last 15 minutes.'
    }
    $headline['Archived today'] = "$([int]$total) emails"
}

# ---- Gmail daily review: the last review sent ---------------------------------------------------------------------
$log = Join-Path $Coding 'Email-remove\Email-remove\review.log'
if (Test-Path $log) {
    $lines = ReadShared $log 64KB | Select-Object -Last 80
    $sent = $lines | Where-Object { $_ -match '\] Sent to ' } | Select-Object -Last 1
    $read = 0
    foreach ($l in $lines) { if ($l -match ': (\d+) new email\(s\)') { $read += [int]$Matches[1] } }
    $numbers = [ordered]@{}
    if ($sent) { $numbers['Last review sent'] = $sent.Substring(0, 16) }
    $numbers['New emails in recent runs'] = $read
    $jobs['review'] = [ordered]@{ title = 'Gmail daily review'; summary = 'Daily at 17:00: an LLM reads the day''s mail and emails a review';
                                  numbers = $numbers }
    if ($sent) { $headline['Daily review'] = 'sent ' + $sent.Substring(5, 11) }
}

# ---- Upwork round ---------------------------------------------------------------------------------------------------
$dir = Join-Path $Coding 'job-scrapers\data'
if (Test-Path (Join-Path $dir 'upwork-run.log')) {
    $lines = ReadShared (Join-Path $dir 'upwork-run.log') 64KB | Select-Object -Last 60
    $round = $lines | Where-Object { $_ -match '^\d+ pages, \d+ new jobs' } | Select-Object -Last 1
    $scored = $lines | Where-Object { $_ -match '^\d+ scored' } | Select-Object -Last 1
    $stopped = Join-Path $dir 'upwork-stopped.txt'
    $numbers = [ordered]@{}
    if ($round -match '^(\d+) pages, (\d+) new jobs') { $numbers['Pages, last round'] = [int]$Matches[1]; $numbers['New jobs, last round'] = [int]$Matches[2] }
    if ($scored -match '^(\d+) scored') { $numbers['Scored, last round'] = [int]$Matches[1] }
    $job = [ordered]@{ title = 'Upwork scan'; summary = '4 rounds a day; new jobs scoring 3.5+ are emailed'; numbers = $numbers;
                       status = 'ok' }
    if (Test-Path $stopped) {
        $since = (Get-Item $stopped).LastWriteTime
        $job.status = 'failed'
        $job.note = 'Stopped since {0:d MMM HH:mm}: Upwork sent the scraper to its login page. Rounds stay off until upwork-stopped.txt is cleared.' -f $since
        $headline['Upwork'] = 'stopped (login page)'
        foreach ($t in $tasks) { if ($t.name -eq 'job-scrapers upwork round') { $t.status = 'failed'; $t.note = 'stopped: Upwork asked for a login' } }
    } elseif ($round) { $headline['Upwork'] = ($round -split ',')[0..1] -join ',' }
    $jobs['upwork'] = $job
}

# ---- FreeMotion: the last scan and the open inbox items -------------------------------------------------------
$data = Join-Path $Coding 'fm\fm-data\data'
if (Test-Path (Join-Path $data 'scan-runs.tsv')) {
    $rows = ReadShared (Join-Path $data 'scan-runs.tsv') 4MB | Where-Object { $_ }
    $head = $rows[0] -split "`t"; $last = $rows[-1] -split "`t"
    $row = @{}; for ($k = 0; $k -lt $head.Count; $k++) { $row[$head[$k]] = $last[$k] }
    $open = 0
    $inbox = Join-Path $data 'agent-inbox.md'
    if (Test-Path $inbox) { $open = @(Select-String -Path $inbox -Pattern '^- \[ \]').Count }
    $numbers = [ordered]@{ 'Last scan' = ([datetime]$row['timestamp']).ToLocalTime().ToString('d MMM HH:mm');
                           'Jobs found' = $row['found']; 'New added' = $row['new_added']; 'Scan errors' = $row['errors'];
                           'Inbox items for you' = $open }
    $jobs['freemotion'] = [ordered]@{ title = 'FreeMotion'; summary = 'Job search pipeline (details at freemotion.machaka.net)';
                                      status = $(if ($row['status'] -eq 'completed') { 'ok' } else { 'failed' }); numbers = $numbers }
    $headline['FreeMotion'] = "$($row['new_added']) new jobs, $open inbox items"
}

# ---- Send ------------------------------------------------------------------------------------------------------
$body = [ordered]@{ host = $env:COMPUTERNAME; at = (Get-Date).ToString('o'); tasks = $tasks; jobs = $jobs; headline = $headline }
$json = $body | ConvertTo-Json -Depth 6 -Compress
if ($DryRun) { $body | ConvertTo-Json -Depth 6; exit 0 }
if (-not (Test-Path $TokenFile)) { Log 'no token file: nothing sent'; exit 0 }
$token = (Get-Content $TokenFile -Raw).Trim()
try {
    Invoke-RestMethod -Uri $Intake -Method Post -ContentType 'application/json; charset=utf-8' -TimeoutSec 15 `
        -Headers @{ Authorization = "Bearer $token" } -Body ([Text.Encoding]::UTF8.GetBytes($json)) | Out-Null
} catch {
    Log "send failed: $($_.Exception.Message)"
    exit 1
}
