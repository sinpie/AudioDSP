#!/bin/bash
set -euo pipefail

BOOT_ROOT=/boot/firmware
if [ ! -d "$BOOT_ROOT/audiodsp" ] && [ -d /boot/audiodsp ]; then
    BOOT_ROOT=/boot
fi
PAYLOAD_DIR="$BOOT_ROOT/audiodsp"

# Keep a copy on the FAT boot partition so a failed first boot can be
# diagnosed from Windows without mounting the Linux root filesystem.
exec > >(tee -a /var/log/audiodsp-firstboot.log "$BOOT_ROOT/audiodsp-firstboot.log") 2>&1

test -d "$PAYLOAD_DIR"
# The boot partition is FAT32, so executable bits are mount-option dependent.
# `install -m 0755` below sets the proper permission on the Linux rootfs.
test -f "$PAYLOAD_DIR/camilladsp"
test -f "$PAYLOAD_DIR/camilladsp.yml"
test -f "$PAYLOAD_DIR/Harman_StrongBassControl_Stereo_48k_NoPreamp.wav"
test -f "$PAYLOAD_DIR/announce_speaker_48k_front_lr.wav"
test -f "$PAYLOAD_DIR/announce_headphone_48k_front_lr.wav"
test -f "$PAYLOAD_DIR/announce_dsp_ready_48k_front_lr.wav"
test -f "$PAYLOAD_DIR/asound-audiodsp.conf"
test -f "$PAYLOAD_DIR/audiodsp-output-profile"
test -f "$PAYLOAD_DIR/audiodsp-profile-manager.py"
test -f "$PAYLOAD_DIR/audiodsp-profile-web.py"
test -f "$PAYLOAD_DIR/audiodsp-web.service"
test -f "$PAYLOAD_DIR/audiodsp-measurement.py"
test -f "$PAYLOAD_DIR/audiodsp-import-session.py"
test -f "$PAYLOAD_DIR/audiodsp-mimo.py"
test -f "$PAYLOAD_DIR/7200660.txt"
test -f "$PAYLOAD_DIR/7200660_90deg.txt"
test -f "$PAYLOAD_DIR/target_Harman_Kardon.txt"
test -f "$PAYLOAD_DIR/audiodsp-profile-monitor.py"
test -f "$PAYLOAD_DIR/audiodsp-profile-monitor.service"
test -f "$PAYLOAD_DIR/audiodsp-dsp-ready"
test -f "$PAYLOAD_DIR/audiodsp-ready.service"
test -f "$PAYLOAD_DIR/audiodsp-network-apply"
command -v python3 >/dev/null
command -v aplay >/dev/null
command -v flock >/dev/null
command -v arecord >/dev/null
command -v amixer >/dev/null

if [ -x /usr/lib/raspberrypi-sys-mods/imager_custom ]; then
    /usr/lib/raspberrypi-sys-mods/imager_custom set_hostname audiodsp-pi
else
    old_hostname="$(tr -d ' \t\n\r' < /etc/hostname)"
    echo audiodsp-pi > /etc/hostname
    sed -i "s/127.0.1.1.*${old_hostname}/127.0.1.1\taudiodsp-pi/g" /etc/hosts
fi

if ! id audiodsp >/dev/null 2>&1; then
    existing_user="$(getent passwd 1000 | cut -d: -f1 || true)"
    if [ -n "$existing_user" ] && [ "$existing_user" != "audiodsp" ]; then
        existing_group="$(id -gn "$existing_user")"
        usermod -l audiodsp -d /home/audiodsp -m "$existing_user"
        if [ "$existing_group" = "$existing_user" ]; then
            groupmod -n audiodsp "$existing_group"
        fi
    else
        useradd -m -U -u 1000 -s /bin/bash audiodsp
    fi
fi

# A renamed Raspberry Pi placeholder account may retain /usr/sbin/nologin.
usermod -s /bin/bash audiodsp

for group_name in sudo audio video plugdev netdev input gpio spi i2c render; do
    if getent group "$group_name" >/dev/null; then
        usermod -aG "$group_name" audiodsp
    fi
done
passwd -l audiodsp

install -d -m 0700 -o audiodsp -g audiodsp /home/audiodsp/.ssh
cat > /home/audiodsp/.ssh/authorized_keys <<'KEYEOF'
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJAFs0hAMAw7/9Ueb+2vOQUe7kEPWVcw+WlBckMmODJc audiodsp@audiodsp-pi
KEYEOF
chown audiodsp:audiodsp /home/audiodsp/.ssh/authorized_keys
chmod 0600 /home/audiodsp/.ssh/authorized_keys

