# 9. Troubleshooting

## The app says "No panel found"

1. **Use a data cable.** Many USB-C cables only carry power. If the panel lights up but
   your computer doesn't make the "device connected" sound (or `lsusb` / System
   Information doesn't list an *Espressif* device), try another cable.
2. **Plug it straight into the computer**, not through a monitor or a passive hub.
3. **Linux:** your user needs access to serial ports. Run
   `sudo usermod -aG dialout $USER` (Fedora/Arch: `uucp`), then log out and back in.
4. **Another program has the port open** (Arduino IDE serial monitor, a second PandaDeck
   app, …). Close it.
5. Still nothing? In the app's **Panel** section, choose **Wi-Fi** and type the panel's
   address (shown on the panel's HUD page footer when Wi-Fi is connected).

## The picture is upside down / touches land in the wrong place

Press and hold the **clock** in the top-right corner for 2 seconds to rotate
the picture 180°. Touch and picture can be flipped separately: if the picture is right but
touches are mirrored, toggle **Flip touch** in **Panel** settings.

## Lines, flicker or a shifted picture

Unplug the panel for 5 seconds and plug it back in. If it keeps happening, make sure
you're on the latest firmware (**Panel → Firmware**). Very long or thin USB cables can
also cause brown-outs — use a short cable plugged directly into the computer.

## Deck keys don't do anything

The deck keys are sent as a USB keyboard, so they work without the app — but only over
USB, not Wi-Fi. Check the small keyboard icon in the panel's top bar: dim means the
computer hasn't accepted the panel as a keyboard yet. On macOS, the first time you plug
it in, a *Keyboard Setup Assistant* may appear: just close it.

App buttons (launching an app) and music controls need the app running.

## Music page says "Nothing playing"

* **Windows:** Spotify, browsers and most players report to Windows automatically.
* **macOS:** Spotify and Apple Music are supported. The first time, macOS asks whether
  PandaDeck may control them — click **OK** (or enable it later in *System Settings →
  Privacy & Security → Automation*).
* **Linux:** any player that supports MPRIS (Spotify, VLC, Firefox, Chromium, Rhythmbox,
  …) works.
* Or [sign in to Spotify](05-music.md#sign-in-with-spotify), which works on every system.

## The ❤ heart doesn't show

* It only appears while **Spotify** is playing: YouTube, other browsers and other players
  don't get a heart.
* You need to be [signed in to Spotify](05-music.md#sign-in-with-spotify). If the Music
  section says *Sign in again to enable ❤ Like*, click it once to give Spotify's OK.
* The settings page shows *♡ not on Spotify* when the song has no exact match on Spotify.

## Sound buttons are silent

Sounds play on the computer running the PandaDeck app, through its default speakers, so
the app has to be running. Check that computer's volume and output device, and the
sound's own volume slider in **Deck → Sounds**.

## The settings page doesn't open

The app serves it at <http://127.0.0.1:47321>. If something else uses that port, the app
picks the next free one — use the tray menu's **Open settings**, which always knows the
right address.

## Resetting

* **App:** quit it and delete its settings folder:
  Windows `%APPDATA%\PandaDeck`, macOS `~/Library/Application Support/PandaDeck`,
  Linux `~/.config/pandadeck`.
* **Panel:** **Panel → Reset panel settings** in the app (Wi-Fi, pages, brightness).

## Going back to the stock firmware

1. Download the latest stock firmware from
   [BIGTREETECH's Panda Touch repository](https://github.com/bigtreetech/PandaTouch).
2. In the app, open **Panel → Firmware → Install from file** and pick the stock `.bin`.
   (Or, with Wi-Fi set up, upload it at `http://<panel>/update`.)
3. The panel restarts into the stock firmware. Your printer settings are untouched,
   because PandaDeck never erases the panel's data partitions.
