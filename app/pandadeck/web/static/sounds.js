/* PandaDeck settings page: SOUNDS, the soundboard. The Sounds card edits the
 * built-in SOUNDS page (name, symbol or picture, colour, own sound file,
 * volume); any custom-page button can play a sound too ("Sound" in the button
 * editor). Uses app.js (h, api, S, toast, ...) and deck.js (editing, deckTry, ...). */
"use strict";

const SOUND_MAX_BYTES = 5 * 1024 * 1024;
const SOUND_TAB = "sounds";
// Famous meme sounds: we link to a search for them (they belong to their creators; nothing is bundled).
const MEME_SOUNDS = [
  ["Vine boom", "vine boom"], ["Bruh", "bruh"], ["MLG air horn", "mlg air horn"], ["Emotional damage", "emotional damage"],
  ["Oh no no no (Kreepa)", "oh no no no"], ["Metal pipe falling", "metal pipe falling"], ["FBI open up", "fbi open up"],
  ["Windows XP error", "windows xp error"], ["Among Us role reveal", "among us role reveal"], ["Sad violin", "sad violin"],
  ["Bonk", "bonk"], ["Taco Bell bong", "taco bell bong"],
];
const memeSearch = (q) => "https://www.myinstants.com/en/search/?name=" + encodeURIComponent(q);
let memesOpen = false;

const soundLib = () => S.meta.sound_library || {};
const soundCfg = () => deckCfg().sounds || {};
const soundDefaultVolume = () => (S.meta.sound_page || {}).volume || 80;

/* One sound with its defaults filled in (null when there's no such sound). */
function soundInfo(id) {
  const lib = soundLib()[id];
  const own = soundCfg()[id] || {};
  if (!lib && !own.file) return null;
  return { id, builtin: !!lib, label: own.label || (lib ? lib.label : id), icon: own.icon || (lib ? lib.icon : "audio"),
           defLabel: lib ? lib.label : "", defIcon: lib ? lib.icon : "", color: own.color || "", image: own.image || "",
           file: own.file || "", volume: own.volume ?? soundDefaultVolume(),
           art: { image_auto: !!own.image_auto, image_origin: own.image_origin || "", image_src: own.image_src || "" },
           link: own.link || "" };
}
const pictureChoicesFor = (file) => !!file && ((S.status && S.status.deck && S.status.deck.picture_choices) || []).includes(file);

function allSounds() {
  const ids = [...Object.keys(soundLib()), ...Object.keys(soundCfg()).filter((k) => !soundLib()[k])];
  return ids.map(soundInfo).filter(Boolean);
}

async function playSound(body) {
  try { await api("/api/sounds/play", { body }); } catch (e) { toast(e.message, "err"); }
}
async function stopSounds() {
  try { await api("/api/sounds/stop", { body: {} }); } catch (e) { toast(e.message, "err"); }
}

function describeSound(a) {
  if (a === "agent:sound:stop") return "stop all sounds";
  const s = soundInfo(a.slice(12));
  return s ? "play " + s.label : "play (missing sound)";
}

/* User sounds nothing plays any more are dropped on save (built-in ones stay). */
function prunedSounds(deck) {
  const used = new Set();
  for (const p of deck.custom) for (const b of p.buttons) if (b.action.startsWith("agent:sound:")) used.add(b.action.slice(12));
  deck.sounds = Object.fromEntries(Object.entries(deck.sounds || {}).filter(([k]) => soundLib()[k] || used.has(k)));
  return deck;
}

