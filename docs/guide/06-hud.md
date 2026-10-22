# 6. HUD and homelab

The **HUD** page is a live dashboard: CPU, memory, GPU (NVIDIA), temperatures, disk and
network of the computer the panel is plugged into — and, if you have one, a second
computer such as a home server or NAS.

<p align="center"><img src="../images/hud.png" alt="HUD page" width="640"></p>

## Your computer: nothing to set up

The app fills it in by itself. In **HUD → PC name** you can replace the host name with
something friendlier ("Studio PC", "Gaming rig").

### CPU temperature on Windows

Windows doesn't let normal programs read the CPU temperature. If you want it on the
HUD, install the free **[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)**:

1. `winget install LibreHardwareMonitor.LibreHardwareMonitor` (or download it from its page).
2. Start it, then in its menu: **Options → Remote Web Server → Run** (port 8085), and
   **Options → Start Minimized** + **Minimize To Tray** + **Run On Windows Startup**.

PandaDeck finds it automatically. (Linux shows CPU temperature out of the box; macOS
doesn't expose it without admin tools, so the HUD shows the rest.)

## Connect a homelab

The right half of the HUD can show any other computer: a Linux server, a Mac mini, a
Windows box. That computer runs a tiny companion script, **`pandadeck_lab.py`**, which
reports its stats (and its Docker containers, if it has Docker). The settings page has
the same steps with copy buttons under **HUD → Connect a homelab**.

### On the server

You need **Python 3.9+** on that computer.

<details open>
<summary><b>Linux</b> (Ubuntu, Debian, Raspberry Pi OS, Fedora, …)</summary>

```bash
python3 -m pip install --user psutil
mkdir -p ~/pandadeck && cd ~/pandadeck
curl -LO https://github.com/wikydtron/pandadeck/releases/latest/download/pandadeck_lab.py
python3 pandadeck_lab.py
```

The first run prints a **token** — copy it. Keep it running in the background with
systemd:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/pandadeck-lab.service <<'EOF'
[Unit]
Description=PandaDeck homelab stats
[Service]
ExecStart=/usr/bin/python3 %h/pandadeck/pandadeck_lab.py
Restart=always
[Install]
WantedBy=default.target
EOF
systemctl --user enable --now pandadeck-lab
sudo loginctl enable-linger $USER   # keep it running when you're logged out
```

Docker containers show up if your user can run `docker ps` (be in the `docker` group).
</details>

<details>
<summary><b>macOS</b></summary>

```bash
python3 -m pip install --user psutil
mkdir -p ~/pandadeck && cd ~/pandadeck
curl -LO https://github.com/wikydtron/pandadeck/releases/latest/download/pandadeck_lab.py
python3 pandadeck_lab.py
```

Copy the printed **token**. To start it at login, create
`~/Library/LaunchAgents/com.pandadeck.lab.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.pandadeck.lab</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/python3</string><string>/Users/YOUR_NAME/pandadeck/pandadeck_lab.py</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
</dict></plist>
```

then `launchctl load ~/Library/LaunchAgents/com.pandadeck.lab.plist`. When macOS asks
whether Python may accept incoming connections, click **Allow**.
</details>

<details>
<summary><b>Windows</b></summary>

In PowerShell:

```powershell
py -m pip install psutil
mkdir $HOME\pandadeck; cd $HOME\pandadeck
Invoke-WebRequest https://github.com/wikydtron/pandadeck/releases/latest/download/pandadeck_lab.py -OutFile pandadeck_lab.py
py pandadeck_lab.py
```

Copy the printed **token**. To start it at logon:

```powershell
schtasks /Create /TN "PandaDeck lab" /SC ONLOGON /RL LIMITED /TR "pythonw $HOME\pandadeck\pandadeck_lab.py"
```

When Windows Firewall asks, allow Python on **private networks**. For CPU temperatures,
also run LibreHardwareMonitor on that machine (see above).
</details>

### In the app

Open **HUD → Connect a homelab** and enter:

* **Address:** `http://<server address>:47380` — e.g. `http://homeserver.local:47380`
  or its IP address,
* **Token:** the one the script printed (it's also saved next to the script, in
  `pandadeck_lab.token`).

The page shows **Connected: \<server name\>** when it works, and the HUD's right column
fills in. Give it a nicer name in **Server name** if you like.

> **Security:** the script only answers requests that carry the token, and it only
> reports stats — except for one action: restarting a Docker container you hold
> down on the panel. Keep it on your home network; don't expose port 47380 to the
> internet.
