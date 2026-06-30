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
    yield _event(2, "System Update & Upgrade", "running", "This may take a few minutes...")
    try:
        out, code = await loop.run_in_executor(
            None,
            lambda: ssh_run(
                "sudo dpkg --configure -a --force-confdef && "
                "sudo apt-get install -f -y && "
                "sudo apt-get update -y && "
                "sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y"
            ),
        )
        if code != 0:
            yield _event(2, "System Update & Upgrade", "error", out[-500:])
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
    yield _event(10, "Reboot Pi", "running", "Rebooting — waiting 75 seconds for Pi to come back...")
    try:
        await loop.run_in_executor(None, lambda: ssh_run("sudo reboot"))
    except Exception:
        pass  # connection drop on reboot is expected

    ssh.close()
    await asyncio.sleep(75)

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

        ps_out = await loop.run_in_executor(None, check_service)
        ssh2.close()

        lines = [l for l in ps_out.splitlines() if "gpio_server" in l and "grep" not in l]
        if lines:
            yield _event(11, "Verify Service Running", "ok", f"gpio_server is running:\n{lines[0]}")
        else:
            yield _event(11, "Verify Service Running", "error", "gpio_server not found in process list — check logs manually")
    except Exception as e:
        yield _event(11, "Verify Service Running", "error", str(e))
        return

    yield {"data": json.dumps({"step": 99, "name": "Installation Complete", "status": "done", "output": ""})}
