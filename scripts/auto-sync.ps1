param(
    [string]$RepoPath = (Split-Path -Parent $PSScriptRoot),
    [int]$DebounceSeconds = 15,
    [int]$PollSeconds = 5
)

$ErrorActionPreference = 'Stop'

$mutex = New-Object System.Threading.Mutex($false, 'Local\HostWatchGitAutoSync')
if (-not $mutex.WaitOne(0)) {
    exit 0
}

try {
    Set-Location -LiteralPath $RepoPath
    $logPath = Join-Path $RepoPath 'auto-sync.log'

    function Write-Log([string]$Message) {
        $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
        Add-Content -LiteralPath $logPath -Value $line
    }

    function Get-StatusText {
        return ((git status --porcelain=v1 2>&1) -join "`n")
    }

    function Sync-Repository {
        $branch = (git branch --show-current).Trim()
        if ([string]::IsNullOrWhiteSpace($branch)) {
            Write-Log 'Skipped sync because HEAD is detached.'
            return
        }

        git add --all -- .

        # Keep credentials, local state, and runtime caches out of automatic commits.
        $stagedPaths = @(git diff --cached --name-only)
        foreach ($path in $stagedPaths) {
            if ($path -match '(^|[/\\])(?:\.env(?:\..*)?|config\.json|status\.json|catalog\.json|state(?:[-.].*)?|exchange-rates\.json|inventory-cache\.json|auto-sync\.log)$' -or
                $path -match '\.(?:pem|key|p12|pfx)$' -or
                $path -match '(?i)(?:secret|password|credential)') {
                git restore --staged -- $path
                Write-Log "Skipped sensitive or runtime path: $path"
            }
        }

        git diff --cached --quiet
        if ($LASTEXITCODE -eq 0) {
            return
        }

        $message = "Auto-sync: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        git commit -m $message
        if ($LASTEXITCODE -ne 0) {
            Write-Log 'Commit failed.'
            return
        }

        git push origin "HEAD:$branch"
        if ($LASTEXITCODE -eq 0) {
            Write-Log "Pushed commit on $branch."
        } else {
            Write-Log "Push failed on $branch; commit remains local."
        }
    }

    Write-Log "Started for $RepoPath."
    $lastStatus = Get-StatusText
    $changedAt = Get-Date

    while ($true) {
        Start-Sleep -Seconds $PollSeconds
        $status = Get-StatusText
        if ($status -ne $lastStatus) {
            $lastStatus = $status
            $changedAt = Get-Date
            continue
        }

        if (-not [string]::IsNullOrWhiteSpace($status) -and
            ((Get-Date) - $changedAt).TotalSeconds -ge $DebounceSeconds) {
            Sync-Repository
            $lastStatus = Get-StatusText
            $changedAt = Get-Date
        }
    }
}
catch {
    try { Write-Log "Fatal error: $($_.Exception.Message)" } catch {}
    exit 1
}
finally {
    try { $mutex.ReleaseMutex() } catch {}
    $mutex.Dispose()
}