// ---------------------------------------------------------------- the Sounds card
function buildSounds() {
  const card = $("#sounds-card");
  if (!card || keepFocus(card)) return;
  const pages = deckCfg().pages;
  const onDeck = pages.includes(SOUND_TAB);
  const tiles = Object.keys(soundLib()).slice(0, 12).map((id) => {
    const s = soundInfo(id);
    const pic = s.image ? lazyImg(pictureUrl(s.image), h("span", { class: "ic", text: s.icon }), "pic") : h("span", { class: "ic", text: s.icon });
    const t = h("div", { class: "dbtn sound", role: "button", tabindex: "0", title: "Edit " + s.label,
      onclick: () => editSound(id), onkeydown: (e) => { if (e.key === "Enter") editSound(id); } },
      pic, h("span", { class: "lb", text: s.label }),
      h("span", { class: "ac", text: (s.file ? "your sound" : "built-in") + (s.volume !== soundDefaultVolume() ? ` · ${s.volume}%` : "") }),
      s.image ? h("span", { class: "art-badge " + (s.art.image_auto ? "auto" : "yours"), text: s.art.image_auto ? "auto" : "yours",
                            title: s.art.image_auto ? "Picture found automatically: it follows the sound" : "Your picture: kept when the sound changes" })
        : pictureChoicesFor(s.file) ? h("span", { class: "art-badge choose", text: "picture: choose" }) : null,
      h("button", { type: "button", class: "icon ghost small splay", title: "Play on this computer", "aria-label": "Play " + s.label,
        onclick: (e) => { e.stopPropagation(); playSound({ id }); } }, icon("play")));
    if (s.color) t.style.borderColor = s.color;
    return t;
  });
  setKids(card,
    cardHead("Sounds", "music", "A soundboard on the panel: tap a button and this computer plays the sound. Click one to rename it, give it a picture or colour, change its volume, or use your own sound: a file (WAV, MP3, OGG or M4A, up to 5 MB) or a link to it.",
      h("div", { class: "inline" },
        h("button", { type: "button", class: "ghost small", title: "Add a sources.json of meme pictures to your picture catalog",
          onclick: importMappings }, "Import picture mappings\u2026"),
        h("button", { type: "button", class: "ghost", onclick: stopSounds }, icon("stop"), "Stop all sounds"))),
    tiles.length ? h("div", { class: "btngrid four" }, tiles) : h("p", { class: "hint", text: "The built-in sounds aren't available in this version." }),
    h("div", { class: "inline", style: { marginTop: "14px" } }, h("span", { class: "hint", text: "SOUNDS tab on the panel" }),
      toggle(onDeck, (v) => {
        if (v && !onDeck && pages.length >= MAX_TABS) { toast("The deck already has 6 tabs (the panel's limit). Turn one off first.", "err"); buildSounds(); return; }
        deckSave({ pages: v ? [...pages, SOUND_TAB] : pages.filter((p) => p !== SOUND_TAB) });
      }, { label: "Show the SOUNDS tab" })),
    h("p", { class: "hint", style: { marginTop: "10px" },
      text: "Sounds play on this computer's speakers, one at a time: tapping another one cuts the one playing, and tapping the one that's playing stops it. More sound buttons, or a Stop button for the panel: on any custom page, add a button and choose Sound." }),
    memeList());
}

function memeList() {
  const d = h("details", { class: "more memes", open: memesOpen },
    h("summary", { text: "Top meme sounds: get the real ones" }),
    h("div", { class: "inner" },
      h("p", { class: "hint", text: "PandaDeck doesn't ship these (they belong to their creators), but adding one takes seconds: click Find it, open the sound, copy that page's link, then click one of your sounds above, choose Replace from link… and paste it." }),
      h("div", { class: "meme-list" }, MEME_SOUNDS.map(([name, q]) => h("div", { class: "meme" }, h("span", { text: name }),
        h("button", { type: "button", class: "small ghost", title: "Search for it on myinstants.com (opens a new tab)",
          onclick: () => window.open(memeSearch(q), "_blank", "noopener,noreferrer") }, icon("web"), "Find it"))))));
  d.addEventListener("toggle", () => { memesOpen = d.open; });
  return d;
}

// ---------------------------------------------------------------- editor (the button dialog)
/* editing.sound = {id, file, volume, builtin, isNew}; id "stop" = stop all sounds. */
function soundState(id) {
  if (id === "stop") return { id, file: "", volume: 0, builtin: false };
  if (id === "new" || id === "newlink") return { id: "new", file: "", volume: soundDefaultVolume(), builtin: false, isNew: true };
  const s = soundInfo(id);
  return s ? { id, file: s.file, volume: s.volume, builtin: s.builtin } : { id: "new", file: "", volume: soundDefaultVolume(), isNew: true };
}

function renderSoundFields() {
  const st = editing && editing.sound;
  if (!st) return;
  const stop = st.id === "stop";
  for (const el of ["#b-splay", "#b-supload", "#b-svol-row", "#b-sfile"]) $(el).hidden = stop;
  $("#b-sreset").hidden = stop || !st.builtin || !st.file;
  $("#b-sfile").textContent = st.file ? `Your file (${st.file.split(".").pop().toUpperCase()})` + (st.dirty ? ", not saved yet" : "")
    : st.builtin ? "The built-in sound" : "No sound yet: upload a file or get one from a link.";
  $("#b-slink-btn").hidden = stop;
  $("#b-slink-btn").textContent = st.isNew ? "From a link…" : "Replace from link…";
  if (stop) $("#b-slink").hidden = true;
  $("#b-svol").value = st.volume;
  $("#b-svol").style.setProperty("--fill", st.volume + "%");
  $("#b-svol-v").textContent = st.volume + "%";
}

