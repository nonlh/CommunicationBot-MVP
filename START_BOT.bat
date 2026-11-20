@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "COMMUNICATIONBOT_ROOT=%~dp0"
set "COMMUNICATIONBOT_LAUNCHER=%~f0"
set "COMMUNICATIONBOT_ACTION=start"
if /i "%~1"=="--stop-internal" set "COMMUNICATIONBOT_ACTION=stop"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText($env:COMMUNICATIONBOT_LAUNCHER,[Text.Encoding]::UTF8); $m='# COMMUNICATIONBOT_POWERSHELL'; & ([ScriptBlock]::Create($s.Substring($s.LastIndexOf($m)+$m.Length)))"
set "BOT_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %BOT_EXIT_CODE%

# COMMUNICATIONBOT_POWERSHELL
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$root = (Resolve-Path -LiteralPath $env:COMMUNICATIONBOT_ROOT).Path.TrimEnd('\')
Set-Location -LiteralPath $root
$data = Join-Path $root 'data'
$python = Join-Path $root '.venv\Scripts\python.exe'
$pidFile = Join-Path $data 'bot.pid'
$recordFile = Join-Path $data 'bot-process.json'
$modulePattern = '(?i)(?:^|\s)-m\s+store_core\s+bot(?:\s|$)'
$messages = @{
    'missing_env' = 'Не найден файл .env. Восстановите настройки и нажмите START_BOT.bat снова.'
    'missing_python' = 'Не найдена папка .venv с Python. Восстановите окружение проекта.'
    'invalid_env' = 'Не удалось прочитать настройки .env. Проверьте заполнение файла.'
    'connection' = 'Не удалось подключиться к Telegram. Проверьте интернет и настройки .env.'
    'webhook' = 'У бота включён другой способ подключения к Telegram. Обратитесь к разработчику.'
    'startup' = 'Бот не смог запуститься. Подробности находятся в data\bot.stderr.log.'
}

function Get-ProjectRoots {
    return @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath -ieq $python -and
        $_.CommandLine -match $modulePattern
    })
}

function Get-Identity($entry) {
    $process = Get-Process -Id $entry.ProcessId -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    try {
        $null = $process.Handle
        return [pscustomobject]@{
            pid = $process.Id
            started = $process.StartTime.ToUniversalTime().Ticks.ToString()
            executable = $entry.ExecutablePath
            command = $entry.CommandLine
        }
    } finally {
        $process.Dispose()
    }
}

function Get-VerifiedProcess($identity) {
    $process = Get-Process -Id ([int]$identity.pid) -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    try {
        # Hold a handle and verify creation time, so PID reuse cannot target a stranger.
        $null = $process.Handle
        if ($process.StartTime.ToUniversalTime().Ticks.ToString() -ne $identity.started) {
            $process.Dispose()
            return $null
        }
        $entry = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)"
        if (-not $entry -or $entry.ExecutablePath -ine $identity.executable -or
            $entry.CommandLine -cne $identity.command -or
            $entry.CommandLine -notmatch $modulePattern) {
            $process.Dispose()
            return $null
        }
        return $process
    } catch {
        $process.Dispose()
        throw
    }
}

function Read-Identities {
    if (Test-Path -LiteralPath $recordFile) {
        try {
            $saved = Get-Content -LiteralPath $recordFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($saved.root -ieq $root) { return @($saved.processes) }
        } catch {
            # A damaged/stale record is never authority to stop a process.
        }
    }
    return @()
}

function Find-Identities {
    $identities = @(Read-Identities)
    foreach ($entry in @(Get-ProjectRoots)) {
        $identity = Get-Identity $entry
        if ($identity) { $identities += $identity }
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($entry.ProcessId)")) {
            if ($child.Name -eq 'python.exe' -and $child.CommandLine -match $modulePattern -and
                $child.CreationDate -ge $entry.CreationDate) {
                $identity = Get-Identity $child
                if ($identity) { $identities += $identity }
            }
        }
    }
    # Freshly inspected roots override an older record if Windows reused a PID.
    $byPid = @{}
    foreach ($identity in $identities) { $byPid[[string]$identity.pid] = $identity }
    return @($byPid.Values)
}

function Save-Identities($identities) {
    $payload = @{ root = $root; processes = @($identities) } | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($recordFile, $payload, (New-Object Text.UTF8Encoding($false)))
}

function Clear-Record {
    foreach ($path in @($pidFile, $recordFile)) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
}

function Read-SharedLog($path) {
    # Start-Process holds a writer open: Windows readers must allow write sharing.
    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
    try { return $reader.ReadToEnd() } finally { $reader.Dispose(); $stream.Dispose() }
}

function Stop-Identities($identities) {
    # Only verified Python processes running this bot are stopped, not arbitrary descendants.
    $processes = @()
    try {
        foreach ($identity in @($identities)) {
            $process = Get-VerifiedProcess $identity
            if ($process) { $processes += $process }
        }
        foreach ($process in $processes) {
            if (-not $process.HasExited) {
                try { $process.Kill() } catch { if (-not $process.HasExited) { throw } }
            }
        }
        foreach ($process in $processes) { $null = $process.WaitForExit(5000) }
    } finally {
        foreach ($process in $processes) { $process.Dispose() }
    }
}

