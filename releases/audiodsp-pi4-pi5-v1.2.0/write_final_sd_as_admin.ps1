param(
    [string]$DefaultWifiSsid = 'StarryNight',
    [int]$TargetDiskNumber = -1,
    [switch]$ValidateOnly,
    [switch]$NoPause
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:StageRoot = $PSScriptRoot
$script:RepoRoot = Split-Path -Parent (Split-Path -Parent $script:StageRoot)
$script:BundleRoot = Join-Path $script:RepoRoot 'build\pi4'
$script:BundleAssembler = Join-Path $script:RepoRoot 'tools\materialize_releases.py'
$script:WriterLog = Join-Path $script:StageRoot 'audiodsp-pi4-pi5-writer.log'
$script:TranscriptStarted = $false
$script:CredentialWrittenToCard = $false
$script:ResolvedDiskNumber = $null

function Invoke-BundleAssembly {
    if (-not (Test-Path -LiteralPath $script:BundleAssembler -PathType Leaf)) {
        throw "Bundle assembler is missing: $script:BundleAssembler"
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $python) {
        throw 'Python 3 is required to assemble the canonical AudioDSP bundle.'
    }
    & $python.Source $script:BundleAssembler --platform pi4 --assemble
    if ($LASTEXITCODE -ne 0) {
        throw "Bundle assembly failed with exit code $LASTEXITCODE"
    }
}

function Assert-Administrator {
    $principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script from an Administrator PowerShell window.'
    }
}

function Get-VerifiedTargetDisk {
    param([switch]$AllowMissingDriveLetter)

    if ($null -ne $script:ResolvedDiskNumber) {
        $diskNumber = [int]$script:ResolvedDiskNumber
    }
    elseif ($TargetDiskNumber -ge 0) {
        $diskNumber = $TargetDiskNumber
    }
    else {
        $candidates = @(Get-Disk -ErrorAction Stop | Where-Object {
            ($_.BusType -eq 'USB' -or $_.BusType -eq 'SD') -and
            -not $_.IsBoot -and -not $_.IsSystem -and
            ($_.Size / 1GB) -ge 7.2 -and ($_.Size / 1GB) -le 128
        })
        if ($candidates.Count -ne 1) {
            $summary = ($candidates | ForEach-Object {
                "Disk $($_.Number): $($_.FriendlyName), $([math]::Round($_.Size / 1GB, 2)) GB, $($_.BusType)"
            }) -join "`n  "
            throw "Safety stop: expected exactly one removable 8-128 GB target, found $($candidates.Count). Pass -TargetDiskNumber explicitly.`n  $summary"
        }
        $diskNumber = [int]$candidates[0].Number
    }

    $disk = Get-Disk -Number $diskNumber -ErrorAction Stop
    if ($disk.BusType -ne 'USB' -and $disk.BusType -ne 'SD') {
        throw "Safety stop: disk $diskNumber bus is '$($disk.BusType)', not USB/SD."
    }
    if ($disk.IsBoot -or $disk.IsSystem) {
        throw "Safety stop: disk $diskNumber is marked as a boot or system disk."
    }
    if ($disk.IsOffline -or $disk.IsReadOnly) {
        throw "Safety stop: disk $diskNumber is offline or read-only."
    }
    $sizeGB = $disk.Size / 1GB
    if ($sizeGB -lt 7.2 -or $sizeGB -gt 128) {
        throw "Safety stop: disk $diskNumber size is $([math]::Round($sizeGB, 2)) GB."
    }
    $script:ResolvedDiskNumber = $diskNumber
    return $disk
}

