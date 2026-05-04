#!/usr/bin/env pwsh
# PowerShell Alias Installer
# Usage: install.ps1 [script1.ps1] [script2.ps1] ...
#        install.ps1 -all           # Install all .ps1 scripts in current directory
#        install.ps1 -uninstall     # Remove all installed aliases
#        install.ps1 -help

param(
    [Parameter(Position=0)]
    [string[]]$Script,
    
    [switch]$all,
    [switch]$uninstall,
    [switch]$help
)

$ErrorActionPreference = 'Stop'

if ($help) {
    Write-Host @"
PowerShell Alias Installer

Usage:
  install.ps1 [script1.ps1] [script2.ps1] ...  # Install specific scripts
  install.ps1 -all                              # Install all .ps1 scripts
  install.ps1 -uninstall                        # Remove all installed aliases
  install.ps1 -help                             # Show this help

Examples:
  .\install.ps1                          # Install all scripts in current directory
  .\install.ps1 head.ps1 tail.ps1        # Install specific scripts
  .\install.ps1 -all                     # Install all .ps1 files
  .\install.ps1 -uninstall               # Remove all aliases

Note: Aliases are installed to your PowerShell profile:
      $($PROFILE.CurrentUserCurrentHost)
"@
    return
}

$profilePath = $PROFILE.CurrentUserCurrentHost
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptDirFull = (Get-Item $scriptDir).FullName

# Unique marker for this directory
$aliasMarker = "# Aliases managed by install.ps1 [$scriptDirFull]"

# Ensure profile exists
if (-not (Test-Path $profilePath)) {
    New-Item -ItemType File -Path $profilePath -Force | Out-Null
    Write-Host "Created profile: $profilePath"
}

