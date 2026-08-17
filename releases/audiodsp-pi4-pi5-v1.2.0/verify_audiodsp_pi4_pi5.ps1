param(
    [string]$PiHost = 'audiodsp-pi.local',
    [string]$PiUser = 'audiodsp',
    [string]$KeyPath = (Join-Path $PSScriptRoot 'audiodsp_pi_ed25519')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "SSH key is missing: $KeyPath"
}

$remote = @'
set -eu
echo "host=$(hostname)"
arch="$(uname -m)"
test "$arch" = aarch64
echo "arch=$arch"
for service in camilladsp.service audiodsp-web.service audiodsp-profile-monitor.service; do
    test "$(systemctl is-active "$service")" = active
    echo "$service=active"
done
test -x /usr/local/bin/audiodsp-mimo.py
sudo -n python3 /usr/local/bin/audiodsp-profile-manager.py status >/tmp/audiodsp-verify-status.json
python3 - <<'PY'
import json
s=json.load(open('/tmp/audiodsp-verify-status.json', encoding='utf-8'))
assert s['settings']['chunksize'] in (512,1024,2048,4096)
assert s['resolved']['convolution_channels'] in (0,2,4,8)
assert s['capabilities']['mimo_supported'] is True
f=s['files']['speaker']['front']
assert f and f['sample_rate']==48000 and f['channels']==2 and f['frames']==32768
print('profile='+s['resolved']['effective_profile'])
print('chunksize='+str(s['settings']['chunksize']))
print('speaker_fir_sha256='+f['sha256'])
PY
curl -fsS http://127.0.0.1:8080/ | grep -q 'AudioDSP'
curl -fsS http://127.0.0.1:8080/measure | grep -q 'non_destructive_step_navigation'
curl -fsS http://127.0.0.1:8080/settings | grep -q '/api/backup/download'
curl -fsS http://127.0.0.1:8080/api/health >/tmp/audiodsp-verify-health.json
python3 - <<'PY'
import json
h=json.load(open('/tmp/audiodsp-verify-health.json', encoding='utf-8'))
assert h['xonar_u7'] is True
print('xonar_u7=connected')
print('umik1='+('connected' if h['umik1'] else 'not_connected'))
print('load1='+str(h['load'][0]))
print('memory_used_percent='+str(h['memory_used_percent']))
PY
echo 'AUDIODSP_PI4_PI5_VERIFY=PASS'
'@

& ssh.exe -i $KeyPath -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$PiUser@$PiHost" $remote
if ($LASTEXITCODE -ne 0) {
    throw "AudioDSP Pi 4/5 verification failed with exit code $LASTEXITCODE"
}
