"use strict";
/* 06 HUD: "Connect a homelab". Copy-paste steps to run pandadeck-lab
   (app/lab_server/pandadeck_lab.py) on a Linux, macOS or Windows server, and a
   live check of the connection. Uses h/icon/seg/pill/toast from app.js. */

// Where servers download the script from (the repository's raw file).
const LAB_SCRIPT_URL = "https://raw.githubusercontent.com/wikydtron/pandadeck/main/app/lab_server/pandadeck_lab.py";
const LAB_PORT = 47380;
let labOs = "linux";

function codeBlock(text) {
  return h("div", { class: "codeblock" },
    h("pre", { class: "code", text }),
    h("button", { type: "button", class: "ghost small", "aria-label": "Copy", onclick: async () => {
      try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (_) { toast("Select the text and copy it", "err"); }
    } }, icon("copy"), "Copy"));
}

const LAB_STEPS = {
  linux: () => [
    ["Install Python 3 and psutil", "Debian, Ubuntu, Raspberry Pi OS:",
      "sudo apt install -y python3 python3-psutil",
      "Fedora: sudo dnf install python3-psutil · Arch: sudo pacman -S python-psutil · elsewhere: python3 -m pip install psutil"],
    ["Download the script", null,
      `mkdir -p ~/pandadeck-lab && curl -fsSL ${LAB_SCRIPT_URL} -o ~/pandadeck-lab/pandadeck_lab.py`],
    ["Run it once", "It prints the address to use and a new token (saved next to the script). Ctrl+C stops it.",
      "python3 ~/pandadeck-lab/pandadeck_lab.py",
      "Docker containers show up when your user may run docker: sudo usermod -aG docker $USER, then log in again."],
    ["Keep it running (systemd)", "Stop the copy from step 3 first, then paste this whole block:",
      `sudo tee /etc/systemd/system/pandadeck-lab.service > /dev/null <<EOF
[Unit]
Description=PandaDeck homelab agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=$USER
ExecStart=/usr/bin/python3 $HOME/pandadeck-lab/pandadeck_lab.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now pandadeck-lab`],
    ["Open the port, if a firewall is on", null, `sudo ufw allow ${LAB_PORT}/tcp`,
      `firewalld: sudo firewall-cmd --permanent --add-port=${LAB_PORT}/tcp && sudo firewall-cmd --reload`],
  ],
  macos: () => [
    ["Install Python 3 and psutil", "Install Python 3 from python.org (or brew install python), then:",
      "python3 -m venv ~/pandadeck-lab/venv && ~/pandadeck-lab/venv/bin/pip install psutil"],
    ["Download the script", null,
      `curl -fsSL ${LAB_SCRIPT_URL} -o ~/pandadeck-lab/pandadeck_lab.py`],
    ["Run it once", "It prints the address to use and a new token (saved next to the script). Ctrl+C stops it. When macOS asks whether Python may accept incoming connections, choose Allow.",
      "~/pandadeck-lab/venv/bin/python ~/pandadeck-lab/pandadeck_lab.py",
      "macOS doesn't report CPU temperature to apps, so that tile stays empty."],
    ["Keep it running (launchd)", "Stop the copy from step 3 first, then paste this whole block. It runs while you're logged in (turn on automatic login for a headless Mac).",
      `mkdir -p ~/Library/LaunchAgents && cat > ~/Library/LaunchAgents/com.pandadeck.lab.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.pandadeck.lab</string>
  <key>ProgramArguments</key><array>
    <string>$HOME/pandadeck-lab/venv/bin/python</string>
    <string>$HOME/pandadeck-lab/pandadeck_lab.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$HOME/pandadeck-lab/lab.log</string>
</dict></plist>
EOF
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pandadeck.lab.plist`],
    ["Firewall", "If the macOS firewall is on, Python must be allowed (System Settings > Network > Firewall > Options).", null],
  ],
  windows: () => [
    ["Install Python 3 and psutil", "In PowerShell (open a new window after the first line):",
      "winget install -e --id Python.Python.3.12\npy -m pip install psutil",
      "If Python is installed for all users, run the pip line in PowerShell as administrator, so the startup task of step 4 finds psutil too."],
    ["Download the script", null,
      `New-Item -ItemType Directory -Force C:\\pandadeck-lab | Out-Null\nInvoke-WebRequest ${LAB_SCRIPT_URL} -OutFile C:\\pandadeck-lab\\pandadeck_lab.py`],
    ["Run it once", "It prints the address to use and a new token (saved next to the script). Ctrl+C stops it.",
      "py C:\\pandadeck-lab\\pandadeck_lab.py",
      "CPU temperature needs LibreHardwareMonitor running with Options > Remote Web Server on."],
    ["Keep it running (Task Scheduler)", "Stop the copy from step 3 first. In PowerShell as administrator, it then starts with Windows, before anyone logs in:",
      `$py = (py -c "import sys; print(sys.executable)").Replace("python.exe", "pythonw.exe")
schtasks /Create /F /TN "PandaDeck lab" /SC ONSTART /RU SYSTEM /RL HIGHEST /TR "\`"$py\`" C:\\pandadeck-lab\\pandadeck_lab.py"
schtasks /Run /TN "PandaDeck lab"`,
      "Docker Desktop runs per user: to see its containers, use /SC ONLOGON /RU $env:USERNAME instead of ONSTART and SYSTEM."],
    ["Open the port", "In PowerShell as administrator:",
      `New-NetFirewallRule -DisplayName "PandaDeck lab" -Direction Inbound -Protocol TCP -LocalPort ${LAB_PORT} -Action Allow`],
  ],
};

