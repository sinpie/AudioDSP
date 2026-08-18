[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot 'payload'),
    [double]$AnnouncementPeakDbfs = -21.0
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Speech

function Read-Pcm16MonoWave {
    param([Parameter(Mandatory)][string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    $reader = [IO.BinaryReader]::new($stream)
    try {
        if ([Text.Encoding]::ASCII.GetString($reader.ReadBytes(4)) -ne 'RIFF') {
            throw "Not a RIFF file: $Path"
        }
        [void]$reader.ReadUInt32()
        if ([Text.Encoding]::ASCII.GetString($reader.ReadBytes(4)) -ne 'WAVE') {
            throw "Not a WAVE file: $Path"
        }

        $formatCode = $null
        $channels = $null
        $sampleRate = $null
        $bitsPerSample = $null
        $data = $null
        while ($stream.Position -le ($stream.Length - 8)) {
            $chunkId = [Text.Encoding]::ASCII.GetString($reader.ReadBytes(4))
            $chunkSize = $reader.ReadUInt32()
            $chunkStart = $stream.Position
            if ($chunkId -eq 'fmt ') {
                $formatCode = $reader.ReadUInt16()
                $channels = $reader.ReadUInt16()
                $sampleRate = $reader.ReadUInt32()
                [void]$reader.ReadUInt32()
                [void]$reader.ReadUInt16()
                $bitsPerSample = $reader.ReadUInt16()
            }
            elseif ($chunkId -eq 'data') {
                $data = $reader.ReadBytes([int]$chunkSize)
            }
            $stream.Position = $chunkStart + $chunkSize + ($chunkSize % 2)
        }

        if ($formatCode -ne 1 -or $channels -ne 1 -or $sampleRate -ne 48000 -or $bitsPerSample -ne 16) {
            throw "Expected PCM16 mono 48 kHz from SAPI; got format=$formatCode channels=$channels rate=$sampleRate bits=$bitsPerSample"
        }
        if (-not $data -or ($data.Length % 2) -ne 0) {
            throw "Invalid PCM data in $Path"
        }

        $samples = [int16[]]::new($data.Length / 2)
        [Buffer]::BlockCopy($data, 0, $samples, 0, $data.Length)
        return $samples
    }
    finally {
        $reader.Dispose()
        $stream.Dispose()
    }
}

function Write-FrontOnlyAnnouncement {
    param(
        [Parameter(Mandatory)][int16[]]$Samples,
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][double]$PeakDbfs
    )

    $threshold = 32
    $first = 0
    while ($first -lt $Samples.Length -and [Math]::Abs([int]$Samples[$first]) -le $threshold) { $first++ }
    $last = $Samples.Length - 1
    while ($last -gt $first -and [Math]::Abs([int]$Samples[$last]) -le $threshold) { $last-- }
    if ($first -ge $Samples.Length) {
        throw 'Synthesized announcement contains only silence.'
    }

    $preRoll = 3840   # 80 ms at 48 kHz
    $postRoll = 7200  # 150 ms at 48 kHz
    $trimStart = [Math]::Max(0, $first - $preRoll)
    $trimEnd = [Math]::Min($Samples.Length - 1, $last + $postRoll)
    $trimmed = $Samples[$trimStart..$trimEnd]

    $sourcePeak = 1
    foreach ($sample in $trimmed) {
        $magnitude = [Math]::Abs([int]$sample)
        if ($magnitude -gt $sourcePeak) { $sourcePeak = $magnitude }
    }
    $targetPeak = 32767.0 * [Math]::Pow(10.0, $PeakDbfs / 20.0)
    $scale = $targetPeak / $sourcePeak

    $frameCount = $trimmed.Length
    $channels = 4
    $bits = 16
    $blockAlign = $channels * ($bits / 8)
    $dataSize = $frameCount * $blockAlign
    $stream = [IO.File]::Create($Path)
    $writer = [IO.BinaryWriter]::new($stream)
    try {
        $writer.Write([Text.Encoding]::ASCII.GetBytes('RIFF'))
        $writer.Write([uint32](36 + $dataSize))
        $writer.Write([Text.Encoding]::ASCII.GetBytes('WAVE'))
        $writer.Write([Text.Encoding]::ASCII.GetBytes('fmt '))
        $writer.Write([uint32]16)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$channels)
        $writer.Write([uint32]48000)
        $writer.Write([uint32](48000 * $blockAlign))
        $writer.Write([uint16]$blockAlign)
        $writer.Write([uint16]$bits)
        $writer.Write([Text.Encoding]::ASCII.GetBytes('data'))
        $writer.Write([uint32]$dataSize)
        foreach ($sample in $trimmed) {
            $front = [int16][Math]::Round([Math]::Max(-32768, [Math]::Min(32767, $sample * $scale)))
            $writer.Write($front)      # Front Left
            $writer.Write($front)      # Front Right
            $writer.Write([int16]0)    # Rear Left / T5s
            $writer.Write([int16]0)    # Rear Right / T5s
        }
    }
    finally {
        $writer.Dispose()
        $stream.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    throw "Output directory does not exist: $OutputDirectory"
}

$voice = 'Microsoft Zira Desktop'
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
$format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
    48000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono
)
$temporaryFiles = [Collections.Generic.List[string]]::new()
try {
    $synth.SelectVoice($voice)
    $synth.Rate = -1
    $synth.Volume = 100

    $prompts = @{
        'announce_speaker_48k_front_lr.wav' = 'Speaker'
        'announce_headphone_48k_front_lr.wav' = 'Headphones'
        'announce_dsp_ready_48k_front_lr.wav' = 'DSP ready'
    }
    foreach ($entry in $prompts.GetEnumerator()) {
        $temporary = [IO.Path]::GetTempFileName()
        $temporaryFiles.Add($temporary)
        $synth.SetOutputToWaveFile($temporary, $format)
        $synth.Speak($entry.Value)
        $synth.SetOutputToNull()
        $samples = Read-Pcm16MonoWave -Path $temporary
        $destination = Join-Path $OutputDirectory $entry.Key
        Write-FrontOnlyAnnouncement -Samples $samples -Path $destination -PeakDbfs $AnnouncementPeakDbfs
        Write-Host "Generated: $destination"
    }
}
finally {
    $synth.Dispose()
    foreach ($temporary in $temporaryFiles) {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}
