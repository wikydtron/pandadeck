# 4. The deck

The **DECK** page is a touch stream deck. Its tabs (APPS, MEDIA, WINDOWS, …) are pages of
buttons; tap a tab to switch.

<p align="center"><img src="../images/deck.png" alt="Deck page" width="640"></p>

## Your apps

The **APPS** page holds up to 18 apps with their real icons. Tap one to open it (or bring
it to the front).

In the app, open **Deck → Apps**:

* PandaDeck lists the apps installed on your computer, most-used first (Windows ranks them
  by how much you actually use them).
* Click an app to add it, drag to reorder, click the ✕ to remove it.
* **Fill with my most-used apps** picks them for you.
* **Add a file… / folder… / website…** puts anything else on a button: a document, a
  project folder, a script, a game, a web page.
* Click a tile to rename it, pick a colour, or give it **your own picture** (PNG, JPG,
  WebP…). Square pictures get rounded corners automatically.

Changes appear on the panel within a second.

## Button style

**Deck → Button style** changes how the panel draws its buttons:

| Style | Looks like |
|---|---|
| **Classic** | A card with a small icon and the name under it. |
| **Icons** | The icon *is* the button, like a phone's home screen. Buttons without a picture get a coloured tile with their symbol. **Show names under icons** is optional. |

Switch any time — your buttons and pages don't change.

## Built-in pages

Turn on the pages you want under **Deck → Pages**:

| Page | Buttons |
|---|---|
| **MEDIA** | previous, play/pause, next, mute, volume −/+, microphone mute, screenshot, show desktop… |
| **WINDOWS** | snap left/right, maximize, minimize, switch apps, task view, lock the screen… (the right shortcuts for your system: Windows, macOS or Linux) |
| **DEV** | copy, paste, undo, redo, save, find, new terminal… |
| **MACROS** | F13–F24: keys no keyboard has, free for your own shortcuts (see below) |

These buttons are sent by the panel as a real **USB keyboard**, so they work in every
app — even ones that ignore other software — and work without the PandaDeck app running.
(That's also why they need the USB cable: over Wi-Fi only, keyboard buttons can't reach
your computer. See [what works over USB and Wi-Fi](08-wifi-and-updates.md#what-works-over-usb-and-wi-fi).)

## Sounds

The **SOUNDS** tab is a soundboard: tap a button and your computer plays the sound. One
plays at a time: tapping another sound cuts the one playing, and tapping the one that's
playing stops it. It comes with a dozen classic effects — air horn, rimshot, sad
trombone, drum roll… — and every one of them can be changed.

In the app, open **Deck → Sounds**:

* **Show the SOUNDS tab** puts it on the panel.
* Click a sound to rename it, change its picture, colour or volume, or replace it with
  **your own sound**: upload a file (WAV, MP3, OGG or M4A, up to 5 MB), or choose
  **Replace from link…** and paste the address of a sound page or file.
* **Top meme sounds: get the real ones** lists the famous ones with a **Find it**
  button. PandaDeck doesn't ship them (they belong to their creators), but adding one
  takes seconds: find it, copy the page's link, then **Replace from link…**.
* **Stop all sounds** silences everything. On any custom page you can also add a
  **Sound** button, including a *Stop all sounds* one for the panel.

**Pictures come by themselves.** When you get a sound from a link, PandaDeck looks for
that sound's own picture on its page (and ignores the site's logo or generic sharing
image). If the page has none, it checks your **meme catalog**, which learns every
picture you pick yourself, so the same meme gets the same picture next time. When it
isn't sure, the sound shows **picture: choose** with a few thumbnails to pick from. A
picture you chose yourself is never replaced automatically; an automatic one follows
the sound when you replace it. **Find new picture** looks again on demand.

## ✨ Generate a picture with AI

If **Codex** or **Claude Code** is installed and signed in on your computer, every
button's editor has a **✨ Generate with AI** button next to its picture. It makes three
options from the button's name and what it does (the meme, the app, the website…). Add
a style hint if you like (*pixel art*, *cartoon*, *neon*…), pick the one you like, or
press **Again** for new ideas.

| With | You get | Takes |
|---|---|---|
| **Codex** (recommended) | real pictures from Codex's image generation, one idea each: a faithful one, a cartoon one, a dramatic one. Codex first looks the subject up on the web (so a meme is drawn as people know it, not from its words) and works from the button's current picture, or the meme catalog's, when there is one | about 40–90 s |
| **Claude Code** | simple flat vector icons in the button's colour | about 20–60 s |

* It uses your own Codex or Claude subscription, through the command-line tool that's
  already signed in. No API key, nothing to set up.
* Codex runs with its plain defaults (not your own Codex model, plugins or MCP servers),
  with only web search and image generation: no shell, so nothing it reads on the web can
  make it touch your files. The button's current picture is sent along as a reference
  (never a picture the AI made). PandaDeck removes the pictures it made from Codex's
  picture history once it has them.
* Codex limits how many pictures it makes per minute. If it says it's busy, wait a few
  minutes. Claude only writes vector drawings, which PandaDeck
  strips down to plain shapes and draws itself. Neither run appears on your AI page.
* Claude's icons need an SVG renderer, included in the Windows, macOS and Linux
  downloads. Running from source? `pip install "pandadeck[ai-icons]"`.

## Make your own buttons

Under **Deck → Custom pages** you can add pages and buttons. Choose **4 × 3** big
buttons, or a **6 × 3** grid for more apps. Each button has a label, an icon (or your own
picture), a colour and an **action**:

| Action | Does |
|---|---|
| **Keys** | presses a key combination, e.g. `CTRL+SHIFT+ESC`, `GUI+L`, `F13` |
| **Media** | a media key: play/pause, next, previous, mute, volume up/down, stop |
| **App** | opens one of your apps (or brings it to the front) |
| **File, folder, website** | opens a document, a folder or a web page |
| **Sound** | plays a sound on your computer |
| **PandaDeck** | opens the PandaDeck settings, or mutes / unmutes your microphone |

Key names for **Keys**: letters and digits as themselves (`A`, `7`), `F1`–`F24`, `CTRL` `SHIFT` `ALT`
`GUI` (= Windows key / ⌘ Cmd / Super), `RCTRL` `RSHIFT` `RALT` `RGUI`, `UP` `DOWN` `LEFT`
`RIGHT`, `ENTER` `ESC` `BACKSPACE` `TAB` `SPACE` `INSERT` `DELETE` `HOME` `END` `PGUP`
`PGDN` `PRTSC` `MENU` `CAPSLOCK`, and `PERIOD` `COMMA` `MINUS` `EQUAL` `SLASH` `SEMICOLON`
`QUOTE` `BACKSLASH` `GRAVE` `LBRACKET` `RBRACKET`. Up to 6 keys per combination.

### Tip: F13–F24 for OBS, Discord, games…

Keys **F13 to F24** exist in the USB standard but not on normal keyboards, so nothing
else uses them. Assign them as hotkeys in OBS (start/stop recording, switch scene),
Discord (push-to-talk, mute), AutoHotkey, Keyboard Maestro, or your game — then put
those keys on a MACROS page. To set one in another app's hotkey box, click the box and
tap the button on the panel.
