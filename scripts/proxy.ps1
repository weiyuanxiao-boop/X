#!/usr/bin/env pwsh
# LLM Proxy Service Manager
# Usage: proxy.ps1 start|stop|restart|status|logs
#        proxy.ps1 -help

param(
    [Parameter(Position=0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'models', 'set-alias', 'remove-alias', 'install', 'uninstall')]
    [string]$Command = 'status',
    
    [int]$Port = 4936,
    
    [string]$Alias,
    [string]$Target,
    
    [switch]$help
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir
$PidFile = Join-Path $ScriptDir "llm_proxy.pid"
$LogFile = Join-Path $ScriptDir "llm_proxy.log"
$ServiceName = "LLM Proxy Gateway"

function Write-Info {
    param([string]$Message)
    Write-Host "[INFO] $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Error {
    param([string]$Message)
    Write-Host "[ERROR] $Message" -ForegroundColor Red
}

function Get-ProxyProcessId {
    if (Test-Path $PidFile) {
        $storedPid = Get-Content $PidFile
        $process = Get-Process -Id $storedPid -ErrorAction SilentlyContinue
        if ($process) {
            return $storedPid
        } else {
            Remove-Item $PidFile -Force
            return $null
        }
    }
    return $null
}

function Start-Proxy {
    $existingPid = Get-ProxyProcessId
    if ($existingPid) {
        Write-Warn "Proxy is already running (PID: $existingPid)"
        Write-Host "Use '.\proxy.ps1 restart' to restart"
        return $false
    }
    
    Write-Info "Starting $ServiceName on port $Port..."
    
    # Create startup script that redirects both stdout and stderr to log file
    $startupScript = Join-Path $env:TEMP "llm_proxy_$([System.IO.Path]::GetRandomFileName()).ps1"
    @"
`$ErrorActionPreference = 'Continue'
cd '$RootDir'
python -m uvicorn app.main:app --host 0.0.0.0 --port $Port *>> '$LogFile'
"@ | Out-File -FilePath $startupScript -Encoding UTF8
    
    # Start the startup script in background
    $process = Start-Process -FilePath "powershell" `
        -ArgumentList "-ExecutionPolicy Bypass -NoProfile -File `"$startupScript`"" `
        -WindowStyle Hidden `
        -PassThru
    
    # Wait and verify it started
    Start-Sleep -Seconds 3
    $process.Refresh()
    
    if (-not $process.HasExited) {
        # Save the powershell process PID (which runs uvicorn)
        $process.Id | Out-File -FilePath $PidFile -Encoding UTF8
        Write-Info "$ServiceName started (PID: $($process.Id))"
        Write-Host "Logs: $LogFile"
        return $true
    } else {
        Write-Error "Failed to start $ServiceName (exit code: $($process.ExitCode))"
        Write-Error "Check logs: $LogFile"
        if (Test-Path $LogFile) {
            Write-Host "Log content:"
            Get-Content $LogFile
        }
        return $false
    }
}

function Stop-Proxy {
    $procPid = Get-ProxyProcessId
    $stopped = $false
    
    if ($procPid) {
        Write-Info "Stopping $ServiceName (PID: $procPid)..."
        try {
            Stop-Process -Id $procPid -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            $stopped = $true
        } catch {
            Write-Warn "Process $procPid not found"
        }
    }
    
    # Also kill any orphaned uvicorn processes on our port using taskkill
    Write-Info "Checking for processes on port $Port..."
    $netstatOutput = netstat -ano 2>$null | Select-String ":$Port\s.*LISTENING"
    if ($netstatOutput) {
        $parts = $netstatOutput.ToString() -split '\s+'
        $listeningPid = $parts[-1]
        Write-Info "Found process $listeningPid listening on port $Port"
        try {
            taskkill /F /PID $listeningPid 2>$null | Out-Null
            Write-Info "Killed process $listeningPid"
            $stopped = $true
            Start-Sleep -Seconds 2
        } catch {
            Write-Warn "Failed to kill process $listeningPid"
        }
    }
    
    # Clean up PID file
    if (Test-Path $PidFile) {
        Remove-Item $PidFile -Force
    }
    
    if ($stopped) {
        Write-Info "$ServiceName stopped"
    } else {
        Write-Warn "$ServiceName is not running"
    }
    
    return $true
}

function Restart-Proxy {
    Write-Info "Restarting $ServiceName..."
    Stop-Proxy
    Start-Sleep -Seconds 2
    Start-Proxy
}

function Show-Status {
    $procPid = Get-ProxyProcessId
    if ($procPid) {
        $process = Get-Process -Id $procPid
        Write-Host ""
        Write-Host "$ServiceName Status" -ForegroundColor Cyan
        Write-Host "===================" -ForegroundColor Cyan
        Write-Host "  Status:  Running" -ForegroundColor Green
        Write-Host "  PID:     $procPid"
        Write-Host "  Port:    $Port"
        Write-Host "  Memory:  $('{0:N0}' -f $process.WorkingSet) bytes"
        Write-Host "  CPU:     $($process.CPU) s"
        Write-Host ""
        Write-Host "Commands:"
        Write-Host "  .\proxy.ps1 stop     - Stop the proxy"
        Write-Host "  .\proxy.ps1 restart  - Restart the proxy"
        Write-Host "  .\proxy.ps1 logs     - View logs"
        return $true
    } else {
        Write-Host ""
        Write-Host "$ServiceName Status" -ForegroundColor Cyan
        Write-Host "===================" -ForegroundColor Cyan
        Write-Host "  Status:  Stopped" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "Commands:"
        Write-Host "  .\proxy.ps1 start    - Start the proxy"
        return $false
    }
}

function Show-Logs {
    if (Test-Path $LogFile) {
        Write-Host "Showing last 50 lines of $LogFile" -ForegroundColor Cyan
        Get-Content $LogFile -Tail 50
    } else {
        Write-Warn "Log file not found: $LogFile"
    }
}

function Show-Models {
    Write-Host "Fetching model list from http://localhost:$Port/v1/models..." -ForegroundColor Cyan
    
    try {
        $response = Invoke-RestMethod -Uri "http://localhost:$Port/v1/models" -Method Get -TimeoutSec 10
        Write-Host ""
        Write-Host "Available Models:" -ForegroundColor Green
        Write-Host "=================" -ForegroundColor Green
        
        if ($response.models) {
            # Sort: actual models first, then aliases
            $sortedModels = $response.models | Sort-Object -Property @{Expression = { if ($_.is_alias) { 1 } else { 0 } }}, id
            
            foreach ($model in $sortedModels) {
                $modelId = $model.id
                $formats = $model.supported_formats
                $isAlias = $model.is_alias
                $aliasOf = $model.alias_of
                
                # Build format string
                if ($formats -and $formats.Count -gt 0) {
                    # Capitalize first letter of each format (OpenAI, Anthropic)
                    $formattedFormats = $formats | ForEach-Object { 
                        if ($_ -eq "openai") { "OpenAI" }
                        elseif ($_ -eq "anthropic") { "Anthropic" }
                        else { $_.Substring(0, 1).ToUpper() + $_.Substring(1) }
                    }
                    $formatStr = $formattedFormats -join ", "
                } else {
                    $formatStr = ""
                }
                
                # Build alias string
                if ($isAlias -and $aliasOf) {
                    $aliasStr = " [alias of $aliasOf]"
                } else {
                    $aliasStr = ""
                }
                
                # Output with appropriate formatting
                if ($isAlias) {
                    Write-Host "  - $modelId ($formatStr)$aliasStr" -ForegroundColor Yellow
                } else {
                    Write-Host "  - $modelId ($formatStr)$aliasStr"
                }
            }
            Write-Host ""
            Write-Host "Total: $($response.models.Count) model(s)" -ForegroundColor Green
        } else {
            Write-Host "  No models returned"
        }
    } catch {
        Write-Error "Failed to fetch models: $_"
        Write-Host ""
        Write-Host "Make sure the proxy is running:"
        Write-Host "  .\proxy.ps1 start"
    }
}

function Set-Alias {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Alias,
        [Parameter(Mandatory=$true)]
        [string]$Target
    )
    
    Write-Host "Setting alias: $Alias -> $Target" -ForegroundColor Cyan
    
    try {
        $response = Invoke-RestMethod -Uri "http://localhost:$Port/v1/aliases?alias=$Alias&target=$Target" -Method Post -TimeoutSec 10
        Write-Host ""
        Write-Host "$($response.message)" -ForegroundColor Green
        Write-Host "  $($response.alias) -> $($response.target)"
    } catch {
        $errorDetail = $_.ErrorDetails.Message | ConvertFrom-Json
        $errorMsg = $errorDetail.detail
        Write-Error "Failed to set alias: $errorMsg"
        Write-Host ""
        Write-Host "Available models:"
        Write-Host "  Run '.\proxy.ps1 models' to see available models"
    }
}

function Remove-Alias {
    param(
        [Parameter(Mandatory=$true)]
        [string]$Alias
    )
    
    Write-Host "Removing alias: $Alias" -ForegroundColor Cyan
    
    try {
        $response = Invoke-RestMethod -Uri "http://localhost:$Port/v1/aliases/$Alias" -Method Delete -TimeoutSec 10
        Write-Host ""
        Write-Host "$($response.message)" -ForegroundColor Green
        Write-Host "  $($response.alias)"
    } catch {
        $errorDetail = $_.ErrorDetails.Message | ConvertFrom-Json
        $errorMsg = $errorDetail.detail
        Write-Error "Failed to remove alias: $errorMsg"
    }
}

function Install-Service {
    Write-Info "Installing $ServiceName as Windows Service..."
    
    $serviceScript = @"
# Auto-generated service wrapper for $ServiceName
`$ErrorActionPreference = 'Stop'
cd '$RootDir'
python -m uvicorn app.main:app --host 0.0.0.0 --port $Port
"@
    
    $wrapperPath = Join-Path $ScriptDir "llm_proxy_service.ps1"
    $serviceScript | Out-File -FilePath $wrapperPath -Encoding UTF8
    
    Write-Host ""
    Write-Host "Service script created: $wrapperPath"
    Write-Host ""
    Write-Host "To install as Windows Service:"
    Write-Host "  1. Download NSSM from https://nssm.cc/"
    Write-Host "  2. Run: nssm install LLMProxy"
    Write-Host "  3. Set Application: powershell"
    Write-Host "  4. Set Arguments: -ExecutionPolicy Bypass -File `"$wrapperPath`""
    Write-Host "  5. Run: nssm start LLMProxy"
    Write-Host ""
}

function Uninstall-Service {
    Write-Info "Uninstalling $ServiceName..."
    
    $wrapperPath = Join-Path $ScriptDir "llm_proxy_service.ps1"
    if (Test-Path $wrapperPath) {
        Remove-Item $wrapperPath -Force
        Write-Info "Service script removed"
    }
    
    Write-Host ""
    Write-Host "To complete uninstallation:"
    Write-Host "  Run: nssm remove LLMProxy confirm"
}

if ($help) {
    Write-Host @"
$ServiceName - Service Manager

Usage:
  .\proxy.ps1 <command> [options]

Commands:
  start           Start the proxy server
  stop            Stop the proxy server
  restart         Restart the proxy server
  status          Show proxy status
  logs            View last 50 lines of log file
  models          Show available models from API (includes aliases)
  set-alias       Set a model alias (requires -Alias and -Target)
  remove-alias    Remove a model alias (requires -Alias)
  install         Install as Windows Service (requires NSSM)
  uninstall       Remove Windows Service installation

Options:
  -Port <n>       Server port (default: 4936)
  -Alias <name>   Alias name (for set-alias/remove-alias)
  -Target <model> Target model name (for set-alias)
  -help           Show this help

Examples:
  .\proxy.ps1 start                           # Start on default port
  .\proxy.ps1 start -Port 8080                # Start on port 8080
  .\proxy.ps1 stop                            # Stop the server
  .\proxy.ps1 restart                         # Restart the server
  .\proxy.ps1 status                          # Show status
  .\proxy.ps1 logs                            # View logs
  .\proxy.ps1 models                          # Show available models with aliases
  .\proxy.ps1 set-alias -Alias my-ai -Target gpt-4o
  .\proxy.ps1 remove-alias -Alias my-ai
  .\proxy.ps1 install                         # Install as Windows Service

Note:
  - PID file: $PidFile
  - Log file: $LogFile
  - Config file: $RootDir\model_config.yaml
"@
    return
}

switch ($Command) {
    'start' { Start-Proxy }
    'stop' { Stop-Proxy }
    'restart' { Restart-Proxy }
    'status' { Show-Status }
    'logs' { Show-Logs }
    'models' { Show-Models }
    'set-alias' { 
        if (-not $Alias -or -not $Target) {
            Write-Error "Please specify -Alias and -Target parameters"
            Write-Host "Usage: .\proxy.ps1 set-alias -Alias <name> -Target <model>"
        } else {
            Set-Alias -Alias $Alias -Target $Target 
        }
    }
    'remove-alias' { 
        if (-not $Alias) {
            Write-Error "Please specify -Alias parameter"
            Write-Host "Usage: .\proxy.ps1 remove-alias -Alias <name>"
        } else {
            Remove-Alias -Alias $Alias 
        }
    }
    'install' { Install-Service }
    'uninstall' { Uninstall-Service }
}
