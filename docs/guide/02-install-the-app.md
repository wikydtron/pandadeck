# 2. Install the PandaDeck app

The app runs quietly in the background on your computer. It feeds the panel (music,
stats, your apps) and gives you a settings page to customize everything.

Download the file for your system from the [latest release](../../../../releases/latest):

| System | File |
|---|---|
| Windows 10 / 11 | `pandadeck-app-windows.zip` |
| macOS 12 or newer | `pandadeck-app-macos.zip` |
| Linux (x86-64) | `pandadeck-app-linux.tar.gz` |

## Windows

1. Right-click the zip → **Extract All…** → pick a folder that will stay put, e.g.
   `C:\Program Files\PandaDeck` or `Documents\PandaDeck`.
2. Open the folder and double-click **PandaDeck.exe**.
   * Windows may show *"Windows protected your PC"* because the app isn't signed by a
     big company. Click **More info → Run anyway**.
3. Your browser opens the PandaDeck settings page, and a PandaDeck icon appears in the
   system tray (the ^ arrow next to the clock).

PandaDeck starts with Windows from then on. (You can turn that off in **Startup**.)

## macOS

1. Double-click the zip to unpack it, then drag **PandaDeck.app** into **Applications**.
2. The first time, **right-click PandaDeck.app → Open**, then **Open** again. (macOS
   blocks apps from outside the App Store on a plain double-click.)
3. The settings page opens in your browser and the PandaDeck icon appears in the menu bar.

The first time music is playing, macOS asks whether PandaDeck may control Spotify or
Music — click **OK** so the music page works.

## Linux

```bash
tar -xzf pandadeck-app-linux.tar.gz -C ~/.local/share
~/.local/share/PandaDeck/PandaDeck
```

* To talk to the panel over USB your user needs access to serial ports:
  `sudo usermod -aG dialout $USER` (Fedora/Arch: `uucp`), then log out and back in.
* The tray icon needs your desktop's AppIndicator support (GNOME: the *AppIndicator*
  extension). Without it, PandaDeck still runs — open the settings at
  <http://127.0.0.1:47321>.

### Or with Python (any system)

```bash
pip install "pandadeck[tray,mdns] @ git+https://github.com/wikydtron/pandadeck#subdirectory=app"
pandadeck
```

## Check it works

With the panel plugged in by USB, the settings page shows **Panel: connected over USB**
at the top, and the panel's top bar shows a green dot next to **PC**.

**Tip:** tap the **⚙ gear** in the panel's top bar any time — PandaDeck opens its
settings page on your computer.

**Next:** [make it yours →](03-first-setup.md)