function Get-BootPartition {
    Update-HostStorageCache
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $candidate = Get-Partition -DiskNumber $script:ResolvedDiskNumber -ErrorAction Stop |
            Where-Object { $_.Size -gt 100MB -and $_.Size -lt 2GB } |
            Sort-Object Size |
            Select-Object -First 1
        if ($candidate) {
            return $candidate
        }
        Start-Sleep -Seconds 1
        Update-HostStorageCache -ErrorAction SilentlyContinue
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
        Update-HostStorageCache -ErrorAction SilentlyContinue
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
    $image = Join-Path $script:StageRoot '2026-06-18-raspios-trixie-arm64-lite.img.xz'
    $firstRun = Join-Path $script:StageRoot 'firstrun.sh'
    $networkTemplate = Join-Path $script:StageRoot 'audiodsp-network-apply.template'
    $payload = Join-Path $script:BundleRoot 'payload'
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
    $speakerWave = Join-Path $payload 'announce_speaker_48k_front_lr.wav'
    $headphoneWave = Join-Path $payload 'announce_headphone_48k_front_lr.wav'
    $matrixTest = Join-Path $script:BundleRoot 'test_profile_matrix.py'
    $measurementTest = Join-Path $script:BundleRoot 'test_measurement_engine.py'
    $targetOptionTest = Join-Path $script:BundleRoot 'test_target_option_matrix.py'
    $mimoAlgorithmTest = Join-Path $script:BundleRoot 'test_mimo_algorithm_matrix.py'
    $mimoTest = Join-Path $script:BundleRoot 'test_mimo_runtime.py'
    $resourceTest = Join-Path $script:BundleRoot 'test_resource_budget.py'
    $privateKey = Join-Path $script:StageRoot 'audiodsp_pi_ed25519'
    $publicKey = Join-Path $script:StageRoot 'audiodsp_pi_ed25519.pub'
    $imager = 'C:\Program Files\Raspberry Pi Ltd\Imager\rpi-imager.exe'

    foreach ($requiredFile in @(
        $image, $firstRun, $networkTemplate, $starter, $config, $camilla,
        $fir, $service, $asound, $outputProfile, $profileManager, $profileWeb, $measurement, $mimo,
        $cal0, $cal90, $targetHarman,
        $profileWebService, $u7Monitor, $u7Service, $dspReady, $dspReadyService,
        $dspReadyWave, $speakerWave, $headphoneWave, $matrixTest, $measurementTest, $targetOptionTest, $mimoAlgorithmTest, $mimoTest, $resourceTest,
        $privateKey, $publicKey, $imager
    )) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Required file is missing: $requiredFile"
        }
    }

    $expectedHashes = @{
        $image   = 'ACFF736CA7945E3B305F07CDA4ABDB870910E12634991DA69783611756E381B3'
        $camilla = 'E04C7A6603E9482BAB33C1E18AFC41D3C07410B54BA9C246EDA69F7E9CBAEDFA'
        $fir     = '8A8A3B2FC31A080A6BC40205F29EA6471DF95ADF357618B2025BDD193EF45C99'
    }
    foreach ($entry in $expectedHashes.GetEnumerator()) {
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $entry.Key).Hash
        if ($actual -ne $entry.Value) {
            throw "SHA256 mismatch: $($entry.Key)`nExpected: $($entry.Value)`nActual:   $actual"
        }
    }

    foreach ($linuxTextFile in @(
        $firstRun, $networkTemplate, $starter, $config, $service, $asound,
        $outputProfile, $profileManager, $profileWeb, $profileWebService,
        $u7Monitor, $u7Service, $dspReady, $dspReadyService, $measurement, $mimo,
        $matrixTest, $measurementTest, $targetOptionTest, $mimoAlgorithmTest, $mimoTest, $resourceTest
    )) {
        Assert-LfNoBom -Path $linuxTextFile
    }
    foreach ($bashFile in @($firstRun, $networkTemplate, $starter, $outputProfile, $dspReady)) {
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
        $configText -notmatch '(?m)^\s*chunksize:\s*1024\s*$' -or
        $configText -notmatch '(?ms)^\s{2}playback:\s*$.*?^\s{4}channels:\s*4\s*$' -or
        $configText -notmatch '(?m)^\s{2}stereo_to_front_and_rear:\s*$' -or
        $configText -notmatch '(?m)^\s{4}name:\s*"stereo_to_front_and_rear"\s*$' -or
        ([regex]::Matches($configText, '(?m)^\s{6}- dest:\s*[0-3]\s*$').Count -ne 4) -or
        $configText -match '(?mi)^\s*type:\s*Gain\s*$') {
        throw 'Pi4/Pi5 four-channel CamillaDSP mapping, chunksize, or NoPreamp validation failed.'
    }

    $managerText = Get-Content -LiteralPath $profileManager -Raw
    if ($managerText -notmatch 'set-bypass' -or
        $managerText -notmatch 'set-chunksize' -or
        $managerText -notmatch 'set-output-volume' -or
        $managerText -notmatch 'output_volume_db' -or
        $managerText -notmatch 'restore-snapshot' -or
        $managerText -notmatch 'install-mimo' -or
        $managerText -notmatch 'ALLOWED_CHUNKSIZES' -or
        $managerText -notmatch 'selector_status' -or
        $managerText -notmatch 'resolve_preview' -or
        $managerText -notmatch 'remain active for one second') {
        throw 'The FIR profile manager is missing profile, selector, or chunk-size support.'
    }

    $webText = Get-Content -LiteralPath $profileWeb -Raw -Encoding UTF8
    if ($webText -notmatch 'client_svg_graph' -or
        $webText -notmatch '/bypass' -or
        $webText -notmatch '/chunksize' -or
        $webText -notmatch 'name="chunksize"' -or
        $webText -notmatch '/api/measurement/download/front' -or
        $webText -notmatch 'mimo_one_sub' -or
        $webText -notmatch 'measurement-result-graph' -or
        $webText -notmatch 'def backup_archive' -or
        $webText -notmatch '/api/backup/download' -or
        $webText -notmatch '/api/backup/latest' -or
        $webText -notmatch '/api/volume' -or
        $webText -notmatch 'output_volume_control' -or
        $webText -notmatch 'output-volume-control' -or
        $webText -notmatch 'non_destructive_measurement_tabs' -or
        $webText -notmatch 'role="tablist"' -or
        $webText -notmatch 'Woofer 최종 trim' -or
        $webText -notmatch 'session-overview' -or
        $webText -notmatch '/measurement/delete-session' -or
        $webText -notmatch 'lrw_sum' -or
        $webText -notmatch 'predicted_target_and_spatial_non_regression' -or
        $webText -notmatch 'Safe · 높은 안정성' -or
        $webText -notmatch 'build-fieldset' -or
        $webText -notmatch 'validation-checklist' -or
        $webText -notmatch '\-\-step-accent' -or
        $webText -notmatch 'summary::after' -or
        $webText -notmatch 'room_tuning_audit' -or
        $webText -notmatch 'output-level-warning' -or
        $webText -notmatch '−42부터 시작' -or
        $webText -notmatch '실제 측정음을 재생합니다' -or
        $webText -notmatch '저역 late/early' -or
        $webText -notmatch 'live_u7_status_poll' -or
        $webText -notmatch "fetch\('/api/status'" -or
        $webText -notmatch 'active-profile' -or
        $webText -notmatch 'signal_flow_diagram' -or
        $webText -notmatch 'measurement-path-lock' -or
        $webText -notmatch 'name="crossover_enabled"' -or
        $webText -notmatch 'additional_block_latency_samples' -or
        $webText -notmatch 'aria-current="page"' -or
        $webText -notmatch 'aria-current="step"' -or
        $webText -notmatch 'class="skip-link"' -or
        $webText -notmatch 'id="main-content"' -or
        $webText -notmatch 'file-picker-label' -or
        $webText -notmatch '\-\-on-accent' -or
        $webText -notmatch 'RESULT_ALGORITHM_REVISION' -or
        $webText -match 'action="/switch"') {
        throw 'The profile Web UI is missing display-only U7 state, graph, bypass, or chunk-size support.'
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
        'DEFAULT_NOISE_LEVEL_DBFS = -42',
        'DEFAULT_SWEEP_LEVEL_DBFS = -42',
        'recover_interrupted_worker',
        'offline_estimates_seconds',
        'fir_energy_delay',
        'MAX_PLAUSIBLE_BULK_DELAY_SAMPLES',
        'time_alignment_safe',
        'automatic_room_correction_db',
        'preference_correction_db',
        'CROSSOVER_FREQUENCIES',
        'apply_joint_crossover_guard',
        'additional_block_latency_samples',
        '--fatal-errors',
        'audiodsp_announce',
        'invalidate_from_step',
        'install-pair',
        'MIMO_MODES',
        'ensure_measurement_output_path',
        'validate_result_profile',
        'validate_result_revision',
        'RESULT_ALGORITHM_REVISION',
        'set-session-note',
        'load-session',
        'delete-session',
        'session_integrity',
        'lrw_sum',
        'evaluate_premeasured_sum_model',
        'pass_independent_complex_model',
        'normalization_applied": False',
        '/var/lib/audiodsp/u7-selector-state.json'
    )) {
        if ($measurementText -notmatch [regex]::Escape($requiredText)) {
            throw "Measurement engine validation is missing: $requiredText"
        }
    }

    $mimoText = Get-Content -LiteralPath $mimo -Raw
    foreach ($requiredText in @('weighted pressure matching', 'MIMO_manifest.json', 'correlated_input_headroom', 'mimo_one_sub', 'bulk_delay_samples', 'spectral_continuity', 'solution_blend', 'target_level_normalization', 'siso_bank_normalization', 'before_level_alignment_db', 'predicted_modal_tail_non_regression', 'response_confidence', 'crossover_spectra', 'physical_output_limits', 'pass_multichannel_complex_model', 'application_requires_post_filter_measurement": False')) {
        if ($mimoText -notmatch [regex]::Escape($requiredText)) {
            throw "MIMO engine validation is missing: $requiredText"
        }
    }

    $monitorText = Get-Content -LiteralPath $u7Monitor -Raw
    if ($monitorText -notmatch 'HIDIOCGINPUT' -or
        $monitorText -notmatch 'hidio_get_input' -or
        $monitorText -notmatch 'save_selector_state') {
        throw 'The U7 monitor is missing its initial hardware-state query.'
    }

    $firstRunText = Get-Content -LiteralPath $firstRun -Raw
    foreach ($requiredText in @(
        'usermod -s /bin/bash audiodsp',
        '/usr/bin/cancel-rename audiodsp',
        'hostname=audiodsp-pi',
        'audiodsp-network-apply.service',
        'audiodsp-profile-manager.py activate speaker --no-restart',
        'audiodsp-profile-manager.py set-chunksize 1024 --no-restart',
        'audiodsp-web.service',
        'audiodsp-measurement.py self-test',
        'audiodsp-mimo.py',
        'audiodsp-ready.service',
        'test ! -e /etc/ssh/sshd_config.d/rename_user.conf'
    )) {
        if ($firstRunText -notmatch [regex]::Escape($requiredText)) {
            throw "First-run validation is missing: $requiredText"
        }
    }

    $networkText = Get-Content -LiteralPath $networkTemplate -Raw
    if ([regex]::Matches($networkText, [regex]::Escape('__SSID_B64__')).Count -ne 1 -or
        [regex]::Matches($networkText, [regex]::Escape('__PSK_B64__')).Count -ne 1) {
        throw 'The network template must contain exactly one SSID and one PSK placeholder.'
    }

    [pscustomobject]@{
        Image = $image
        FirstRun = $firstRun
        NetworkTemplate = $networkTemplate
        Payload = $payload
        Imager = $imager
        ImageUncompressedSha = 'e235fd24fc5f039c08daba7d3abc04aecc7313f979d16d2a3fdad29dd44c33a9'
        FirSha = $expectedHashes[$fir]
        CamillaSha = $expectedHashes[$camilla]
    }
}

