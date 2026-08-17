param(
    [int]$TargetDiskNumber = -1,
    [switch]$ValidateOnly,
    [switch]$PostImageOnly,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:StageRoot = $PSScriptRoot
$script:WriterLog = Join-Path $script:StageRoot $(
    if ($PostImageOnly) { 'pi2-postimage-console.log' } else { 'pi2-writer-console.log' }
)
$script:TranscriptStarted = $false
$script:ResolvedDiskNumber = $null

function Assert-Administrator {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script from an Administrator PowerShell window.'
    }
}

function Get-VerifiedTargetDisk {
    param([switch]$AllowMissingDriveLetter)

    if ($null -eq $script:ResolvedDiskNumber) {
        if ($TargetDiskNumber -ge 0) {
            $script:ResolvedDiskNumber = $TargetDiskNumber
        }
        else {
            $candidates = @(Get-Disk -ErrorAction Stop | Where-Object {
                $_.BusType -in @('USB', 'SD') -and
                -not $_.IsBoot -and -not $_.IsSystem -and
                ($_.Size / 1GB) -ge 7.2 -and ($_.Size / 1GB) -le 64
            })
            if ($candidates.Count -ne 1) {
                $summary = if ($candidates.Count) {
                    ($candidates | ForEach-Object {
                        "Disk $($_.Number): $($_.FriendlyName), $([math]::Round($_.Size / 1GB, 2)) GB, serial $($_.SerialNumber)"
                    }) -join "`n"
                }
                else { 'none' }
                throw "Exactly one 8-64 GB USB/SD target is required. Candidates:`n$summary`nUse -TargetDiskNumber only after checking the target."
            }
            $script:ResolvedDiskNumber = [int]$candidates[0].Number
        }
    }

    $disk = Get-Disk -Number $script:ResolvedDiskNumber -ErrorAction Stop
    if ($disk.BusType -notin @('USB', 'SD')) {
        throw "Safety stop: disk $($disk.Number) bus type is '$($disk.BusType)'."
    }
    if ($disk.IsBoot -or $disk.IsSystem) {
        throw "Safety stop: disk $($disk.Number) is marked as a boot or system disk."
    }
    if ($disk.IsOffline -or $disk.IsReadOnly) {
        throw "Safety stop: disk $($disk.Number) is offline or read-only."
    }
    $sizeGB = $disk.Size / 1GB
    if ($sizeGB -lt 7.2 -or $sizeGB -gt 64) {
        throw "Safety stop: disk $($disk.Number) size is $([math]::Round($sizeGB, 2)) GB; expected 8-64 GB."
    }
    return $disk
}

function Get-BootPartition {
    if (-not $PostImageOnly) {
        Update-HostStorageCache
    }
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $candidate = Get-Partition -DiskNumber $script:ResolvedDiskNumber -ErrorAction Stop |
            Where-Object { $_.PartitionNumber -eq 1 -and $_.Size -gt 100MB -and $_.Size -lt 1GB } |
            Sort-Object Size |
            Select-Object -First 1
        if ($candidate) {
            return $candidate
        }
        Start-Sleep -Seconds 1
        if (-not $PostImageOnly) {
            Update-HostStorageCache -ErrorAction SilentlyContinue
        }
    }
    throw "The Raspberry Pi FAT boot partition did not appear on disk $script:ResolvedDiskNumber."
}

function Get-StableBootVolume {
    param([Parameter(Mandatory)][string]$DriveLetter)

    $lastError = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $volume = Get-Volume -DriveLetter $DriveLetter -ErrorAction Stop
            if ($volume.FileSystem) {
                return $volume
            }
        }
        catch {
            $lastError = $_
        }
        Start-Sleep -Seconds 1
        if (-not $PostImageOnly) {
            Update-HostStorageCache -ErrorAction SilentlyContinue
        }
    }
    if ($lastError) {
        throw "Boot volume metadata did not stabilize for ${DriveLetter}: $($lastError.Exception.Message)"
    }
    throw "Boot volume metadata did not stabilize for ${DriveLetter}:"
}

