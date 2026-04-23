# Install and Enable OpenSSH Server on Windows 11
# MUST be run as Administrator.
#
# After running this, connect from Termius with:
#   Host: (your PC's IP — see output below)
#   Port: 22
#   Username: marco
#   Password: (your Windows password)

# Check admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: This script must be run as Administrator." -ForegroundColor Red
    Write-Host "Right-click PowerShell -> Run as Administrator, then re-run." -ForegroundColor Yellow
    exit 1
}

Write-Host "=== SSH Server Setup ===" -ForegroundColor Cyan

# Step 1: Check if OpenSSH Server is installed
$sshCapability = Get-WindowsCapability -Online -Name "OpenSSH.Server*"
if ($sshCapability.State -ne "Installed") {
    Write-Host "[..] Installing OpenSSH Server..." -ForegroundColor Yellow
    Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"
    Write-Host "[OK] OpenSSH Server installed" -ForegroundColor Green
} else {
    Write-Host "[OK] OpenSSH Server already installed" -ForegroundColor Green
}

# Step 2: Start sshd service
Write-Host "[..] Starting SSH server..." -ForegroundColor Yellow
Start-Service sshd -ErrorAction SilentlyContinue

# Step 3: Set to auto-start
Set-Service -Name sshd -StartupType Automatic
Write-Host "[OK] SSH server running and set to auto-start" -ForegroundColor Green

# Step 4: Ensure firewall rule exists
$fwRule = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
if (-not $fwRule) {
    Write-Host "[..] Creating firewall rule..." -ForegroundColor Yellow
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" `
        -DisplayName "OpenSSH Server (sshd)" `
        -Enabled True `
        -Direction Inbound `
        -Protocol TCP `
        -Action Allow `
        -LocalPort 22
    Write-Host "[OK] Firewall rule created" -ForegroundColor Green
} else {
    Write-Host "[OK] Firewall rule already exists" -ForegroundColor Green
}

# Step 5: Verify
$sshService = Get-Service sshd
Write-Host ""
Write-Host "=== SSH Server Ready ===" -ForegroundColor Cyan
Write-Host "Status: $($sshService.Status)" -ForegroundColor White
Write-Host "Startup: $($sshService.StartType)" -ForegroundColor White
Write-Host ""

# Show IP addresses for Termius
Write-Host "Connect from Termius with:" -ForegroundColor Yellow
Write-Host "  Port: 22" -ForegroundColor White
Write-Host "  Username: $env:USERNAME" -ForegroundColor White
Write-Host "  Password: (your Windows password)" -ForegroundColor White
Write-Host ""
Write-Host "  Local IPs:" -ForegroundColor White
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -ne "127.0.0.1" -and $_.PrefixOrigin -ne "WellKnown" } |
    ForEach-Object { Write-Host "    $($_.IPAddress) ($($_.InterfaceAlias))" -ForegroundColor Gray }