function Read-WifiCredential {
    param([Parameter(Mandatory)][string]$DefaultSsid)

    $ssidInput = Read-Host "Wi-Fi SSID [$DefaultSsid]"
    $ssid = if ([string]::IsNullOrWhiteSpace($ssidInput)) { $DefaultSsid } else { $ssidInput.Trim() }
    $ssidByteCount = [Text.Encoding]::UTF8.GetByteCount($ssid)
    if ($ssidByteCount -lt 1 -or $ssidByteCount -gt 32) {
        throw 'Wi-Fi SSID must be 1-32 UTF-8 bytes.'
    }

    $securePassword = Read-Host "Wi-Fi password for '$ssid'" -AsSecureString
    $bstr = [IntPtr]::Zero
    $plainPassword = $null
    try {
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ($plainPassword.Length -lt 8 -or $plainPassword.Length -gt 63) {
            throw 'Wi-Fi password must be 8-63 characters.'
        }
        return [pscustomobject]@{
            Ssid = $ssid
            SsidB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($ssid))
            PasswordB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($plainPassword))
        }
    }
    finally {
        $plainPassword = $null
        $securePassword = $null
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

try {
    Start-Transcript -LiteralPath $script:WriterLog -Force | Out-Null
    $script:TranscriptStarted = $true

    Write-Host 'Assembling the canonical AudioDSP Pi 4/5 bundle...' -ForegroundColor Cyan
    Invoke-BundleAssembly
    Write-Host 'Validating the complete AudioDSP Pi 4/5 SD bundle...' -ForegroundColor Cyan
    $bundle = Assert-FinalBundle
    Write-Host 'Bundle validation: PASS' -ForegroundColor Green

    if ($ValidateOnly) {
        Write-Host ''
        Write-Host 'AUDIODSP PI 4/5 BUNDLE VALIDATION COMPLETE' -ForegroundColor Green
        Write-Host '  Raspberry Pi OS Lite 64-bit image: verified'
        Write-Host '  CamillaDSP 4.1.3 aarch64: verified'
        Write-Host '  FIR NoPreamp SHA256: verified'
        Write-Host '  Account/network/U7 scripts: syntax and policy verified'
        return
    }

    Assert-Administrator
    $targetDisk = Get-VerifiedTargetDisk

    Write-Host ''
    Write-Host 'TARGET VERIFIED:' -ForegroundColor Yellow
    Write-Host "  Disk $($targetDisk.Number) / $($targetDisk.FriendlyName) / $([math]::Round($targetDisk.Size / 1GB, 2)) GB"
    Write-Host '  ALL EXISTING CONTENTS ON THIS SD CARD WILL BE OVERWRITTEN.' -ForegroundColor Yellow
    $expectedConfirmation = "WRITE DISK $($targetDisk.Number)"
    $confirmation = Read-Host "Type $expectedConfirmation to continue"
    if ($confirmation -cne $expectedConfirmation) {
        throw 'Confirmation did not match; no write was started.'
    }

    $wifi = Read-WifiCredential -DefaultSsid $DefaultWifiSsid
    $networkTemplateText = Get-Content -LiteralPath $bundle.NetworkTemplate -Raw
    $generatedNetworkScript = $networkTemplateText.Replace('__SSID_B64__', $wifi.SsidB64)
    $generatedNetworkScript = $generatedNetworkScript.Replace('__PSK_B64__', $wifi.PasswordB64)
    $wifiSsidForSummary = $wifi.Ssid
    $wifi = $null

    if ($generatedNetworkScript -match '__SSID_B64__|__PSK_B64__') {
        throw 'Network credential template replacement failed.'
    }

    $imagerLog = Join-Path $script:StageRoot 'final-imager-write.log'
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
        throw "Safety stop: payload destination already exists after a fresh image: $destination"
    }
    New-Item -ItemType Directory -Path $destination -ErrorAction Stop | Out-Null

    Get-ChildItem -LiteralPath $bundle.Payload -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $destination $_.Name) -ErrorAction Stop
    }

    $utf8NoBom = [Text.UTF8Encoding]::new($false)
    $cardNetworkScript = Join-Path $destination 'audiodsp-network-apply'
    [IO.File]::WriteAllText($cardNetworkScript, $generatedNetworkScript, $utf8NoBom)
    $script:CredentialWrittenToCard = $true
    $generatedNetworkHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $cardNetworkScript).Hash
    $generatedNetworkScript = $null

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
        throw "Unexpected existing file after fresh image: $cmdlineBackup"
    }
    Copy-Item -LiteralPath $cmdline -Destination $cmdlineBackup -ErrorAction Stop
    $cmdlineFlat = (Get-Content -LiteralPath $cmdline -Raw) -replace '[\r\n]+', ' '
    $tokens = $cmdlineFlat.Split(' ', [StringSplitOptions]::RemoveEmptyEntries) |
        Where-Object {
            $_ -notmatch '^systemd\.run=' -and
            $_ -notmatch '^systemd\.run_success_action=' -and
            $_ -ne 'systemd.unit=kernel-command-line.target'
        }
    if ($tokens -notcontains 'cfg80211.ieee80211_regdom=KR') {
        $tokens += 'cfg80211.ieee80211_regdom=KR'
    }
    $tokens += 'systemd.run=/boot/firmware/firstrun.sh'
    $tokens += 'systemd.run_success_action=reboot'
    $tokens += 'systemd.unit=kernel-command-line.target'
    [IO.File]::WriteAllText($cmdline, (($tokens -join ' ') + "`n"), $utf8NoBom)

    $safeNetworkConfig = @'