function Load-Settings {
    $envPath = Join-Path $root '.env'
    if (-not (Test-Path -LiteralPath $envPath)) { throw 'missing_env' }
    if (-not (Test-Path -LiteralPath $python)) { throw 'missing_python' }
    $allowed = @(
        'TELEGRAM_BOT_TOKEN', 'TELEGRAM_ADMIN_ID', 'CATALOG_PATH', 'SQLITE_PATH',
        'NOTIFICATION_BACKEND', 'STORE_TEST_MODE', 'NOTIFICATION_FAIL_FIRST'
    )
    # Do not inherit old values from an IDE after settings were removed from .env.
    foreach ($key in $allowed) { [Environment]::SetEnvironmentVariable($key, $null, 'Process') }
    $seen = @{}
    foreach ($line in [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)) {
        if (-not $line.Trim() -or $line.TrimStart().StartsWith('#')) { continue }
        if ($line -notmatch '^\s*([A-Z_]+)\s*=(.*)$') { throw 'invalid_env' }
        $key = $matches[1]
        $value = $matches[2].Trim()
        if ($key -notin $allowed -or $seen.ContainsKey($key)) { throw 'invalid_env' }
        $seen[$key] = $true
        if ($value.Length -ge 2 -and (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
    }
    if (-not $env:TELEGRAM_BOT_TOKEN) { throw 'invalid_env' }
}

function Test-Connection {
    $probe = @'
import asyncio
import sys

async def check():
    from aiogram import Bot
    from parser_mvp import load_catalog
    from store_core.config import Settings
    settings = Settings.from_env()
    load_catalog(settings.catalog_path)
    async with Bot(settings.token) as bot:
        await bot.get_me(request_timeout=10)
        webhook = await bot.get_webhook_info(request_timeout=10)
        if webhook.url:
            return 3
    return 0

try:
    sys.exit(asyncio.run(check()))
except Exception:
    sys.exit(2)
'@
    $null = & $python -c $probe 2>$null
    if ($LASTEXITCODE -eq 3) { throw 'webhook' }
    if ($LASTEXITCODE -ne 0) { throw 'connection' }
}

$mutex = $null
$locked = $false
$started = $false
try {
    $digest = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($root.ToLowerInvariant()))).Replace('-', '')
    } finally { $digest.Dispose() }
    $mutex = New-Object Threading.Mutex($false, ('Local\CommunicationBot-' + $hash))
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) {
        Write-Host 'Подождите: другая команда запуска или остановки ещё выполняется.'
        exit 1
    }
    $null = New-Item -ItemType Directory -Path $data -Force
    $identities = @(Find-Identities)
    $running = @()
    foreach ($identity in $identities) {
        $process = Get-VerifiedProcess $identity
        if ($process) { $running += $identity; $process.Dispose() }
    }
    if ($env:COMMUNICATIONBOT_ACTION -eq 'stop') {
        if ($running.Count -eq 0) {
            Clear-Record
            Write-Host 'Бот сейчас не запущен.'
        } else {
            Stop-Identities $running
            Clear-Record
            Write-Host 'Бот остановлен. Другие программы продолжают работать.'
        }
        exit 0
    }
    if ($running.Count -gt 0) {
        Save-Identities $running
        Write-Host 'Бот уже работает. Повторный запуск не нужен.'
        exit 0
    }
    Clear-Record
    Load-Settings
    Write-Host 'Проверяем настройки и соединение с Telegram...'
    Test-Connection
    $botProcess = Start-Process -FilePath $python -ArgumentList @('-m', 'store_core', 'bot') `
        -WorkingDirectory $root -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $data 'bot.stdout.log') `
        -RedirectStandardError (Join-Path $data 'bot.stderr.log')
    $started = $true
    [IO.File]::WriteAllText($pidFile, $botProcess.Id.ToString(), [Text.Encoding]::ASCII)
    for ($step = 0; $step -lt 20; $step++) {
        Start-Sleep -Milliseconds 200
        $botProcess.Refresh()
        if ($botProcess.HasExited) { throw 'startup' }
    }
    $startupLog = Read-SharedLog (Join-Path $data 'bot.stderr.log')
    if (-not $startupLog.Contains('Store bot started') -or
        $startupLog.Contains('Traceback') -or $startupLog.Contains('ERROR')) { throw 'startup' }
    $running = @(Find-Identities)
    if ($running.Count -eq 0) { throw 'startup' }
    Save-Identities $running
    $started = $false
    Write-Host 'Бот успешно запущен! Откройте его в Telegram.'
    Write-Host 'Это окно можно закрыть. Для остановки нажмите STOP_BOT.bat.'
    exit 0
} catch {
    if ($started) {
        try { Stop-Identities @(Find-Identities); Clear-Record } catch { }
    }
    $code = $_.Exception.Message
    if ($messages.ContainsKey($code)) { Write-Host $messages[$code] }
    else {
        Write-Host 'Не удалось выполнить действие. Проверьте доступ к папке проекта и повторите попытку.'
        # Only the error type and source line are recorded, never environment values.
        try {
            [IO.File]::WriteAllText((Join-Path $data 'launcher-error.log'),
                ($_.Exception.GetType().Name + ' line=' + $_.InvocationInfo.ScriptLineNumber),
                [Text.Encoding]::UTF8)
        } catch { }
    }
    exit 1
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