cat > /etc/sudoers.d/010_audiodsp-nopasswd <<'SUDOEOF'
audiodsp ALL=(ALL) NOPASSWD: ALL
SUDOEOF
chmod 0440 /etc/sudoers.d/010_audiodsp-nopasswd

if systemctl list-unit-files ssh.service >/dev/null 2>&1; then
    systemctl enable ssh.service
fi

if [ -x /usr/bin/cancel-rename ]; then
    if ! /usr/bin/cancel-rename audiodsp; then
        rm -f /etc/ssh/sshd_config.d/rename_user.conf
        systemctl disable userconfig.service 2>/dev/null || true
        systemctl enable getty@tty1.service 2>/dev/null || true
        systemctl reload ssh.service 2>/dev/null || true
    fi
else
    rm -f /etc/ssh/sshd_config.d/rename_user.conf
    systemctl disable userconfig.service 2>/dev/null || true
    systemctl enable getty@tty1.service 2>/dev/null || true
    systemctl reload ssh.service 2>/dev/null || true
fi
usermod -s /bin/bash audiodsp
chown -R audiodsp:audiodsp /home/audiodsp
test "$(getent passwd audiodsp | cut -d: -f6)" = /home/audiodsp
test "$(getent passwd audiodsp | cut -d: -f7)" = /bin/bash
test ! -e /etc/ssh/sshd_config.d/rename_user.conf

ln -snf /usr/share/zoneinfo/Asia/Seoul /etc/localtime
echo Asia/Seoul > /etc/timezone

install -m 0755 "$PAYLOAD_DIR/camilladsp" /usr/local/bin/camilladsp
install -m 0755 "$PAYLOAD_DIR/audiodsp-camilladsp-start" /usr/local/bin/audiodsp-camilladsp-start
install -m 0755 "$PAYLOAD_DIR/audiodsp-output-profile" /usr/local/bin/audiodsp-output-profile
install -m 0755 "$PAYLOAD_DIR/audiodsp-profile-manager.py" /usr/local/bin/audiodsp-profile-manager.py
install -m 0755 "$PAYLOAD_DIR/audiodsp-profile-web.py" /usr/local/bin/audiodsp-profile-web.py
install -m 0755 "$PAYLOAD_DIR/audiodsp-measurement.py" /usr/local/bin/audiodsp-measurement.py
install -m 0755 "$PAYLOAD_DIR/audiodsp-import-session.py" /usr/local/bin/audiodsp-import-session.py
install -m 0755 "$PAYLOAD_DIR/audiodsp-mimo.py" /usr/local/bin/audiodsp-mimo.py
install -m 0755 "$PAYLOAD_DIR/audiodsp-profile-monitor.py" /usr/local/bin/audiodsp-profile-monitor.py
install -m 0755 "$PAYLOAD_DIR/audiodsp-dsp-ready" /usr/local/bin/audiodsp-dsp-ready
install -m 0644 "$PAYLOAD_DIR/asound-audiodsp.conf" /etc/asound.conf
install -d -m 0755 /etc/camilladsp
install -m 0644 "$PAYLOAD_DIR/Harman_StrongBassControl_Stereo_48k_NoPreamp.wav" /etc/camilladsp/Harman_StrongBassControl_Stereo_48k_NoPreamp.wav
install -d -m 0755 /etc/camilladsp/profiles
install -m 0644 "$PAYLOAD_DIR/Harman_StrongBassControl_Stereo_48k_NoPreamp.wav" /etc/camilladsp/profiles/Factory_Speaker_Front_LR.wav
install -m 0644 "$PAYLOAD_DIR/Harman_StrongBassControl_Stereo_48k_NoPreamp.wav" /etc/camilladsp/profiles/Speaker_Front_LR.wav
install -d -m 0755 /var/lib/audiodsp /usr/local/share/audiodsp
install -d -m 0755 /var/lib/audiodsp/measurements /var/lib/audiodsp/calibration /usr/local/share/audiodsp/targets
install -m 0644 "$PAYLOAD_DIR/7200660.txt" /var/lib/audiodsp/calibration/7200660.txt
install -m 0644 "$PAYLOAD_DIR/7200660_90deg.txt" /var/lib/audiodsp/calibration/7200660_90deg.txt
for target_file in "$PAYLOAD_DIR"/target_*.txt; do
    install -m 0644 "$target_file" "/usr/local/share/audiodsp/targets/$(basename "$target_file")"
