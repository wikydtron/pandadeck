# 1. Put PandaDeck on the panel

**Time:** about 5 minutes · **You need:** the Panda Touch on your Wi-Fi (as it is when used
with a printer) and a computer on the same network.

The stock Panda Touch firmware has a built-in web updater. You'll use it once to install
PandaDeck; after that, updates come through the PandaDeck app.

> **Before you start:** download the latest stock firmware from
> [BIGTREETECH's Panda Touch page](https://github.com/bigtreetech/PandaTouch) and keep it
> somewhere safe. You'll want it if you ever go back to using the panel with a printer.

## Step 1 — Download PandaDeck

Go to the [latest release](../../../../releases/latest) and download
**`pandadeck-firmware.bin`**.

## Step 2 — Find the panel's IP address

On the Panda Touch (stock firmware), open **Settings → Wi-Fi** (sometimes shown as
**WLAN**). The address looks like `192.168.1.57`. Write it down.

## Step 3 — Open the updater

On a computer connected to the same network, open a web browser and go to:

```
http://<the panel's address>/update
```

for example `http://192.168.1.57/update`. You'll see a simple firmware update page.

## Step 4 — Upload

1. Make sure **Firmware** is selected (not *Filesystem*).
2. Choose `pandadeck-firmware.bin`.
3. Wait for the progress bar to finish — about 20 seconds. **Don't unplug the panel.**

The panel restarts by itself. After a few seconds you'll see the PandaDeck tabs at the
top of the screen.

## Step 5 — Plug it into your computer

Connect the panel to your computer with a **USB-C data cable**. The panel now gets its
power, sends keys, and talks to the app over this one cable.

> **Screen upside down?** Depending on how your panel is mounted, the picture may be
> rotated. **Press and hold the clock in the top-right corner for 2 seconds**
> to flip it. The setting is remembered. If touches land in the wrong place after flipping,
> change *Flip touch* in the app's Panel settings.

**Next:** [install the PandaDeck app →](02-install-the-app.md)

---

<details>
<summary><b>Alternative: flash over USB (advanced)</b></summary>

If the web updater isn't available (very old stock firmware, or you already replaced it),
you can flash over USB with Espressif's `esptool`:

1. Install it: `pip install esptool`
2. Put the ESP32-S3 in download mode. The panel's USB-C port is the ESP32-S3's own USB;
   on PandaDeck firmware the app's **Panel → Firmware → Restart into bootloader** button
   does it for you. On other firmware you may need to hold the BOOT button while plugging
   in (it sits under the back cover).
3. Flash the complete image (bootloader + partitions + app) — use the
   **`pandadeck-firmware-factory.bin`** file from the release:
   ```
   esptool --chip esp32s3 write_flash 0x0 pandadeck-firmware-factory.bin
   ```

The factory image uses the same partition layout as the stock firmware.
</details>