function fillSoundSelect(selected) {
  const opts = allSounds().map((s) => h("option", { value: s.id, text: s.label + (s.builtin ? "" : " (yours)"), selected: s.id === selected }));
  setKids($("#b-sound"), ...opts,
    h("option", { value: "new", text: "A new sound from a file…", selected: selected === "new" }),
    h("option", { value: "newlink", text: "A new sound from a link…", selected: selected === "newlink" }),
    h("option", { value: "stop", text: "■ Stop all sounds", selected: selected === "stop" }));
}

/* deck.js editButton(): a custom button whose action is (or becomes) a sound. */
function soundFillForButton(action) {
  const id = action.startsWith("agent:sound:") ? action.slice(12) : (Object.keys(soundLib())[0] || "new");
  editing.sound = soundState(id);
  fillSoundSelect(editing.sound.id);
  renderSoundFields();
}

/* The SOUNDS page: edit one built-in sound directly. */
function editSound(id) {
  const s = soundInfo(id);
  if (!s) return;
  editing = { mode: "sound", soundId: id, image: s.image, icon: s.icon, color: s.color, sound: soundState(id),
              art: { ...s.art }, link: s.link, choices: [] };
  $("#b-title").textContent = "Sound";
  setKids($("#b-icons"), ...["", ...(S.meta.icons || [])].map((n) => h("button", {
    type: "button", class: (editing.icon || "") === n ? "on" : "", text: n || "none",
    onclick: (e) => { editing.icon = n; for (const x of $("#b-icons").children) x.classList.toggle("on", x === e.currentTarget); renderPreview(); },
  })));
  fillCommonFields(s.label, s.color);
  $("#b-label").placeholder = s.defLabel;
  $("#b-delete").hidden = true;
  showKindFields();
  renderSoundFields();
  renderArt();
  renderPreview();
  $("#dlg-button").showModal();
}

async function saveSound() {
  const st = editing.sound;
  const s = soundInfo(editing.soundId);
  const label = $("#b-label").value.trim();
  const sounds = { ...soundCfg() };
  sounds[editing.soundId] = { label: label === s.defLabel ? "" : label, icon: editing.icon === s.defIcon ? "" : (editing.icon || ""),
                              color: editing.color || "", image: editing.image || "", file: st.file || "", volume: Number(st.volume),
                              image_auto: !!editing.art.image_auto && !!editing.image, image_origin: editing.art.image_origin || "",
                              image_src: editing.art.image_src || "", link: editing.link || "" };
  await deckTry({ sounds });
}

/* deck.js saveButton(): the action for a "Sound" button, saving the sound's file and volume. */
function soundActionForSave(label) {
  const st = editing.sound;
  if (st.id === "stop") return { action: "agent:sound:stop", sounds: soundCfg() };
  const sounds = { ...soundCfg() };
  let id = st.id;
  if (st.isNew) {
    if (!st.file) throw new Error("Upload a sound file for this button first.");
    id = newId("s");
    sounds[id] = { label: label || "Sound", icon: "", color: "", image: "", file: st.file, volume: Number(st.volume) };
  } else {
    sounds[id] = { ...(sounds[id] || { label: "", icon: "", color: "", image: "" }), file: st.file || "", volume: Number(st.volume) };
  }
  return { action: "agent:sound:" + id, sounds };
}