# AudioDSP networking is provisioned by a root-only one-time service.
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
        'AudioDSP Raspberry Pi 4/5 SD build'
        'build_date=2026-08-18'
        'release=audiodsp-pi4-pi5-v1.2.0'
        'hostname=audiodsp-pi'
        'user=audiodsp'
        "wifi_ssid=$wifiSsidForSummary"
        'wifi_country=KR'
        'camilladsp=4.1.3'
        'capture=plughw:CARD=U7,DEV=0'
        'playback=audiodsp_dsp'
        'capture_channels=2'
        'playback_channels=4'
        'initial_chunksize=1024'
        'chunksize_web_options=512,1024,2048,4096'
        'profile_web=http://audiodsp-pi.local:8080'
        'u7_source=Line'
        'u7_output_db=-10'
        'dsp_preamp=none'
        'fir=Harman_StrongBassControl_Stereo_48k_NoPreamp.wav'
        "fir_sha256=$($bundle.FirSha)"
        'first_boot=automatic_reboot_then_network_provisioning'
        'wifi_boot_payload=deleted_during_first_boot'
    ) -join "`n"
    [IO.File]::WriteAllText((Join-Path $bootRoot 'AudioDSP-PI4-PI5-BUILD.txt'), ($manifest + "`n"), $utf8NoBom)

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
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $cardNetworkScript).Hash -ne $generatedNetworkHash) {
        throw 'Generated network script changed after writing.'
    }
    Assert-LfNoBom -Path $cardFirstRun
    Assert-LfNoBom -Path $cardNetworkScript

    $writtenCmdlineBytes = [IO.File]::ReadAllBytes($cmdline)
    $writtenCmdline = ([Text.Encoding]::UTF8.GetString($writtenCmdlineBytes)).TrimEnd("`r", "`n")
    if ($writtenCmdline.Contains("`r") -or $writtenCmdline.Contains("`n")) {
        throw 'cmdline.txt must contain exactly one line.'
    }
    $runTokens = $writtenCmdline.Split(' ') | Where-Object { $_ -match '^systemd\.run=' }
    if ($runTokens.Count -ne 1 -or $runTokens[0] -ne 'systemd.run=/boot/firmware/firstrun.sh') {
        throw 'cmdline.txt does not contain exactly one correct first-run command.'
    }
    if ($writtenCmdline -notmatch 'root=PARTUUID=[^ ]+-02') {
        throw 'cmdline.txt root PARTUUID validation failed.'
    }

    $writtenNetworkConfig = Get-Content -LiteralPath (Join-Path $bootRoot 'network-config') -Raw
    if ($writtenNetworkConfig -match [regex]::Escape($wifiSsidForSummary) -or
        $writtenNetworkConfig -match '(?i)password|psk') {
        throw 'Plain cloud-init network-config unexpectedly contains Wi-Fi data.'
    }

    $diskCheck = Get-VerifiedTargetDisk -AllowMissingDriveLetter
    $partitionCheck = Get-Partition -DriveLetter $bootLetter -ErrorAction Stop
    if ($partitionCheck.DiskNumber -ne $diskCheck.Number -or
        $partitionCheck.PartitionNumber -ne $bootPartition.PartitionNumber) {
        throw 'Final target identity validation failed.'
    }
    $dirtyOutput = & "$env:SystemRoot\System32\fsutil.exe" dirty query "${bootLetter}:" 2>&1 | Out-String
    if ($dirtyOutput -match '(?i)\bis Dirty\b') {
        & "$env:SystemRoot\System32\chkdsk.exe" "${bootLetter}:" /F /X
        if ($LASTEXITCODE -gt 1) {
            throw "CHKDSK failed with exit code $LASTEXITCODE"
        }
    }

    Write-Host ''
    Write-Host 'AUDIODSP PI 4/5 SD CARD COMPLETE' -ForegroundColor Green
    Write-Host "  Target: Disk $($diskCheck.Number) / $($diskCheck.FriendlyName)"
    Write-Host '  Raspberry Pi OS Lite 64-bit + CamillaDSP 4.1.3'
    Write-Host '  U7: Line In stereo -> FIR/profile DSP -> Front + Rear 4-channel output'
    Write-Host '  Web UI: http://audiodsp-pi.local:8080 (chunk size 512/1024/2048/4096)'
    Write-Host '  U7 analog output: -10 dB'
    Write-Host "  Wi-Fi: $wifiSsidForSummary / KR (credential is never printed)"
    Write-Host '  First boot: installs everything, scrubs FAT credential, reboots once'
    Write-Host '  Expected first availability: about 2-3 minutes after power-on'

    # Flush and dismount only the re-verified FAT partition. If Windows does
    # not permit it, leave the card mounted and require the tray Eject action.
    $dismountExit = 1
    try {
        & "$env:SystemRoot\System32\mountvol.exe" "${bootLetter}:\" /P
        $dismountExit = $LASTEXITCODE
    }
    catch {
        $dismountExit = 1
    }
    Start-Sleep -Seconds 2
    if ($dismountExit -eq 0 -or -not (Test-Path -LiteralPath $bootRoot)) {
        Write-Host '  SD boot partition safely dismounted: YES' -ForegroundColor Green
        Write-Host '  The SD card can now be removed.' -ForegroundColor Green
    }
    else {
        Write-Host '  Use the Windows tray Eject action before removing the SD card.' -ForegroundColor Yellow
    }

    $script:CredentialWrittenToCard = $false
}
catch {
    $errorText = $_ | Out-String
    Write-Host ''
    Write-Host 'FINAL SD WRITER ERROR' -ForegroundColor Red
    Write-Host $errorText -ForegroundColor Red
    if ($script:CredentialWrittenToCard) {
        Write-Host 'Security notice: the incomplete card may still contain the temporary Wi-Fi credential payload. Re-image it before reuse.' -ForegroundColor Yellow
    }
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
