import asyncio
import json
import re
from pathlib import Path

import httpx
import paramiko

FILES_DIR = Path(__file__).parent / "files"


def _event(step, name, status, output=""):
    return {"data": json.dumps({"step": step, "name": name, "status": status, "output": output})}


async def run_installation(ip, port, user, password, bid):
    loop = asyncio.get_event_loop()
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    def ssh_run(cmd):
        chan = ssh.get_transport().open_session()
        chan.get_pty()
        chan.exec_command(cmd)
        output = b""
        while True:
            chunk = chan.recv(4096)
            if not chunk:
                break
            output += chunk
        exit_code = chan.recv_exit_status()
        return output.decode("utf-8", errors="replace"), exit_code

    async def ssh_stream(step, name, cmd, result):
        chan = ssh.get_transport().open_session()
        chan.get_pty()
        chan.exec_command(cmd)
        output = b""
        chan.setblocking(False)
        while True:
            await asyncio.sleep(0.3)
            try:
                chunk = chan.recv(4096)
            except Exception:
                chunk = None
            if chunk:
                output += chunk
                text = chunk.decode("utf-8", errors="replace")
                last_line = [l for l in text.splitlines() if l.strip()]
                if last_line:
                    yield _event(step, name, "running", last_line[-1])
            else:
                if chan.exit_status_ready():
                    break
        result["exit_code"] = chan.recv_exit_status()
        result["output"] = output.decode("utf-8", errors="replace")

    # ── Step 1: SSH ──────────────────────────────────────────────────────────
    yield _event(1, "Verify SSH Connection", "running")
    try:
        await loop.run_in_executor(
            None,
            lambda: ssh.connect(ip, port=int(port), username=user, password=password, timeout=15),
        )
        yield _event(1, "Verify SSH Connection", "ok", f"Connected to {ip}:{port}")
    except Exception as e:
        yield _event(1, "Verify SSH Connection", "error", str(e))
        return

    # ── Step 2: apt update + upgrade ─────────────────────────────────────────
    yield _event(2, "System Update & Upgrade", "running", "Starting system update...")
    try:
        result = {}
        cmd = """
set -e

# Wait for any running apt/dpkg to finish (handles re-run after dropped connection)
i=0
while fuser /var/lib/dpkg/lock-frontend \
            /var/lib/dpkg/lock \
            /var/cache/apt/archives/lock >/dev/null 2>&1; do
    i=$((i+1))
    if [ $i -ge 360 ]; then
        echo "APT lock timeout after 30 minutes"
        exit 1
    fi
    echo "Waiting for apt lock... ($i/360)"
    sleep 5
done

# Fix any interrupted dpkg state (lock is free, safe to run)
sudo DEBIAN_FRONTEND=noninteractive dpkg --configure -a --force-confdef --force-confold

sudo apt-get update -y

sudo DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none \\
    apt-get upgrade -y \\
    -o Dpkg::Options::="--force-confdef" \\
    -o Dpkg::Options::="--force-confold"
"""
        async for event in ssh_stream(2, "System Update & Upgrade", cmd, result):
            yield event
        if result.get("exit_code", 1) != 0:
            yield _event(2, "System Update & Upgrade", "error", result.get("output", "")[-500:])
            return
        yield _event(2, "System Update & Upgrade", "ok", "Packages updated successfully")
    except Exception as e:
        yield _event(2, "System Update & Upgrade", "error", str(e))
        return

    # ── Step 3: Read serial ───────────────────────────────────────────────────
    yield _event(3, "Read Serial Number", "running")
    try:
        out, code = await loop.run_in_executor(None, lambda: ssh_run("cat /proc/cpuinfo"))
        match = re.search(r"Serial\s*:\s*([0-9a-f]+)", out, re.IGNORECASE)
        if not match:
            yield _event(3, "Read Serial Number", "error", "Serial not found in /proc/cpuinfo")
            return
        serial = match.group(1)
        yield _event(3, "Read Serial Number", "ok", f"Serial: {serial}")
    except Exception as e:
        yield _event(3, "Read Serial Number", "error", str(e))
        return

    # ── Step 4: Generate license ──────────────────────────────────────────────
    yield _event(4, "Generate License", "running")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"https://www.ioslinks.com/minisun/licgen.php?mac={serial}")
        lic_match = re.search(r"MyPi-[a-f0-9]+", resp.text)
        if not lic_match:
            yield _event(4, "Generate License", "error", f"Unexpected response: {resp.text}")
            return
        license_code = lic_match.group(0)
        yield _event(4, "Generate License", "ok", f"License: {license_code}")
    except Exception as e:
        yield _event(4, "Generate License", "error", str(e))
        return

    # ── Step 5+6: Prepare gpio.cfg + upload all files ─────────────────────────
    yield _event(5, "Prepare & Upload Files", "running")
    try:
        cfg_content = (FILES_DIR / "gpio.cfg").read_text()
        cfg_content = re.sub(r"^LIC = .*$", f"LIC = {license_code}", cfg_content, flags=re.MULTILINE)

        def upload_files():
            sftp = ssh.open_sftp()
            sftp.putfo(__import__("io").StringIO(cfg_content), "/home/pi/gpio.cfg")
            for fname in ("gpio_server.pyc", "pulse.pyc"):
                sftp.put(str(FILES_DIR / fname), f"/home/pi/{fname}")
            sftp.close()

        await loop.run_in_executor(None, upload_files)
        yield _event(5, "Prepare & Upload Files", "ok", "gpio.cfg (with license), gpio_server.pyc, pulse.pyc uploaded to /home/pi/")
    except Exception as e:
        yield _event(5, "Prepare & Upload Files", "error", str(e))
        return

    # ── Step 7: Write rc.local ────────────────────────────────────────────────
    yield _event(6, "Write rc.local", "running")
    rc_content = f"""#!/bin/sh -e
# rc.local
# This script is executed at the end of each multiuser runlevel.
# Make sure that the script will "exit 0" on success or any other
# value on error.
# In order to enable or disable this script just change the execution bits.
# By default this script does nothing.
# Print the IP address

_IP=$(hostname -I) || true
if [ "$_IP" ]; then
printf "My IP address is %s\\n" "$_IP"
fi
sleep 180
sudo python3 /home/pi/gpio_server.pyc &
curl https://hook.eu1.make.com/fov3gjfi4n1gt8jhnawpb2fodvmo1x15?bid={bid} &
exit 0
"""
    try:
        def write_rclocal():
            sftp = ssh.open_sftp()
            sftp.putfo(__import__("io").StringIO(rc_content), "/tmp/rc.local.new")
            sftp.close()

        await loop.run_in_executor(None, write_rclocal)
        out, code = await loop.run_in_executor(
            None, lambda: ssh_run("sudo mv /tmp/rc.local.new /etc/rc.local")
        )
        if code != 0:
            yield _event(6, "Write rc.local", "error", out)
            return
        yield _event(6, "Write rc.local", "ok", "/etc/rc.local written successfully")
    except Exception as e:
        yield _event(6, "Write rc.local", "error", str(e))
        return

    # ── Step 8: chmod rc.local ────────────────────────────────────────────────
    yield _event(7, "Fix rc.local Permissions", "running")
    try:
        out, code = await loop.run_in_executor(None, lambda: ssh_run("sudo chmod +x /etc/rc.local"))
        if code != 0:
            yield _event(7, "Fix rc.local Permissions", "error", out)
            return
        yield _event(7, "Fix rc.local Permissions", "ok", "chmod +x applied to /etc/rc.local")
    except Exception as e:
        yield _event(7, "Fix rc.local Permissions", "error", str(e))
        return

    # ── Step 9: Check GPIO version ────────────────────────────────────────────
    yield _event(8, "Check GPIO Version", "running")
    try:
        out, code = await loop.run_in_executor(
            None, lambda: ssh_run('python3 -c "import RPi.GPIO; print(RPi.GPIO.VERSION)"')
        )
        gpio_version = out.strip()
        yield _event(8, "Check GPIO Version", "ok", f"GPIO version: {gpio_version}")
    except Exception as e:
        yield _event(8, "Check GPIO Version", "error", str(e))
        return

    # ── Step 10-11: Downgrade GPIO if needed ──────────────────────────────────
    if gpio_version != "0.7.1a4":
        yield _event(9, "Downgrade GPIO to 0.7.1a4", "running")
        try:
            out, code = await loop.run_in_executor(
                None, lambda: ssh_run("sudo apt-get -y install python3-rpi.gpio")
            )
            if code != 0:
                yield _event(9, "Downgrade GPIO to 0.7.1a4", "error", out[-500:])
                return
            yield _event(9, "Downgrade GPIO to 0.7.1a4", "ok", "GPIO package reinstalled")
        except Exception as e:
            yield _event(9, "Downgrade GPIO to 0.7.1a4", "error", str(e))
            return
    else:
        yield _event(9, "GPIO Version Check", "ok", "Version is 0.7.1a4 — no downgrade needed")

    # ── Step 12: Reboot ───────────────────────────────────────────────────────
    yield _event(10, "Reboot Pi", "running", "Rebooting — waiting 3 minutes for Pi to come back and start services...")
    try:
        await loop.run_in_executor(None, lambda: ssh_run("sudo reboot"))
    except Exception:
        pass  # connection drop on reboot is expected

    ssh.close()
    await asyncio.sleep(200)

    # ── Step 13: Reconnect + verify service ───────────────────────────────────
    yield _event(10, "Reboot Pi", "ok", "Reboot triggered — reconnecting...")
    yield _event(11, "Verify Service Running", "running")
    try:
        ssh2 = paramiko.SSHClient()
        ssh2.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        def reconnect():
            for attempt in range(5):
                try:
                    ssh2.connect(ip, port=int(port), username=user, password=password, timeout=10)
                    return True
                except Exception:
                    import time
                    time.sleep(10)
            return False

        connected = await loop.run_in_executor(None, reconnect)
        if not connected:
            yield _event(11, "Verify Service Running", "error", "Could not reconnect after reboot")
            return

        def check_service():
            chan = ssh2.get_transport().open_session()
            chan.get_pty()
            chan.exec_command("ps aux | grep gpio_server")
            out = b""
            while True:
                chunk = chan.recv(4096)
                if not chunk:
                    break
                out += chunk
            chan.recv_exit_status()
            return out.decode("utf-8", errors="replace")

        # Poll for the service — rc.local sleeps 180s after boot, so give it time
        ps_out = ""
        service_found = False
        for poll_attempt in range(12):  # up to ~2 extra minutes
            ps_out = await loop.run_in_executor(None, check_service)
            lines = [l for l in ps_out.splitlines() if "gpio_server" in l and "grep" not in l]
            if lines:
                service_found = True
                break
            await asyncio.sleep(10)

        ssh2.close()

        if service_found:
            yield _event(11, "Verify Service Running", "ok", f"gpio_server is running:\n{lines[0]}")
        else:
            yield _event(11, "Verify Service Running", "error", "gpio_server not found in process list.\n\nRun this on the Pi to check manually:\n  ps aux | grep gpio_server")
    except Exception as e:
        yield _event(11, "Verify Service Running", "error", str(e))
        return

    yield {"data": json.dumps({"step": 99, "name": "Installation Complete", "status": "done", "output": ""})}
