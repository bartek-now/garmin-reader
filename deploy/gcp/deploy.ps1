<#
.SYNOPSIS
Deploys kcal to a Google Cloud e2-micro VM (free tier), or updates it.

.DESCRIPTION
Creates whatever is missing (static IP, firewall rule, VM), then runs
setup-vm.sh on the VM, which builds the code from GitHub and restarts.
Run it again to deploy a newer commit. See docs/deployment.md,
"Google Cloud VM".

.EXAMPLE
.\deploy\gcp\deploy.ps1 -Domain garminmcp.example.com -GarminOwner 1234567
#>
param(
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9.-]+$')]
    [string]$Domain,
    # Garmin profile ID allowed to sign in; `kcal whoami` prints it.
    [Parameter(Mandatory)][ValidatePattern('^[0-9]+$')]
    [string]$GarminOwner,
    # Branch, tag or commit to deploy; it must be pushed to GitHub.
    [ValidatePattern('^[A-Za-z0-9._/-]+$')]
    [string]$Ref = 'master',
    # Google Cloud project; gcloud's current one if not given.
    [string]$Project,
    # The free tier covers an e2-micro in us-east1, us-central1 and us-west1 only.
    [string]$Zone = 'us-east1-b'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0

$Region = $Zone -replace '-[a-z]$', ''
$Vm = 'kcal-mcp'
$Address = 'kcal-mcp-ip'
$Firewall = 'kcal-mcp-web'
$Tag = 'kcal-mcp'
$ProjectArgs = if ($Project) { @('--project', $Project) } else { @() }

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

$SshArgs = @('--zone', $Zone, '--strict-host-key-checking=no')

$CurrentProject = if ($Project) { $Project } else { (& gcloud config get-value project 2>$null) }
Write-Host "Project: $CurrentProject, zone: $Zone"
Invoke-GCloud services enable compute.googleapis.com

if (-not (Test-GCloud compute addresses describe $Address --region $Region)) {
    Invoke-GCloud compute addresses create $Address --region $Region
}
$Ip = Invoke-GCloud compute addresses describe $Address --region $Region '--format=value(address)'

# HTTPS (and HTTP for certificate checks) to the VM only. SSH comes from
# the default network's own rule.
if (-not (Test-GCloud compute firewall-rules describe $Firewall)) {
    Invoke-GCloud compute firewall-rules create $Firewall --network default `
        --allow 'tcp:80,tcp:443,udp:443' --target-tags $Tag --source-ranges 0.0.0.0/0
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
$Setup = Join-Path ([IO.Path]::GetTempPath()) 'kcal-setup-vm.sh'
$Text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'setup-vm.sh')) -replace "`r`n", "`n"
[IO.File]::WriteAllText($Setup, $Text, (New-Object Text.UTF8Encoding $false))
try {
    Invoke-GCloud compute scp @SshArgs $Setup "${Vm}:/tmp/kcal-setup-vm.sh"
} finally {
    Remove-Item $Setup -ErrorAction SilentlyContinue
}
# The arguments were validated above, so they need no quoting on the VM.
Invoke-GCloud compute ssh $Vm @SshArgs --command "bash /tmp/kcal-setup-vm.sh $Domain $GarminOwner $Ref"

Write-Host @"

Deployed. The VM's address is $Ip.
- DNS: $Domain needs an A record pointing at $Ip. Caddy keeps retrying
  the certificate until it does.
- Then sign in to Garmin at https://$Domain/login and add the connector
  with https://$Domain/mcp.
"@
