# 8. Wi-Fi and firmware updates

## Do I need Wi-Fi?

No. Over USB the panel gets power, sends its keys, and talks to the app. Wi-Fi is useful
if you want to:

* update the firmware without the USB cable,
* show the panel's data while it's plugged into a charger instead of your computer
  (deck keys need USB, everything else works over Wi-Fi),
* look at it from a script (it has a small HTTP API, see [protocol.md](../protocol.md)).

## What works over USB and Wi-Fi

| How the panel is connected | Works | Doesn't |
|---|---|---|
| **USB cable to this computer** | Everything: deck keys, media keys and macros (instant, in every app, even without the PandaDeck app running), app / file / website buttons, music, HUD, AI approvals, sounds, the ⚙ gear, fast firmware updates | — |
| **Wi-Fi only** (panel on a charger) | Music, HUD, AI approvals, app / file / website buttons, sounds, firmware updates | Keyboard shortcuts, media keys and macros: they need the USB cable |
| **USB cable to another computer** | Keys and macros on *that* computer; music, HUD, AI, app buttons and sounds still come from this one over Wi-Fi | Keys don't reach this computer |
| **Power only, no Wi-Fi** | — | Nothing reaches the computer; the panel keeps its last screen and shows *PC agent offline* |

The app's **Panel** section shows which case you're in, with a **What works how?** link.

## Set up Wi-Fi

With the panel plugged in by USB, open the app's **Panel** section → **Wi-Fi**, pick your
network, type the password and click **Connect**. The password is sent over the USB cable
and stored on the panel only.

If the panel was on your Wi-Fi with the stock firmware, it often reconnects by itself —
PandaDeck reuses the network the stock firmware remembered.

Once connected, the HUD page shows the panel's address at the bottom, and the panel
answers at `http://pandadeck.local` on most networks.

Every Wi-Fi request needs the panel's **token** (a password the panel created for itself
on first start). The app gets it over USB automatically; you'll only need it for scripts —
**Panel → Advanced → Show token**.

## Update the firmware

**From the app** (recommended): **Panel → Firmware**. The app checks for a new release and
installs it over USB (or Wi-Fi). The panel restarts by itself in about 20 seconds.

**From a file:** **Panel → Firmware → Install from file** and pick a `.bin`.

**From the browser:** open `http://<panel>/update`. You'll be asked for the token.

If an update doesn't start properly, the panel automatically goes back to the previous
version on the next restart.