function labLive() {
  const lab = (S.status && S.status.hud.lab) || { state: "off" };
  const url = (S.settings.hud.lab_url || "").trim();
  if (lab.state === "ok") return pill("ok", "Connected: " + (lab.host || (lab.data && lab.data.name) || "homelab"));
  if (lab.state === "error") return pill("err", lab.error);
  if (url && !S.secrets.lab_token) return pill("warn", "Address saved; add the token");
  return pill("", url ? "Waiting for the server…" : "Not connected yet");
}

function buildLabHelp() {
  const card = $("#labhelp-card");
  const showToken = { linux: "python3 ~/pandadeck-lab/pandadeck_lab.py --show-token",
                      macos: "~/pandadeck-lab/venv/bin/python ~/pandadeck-lab/pandadeck_lab.py --show-token",
                      windows: "py C:\\pandadeck-lab\\pandadeck_lab.py --show-token" }[labOs];
  const steps = LAB_STEPS[labOs]().map(([title, lead, code, hint]) => h("li", {}, h("div", { class: "lstep" },
    h("b", { text: title }),
    lead ? h("span", { text: lead }) : null,
    code ? codeBlock(code) : null,
    hint ? h("p", { class: "hint", text: hint }) : null)));
  steps.push(h("li", {}, h("div", { class: "lstep" },
    h("b", { text: "Connect" }),
    h("span", { text: `Paste the address (http://<server>:${LAB_PORT}) and the token into the Homelab card above. Lost the token? Show it again with:` }),
    codeBlock(showToken),
    h("div", { class: "lab-live" }, labLive()))));
  setKids(card,
    cardHead("Connect a homelab", "server", "Run the small pandadeck-lab script on any computer that stays on: a NAS, a Raspberry Pi, a spare PC or Mac. It needs Python 3 and psutil; Docker is optional. About five minutes."),
    h("div", { class: "lab-os" }, h("span", { text: "The server runs" }),
      seg("labos", [["linux", "Linux"], ["macos", "macOS"], ["windows", "Windows"]], labOs, (v) => { labOs = v; buildLabHelp(); }, { label: "Server system" })),
    h("ol", { class: "guide blue" }, ...steps),
    h("p", { class: "hint", style: { marginTop: "14px" }, text: `It answers on port ${LAB_PORT} over plain HTTP and refuses every request without the token. Use it on a network you trust.` }));
}

function refreshLabLive() {
  const box = $("#labhelp-card .lab-live");
  if (box) setKids(box, labLive());
}