function wireSoundFields() {
  $("#b-sound").addEventListener("change", () => {
    const fromLink = $("#b-sound").value === "newlink";
    editing.sound = soundState($("#b-sound").value);
    showLinkRow(fromLink);
    const s = soundInfo(editing.sound.id);
    // label and symbol follow the chosen sound until the user types / picks their own
    const label = editing.sound.id === "stop" ? "Stop" : s ? s.label.slice(0, 19) : "";
    if (!$("#b-label").value || $("#b-label").value === editing.autoLabel) { $("#b-label").value = label; editing.autoLabel = label; }
    if (!editing.icon || editing.icon === "bolt" || editing.icon === editing.autoIcon) {
      editing.icon = editing.autoIcon = editing.sound.id === "stop" ? "close" : (s ? s.icon : "audio");
      for (const x of $("#b-icons").children) x.classList.toggle("on", x.textContent === editing.icon);
    }
    renderSoundFields();
    renderPreview();
  });
  $("#b-svol").addEventListener("input", () => {
    editing.sound.volume = Number($("#b-svol").value);
    $("#b-svol").style.setProperty("--fill", editing.sound.volume + "%");
    $("#b-svol-v").textContent = editing.sound.volume + "%";
  });
  $("#b-splay").addEventListener("click", () => {
    const st = editing.sound;
    if (st.file) playSound({ file: st.file, volume: st.volume });
    else if (st.builtin) playSound({ id: st.id, default: true, volume: st.volume });
    else toast("Upload a sound first.", "err");
  });
  $("#b-sreset").addEventListener("click", () => {
    editing.sound.file = ""; editing.sound.dirty = false; editing.link = "";
    renderSoundFields();
    if (editing.mode === "sound") refreshArtwork({});
  });
  $("#b-art-find").addEventListener("click", () => refreshArtwork({ link: editing.link, force: true }));
  $("#b-art-choose").addEventListener("click", showChoices);
  $("#b-slink-btn").addEventListener("click", () => showLinkRow($("#b-slink").hidden));
  $("#b-slink-go").addEventListener("click", importFromLink);
  $("#b-slink-url").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); importFromLink(); } });
  $("#dlg-button").addEventListener("close", () => showLinkRow(false));
  $("#b-supload").addEventListener("click", () => $("#b-sound-file").click());
  $("#b-sound-file").addEventListener("change", async () => {
    const f = $("#b-sound-file").files[0];
    $("#b-sound-file").value = "";
    if (!f) return;
    if (f.size > SOUND_MAX_BYTES) { $("#b-err").textContent = "Sounds can be at most 5 MB."; return; }
    try {
      const r = await api("/api/sounds/upload?name=" + encodeURIComponent(f.name), { raw: await f.arrayBuffer() });
      editing.sound.file = r.file;
      editing.sound.dirty = true;
      $("#b-err").textContent = "";
      if (editing.mode !== "sound" && !$("#b-label").value) $("#b-label").value = f.name.replace(/\.[^.]+$/, "").slice(0, 19);
      if (editing.mode === "sound") editing.link = "";
      renderSoundFields();
      renderPreview();
      if (editing.mode === "sound") refreshArtwork({});
    } catch (e) { $("#b-err").textContent = e.message; }
  });
}

// ---------------------------------------------------------------- import from a link
function showLinkRow(on) {
  $("#b-slink").hidden = !on;
  if (on) {
    $("#b-slink-msg").className = "hint";
    $("#b-slink-msg").textContent = "Paste the link of a sound's page (for example on myinstants.com) or of the sound file itself.";
    setTimeout(() => $("#b-slink-url").focus(), 0);
  } else {
    $("#b-slink-url").value = "";
  }
}