function Assert-LfNoBom {
    param([Parameter(Mandatory)][string]$Path)

    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 2) {
        throw "File is unexpectedly short: $Path"
    }
    if ($bytes.Length -ge 3 -and
        $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "UTF-8 BOM is not allowed: $Path"
    }
    if ($bytes -contains 13) {
        throw "CRLF is not allowed for a Linux file: $Path"
    }
}

function Assert-BashSyntax {
    param([Parameter(Mandatory)][string]$Path)

    $gitBash = 'C:\Program Files\Git\bin\bash.exe'
    if (-not (Test-Path -LiteralPath $gitBash -PathType Leaf)) {
        throw 'Git Bash is required for pre-write Bash syntax validation.'
    }
    $bashPath = $Path.Replace('\', '/')
    & $gitBash -n $bashPath
    if ($LASTEXITCODE -ne 0) {
        throw "Bash syntax validation failed: $Path"
    }
}

function Assert-FinalBundle {
    $image = Join-Path $script:StageRoot '2026-06-18-raspios-trixie-armhf-lite.img.xz'
    $firstRun = Join-Path $script:StageRoot 'firstrun.sh'
    $payload = Join-Path $script:StageRoot 'payload'
    $starter = Join-Path $payload 'audiodsp-camilladsp-start'
    $config = Join-Path $payload 'camilladsp.yml'
    $camilla = Join-Path $payload 'camilladsp'
    $fir = Join-Path $payload 'Harman_StrongBassControl_Stereo_48k_NoPreamp.wav'
    $service = Join-Path $payload 'camilladsp.service'
    $asound = Join-Path $payload 'asound-audiodsp.conf'
    $outputProfile = Join-Path $payload 'audiodsp-output-profile'
    $profileManager = Join-Path $payload 'audiodsp-profile-manager.py'
    $profileWeb = Join-Path $payload 'audiodsp-profile-web.py'
    $measurement = Join-Path $payload 'audiodsp-measurement.py'
    $mimo = Join-Path $payload 'audiodsp-mimo.py'
    $cal0 = Join-Path $payload '7200660.txt'
    $cal90 = Join-Path $payload '7200660_90deg.txt'
    $targetHarman = Join-Path $payload 'target_Harman_Kardon.txt'
    $profileWebService = Join-Path $payload 'audiodsp-web.service'
    $u7Monitor = Join-Path $payload 'audiodsp-profile-monitor.py'
    $u7Service = Join-Path $payload 'audiodsp-profile-monitor.service'
    $dspReady = Join-Path $payload 'audiodsp-dsp-ready'
    $dspReadyService = Join-Path $payload 'audiodsp-ready.service'
    $dspReadyWave = Join-Path $payload 'announce_dsp_ready_48k_front_lr.wav'
    $ethernetApply = Join-Path $payload 'audiodsp-pi2-ethernet-apply'
    $ethernetService = Join-Path $payload 'audiodsp-pi2-ethernet-apply.service'
    $privateKey = Join-Path $script:StageRoot 'audiodsp_pi_ed25519'
    $publicKey = Join-Path $script:StageRoot 'audiodsp_pi_ed25519.pub'
    $matrixTest = Join-Path $script:StageRoot 'test_profile_matrix.py'
    $measurementTest = Join-Path $script:StageRoot 'test_measurement_engine.py'
    $mimoTest = Join-Path $script:StageRoot 'test_mimo_runtime.py'
    $imager = 'C:\Program Files\Raspberry Pi Ltd\Imager\rpi-imager.exe'

    foreach ($requiredFile in @(
        $image, $firstRun, $starter, $config, $camilla,
        $fir, $service, $asound, $outputProfile, $profileManager, $profileWeb, $measurement, $mimo,
        $cal0, $cal90, $targetHarman,
        $profileWebService, $u7Monitor, $u7Service, $dspReady, $dspReadyService,
        $dspReadyWave, $ethernetApply, $ethernetService, $matrixTest, $measurementTest, $mimoTest,
        $privateKey, $publicKey, $imager
    )) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Required file is missing: $requiredFile"
        }
    }

    $expectedHashes = @{
        $image   = 'EA4E84C501D6DD4F4B1D04EB84DF133A03F90A05EE2E8AB849185C17C2B0707B'
        $camilla = 'DD47CA27285661AAC2C51E4023E885C8F14A98455B58B36F2E11C9D44254582B'
        $fir     = '8A8A3B2FC31A080A6BC40205F29EA6471DF95ADF357618B2025BDD193EF45C99'
    }
    foreach ($entry in $expectedHashes.GetEnumerator()) {
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $entry.Key).Hash
        if ($actual -ne $entry.Value) {
            throw "SHA256 mismatch: $($entry.Key)`nExpected: $($entry.Value)`nActual:   $actual"
        }
    }

    foreach ($linuxTextFile in @(
        $firstRun, $starter, $config, $service, $asound, $outputProfile,
        $profileManager, $profileWeb, $measurement, $mimo, $profileWebService, $u7Monitor, $u7Service,
        $dspReady, $dspReadyService, $ethernetApply, $ethernetService, $matrixTest, $measurementTest, $mimoTest
    )) {
        Assert-LfNoBom -Path $linuxTextFile
    }
    foreach ($bashFile in @($firstRun, $starter, $outputProfile, $dspReady, $ethernetApply)) {
        Assert-BashSyntax -Path $bashFile
    }

    $starterText = Get-Content -LiteralPath $starter -Raw
    if ($starterText -notmatch 'capture_device="plughw:CARD=\$\{card_id\},DEV=0"' -or
        $starterText -notmatch 'playback_device="audiodsp_dsp"' -or
        $starterText -notmatch 'output_volume_db' -or
        $starterText -notmatch '127 \+ volume_db') {
        throw 'The U7 starter is missing capture DEV=0, dmix playback, or saved output-volume restoration.'
    }

    $asoundText = Get-Content -LiteralPath $asound -Raw
    if ($asoundText -notmatch 'pcm\.audiodsp_dmix' -or
        $asoundText -notmatch 'pcm\.audiodsp_announce' -or
        $asoundText -notmatch '(?m)^\s*format S24_3LE\s*$' -or
        $asoundText -notmatch '(?m)^\s*channels 4\s*$') {
        throw 'The shared Xonar U7 dmix/announcement ALSA configuration is invalid.'
    }

    $configText = Get-Content -LiteralPath $config -Raw
    if ($configText -notmatch '__CAPTURE_DEVICE__' -or
        $configText -notmatch '__PLAYBACK_DEVICE__' -or
        $configText -notmatch '(?m)^\s*chunksize:\s*2048\s*$' -or
        $configText -notmatch '(?ms)^\s{2}playback:\s*$.*?^\s{4}channels:\s*4\s*$' -or
        $configText -notmatch '(?m)^\s{2}stereo_to_front_and_rear:\s*$' -or
        $configText -notmatch '(?m)^\s{4}name:\s*"stereo_to_front_and_rear"\s*$' -or
        ([regex]::Matches($configText, '(?m)^\s{6}- dest:\s*[0-3]\s*$').Count -ne 4) -or
        $configText -match '(?mi)^\s*type:\s*Gain\s*$') {
        throw 'Pi 2 four-channel CamillaDSP mapping, chunksize, or NoPreamp validation failed.'
    }

    $managerText = Get-Content -LiteralPath $profileManager -Raw
    if ($managerText -notmatch '"bypass"' -or
        $managerText -notmatch 'set-bypass' -or
        $managerText -notmatch 'set-chunksize' -or
        $managerText -notmatch 'set-output-volume' -or
        $managerText -notmatch 'output_volume_db' -or
        $managerText -notmatch 'set-woofer-trim' -or
        $managerText -notmatch 'install-pair' -or
        $managerText -notmatch 'set-mimo-enabled' -or
        $managerText -notmatch 'ALLOWED_CHUNKSIZES' -or
        $managerText -notmatch 'filters: \{\{\}\}' -or
        $managerText -notmatch 'convolution_channels' -or
        $managerText -notmatch 'MAX_FIR_FRAMES = 262144' -or
        $managerText -notmatch 'selector_status' -or
        $managerText -notmatch 'remain active for one second') {
        throw 'The FIR profile manager is missing fallback, bypass, or FIR validation support.'
    }

    $webText = Get-Content -LiteralPath $profileWeb -Raw
    if ($webText -notmatch 'client_svg_graph' -or
        $webText -notmatch '/bypass' -or
        $webText -notmatch '/chunksize' -or
        $webText -notmatch 'name="chunksize"' -or
        $webText -notmatch '/api/measurement/download/front' -or
        $webText -notmatch 'mimo_one_sub' -or
        $webText -notmatch 'measurement-result-graph' -or
        $webText -notmatch 'def backup_archive' -or
        $webText -notmatch '/api/targets' -or
        $webText -notmatch 'live_u7_status_poll' -or
        $webText -notmatch "fetch\('/api/status'" -or
        $webText -notmatch 'prefers-color-scheme' -or
        $webText -notmatch 'audiodsp-theme' -or
        $webText -notmatch '/api/backup/download' -or
        $webText -notmatch '/api/backup/latest' -or
        $webText -notmatch '/api/volume' -or
        $webText -notmatch 'output_volume_control' -or
        $webText -notmatch 'output-volume-control' -or
        $webText -notmatch 'non_destructive_step_navigation' -or
        $webText -notmatch 'room_tuning_audit' -or
        $webText -notmatch 'ThreadingHTTPServer\(\(WEB_HOST, WEB_PORT\)' -or
        $webText -notmatch 'active-profile' -or
        $webText -notmatch 'id="u7-physical"' -or
        $webText -match 'action="/switch"') {
        throw 'The profile Web UI is missing display-only U7 state, client SVG, theme, bypass, or port support.'
    }

    $measurementText = Get-Content -LiteralPath $measurement -Raw
    foreach ($requiredText in @(
        'TAPS = 32_768',
        'run_direct_capture',
        'hw:CARD=UMIK1,DEV=0',
        '"Line", "nocap"',
        'spatial_std_db',
        'Generated_Front_LR_32768.wav',
        'maximum_transfer_db',
        'WOOFER_MEASUREMENT_ATTENUATION_DB',
        'room_decay_metrics',
        'finalize_graph_with_fir',
        'audiodsp_announce',
        'install-pair',
        'MIMO_MODES'
    )) {
        if ($measurementText -notmatch [regex]::Escape($requiredText)) {
            throw "Measurement engine validation is missing: $requiredText"
        }
    }

    $mimoText = Get-Content -LiteralPath $mimo -Raw
    foreach ($requiredText in @('weighted pressure matching', 'MIMO_manifest.json', 'correlated_input_headroom', 'mimo_one_sub')) {
        if ($mimoText -notmatch [regex]::Escape($requiredText)) {
            throw "MIMO engine validation is missing: $requiredText"
        }
    }

    $monitorText = Get-Content -LiteralPath $u7Monitor -Raw
    if ($monitorText -notmatch 'HIDIOCGINPUT' -or
        $monitorText -notmatch 'hidio_get_input' -or
        $monitorText -notmatch 'save_selector_state') {
        throw 'The U7 monitor is missing initial hardware-state query or selector persistence.'
    }

    $firstRunText = Get-Content -LiteralPath $firstRun -Raw
    foreach ($requiredText in @(
        'usermod -s /bin/bash audiodsp',
        '/usr/bin/cancel-rename audiodsp',
        'hostname=audiodsp-pi2',
        'audiodsp-pi2-ethernet-apply.service',
        'audiodsp-profile-manager.py activate speaker --no-restart',
        'audiodsp-profile-manager.py set-chunksize 2048 --no-restart',
        'audiodsp-web.service',
        'audiodsp-measurement.py self-test',
        'audiodsp-mimo.py',
        '7200660_90deg.txt',
        'audiodsp-ready.service',
        'test ! -e /etc/ssh/sshd_config.d/rename_user.conf'
    )) {
        if ($firstRunText -notmatch [regex]::Escape($requiredText)) {
            throw "First-run validation is missing: $requiredText"
        }
    }

    [pscustomobject]@{
        Image = $image
        FirstRun = $firstRun
        Payload = $payload
        Imager = $imager
        ImageUncompressedSha = '235aae6e32f40eb294b6485f99232d9ea5b6ee0251c8dc40e370177fac4754c2'
        FirSha = $expectedHashes[$fir]
        CamillaSha = $expectedHashes[$camilla]
    }
}

