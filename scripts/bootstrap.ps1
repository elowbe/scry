param([string]$DlssDll = '')
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $PSScriptRoot
Set-Location $projectDir
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Scry requires 64-bit Windows 10 or 11.' }
function Run-Checked { param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}
$toolsDir = Join-Path $projectDir '.state\tools'
New-Item -ItemType Directory -Force $toolsDir | Out-Null
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $env:UV_UNMANAGED_INSTALL = $toolsDir
    $installer = Join-Path $toolsDir 'install-uv.ps1'
    Invoke-WebRequest 'https://astral.sh/uv/install.ps1' -OutFile $installer
    & $installer
    if (-not (Test-Path (Join-Path $toolsDir 'uv.exe'))) { throw 'uv installation failed.' }
    $env:PATH = "$toolsDir;$env:PATH"
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    throw 'Install Microsoft App Installer from Microsoft Store (provides winget), then rerun Setup.cmd.'
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Run-Checked 'winget' @('install','--id','Gyan.FFmpeg','--exact','--accept-source-agreements','--accept-package-agreements')
}
# vgamepad needs the signed kernel driver; its installer handles elevation/reboot.
$driver = Get-CimInstance Win32_SystemDriver -Filter "Name='ViGEmBus'"
if (-not $driver) {
    Run-Checked 'winget' @('install','--id','ViGEm.ViGEmBus','--exact','--accept-source-agreements','--accept-package-agreements')
}
$env:PATH = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') + ";$toolsDir"
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw 'FFmpeg installed; reopen Setup.cmd to refresh PATH.' }
Run-Checked 'uv' @('python','install','3.12')
$python = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { Run-Checked 'uv' @('venv','--python','3.12',(Join-Path $projectDir '.venv')) }
Run-Checked 'uv' @('pip','install','--python',$python,'-e',$projectDir)
if (-not $DlssDll) { $DlssDll = Read-Host 'Optional DLSS: path to your nvngx_dlssnr.dll (Enter to skip)' }
$setupArgs = @('-m','gamestream','--config',(Join-Path $projectDir 'config.toml'),'setup')
if ($DlssDll) { $setupArgs += @('--dlss-dll',$DlssDll) }
Run-Checked $python $setupArgs
Run-Checked $python @((Join-Path $PSScriptRoot 'install-desktop.py'))
Write-Host 'Setup complete. Open Scry - Server from the Start menu. Restart if the controller driver requests it.'