done
install -m 0644 "$PAYLOAD_DIR/announce_speaker_48k_front_lr.wav" /usr/local/share/audiodsp/announce_speaker_48k_front_lr.wav
install -m 0644 "$PAYLOAD_DIR/announce_headphone_48k_front_lr.wav" /usr/local/share/audiodsp/announce_headphone_48k_front_lr.wav
install -m 0644 "$PAYLOAD_DIR/announce_dsp_ready_48k_front_lr.wav" /usr/local/share/audiodsp/announce_dsp_ready_48k_front_lr.wav
install -m 0644 "$PAYLOAD_DIR/camilladsp.service" /etc/systemd/system/camilladsp.service
install -m 0644 "$PAYLOAD_DIR/audiodsp-profile-monitor.service" /etc/systemd/system/audiodsp-profile-monitor.service
install -m 0644 "$PAYLOAD_DIR/audiodsp-web.service" /etc/systemd/system/audiodsp-web.service
install -m 0644 "$PAYLOAD_DIR/audiodsp-ready.service" /etc/systemd/system/audiodsp-ready.service

# Generate and validate the initial Speaker/copy-front configuration. This
# also creates profile-settings.json with both per-profile bypass flags off.
/usr/local/bin/audiodsp-profile-manager.py activate speaker --no-restart >/var/log/audiodsp-profile-initial.json
/usr/local/bin/audiodsp-profile-manager.py set-chunksize 1024 --no-restart >/var/log/audiodsp-chunksize-initial.json
/usr/local/bin/audiodsp-measurement.py self-test >/var/log/audiodsp-measurement-selftest.json

SESSION_MIGRATION="$BOOT_ROOT/audiodsp-session-migration.tar.gz"
if [ -f "$SESSION_MIGRATION" ]; then
    /usr/local/bin/audiodsp-import-session.py "$SESSION_MIGRATION" >/var/log/audiodsp-session-migration.json
    rm -f "$SESSION_MIGRATION"
fi

/usr/local/bin/camilladsp --version
systemctl daemon-reload
systemctl enable camilladsp.service
systemctl enable audiodsp-profile-monitor.service
systemctl enable audiodsp-web.service
systemctl enable audiodsp-ready.service

# Keep the supplied Wi-Fi secret only on the root filesystem. The one-time
# service also creates Ethernet DHCP and then deletes its credential script.
systemctl enable NetworkManager.service 2>/dev/null || true
install -m 0700 "$PAYLOAD_DIR/audiodsp-network-apply" /usr/local/sbin/audiodsp-network-apply
cat > /etc/systemd/system/audiodsp-network-apply.service <<'NETSERVICEEOF'
[Unit]
Description=AudioDSP one-time NetworkManager provisioning
After=NetworkManager.service
Wants=NetworkManager.service
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/audiodsp-network-apply
Restart=on-failure
RestartSec=15
TimeoutStartSec=300

[Install]
WantedBy=multi-user.target
NETSERVICEEOF
systemctl daemon-reload
systemctl enable audiodsp-network-apply.service
cat > "$BOOT_ROOT/network-config" <<'NETCONFIGEOF'
# AudioDSP networking is provisioned by a root-only one-time service.
network:
  version: 2
  renderer: NetworkManager
NETCONFIGEOF
# Do not retain the Wi-Fi credential script on the Windows-readable FAT volume.
rm -f "$PAYLOAD_DIR/audiodsp-network-apply"

{
    echo 'status=success'
    echo "completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo 'hostname=audiodsp-pi'
    echo 'service=camilladsp.service'
    echo 'network_next_boot=audiodsp-network-apply.service'
    echo 'capture=plughw:CARD=U7,DEV=0'
    echo 'playback=audiodsp_dsp_to_u7_dev0'
    echo 'profiles=speaker_strong_bass,headphone_falls_back_to_speaker,bypass_per_profile'
    echo 'u7_hid_auto_switch=enabled'
    echo 'announcement=english_female_front_lr_only,dsp_ready_after_boot'
    echo 'profile_web_ui=http_port_8080'
    echo 'u7_output_db=-10'
    echo 'chunksize_default=1024,web_adjustable=512|1024|2048|4096'
    if [ -f /var/lib/audiodsp/session-migration.json ]; then
        echo 'session_migration=success'
    else
        echo 'session_migration=none'
    fi
} > "$BOOT_ROOT/audiodsp-firstboot-success.txt"

rm -f /boot/firstrun.sh /boot/firmware/firstrun.sh
for cmdline_file in /boot/cmdline.txt /boot/firmware/cmdline.txt; do
    if [ -f "$cmdline_file" ]; then
        sed -i -E \
            -e 's/ ?systemd\.run=[^ ]+//g' \
            -e 's/ ?systemd\.run_success_action=[^ ]+//g' \
            -e 's/ ?systemd\.unit=kernel-command-line\.target//g' \
            -e 's/[[:space:]]+/ /g' \
            -e 's/[[:space:]]+$//' \
            "$cmdline_file"
    fi
done
sync
exit 0