try {
    try {
        Start-Transcript -LiteralPath $script:WriterLog -Force | Out-Null
        $script:TranscriptStarted = $true
    }
    catch {
        Write-Warning "Transcript is unavailable; continuing with console output: $($_.Exception.Message)"
    }

    Write-Host 'Validating the complete AudioDSP SD bundle...' -ForegroundColor Cyan
    $bundle = Assert-FinalBundle
    Write-Host 'Bundle validation: PASS' -ForegroundColor Green

    if ($ValidateOnly) {
        Write-Host ''
        Write-Host 'PI 2 BUNDLE VALIDATION COMPLETE' -ForegroundColor Green
        Write-Host '  Raspberry Pi OS Lite 32-bit armhf image: verified'
        Write-Host '  CamillaDSP 4.1.3 ARMv7 NEON: verified'
        Write-Host '  FIR NoPreamp SHA256: verified'
        Write-Host '  Account/Ethernet/U7 scripts: syntax and policy verified'
        return
    }

    if (-not $PostImageOnly) {
        Assert-Administrator
    }
    $targetDisk = Get-VerifiedTargetDisk

    Write-Host ''
    Write-Host 'TARGET VERIFIED:' -ForegroundColor Yellow
    Write-Host "  Disk $($targetDisk.Number) / $($targetDisk.FriendlyName) / $([math]::Round($targetDisk.Size / 1GB, 2)) GB"
    Write-Host "  Serial: $($targetDisk.SerialNumber)"
    if (-not $PostImageOnly) {
        Write-Host '  ALL EXISTING CONTENTS ON THIS SD CARD WILL BE OVERWRITTEN.' -ForegroundColor Yellow
        $requiredConfirmation = "WRITE DISK $($targetDisk.Number)"
        $confirmation = Read-Host "Type $requiredConfirmation to continue"
        if ($confirmation -cne $requiredConfirmation) {
            throw 'Confirmation did not match; no write was started.'
        }

        $imagerLog = Join-Path $script:StageRoot 'pi2-imager-write.log'
        $target = "\\.\PhysicalDrive$($targetDisk.Number)"
        $arguments = @(
            '--cli'
            '--debug'
            '--disable-eject'
            '--enable-writing-system-drives'
            "--sha256=$($bundle.ImageUncompressedSha)"
            "--first-run-script=$($bundle.FirstRun)"
            "--log-file=$imagerLog"
            $bundle.Image
            $target
        )

        Write-Host ''
        Write-Host 'Writing and verifying Raspberry Pi OS image...' -ForegroundColor Cyan
        $process = Start-Process -FilePath $bundle.Imager -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            if (Test-Path -LiteralPath $imagerLog) {
                Get-Content -LiteralPath $imagerLog -Tail 120
            }
            throw "Raspberry Pi Imager failed with exit code $($process.ExitCode)."
        }
    }
    else {
        if ($targetDisk.PartitionStyle -ne 'MBR') {
            throw "PostImageOnly safety stop: expected MBR image, got $($targetDisk.PartitionStyle)."
        }
        Write-Host '  PostImageOnly: preserving the verified OS image and resuming boot customization.' -ForegroundColor Cyan
    }

    $null = Get-VerifiedTargetDisk -AllowMissingDriveLetter
    $bootPartition = Get-BootPartition
    if ([string]::IsNullOrWhiteSpace([string]$bootPartition.DriveLetter)) {
        $newLetter = $null
        foreach ($candidateLetter in @('E', 'R', 'S', 'T')) {
            if (-not (Get-Volume -DriveLetter $candidateLetter -ErrorAction SilentlyContinue)) {
                $newLetter = $candidateLetter
                break
            }
        }
        if (-not $newLetter) {
            throw 'No safe drive letter is available for the boot partition.'
        }
        Set-Partition -DiskNumber $script:ResolvedDiskNumber -PartitionNumber $bootPartition.PartitionNumber -NewDriveLetter $newLetter
        $bootPartition = Get-Partition -DiskNumber $script:ResolvedDiskNumber -PartitionNumber $bootPartition.PartitionNumber
    }

    $bootLetter = [string]$bootPartition.DriveLetter
    $bootVolume = Get-StableBootVolume -DriveLetter $bootLetter
    if ($bootVolume.FileSystem -ne 'FAT32' -or $bootVolume.FileSystemLabel -ne 'bootfs') {
        throw "Safety stop: expected FAT32 bootfs, got '$($bootVolume.FileSystem)' '$($bootVolume.FileSystemLabel)'."
    }
    $bootRoot = "${bootLetter}:\"
    $destination = Join-Path $bootRoot 'audiodsp'
    if (Test-Path -LiteralPath $destination) {
        if (-not $PostImageOnly) {
            throw "Safety stop: payload destination already exists after a fresh image: $destination"
        }
    }
    else {
        New-Item -ItemType Directory -Path $destination -ErrorAction Stop | Out-Null
    }

    Get-ChildItem -LiteralPath $bundle.Payload -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destination $_.Name) -Force -ErrorAction Stop
    }

    $utf8NoBom = [Text.UTF8Encoding]::new($false)

    # Explicitly install the known-good first-run file and command line. This
    # avoids any /boot versus /boot/firmware ambiguity in Imager versions.
    $cardFirstRun = Join-Path $bootRoot 'firstrun.sh'
    Copy-Item -LiteralPath $bundle.FirstRun -Destination $cardFirstRun -Force -ErrorAction Stop
    $cmdline = Join-Path $bootRoot 'cmdline.txt'
    if (-not (Test-Path -LiteralPath $cmdline -PathType Leaf)) {
        throw 'cmdline.txt is missing from the boot partition.'
    }
    $cmdlineBackup = Join-Path $bootRoot 'cmdline.pre-audiodsp-final.bak'
    if (Test-Path -LiteralPath $cmdlineBackup) {
        if (-not $PostImageOnly) {
            throw "Unexpected existing file after fresh image: $cmdlineBackup"
        }
    }
    else {
        Copy-Item -LiteralPath $cmdline -Destination $cmdlineBackup -ErrorAction Stop
    }
    $cmdlineFlat = (Get-Content -LiteralPath $cmdline -Raw) -replace '[\r\n]+', ' '
    $tokens = $cmdlineFlat.Split(' ', [StringSplitOptions]::RemoveEmptyEntries) |
        Where-Object {
            $_ -notmatch '^systemd\.run=' -and
            $_ -notmatch '^systemd\.run_success_action=' -and
            $_ -ne 'systemd.unit=kernel-command-line.target'
        }
    $tokens += 'systemd.run=/boot/firmware/firstrun.sh'
    $tokens += 'systemd.run_success_action=reboot'
    $tokens += 'systemd.unit=kernel-command-line.target'
    [IO.File]::WriteAllText($cmdline, (($tokens -join ' ') + "`n"), $utf8NoBom)

    $safeNetworkConfig = @'