# Determine which scripts to install
if ($uninstall) {
    # Remove aliases managed by THIS directory's installer
    $content = Get-Content $profilePath -Raw -ErrorAction SilentlyContinue

    if ($content.Contains("$aliasMarker")) {
        # Find and remove the managed section for this directory only
        $lines = Get-Content $profilePath
        $newLines = @()
        $inManagedSection = $false
        $skipNextEmptyLine = $false

        foreach ($line in $lines) {
            if ($line.Contains("$aliasMarker")) 
            {
                $inManagedSection = $true
                $skipNextEmptyLine = $true
                continue  # Skip the start marker
            }
            if ($inManagedSection -and $line -match '# End of managed aliases') {
                $inManagedSection = $false
                continue  # Skip the end marker
            }
            # Skip empty lines right after managed section
            if ($skipNextEmptyLine -and [string]::IsNullOrWhiteSpace($line)) {
                $skipNextEmptyLine = $false
                continue
            }
            if (-not $inManagedSection) {
                $newLines += $line
            }
        }

        # Remove trailing empty lines
        while ($newLines.Count -gt 0 -and [string]::IsNullOrWhiteSpace($newLines[-1])) {
            $newLines = $newLines[0..($newLines.Count - 2)]
        }

        $newLines | Set-Content $profilePath -Encoding UTF8
        Write-Host "Removed all managed aliases from profile."
        Write-Host "Please restart PowerShell or run '. `$PROFILE' to apply changes."

        # Also remove current session aliases (both lowercase and capitalized)
        foreach ($scriptName in (Get-ChildItem $scriptDir -Filter *.ps1 | Where-Object { $_.Name -ne 'install.ps1' })) {
            $baseName = [System.IO.Path]::GetFileNameWithoutExtension($scriptName.Name)
            $lowerAlias = $baseName.ToLower()
            $capitalAlias = $baseName.Substring(0, 1).ToUpper() + $baseName.Substring(1).ToLower()
            Remove-Item "Alias:$lowerAlias" -Force -ErrorAction SilentlyContinue
            Remove-Item "Alias:$capitalAlias" -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "No managed aliases found in profile."
    }
    return
}

# Build list of scripts to install
if ($Script) {
    $scriptsToInstall = $Script
} elseif ($all) {
    $scriptsToInstall = Get-ChildItem $scriptDir -Filter *.ps1 | 
        Where-Object { $_.Name -ne 'install.ps1' } | 
        Select-Object -ExpandProperty Name
} else {
    # Default: install all scripts in current directory
    $scriptsToInstall = Get-ChildItem $scriptDir -Filter *.ps1 | 
        Where-Object { $_.Name -ne 'install.ps1' } | 
        Select-Object -ExpandProperty Name
}

if (-not $scriptsToInstall) {
    Write-Host "No scripts found to install."
    return
}

# Check if profile ends with empty line
$existingContent = Get-Content $profilePath -Raw -ErrorAction SilentlyContinue
$needsLeadingNewline = $true
if ($existingContent) {
    $trimmedContent = $existingContent.TrimEnd()
    if ($trimmedContent.Length -gt 0) {
        # Profile has content, check if it ends with newline
        $lastChar = $trimmedContent.Substring($trimmedContent.Length - 1, 1)
        $needsLeadingNewline = $lastChar -notmatch "[`r`n]"
    }
}

# Build alias content with unique marker
if ($needsLeadingNewline) {
    $aliasContent = @"

$aliasMarker
# Installed on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
"@
} else {
    $aliasContent = @"
$aliasMarker
# Installed on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
"@
}

foreach ($scriptName in $scriptsToInstall) {
    $baseName = [System.IO.Path]::GetFileNameWithoutExtension($scriptName)
    $scriptPath = Join-Path $scriptDir $scriptName

    if (Test-Path $scriptPath) {
        # Install lowercase alias
        $lowerAlias = $baseName.ToLower()
        $aliasContent += "`nSet-Alias -Name $lowerAlias -Value '$scriptPath' -Force"
        Write-Host "  $lowerAlias -> $scriptName"

        # Install capitalized alias (first letter uppercase)
        $capitalAlias = $baseName.Substring(0, 1).ToUpper() + $baseName.Substring(1).ToLower()
        $aliasContent += "`nSet-Alias -Name $capitalAlias -Value '$scriptPath' -Force"
        Write-Host "  $capitalAlias -> $scriptName"
    } else {
        Write-Warning "Script not found: $scriptPath"
    }
}

$aliasContent += "`n# End of managed aliases`n"

# Check if aliases from THIS directory already exist
$existingContent = Get-Content $profilePath -Raw -ErrorAction SilentlyContinue

if ($existingContent.Contains("$aliasMarker"))
 {
    Write-Host "Aliases already installed. Updating..."
    # Remove only this directory's managed section
    $lines = Get-Content $profilePath
    $newLines = @()
    $inManagedSection = $false
    $skipNextEmptyLine = $false

    foreach ($line in $lines) {
        if ($line.Contains("$aliasMarker")) {
            $inManagedSection = $true
            $skipNextEmptyLine = $true
            continue  # Skip the start marker
        }
        if ($inManagedSection -and $line -match '# End of managed aliases') {
            $inManagedSection = $false
            continue  # Skip the end marker
        }
        # Skip empty lines right after managed section
        if ($skipNextEmptyLine -and [string]::IsNullOrWhiteSpace($line)) {
            $skipNextEmptyLine = $false
            continue
        }
        if (-not $inManagedSection) {
            $newLines += $line
        }
    }

    # Remove trailing empty lines
    while ($newLines.Count -gt 0 -and [string]::IsNullOrWhiteSpace($newLines[-1])) {
        $newLines = $newLines[0..($newLines.Count - 2)]
    }

    $newLines | Set-Content $profilePath -Encoding UTF8
}

# Add new aliases
Add-Content -Path $profilePath -Value $aliasContent -Encoding UTF8

Write-Host ""
Write-Host "Aliases installed successfully!"
Write-Host "Profile: $profilePath"
Write-Host ""
Write-Host "To use the aliases, either:"
Write-Host "  1. Restart PowerShell"
Write-Host "  2. Or run: . `$PROFILE"
Write-Host ""
Write-Host "To uninstall, run: .\install.ps1 -uninstall"
