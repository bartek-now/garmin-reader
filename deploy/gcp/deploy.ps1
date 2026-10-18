<#
.SYNOPSIS
Deploys garmin-reader to a Google Cloud e2-micro VM (free tier), or updates it.

.DESCRIPTION
Creates whatever is missing (static IP, firewall rules, VM), then runs
setup-vm.sh on the VM, which builds the code from GitHub and restarts.
Run it again to deploy a newer commit, or after changing who may sign
in. Use a project dedicated to garmin-reader: this removes the default network's
rules that open SSH, RDP and ping to the internet. See
docs/deployment.md, "Google Cloud VM".

Settings not given as parameters come from settings.local.json next to
this script (gitignored, since the repository is public; copy
settings.example.json).

.EXAMPLE
.\deploy\gcp\deploy.ps1
.EXAMPLE
.\deploy\gcp\deploy.ps1 -Ref my-branch
#>
param(
    [string]$Domain,
    # Garmin login emails allowed to sign in.
    [string[]]$AllowedEmails,
    # Branch, tag or commit to deploy; it must be pushed to GitHub.
    [ValidatePattern('^[A-Za-z0-9._/-]+$')]
    [string]$Ref = 'main',
    # Google Cloud project. Required: gcloud's current one may be another account's.
    [string]$Project,
    # The free tier covers an e2-micro in us-east1, us-central1 and us-west1 only.
    [string]$Zone
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0

$SettingsFile = Join-Path $PSScriptRoot 'settings.local.json'
$Settings = if (Test-Path $SettingsFile) { Get-Content $SettingsFile -Raw | ConvertFrom-Json } else { $null }

function Get-Setting($Given, [string]$Name, $Default) {
    if ($Given) { return $Given }
    if ($null -ne $Settings -and $Settings.PSObject.Properties[$Name]) { return $Settings.$Name }
    return $Default
}

$Domain = Get-Setting $Domain 'domain' $null
$AllowedEmails = @(Get-Setting $AllowedEmails 'allowedEmails' @())
$Project = Get-Setting $Project 'project' $null
$Zone = Get-Setting $Zone 'zone' 'us-east1-b'

# Checked here because they're passed unquoted to the VM's shell.
if (-not $Domain -or $Domain -notmatch '^[a-z0-9.-]+$') {
    throw "Set the domain (lowercase hostname) with -Domain or in $SettingsFile"
}
if (-not $Project -or $Project -notmatch '^[a-z][a-z0-9-]+$') {
    throw "Set the Google Cloud project ID with -Project or in $SettingsFile"
}
if ($Zone -notmatch '^[a-z0-9-]+$') { throw "Not a zone: $Zone" }
if ($AllowedEmails.Count -eq 0) {
    throw "Set the Garmin emails allowed to sign in with -AllowedEmails or in $SettingsFile"
}
foreach ($email in $AllowedEmails) {
    if ($email -notmatch '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$') { throw "Not an email: $email" }
}
$Emails = ($AllowedEmails | ForEach-Object { $_.ToLowerInvariant() } | Sort-Object -Unique) -join ','

$Region = $Zone -replace '-[a-z]$', ''
$Vm = 'kcal-mcp'
$Address = 'kcal-mcp-ip'
$Firewall = 'kcal-mcp-web'
$SshFirewall = 'kcal-mcp-ssh-iap'
# Google's Identity-Aware Proxy, which SSH goes through, after checking
# the caller's Google sign-in.
$IapRange = '35.235.240.0/20'
$Tag = 'kcal-mcp'
$ProjectArgs = @('--project', $Project)

function Invoke-GCloud {
    # gcloud reports progress on stderr, so only its exit code means failure.
    $ErrorActionPreference = 'Continue'
    & gcloud --quiet @ProjectArgs @args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed (exit code $LASTEXITCODE)" }
}

function Test-GCloud {
    # Whether the command succeeds, e.g. a `describe` finding its resource.
    $ErrorActionPreference = 'Continue'
    & gcloud --quiet @ProjectArgs @args 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

$SshArgs = @('--zone', $Zone, '--strict-host-key-checking=no', '--tunnel-through-iap')

Write-Host "Project: $Project, zone: $Zone, $Domain, allowed: $Emails"
Invoke-GCloud services enable compute.googleapis.com iap.googleapis.com

if (-not (Test-GCloud compute addresses describe $Address --region $Region)) {
    Invoke-GCloud compute addresses create $Address --region $Region
}
$Ip = Invoke-GCloud compute addresses describe $Address --region $Region '--format=value(address)'

# HTTPS (and HTTP for certificate checks) from anywhere: users sign in
# from any network, and Let's Encrypt's checks come from unpublished
# addresses. Caddy refuses requests for any other hostname.
if (-not (Test-GCloud compute firewall-rules describe $Firewall)) {
    Invoke-GCloud compute firewall-rules create $Firewall --network default `
        --allow 'tcp:80,tcp:443,udp:443' --target-tags $Tag --source-ranges 0.0.0.0/0
}
# SSH only through Identity-Aware Proxy...
if (-not (Test-GCloud compute firewall-rules describe $SshFirewall)) {
    Invoke-GCloud compute firewall-rules create $SshFirewall --network default `
        --allow 'tcp:22' --target-tags $Tag --source-ranges $IapRange
}
# ...so the default network's rules opening SSH, RDP and ping to the
# internet go.
foreach ($rule in 'default-allow-ssh', 'default-allow-rdp', 'default-allow-icmp') {
    if (Test-GCloud compute firewall-rules describe $rule) {
        Write-Host "Removing firewall rule $rule"
        Invoke-GCloud compute firewall-rules delete $rule
    }
}

if (-not (Test-GCloud compute instances describe $Vm --zone $Zone)) {
    # No service account: the VM needs no Google Cloud access.
    Invoke-GCloud compute instances create $Vm --zone $Zone `
        --machine-type e2-micro `
        --image-family debian-12 --image-project debian-cloud `
        --boot-disk-size 30GB --boot-disk-type pd-standard `
        --address $Ip --tags $Tag `
        --no-service-account --no-scopes `
        --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
    Write-Host 'Waiting for the VM to accept SSH...'
    foreach ($attempt in 1..30) {
        if (Test-GCloud compute ssh $Vm @SshArgs --command true) { break }
        Start-Sleep -Seconds 10
    }
}

# Upload setup-vm.sh with Unix line endings, whatever the checkout has.
$Setup = Join-Path ([IO.Path]::GetTempPath()) 'garmin-reader-setup-vm.sh'
$Text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'setup-vm.sh')) -replace "`r`n", "`n"
[IO.File]::WriteAllText($Setup, $Text, (New-Object Text.UTF8Encoding $false))
try {
    Invoke-GCloud compute scp @SshArgs $Setup "${Vm}:/tmp/garmin-reader-setup-vm.sh"
} finally {
    Remove-Item $Setup -ErrorAction SilentlyContinue
}
# The arguments were validated above, so they need no quoting on the VM.
# SSH through IAP occasionally drops at the end; setup-vm.sh is safe to
# run again, so retry once.
$SetupCommand = "bash /tmp/garmin-reader-setup-vm.sh $Domain $Emails $Ref"
try {
    Invoke-GCloud compute ssh $Vm @SshArgs --command $SetupCommand
} catch {
    Write-Host "Setup failed ($($_.Exception.Message)); trying once more."
    Invoke-GCloud compute ssh $Vm @SshArgs --command $SetupCommand
}

Write-Host @"

Deployed. The VM's address is $Ip.
- DNS: $Domain needs an A record pointing at $Ip. Caddy keeps retrying
  the certificate until it does.
- Then sign in to Garmin at https://$Domain/login and add the connector
  with https://$Domain/mcp.
"@
