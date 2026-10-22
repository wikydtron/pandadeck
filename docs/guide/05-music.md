# 5. Music

The **MUSIC** page shows what your computer is playing — cover, title, artist, a progress
bar you can drag to seek, and big controls: shuffle, previous, play/pause, next, repeat.
At the top it names the player (*Spotify*, *Chrome*, *VLC*…) with a dot in its colour,
dimmed while paused.

## Background

Choose it in the app under **Music → Background**:

* **Blurred cover** — a soft, darkened version of the album cover.
* **Artist photo** — the artist's picture, looked up by name in Deezer's public catalog
  (no sign-in needed; with Spotify signed in, Spotify's own photos come first). When an
  artist has no photo, the page falls back to the blurred cover.

<p align="center"><img src="../images/music.png" alt="Music page" width="640"></p>

## It just works (no sign-in)

| System | What's supported |
|---|---|
| **Windows** | Anything that shows up in the Windows media overlay: Spotify, Chrome/Edge/Firefox (YouTube, SoundCloud…), Apple Music, VLC, the Media Player app… |
| **macOS** | Spotify and Apple Music. The first time, macOS asks *"PandaDeck wants to control Spotify/Music"* — click **OK**. |
| **Linux** | Every player that speaks MPRIS: Spotify, VLC, Firefox, Chromium, Rhythmbox, Elisa, mpv (with mpv-mpris)… |

Play / pause / next / previous are sent as **media keys** through the USB keyboard, so
they react instantly. Seeking, shuffle and repeat go through the app.

The **play** key on the DECK page also shows the real state: ⏸ while music plays,
▶ when it's paused.

## ❤ Like

The heart in the top-right corner adds the song to your Spotify **Liked Songs** (tap
again to remove it). It works whatever is playing the music — the Spotify app, Spotify in
a browser, … — as long as the song exists on Spotify. It needs the Spotify sign-in below;
until then, tapping the heart opens the Music settings.

## Sign in with Spotify

Signing in is optional. It gives you:

* the **❤ Like** button,
* Spotify's own **artist photos**,
* reliable shuffle / repeat / seek on every system,
* control of Spotify playing on another device (phone, speaker, console).

Spotify asks every app to use its own developer key ("Client ID"). Creating one is free
and takes two minutes:

1. Go to <https://developer.spotify.com/dashboard> and log in with your Spotify account.
2. Click **Create app**.
   * **App name:** `PandaDeck` (anything works)
   * **App description:** `My desk panel`
   * **Redirect URI:** `http://127.0.0.1:47321/spotify/callback` — copy it exactly, then
     click **Add**.
   * **Which API/SDKs are you planning to use?** tick **Web API**.
   * Accept the terms and click **Save**.
3. On the app's page, click **Settings** and copy the **Client ID**.
4. In PandaDeck's settings, open **Music**, paste the Client ID and click
   **Sign in with Spotify**. Your browser opens Spotify's consent page — click **Agree**.

You're done. The Music section now shows *Connected as …*.

> **Signed in before ❤ Like existed?** The Music section shows *Sign in again to enable
> ❤ Like*: Spotify needs your OK to read and change your Liked Songs. Click it once.

> **Why my own Client ID?** Spotify limits shared keys, so each user creating their own is
> the reliable way. The key and your sign-in stay on your computer; PandaDeck never sends
> them anywhere but to Spotify.

To disconnect, click **Sign out** in the Music section (and optionally remove the app in
the Spotify dashboard).