const sizeText = (n) => (n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");

async function importFromLink() {
  const url = $("#b-slink-url").value.trim();
  const msg = $("#b-slink-msg");
  if (!url) { msg.className = "hint err-text"; msg.textContent = "Paste a link first."; return; }
  msg.className = "hint";
  msg.textContent = "Looking at the link…";
  $("#b-slink-go").disabled = true;
  try {
    let st = await api("/api/sounds/fetch", { body: { url } });
    while (st.state === "running") {
      msg.textContent = st.stage === "download" && st.done
        ? `Downloading… ${sizeText(st.done)}${st.total ? " of " + sizeText(st.total) : ""}`
        : st.stage === "download" ? "Downloading the sound…" : "Looking at the link…";
      await new Promise((r) => setTimeout(r, 300));
      st = await api("/api/sounds/fetch");
    }
    if (st.state !== "done") throw new Error(st.error || "Couldn't import that sound.");
    editing.sound.file = st.file;
    editing.sound.dirty = true;
    const label = (st.label || "").slice(0, 19);
    const current = $("#b-label").value;
    const def = editing.mode === "sound" ? (soundInfo(editing.soundId) || {}).defLabel : "";
    if (label && (!current || current === editing.autoLabel || current === def)) { $("#b-label").value = label; editing.autoLabel = label; }
    let host = "";
    try { host = new URL(st.source).hostname; } catch (_) { host = ""; }
    msg.className = "hint ok-text";
    msg.textContent = `Got it: ${String(st.kind || "").toUpperCase()}${host ? " from " + host : ""}. Press ▶ Play to hear it, then Save.`;
    renderSoundFields();
    renderPreview();
    if (editing.mode === "sound") { editing.link = url; refreshArtwork({ link: url }); }
  } catch (e) {
    msg.className = "hint err-text";
    msg.textContent = e.message;
  } finally {
    $("#b-slink-go").disabled = false;
  }
}

// ---------------------------------------------------------------- pictures for sounds (sounds/artwork.py)
const ART_TEXT = {
  page: "Picture: found on the sound's page (automatic)", catalog: "Picture: from your meme catalog (automatic)",
  manual: "Picture: yours, kept when the sound changes",
};

function renderArt(note) {
  const box = $("#b-art");
  if (!box) return;
  box.hidden = !(editing && editing.mode === "sound");
  if (box.hidden) return;
  const a = editing.art || {};
  $("#b-art-status").textContent = note || (editing.image ? (a.image_auto ? ART_TEXT[a.image_origin] || "Picture: automatic" : ART_TEXT.manual)
    : a.image_origin === "manual" ? "No picture: the symbol shows (your choice)" : "No picture: the symbol shows");
  $("#b-art-choose").hidden = !(editing.choices && editing.choices.length) && !pictureChoicesFor(editing.sound.file);
}

/* A sound was imported / replaced (or "Find new picture"): ask for a picture. An automatic picture
   follows the sound (replaced, or removed when nothing is found); the user's own is kept. */
async function refreshArtwork({ link = "", force = false }) {
  const label = $("#b-label").value.trim() || (soundInfo(editing.soundId) || {}).defLabel || "";
  const entry = force ? {} : { image: editing.image, ...editing.art };
  renderArt("Looking for a picture\u2026");
  try {
    const r = await api("/api/sounds/artwork", { body: { label, link: link || "", file: editing.sound.file || "", entry } });
    editing.choices = r.choices || [];
    if (r.entry.image || !force) {
      if (!r.manual || force) {
        editing.image = r.entry.image;
        editing.art = { image_auto: r.entry.image_auto, image_origin: r.entry.image_origin, image_src: r.entry.image_src };
        $("#b-pic-remove").hidden = !editing.image;
      }
    }
    renderPreview();
    renderArt(r.manual && !force ? null : r.status === "choose" ? "Not sure which picture: choose one below" : r.status === "none" ? (force ? "No picture found" : null) : null);
    if (r.status === "choose" && (!r.manual || force)) showChoices();
    else $("#b-art-choices").hidden = true;
  } catch (e) {
    renderArt("Picture look-up failed: " + e.message);
  }
}

async function showChoices() {
  let choices = editing.choices || [];
  if (!choices.length && editing.sound.file) {
    try { choices = (await api("/api/sounds/artwork/choices?file=" + encodeURIComponent(editing.sound.file))).choices || []; } catch (_) { choices = []; }
  }
  const box = $("#b-art-choices");
  const pick = async (c) => {
    try {
      const r = await api("/api/sounds/artwork/choose", { body: { cid: c.cid, file: editing.sound.file || "" } });
      editing.image = r.image;
      editing.art = { image_auto: false, image_origin: "manual", image_src: "" };  // the user's pick: theirs from now on
      editing.choices = [];
      $("#b-pic-remove").hidden = false;
      box.hidden = true;
      renderPreview();
      renderArt();
    } catch (e) { toast(e.message, "err"); }
  };
  setKids(box, ...choices.slice(0, 4).map((c) => h("button", { type: "button", class: "art-choice", title: c.name || "",
      onclick: () => pick(c) }, h("img", { src: c.thumb, alt: "" }), h("span", { text: c.name || "" }))),
    h("button", { type: "button", class: "small ghost", onclick: () => {
      editing.image = ""; editing.art = { image_auto: false, image_origin: "manual", image_src: "" };
      box.hidden = true; $("#b-pic-remove").hidden = true; renderPreview(); renderArt();
    } }, "Keep symbol"),
    h("button", { type: "button", class: "small ghost", onclick: () => $("#b-pic-file").click() }, "Upload\u2026"));
  box.hidden = false;
}

async function importMappings() {
  try {
    const r = await api("/api/sounds/artwork/import", { body: {} });
    if (r.cancelled) return;
    toast(`Picture catalog: ${r.added} added, ${r.updated} updated (${r.entries} memes known)`);
  } catch (e) { toast(e.message, "err"); }
}