# AudioDSP Pi 2 Ethernet-only networking.
network:
  version: 2
  renderer: NetworkManager
  ethernets:
    eth0:
      dhcp4: true
      optional: true
'@
    [IO.File]::WriteAllText((Join-Path $bootRoot 'network-config'), $safeNetworkConfig, $utf8NoBom)
    $sshMarker = Join-Path $bootRoot 'ssh'
    if (-not (Test-Path -LiteralPath $sshMarker)) {
        New-Item -ItemType File -Path $sshMarker -ErrorAction Stop | Out-Null
    }

    $manifest = @(
        'AudioDSP Raspberry Pi 2 Model B Rev 1.1 four-channel SD build'
        'build_date=2026-08-18'
        'release=audiodsp-pi2-v1.2.0'
        'hostname=audiodsp-pi2'
        'user=audiodsp'
        'network=ethernet_dhcp'
        'os=Raspberry Pi OS Lite 32-bit armhf'
        'architecture=armv7-neon'
        'camilladsp=4.1.3'
        'chunksize=2048'
        'capture_channels=2'
        'playback_channels=4'
        'output_map=front_lr_integrated_amp,rear_lr_edifier_t5s'
        'capture=plughw:CARD=U7,DEV=0'
        'playback=plughw:CARD=U7,DEV=0'
        'u7_source=Line'
        'u7_output_db=-10'
        'dsp_preamp=none'
        'fir=Harman_StrongBassControl_Stereo_48k_NoPreamp.wav'
        "fir_sha256=$($bundle.FirSha)"
        'first_boot=automatic_install_then_reboot'
    ) -join "`n"
    [IO.File]::WriteAllText((Join-Path $bootRoot 'AudioDSP-PI2-BUILD.txt'), ($manifest + "`n"), $utf8NoBom)

    # Verify every non-secret payload byte after copying.
    $sourceFiles = Get-ChildItem -LiteralPath $bundle.Payload -File | Sort-Object Name
    foreach ($sourceFile in $sourceFiles) {
        $copiedFile = Join-Path $destination $sourceFile.Name
        if (-not (Test-Path -LiteralPath $copiedFile -PathType Leaf)) {
            throw "Payload copy is missing: $copiedFile"
        }
        $sourceHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $sourceFile.FullName).Hash
        $copiedHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $copiedFile).Hash
        if ($sourceHash -ne $copiedHash) {
            throw "Payload verification failed: $($sourceFile.Name)"
        }
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $cardFirstRun).Hash -ne
        (Get-FileHash -Algorithm SHA256 -LiteralPath $bundle.FirstRun).Hash) {
        throw 'First-run source/card hash mismatch.'
    }
    Assert-LfNoBom -Path $cardFirstRun

    $writtenCmdlineBytes = [IO.File]::ReadAllBytes($cmdline)
    $writtenCmdline = ([Text.Encoding]::UTF8.GetString($writtenCmdlineBytes)).TrimEnd("`r", "`n")
    if ($writtenCmdline.Contains("`r") -or $writtenCmdline.Contains("`n")) {
        throw 'cmdline.txt must contain exactly one line.'
    }
    $runTokens = @($writtenCmdline.Split(' ') | Where-Object { $_ -match '^systemd\.run=' })
    if ($runTokens.Count -ne 1 -or $runTokens[0] -ne 'systemd.run=/boot/firmware/firstrun.sh') {
        throw 'cmdline.txt does not contain exactly one correct first-run command.'
    }
    if ($writtenCmdline -notmatch 'root=PARTUUID=[^ ]+-02') {
        throw 'cmdline.txt root PARTUUID validation failed.'
    }

    $writtenNetworkConfig = Get-Content -LiteralPath (Join-Path $bootRoot 'network-config') -Raw
    if ($writtenNetworkConfig -match '(?i)wifi|password|psk' -or
        $writtenNetworkConfig -notmatch '(?m)^\s*eth0:\s*$') {
        throw 'Pi 2 network-config is not Ethernet-only.'
    }

    $diskCheck = Get-VerifiedTargetDisk -AllowMissingDriveLetter
    $partitionCheck = Get-Partition -DriveLetter $bootLetter -ErrorAction Stop
    if ($partitionCheck.DiskNumber -ne $diskCheck.Number -or
        $partitionCheck.PartitionNumber -ne $bootPartition.PartitionNumber) {
        throw 'Final target identity validation failed.'
    }
    if (-not $PostImageOnly) {
        $dirtyOutput = & "$env:SystemRoot\System32\fsutil.exe" dirty query "${bootLetter}:" 2>&1 | Out-String
        if ($dirtyOutput -match '(?i)\bis Dirty\b') {
            & "$env:SystemRoot\System32\chkdsk.exe" "${bootLetter}:" /F /X
            if ($LASTEXITCODE -gt 1) {
                throw "CHKDSK failed with exit code $LASTEXITCODE"
            }
        }
    }

    Write-Host ''
    Write-Host 'PI 2 SD CARD COMPLETE' -ForegroundColor Green
    Write-Host "  Target: Disk $($diskCheck.Number) / $($diskCheck.FriendlyName)"
    Write-Host '  Raspberry Pi OS Lite 32-bit armhf + CamillaDSP 4.1.3 ARMv7'
    Write-Host '  U7: Line In -> NoPreamp stereo FIR -> Front L/R + Rear L/R'
    Write-Host '  Wiring: Front RCA -> integrated amp; Rear 3.5 mm -> T5s Signal In'
    Write-Host '  U7 analog output: -10 dB'
    Write-Host '  Network: Ethernet DHCP (Pi 2 Rev 1.1 has no onboard Wi-Fi)'
    Write-Host '  First boot: installs everything and reboots once'
    Write-Host '  Expected first availability: about 4-6 minutes after power-on'

    # Flush and dismount only the re-verified FAT partition. If Windows does
    # not permit it, leave the card mounted and require the tray Eject action.
    $dismountExit = 1
    if (-not $PostImageOnly) {
        try {
            & "$env:SystemRoot\System32\mountvol.exe" "${bootLetter}:\" /P
            $dismountExit = $LASTEXITCODE
        }
        catch {
            $dismountExit = 1
        }
    }
    Start-Sleep -Seconds 2
    if ($dismountExit -eq 0 -or -not (Test-Path -LiteralPath $bootRoot)) {
        Write-Host '  SD boot partition safely dismounted: YES' -ForegroundColor Green
        Write-Host '  The SD card can now be removed.' -ForegroundColor Green
    }
    else {
        Write-Host '  Use the Windows tray Eject action before removing the SD card.' -ForegroundColor Yellow
    }

}
catch {
    $errorText = $_ | Out-String
    Write-Host ''
    Write-Host 'FINAL SD WRITER ERROR' -ForegroundColor Red
    Write-Host $errorText -ForegroundColor Red
    try {
        Add-Content -LiteralPath $script:WriterLog -Value "`r`nFINAL SD WRITER ERROR`r`n$errorText" -ErrorAction SilentlyContinue
    }
    catch {
    }
    if (-not $NoPause) {
        Read-Host 'Press Enter to close'
    }
    exit 1
}
finally {
    if ($script:TranscriptStarted) {
        Stop-Transcript -ErrorAction SilentlyContinue | Out-Null
    }
}

if (-not $NoPause -and -not $ValidateOnly) {
    Read-Host 'Press Enter to close'
}
