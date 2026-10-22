/* PandaDeck settings page: the Deck editor (APPS tiles, files/folders/websites,
 * macros with a key recorder, built-in and custom pages, button pictures).
 * Uses the helpers and state defined in app.js (h, api, S, save, toast, ...).
 * Text from the OS (app names, paths) only ever goes through textContent. */
"use strict";

const MAX_APPS = 18;
const MAX_TABS = 6;
const MACRO_PAGE = "cmacro";
const COLORS = ["#D97757", "#F2B14B", "#34D399", "#60A5FA", "#C084FC", "#E2675A", "#F0EEE6"];
const DECK_BUILTIN = {
  apps: ["APPS", "Your apps, files, folders and websites, with their real icons."],
  media: ["MEDIA", "Previous, play/pause, next, volume, mic mute, screenshot, show desktop…"],
  windows: ["WINDOWS", "Snap, maximize, switch apps, lock: the right shortcuts for your system."],
  dev: ["DEV", "Copy, paste, undo, redo, save, find, a new terminal…"],
  macros: ["MACROS", "F13–F24: keys no keyboard has, free for OBS, Discord or games."],
  sounds: ["SOUNDS", "A soundboard: air horn, boom, sad trombone, ta-da… Edit them in Sounds, above."],
};
const LAYOUT_CAP = { keys: 12, apps: 18 };
let appQuery = "";

const deckCfg = () => S.settings.deck;
const newId = (prefix) => prefix + Math.random().toString(36).slice(2, 8);
const appById = (id) => S.apps.find((a) => a.id === id);
const pictureUrl = (sha) => "/api/icons/get?id=" + encodeURIComponent(sha);

function targetLabel(t) {
  if (!t) return "";
  if (t.label) return t.label;
  if (t.kind === "url") {
    try { const host = new URL(t.url).hostname; return host.replace(/^www\./, ""); } catch (_) { return "Website"; }
  }
  const base = String(t.path || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || t.path || "File";
  return base.replace(/\.(lnk|url|desktop|app)$/i, "");
}
const targetGlyph = (t) => (t.kind === "folder" ? "folder" : t.kind === "url" ? "web" : "run");
const runsCode = (path) => (S.meta.runs_code || []).some((ext) => String(path).toLowerCase().endsWith(ext));

/* Everything the deck references; targets and tile overrides nothing uses are dropped on save. */
function pruned(deck) {
  const used = new Set(deck.apps.filter((e) => e.startsWith("target:")).map((e) => e.slice(7)));
  for (const p of deck.custom) for (const b of p.buttons) if (b.action.startsWith("target:")) used.add(b.action.slice(7));
  deck.targets = Object.fromEntries(Object.entries(deck.targets || {}).filter(([k]) => used.has(k)));
  const entries = new Set(deck.apps);
  deck.app_overrides = Object.fromEntries(Object.entries(deck.app_overrides || {}).filter(([k]) => entries.has(k)));
  return prunedSounds(deck);
}

async function deckSave(patch, okText) {
  const next = pruned(Object.assign(clone(deckCfg()), patch));
  await save({ deck: next }, okText);
}

async function deckTry(patch) {  // like deckSave, but throws (for dialogs that show the error inline)
  const next = pruned(Object.assign(clone(deckCfg()), patch));
  const r = await api("/api/settings", { body: { deck: next } });
  S.settings = r.settings;
  buildAll();
}

/* The APPS page entries as shown: the user's list, or the automatic suggestions. */
const appsEntries = () => (deckCfg().apps.length ? deckCfg().apps.slice() : S.suggested.slice(0, MAX_APPS));

function entryInfo(entry) {
  const ov = (deckCfg().app_overrides || {})[entry] || {};
  if (entry.startsWith("target:")) {
    const tid = entry.slice(7);
    const t = (deckCfg().targets || {})[tid];
    if (!t) return null;
    return { name: ov.label || targetLabel(t), def: targetLabel(t), color: ov.color, image: ov.image, glyph: targetGlyph(t),
             src: ov.image ? pictureUrl(ov.image) : "/api/targets/icon?id=" + encodeURIComponent(tid), target: t };
  }
  const a = appById(entry);
  if (!a) return null;
  return { name: ov.label || a.short || a.name, def: a.short || a.name, color: ov.color, image: ov.image, glyph: "apps",
           src: ov.image ? pictureUrl(ov.image) : "/api/apps/icon?id=" + encodeURIComponent(entry), app: a };
}

function tileImg(src, fallbackText) {
  const ph = h("span", { class: "ph", text: (fallbackText || "?").slice(0, 1).toUpperCase() });
  if (!src) return ph;
  return lazyImg(src, ph);
}

/* Apps used anywhere on the deck (so Fill never adds duplicates). */
function usedApps() {
  const used = new Set(deckCfg().apps);
  for (const p of deckCfg().custom) for (const b of p.buttons) if (b.action.startsWith("app:")) used.add(b.action.slice(4));
  return used;
}
const nextRanked = (n, used) => S.suggested.filter((id) => !used.has(id) && appById(id)).slice(0, n);

// ---------------------------------------------------------------- files, folders, websites
async function browse(kind) {
  try {
    const r = await api("/api/browse", { body: { kind } });
    if (r.cancelled) return null;
    if (r.runs_code) toast("Heads up: this button will run that file when you tap it.", "ok");
    return { kind, path: r.path, label: "" };
  } catch (e) {
    if (e.status === 501) { toast(e.message, "err"); return openTargetDialog(kind); }
    toast(e.message, "err");
    return null;
  }
}

function openTargetDialog(kind = "url", value = "") {
  const d = $("#dlg-target");
  for (const r of document.querySelectorAll("input[name=tkind]")) r.checked = r.value === kind;
  $("#t-value").value = value;
  $("#t-label").value = "";
  $("#t-err").textContent = "";
  const sync = () => {
    const k = (document.querySelector("input[name=tkind]:checked") || {}).value || "url";
    $("#t-value-label").textContent = k === "url" ? "Website address" : k === "file" ? "Full path of the file" : "Full path of the folder";
    const win = S.meta.os === "windows";
    $("#t-value").placeholder = k === "url" ? "https://"
      : k === "file" ? (win ? "D:\\Projects\\notes.txt" : "~/Documents/notes.txt")
      : (win ? "D:\\Projects" : "~/Documents");
  };
  sync();
  for (const r of document.querySelectorAll("input[name=tkind]")) r.onchange = sync;
  d.returnValue = "";
  d.showModal();
  return new Promise((resolve) => {
    $("#t-form").onsubmit = (e) => {
      if (e.submitter && e.submitter.value !== "save") return;
      e.preventDefault();
      const k = (document.querySelector("input[name=tkind]:checked") || {}).value || "url";
      let v = $("#t-value").value.trim().replace(/^"(.*)"$/, "$1");
      if (k === "url") {
        if (!/^https?:\/\//i.test(v)) v = "https://" + v;
        try { new URL(v); } catch (_) { $("#t-err").textContent = "That doesn't look like a website address."; return; }
      } else if (!v) { $("#t-err").textContent = "Type or paste the full path."; return; }
      d.close();
      resolve(k === "url" ? { kind: "url", url: v, label: $("#t-label").value.trim() } : { kind: k, path: v, label: $("#t-label").value.trim() });
    };
    d.addEventListener("close", () => resolve(null), { once: true });
  });
}

async function addTargetTile(t) {
  if (!t) return;
  const entries = appsEntries();
  if (entries.length >= MAX_APPS) { toast("The APPS page is full (18). Remove a tile, or make a 6 × 3 app grid page below.", "err"); return; }
  const tid = newId("t");
  try {
    await deckTry({ targets: { ...deckCfg().targets, [tid]: t }, apps: [...entries, "target:" + tid] });
    toast(`Added ${targetLabel(t)}`);
  } catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------- APPS page
function buildApps() {
  const card = $("#apps-card");
  if (keepFocus(card) && document.activeElement.type !== "search") return;
  const auto = deckCfg().apps.length === 0;
  const entries = appsEntries();
  const grid = h("div", { class: "appgrid" });
  let dragIdx = null;
  for (let i = 0; i < MAX_APPS; i++) {
    const entry = entries[i];
    const info = entry && entryInfo(entry);
    if (!info) {
      grid.append(h("button", { type: "button", class: "app add", title: "Add an app, file, folder or website",
        onclick: () => { const s = card.querySelector("input[type=search]"); if (s) { s.scrollIntoView({ block: "center", behavior: "smooth" }); s.focus(); } } },
        icon("plus"), h("span", { class: "nm", text: "Add" })));
      continue;
    }
    const nm = h("span", { class: "nm", text: info.name, title: "Click to rename" });
    nm.addEventListener("click", (e) => { e.stopPropagation(); renameInline(nm, entry, info); });
    const tile = h("div", { class: "app" + (auto ? " auto" : ""), draggable: "true", title: info.target ? (info.target.url || info.target.path) : info.name },
      tileImg(info.src, info.name), nm,
      h("button", { type: "button", class: "x edit", title: "Edit", onclick: (e) => { e.stopPropagation(); editTile(entry); } }, icon("pencil")),
      h("button", { type: "button", class: "x", title: `Remove ${info.name}`, onclick: (e) => {
        e.stopPropagation(); deckSave({ apps: entries.filter((x) => x !== entry) });
      } }, "✕"));
    if (info.color) tile.style.boxShadow = `inset 0 0 0 2px ${info.color}`;
    tile.addEventListener("click", () => editTile(entry));
    tile.addEventListener("dragstart", (e) => { dragIdx = i; tile.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", entry); });
    tile.addEventListener("dragend", () => tile.classList.remove("dragging"));
    tile.addEventListener("dragover", (e) => { e.preventDefault(); tile.classList.add("over"); });
    tile.addEventListener("dragleave", () => tile.classList.remove("over"));
    tile.addEventListener("drop", (e) => {
      e.preventDefault(); tile.classList.remove("over");
      if (dragIdx === null || dragIdx === i) return;
      const o = entries.slice(); const [m] = o.splice(dragIdx, 1); o.splice(i, 0, m); deckSave({ apps: o });
    });
    grid.append(tile);
  }

  const search = h("input", { type: "search", placeholder: "Search your apps", value: appQuery, "aria-label": "Search apps" });
  const list = h("div", { class: "catalog" });
  const fill = () => {
    const q = appQuery.toLowerCase();
    const items = S.apps.filter((a) => !q || a.name.toLowerCase().includes(q)).slice(0, 400);
    const top = new Set(S.suggested.slice(0, 12));
    const full = entries.length >= MAX_APPS;
    setKids(list, items.map((a) => h("button", {
      type: "button", class: "capp" + (entries.includes(a.id) ? " chosen" : ""), title: a.name,
      disabled: entries.includes(a.id) || full,
      onclick: () => deckSave({ apps: [...entries, a.id] }),
    }, appImg(a), h("span", { class: "nm", text: a.name }), top.has(a.id) ? h("span", { class: "tag clay", text: "top", title: "One of your most-used apps" }) : null)));
    if (!items.length) list.append(h("p", { class: "hint", text: S.appsReady ? "No app matches." : "Looking for your apps…" }));
  };
  search.addEventListener("input", () => { appQuery = search.value; fill(); });
  fill();
  const room = MAX_APPS - entries.length;
  setKids(card,
    cardHead("Apps page", null, "18 tiles (6 × 3). Click a tile to rename it, give it your own picture or a colour; drag to reorder; ✕ to remove.",
      h("span", { class: "pill" + (room ? "" : " warn"), text: `${entries.length} / ${MAX_APPS}` })),
    auto && entries.length ? h("p", { class: "note", text: "Showing your most-used apps automatically. Change anything and the list becomes yours.", style: { marginBottom: "12px" } }) : null,
    S.appsError ? h("p", { class: "note warn", text: S.appsError, style: { marginBottom: "12px" } }) : null,
    grid,
    h("div", { class: "inline", style: { marginTop: "14px" } },
      h("button", { type: "button", disabled: !room && !auto, onclick: () => {
        const base = deckCfg().apps.length ? deckCfg().apps.slice() : [];
        const used = usedApps();
        const add = nextRanked(MAX_APPS - base.length, used);
        if (!add.length) { toast("No more apps to add", "err"); return; }
        deckSave({ apps: [...base, ...add] }, `Added ${add.length} of your most-used apps`);
      } }, icon("spark"), "Fill with my most-used apps"),
      h("button", { type: "button", disabled: !room, onclick: async () => addTargetTile(await browse("file")) }, icon("upload"), "Add a file…"),
      h("button", { type: "button", disabled: !room, onclick: async () => addTargetTile(await browse("folder")) }, icon("folder"), "Add a folder…"),
      h("button", { type: "button", disabled: !room, onclick: async () => addTargetTile(await openTargetDialog("url")) }, icon("web"), "Add a website…"),
      deckCfg().apps.length ? h("button", { type: "button", class: "ghost", onclick: () => deckSave({ apps: [] }) }, "Clear") : null),
    h("div", { style: { marginTop: "20px" } }, search), list);
}

function renameInline(span, entry, info) {
  const input = h("input", { type: "text", class: "rename", value: info.name, maxlength: 19, "aria-label": "Tile label" });
  span.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const commit = async (keep) => {
    if (done) return;
    done = true;
    const label = input.value.trim();
    span.textContent = keep && label ? label : info.name;
    input.replaceWith(span);  // before saving: a focused field would hold off the rebuild
    if (!keep || label === info.name) return;
    const entries = appsEntries();
    const ov = { ...(deckCfg().app_overrides || {}) };
    ov[entry] = { ...(ov[entry] || {}), label: label === info.def ? "" : label };
    await deckSave({ apps: entries, app_overrides: ov }, "Renamed");
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") commit(true); if (e.key === "Escape") { e.preventDefault(); commit(false); } });
  input.addEventListener("blur", () => commit(true));
}

function appChips() {
  const names = appsEntries().map(entryInfo).filter(Boolean).map((i) => i.name);
  return names.length ? names.map((n) => h("span", { class: "chip", text: n })) : [h("span", { class: "hint", text: "Pick apps above." })];
}

// ---------------------------------------------------------------- button style
const STYLE_SAMPLE = [["#D97757", "Claude"], ["#60A5FA", "Code"], ["#34D399", "Spotify"]];

function stylePreview(id, labels) {
  return h("div", { class: "style-preview " + id, "aria-hidden": "true" }, STYLE_SAMPLE.map(([c, name]) => id === "classic"
    ? h("div", { class: "pv-card" }, h("i", { style: { background: c } }), h("span", { text: name }))
    : h("div", { class: "pv-icon" }, h("i", { style: { background: c } }), labels ? h("span", { text: name }) : null)));
}

function buildStyle() {
  const card = $("#style-card");
  const d = deckCfg();
  const style = d.style === "icons" ? "icons" : "classic";
  const labels = d.labels !== false;
  const choice = (id, title, desc) => h("button", {
    type: "button", class: "style-choice" + (style === id ? " on" : ""), "aria-pressed": String(style === id),
    onclick: () => { if (style !== id) deckSave({ style: id }, id === "icons" ? "Icons style: the panel updates in a moment" : "Classic style"); } },
    stylePreview(id, id === "classic" || labels), h("b", { text: title }), h("span", { class: "hint", text: desc }));
  setKids(card,
    cardHead("Button style", null, "How the panel's DECK page draws its buttons. Switch any time; nothing else changes."),
    h("div", { class: "style-choices" },
      choice("classic", "Classic", "A card with a small icon and the name under it."),
      choice("icons", "Icons", "The icon is the whole button, like a phone's home screen. Buttons without an icon get a coloured tile with their symbol.")),
    h("div", { class: "inline", style: { marginTop: "14px" } },
      toggle(labels, (v) => deckSave({ labels: v }), { disabled: style !== "icons", label: "Show names under icons" }),
      h("span", { class: style === "icons" ? "" : "hint", text: "Show names under icons" }),
      style !== "icons" ? h("span", { class: "hint", text: "(Icons style only: Classic always shows names)" }) : null));
}

// ---------------------------------------------------------------- macros
function macroPage() { return deckCfg().custom.find((p) => p.id === MACRO_PAGE); }

function buildMacros() {
  const card = $("#macros-card");
  if (keepFocus(card)) return;
  const page = macroPage();
  const pages = deckCfg().pages;
  const onDeck = pages.includes(MACRO_PAGE);
  const tiles = page ? page.buttons.map((b, i) => buttonTile(b, () => editButton(MACRO_PAGE, i))) : [];
  const canAdd = !page || page.buttons.length < LAYOUT_CAP[page.layout || "keys"];
  setKids(card,
    cardHead("Macros", "bolt", "A macro is a key combination the panel types for you, like a real keyboard: Ctrl+Shift+M to mute in Teams, F13 for an OBS scene, Win+Shift+S for a screenshot… Your macros appear on the MACROS tab of the panel's DECK page.",
      h("button", { type: "button", class: "primary", disabled: !canAdd, onclick: () => editButton(MACRO_PAGE, -1) }, icon("plus"), "Add macro")),
    page ? h("div", { class: "btngrid" }, tiles, canAdd ? h("button", { type: "button", class: "dbtn add", onclick: () => editButton(MACRO_PAGE, -1) }, icon("plus"), h("span", { class: "lb", text: "Add macro" })) : null)
      : h("p", { class: "hint", text: "No macros yet. Click Add macro, press Record and press the keys you want." }),
    page ? h("div", { class: "inline", style: { marginTop: "14px" } }, h("span", { class: "hint", text: "MACROS tab on the panel" }),
      toggle(onDeck, (v) => {
        if (v && !onDeck && pages.length >= MAX_TABS) { toast("The deck already has 6 tabs (the panel's limit). Turn one off first.", "err"); buildMacros(); return; }
        deckSave({ pages: v ? [...pages.filter((p) => p !== "macros"), MACRO_PAGE] : pages.filter((p) => p !== MACRO_PAGE) });
      }, { label: "Show the MACROS tab" })) : null,
    h("p", { class: "hint", text: "Tip: F13–F24 are keys no keyboard has. Pick one, then assign it as the hotkey in OBS, Discord or your game by clicking the hotkey box and tapping the button on the panel.", style: { marginTop: "10px" } }),
    usbKeysNote());
}

function buttonPicture(b) {
  const glyph = h("span", { class: "ic", text: b.icon || "–" });
  if (b.image) return lazyImg(pictureUrl(b.image), glyph, "pic");
  if (b.action.startsWith("app:")) return lazyImg("/api/apps/icon?id=" + encodeURIComponent(b.action.slice(4)), glyph, "pic");
  if (b.action.startsWith("target:")) return lazyImg("/api/targets/icon?id=" + encodeURIComponent(b.action.slice(7)), glyph, "pic");
  return glyph;
}

function buttonTile(b, onclick) {
  const t = h("button", { type: "button", class: "dbtn", onclick },
    buttonPicture(b),
    h("span", { class: "lb", text: b.label || "(no label)" }), h("span", { class: "ac", text: describeAction(b.action) }));
  if (b.color) t.style.borderColor = b.color;
  return t;
}

function describeAction(a) {
  if (!a) return "";
  if (a.startsWith("app:")) { const app = appById(a.slice(4)); return "open " + (app ? app.short || app.name : "app"); }
  if (a.startsWith("target:")) { const t = (deckCfg().targets || {})[a.slice(7)]; return t ? "open " + targetLabel(t) : "open (missing)"; }
  if (pdAction(a)) return pdAction(a)[1].toLowerCase();
  if (a.startsWith("agent:sound:")) return describeSound(a);
  return a.replace(/^keys:/, "");
}

// ---------------------------------------------------------------- built-in pages
function tabsFull(extra = 1) { return deckCfg().pages.length + extra > MAX_TABS; }

function buildDeckPages() {
  const card = $("#dpages-card");
  const pages = deckCfg().pages;
  const full = pages.length >= MAX_TABS;
  const set = (id, on) => {
    if (on && full) { toast("The deck already has 6 tabs (the panel's limit). Turn one off first.", "err"); buildDeckPages(); return; }
    const p = pages.filter((x) => x !== id);
    if (on) id === "apps" ? p.unshift(id) : p.push(id);
    deckSave({ pages: p });
  };
  const osName = { windows: "Windows", macos: "macOS", linux: "Linux" }[S.meta.os] || "";
  const tiles = Object.entries(DECK_BUILTIN).map(([id, [name, desc]]) => {
    const on = pages.includes(id);
    const bp = (S.meta.builtin_pages || {})[id];
    const copied = id !== "apps" && deckCfg().custom.some((c) => c.name === name || (id === "macros" && c.id === MACRO_PAGE));
    return h("div", { class: "dpage" },
      h("div", { class: "top" }, h("div", {}, h("b", { class: "mono", text: name }),
        copied ? h("span", { class: "tag clay", text: "your copy below", style: { marginLeft: "8px" } })
          : id !== "apps" && id !== "macros" && id !== "sounds" && osName ? h("span", { class: "tag", text: "for " + osName, style: { marginLeft: "8px" } }) : null),
        h("div", { class: "inline" },
          bp ? h("button", { type: "button", class: "small ghost", title: "Make an editable copy", onclick: () => customize(id) }, icon("pencil"), "Customize") : null,
          toggle(on, (v) => set(id, v), { disabled: !on && full, label: `Show ${name}` }))),
      h("span", { class: "hint", text: desc }),
      bp ? h("div", { class: "chips" }, bp.buttons.slice(0, 12).map((b) => h("span", { class: "chip", text: b.label, title: b.action })))
         : id === "apps" ? h("div", { class: "chips" }, appChips())
         : id === "sounds" ? h("div", { class: "chips" }, allSounds().filter((x) => x.builtin).map((x) => h("span", { class: "chip", text: x.label }))) : null);
  });
  const order = pages.map((id, i) => {
    const custom = deckCfg().custom.find((c) => c.id === id);
    const name = custom ? custom.name : (DECK_BUILTIN[id] || [id])[0];
    const move = (d) => { const o = pages.slice(); const j = i + d; if (j < 0 || j >= o.length) return; [o[i], o[j]] = [o[j], o[i]]; deckSave({ pages: o }); };
    return h("span", { class: "chip order" }, name,
      h("button", { type: "button", class: "icon ghost small", title: "Move left", disabled: i === 0, onclick: () => move(-1) }, icon("left")),
      h("button", { type: "button", class: "icon ghost small", title: "Move right", disabled: i === pages.length - 1, onclick: () => move(1) }, icon("right")));
  });
  setKids(card,
    cardHead("Pages", null, "Built-in pages are sent by the panel as a real USB keyboard, so they work in every app, even without PandaDeck running. Customize makes an editable copy.",
      h("span", { class: "pill" + (full ? " warn" : ""), text: `${pages.length} / ${MAX_TABS} tabs` })),
    full ? h("p", { class: "note warn", text: "The DECK page shows at most 6 tabs (the panel's limit). Turn one off to add another.", style: { marginBottom: "12px" } }) : null,
    usbKeysNote(),
    h("div", { class: "dpages" }, tiles),
    pages.length ? h("div", { style: { marginTop: "16px" } }, h("p", { class: "hint", text: "Order of the tabs on the DECK page:", style: { marginBottom: "8px" } }), h("div", { class: "inline" }, order)) : null);
}

async function customize(id) {
  const bp = (S.meta.builtin_pages || {})[id];
  if (!bp) return;
  const pid = id === "macros" ? MACRO_PAGE : newId("c");
  if (deckCfg().custom.some((c) => c.id === pid)) { toast("Your MACROS page is already editable: see Macros above.", "ok"); return; }
  if (deckCfg().custom.length >= 6) { toast("You already have 6 custom pages.", "err"); return; }
  const copy = { id: pid, name: bp.name, color: bp.color || "#D97757", layout: bp.buttons.length > 12 ? "apps" : "keys",
                 buttons: bp.buttons.map((b) => ({ label: b.label, icon: b.icon || "", color: "", image: "", action: b.action })) };
  const pages = deckCfg().pages;
  const idx = pages.indexOf(id);
  let next = pages.slice();
  if (idx >= 0) next[idx] = pid; else if (!tabsFull()) next.push(pid);
  try {
    await deckTry({ custom: [...deckCfg().custom, copy], pages: next });
    toast(`${bp.name} is now yours to edit` + (id === "macros" ? " (Macros, above)." : " (Custom pages, below)."));
  } catch (e) { toast(e.message, "err"); }
}

// ---------------------------------------------------------------- custom pages
function buildCustom() {
  const card = $("#custom-card");
  if (keepFocus(card)) return;
  const custom = deckCfg().custom;
  const pages = deckCfg().pages;
  const commit = (c, p) => deckSave({ custom: c, pages: p || pages });
  const blocks = custom.map((pg, pi) => {
    if (pg.id === MACRO_PAGE) return null;  // edited in the Macros card
    const layout = pg.layout || "keys";
    const cap = LAYOUT_CAP[layout];
    const name = h("input", { type: "text", value: pg.name, maxlength: 11, "aria-label": "Page name", spellcheck: false });
    name.addEventListener("change", () => { const c = clone(custom); c[pi].name = name.value.toUpperCase(); commit(c); });
    const swatches = COLORS.slice(0, 6).map((col) => {
      const b = h("button", { type: "button", class: "swatch" + (pg.color === col ? " on" : ""), title: col, "aria-label": `Colour ${col}`,
        onclick: () => { const c = clone(custom); c[pi].color = col; commit(c); } });
      b.style.background = col;
      return b;
    });
    const onDeck = pages.includes(pg.id);
    const tiles = pg.buttons.map((b, bi) => buttonTile(b, () => editButton(pg.id, bi)));
    const adds = [];
    for (let i = pg.buttons.length; i < Math.min(cap, pg.buttons.length + (layout === "apps" ? cap : 1)); i++) {
      adds.push(h("button", { type: "button", class: "dbtn add", onclick: () => editButton(pg.id, -1) }, icon("plus"), h("span", { class: "lb", text: "Add" })));
      if (layout === "keys") break;
    }
    return h("div", { class: "cpage" },
      h("div", { class: "cpage-head" }, name, h("div", { class: "inline" }, swatches),
        seg("lay-" + pg.id, [["keys", "Keys 4 × 3"], ["apps", "App grid 6 × 3"]], layout, (v) => {
          if (v === "keys" && pg.buttons.length > 12) { toast("This page has more than 12 buttons: remove some first.", "err"); buildCustom(); return; }
          const c = clone(custom); c[pi].layout = v; commit(c);
        }, { label: "Layout" }),
        h("span", { style: { marginLeft: "auto" }, class: "inline" },
          layout === "apps" ? h("button", { type: "button", class: "small", disabled: pg.buttons.length >= cap, onclick: () => {
            const add = nextRanked(cap - pg.buttons.length, usedApps());
            if (!add.length) { toast("No more apps to add", "err"); return; }
            const c = clone(custom);
            c[pi].buttons.push(...add.map((id) => ({ label: (appById(id).short || appById(id).name).slice(0, 19), icon: "", color: "", image: "", action: "app:" + id })));
            commit(c);
          } }, icon("spark"), "Fill") : null,
          h("span", { class: "hint", text: "On deck" }),
          toggle(onDeck, (v) => {
            if (v && !onDeck && tabsFull()) { toast("The deck already has 6 tabs (the panel's limit). Turn one off first.", "err"); buildCustom(); return; }
            commit(custom, v ? [...pages, pg.id] : pages.filter((x) => x !== pg.id));
          }, { label: "On deck" }),
          h("button", { type: "button", class: "icon ghost", title: "Delete page", onclick: async () => {
            if (!(await confirmBox(`Delete ${pg.name}?`, "Its buttons are deleted too.", "Delete", true))) return;
            commit(custom.filter((_, i) => i !== pi), pages.filter((x) => x !== pg.id));
          } }, icon("x")))),
      h("div", { class: layout === "apps" ? "btngrid six" : "btngrid four" }, tiles, adds),
      h("p", { class: "hint", text: `${pg.buttons.length} / ${cap} buttons` }));
  }).filter(Boolean);
  setKids(card,
    cardHead("Custom pages", null, "Your own pages: key combinations, media keys, apps, files, folders and websites. Choose 4 × 3 big buttons, or a 6 × 3 app grid for more apps.",
      h("button", { type: "button", disabled: custom.length >= 6, onclick: () => {
        const id = newId("c");
        const on = !tabsFull();
        if (!on) toast("Added, but not on the deck yet: it already has 6 tabs (the panel's limit).", "err");
        commit([...custom, { id, name: "MY KEYS", color: "#34D399", layout: "keys", buttons: [] }], on ? [...pages, id] : pages);
      } }, icon("plus"), "Add page")),
    blocks.length ? h("div", { class: "stack" }, blocks) : h("p", { class: "hint", text: "No custom pages yet. Add one, or Customize a built-in page above." }));
}

// ---------------------------------------------------------------- the button / tile editor
const CODE_MAP = {
  ArrowUp: "UP", ArrowDown: "DOWN", ArrowLeft: "LEFT", ArrowRight: "RIGHT", Enter: "ENTER", NumpadEnter: "ENTER",
  Escape: "ESC", Backspace: "BACKSPACE", Tab: "TAB", Space: "SPACE", Insert: "INSERT", Delete: "DELETE", Home: "HOME",
  End: "END", PageUp: "PGUP", PageDown: "PGDN", PrintScreen: "PRTSC", ContextMenu: "MENU", CapsLock: "CAPSLOCK",
  Period: "PERIOD", Comma: "COMMA", Minus: "MINUS", Equal: "EQUAL", Slash: "SLASH", Semicolon: "SEMICOLON", Quote: "QUOTE",
  Backslash: "BACKSLASH", Backquote: "GRAVE", BracketLeft: "LBRACKET", BracketRight: "RBRACKET",
  NumpadDecimal: "PERIOD", NumpadSubtract: "MINUS", NumpadDivide: "SLASH", NumpadMultiply: "*",
};
const MOD_MAP = { ControlLeft: "CTRL", ControlRight: "RCTRL", ShiftLeft: "SHIFT", ShiftRight: "RSHIFT", AltLeft: "ALT",
                  AltRight: "RALT", MetaLeft: "GUI", MetaRight: "RGUI", OSLeft: "GUI", OSRight: "RGUI" };
const MOD_ORDER = ["CTRL", "SHIFT", "ALT", "GUI", "RCTRL", "RSHIFT", "RALT", "RGUI"];

function codeToKey(code) {
  if (MOD_MAP[code]) return MOD_MAP[code];
  let m = /^Key([A-Z])$/.exec(code); if (m) return m[1];
  m = /^(?:Digit|Numpad)(\d)$/.exec(code); if (m) return m[1];
  m = /^F(\d{1,2})$/.exec(code); if (m && +m[1] >= 1 && +m[1] <= 24) return code;
  return CODE_MAP[code] || null;
}

function comboString(keys) {
  const mods = MOD_ORDER.filter((k) => keys.includes(k));
  const rest = keys.filter((k) => !MOD_ORDER.includes(k));
  return [...mods, ...rest].slice(0, 6).join("+");
}

const rec = { on: false, held: [], down: new Set() };
function stopRecording(apply) {
  if (!rec.on) return;
  rec.on = false;
  $("#b-record").textContent = "Record";
  $("#b-record").classList.remove("recording");
  if (apply && rec.held.length) $("#b-keys").value = comboString(rec.held);
}
function onRecordKey(e) {
  if (!rec.on) return;
  e.preventDefault();
  e.stopPropagation();
  const k = codeToKey(e.code);
  if (e.type === "keydown") {
    if (!k) return;
    rec.down.add(e.code);
    if (!rec.held.includes(k) && rec.held.length < 6) rec.held.push(k);
    $("#b-keys").value = comboString(rec.held) + "…";
    if (!MOD_ORDER.includes(k)) stopRecording(true);  // a real key ends the combo
  } else {
    rec.down.delete(e.code);
    if (!rec.down.size && rec.held.length) stopRecording(true);  // modifiers alone (e.g. just GUI)
  }
}

let editing = null;   // {mode: "button"|"tile", pageId, idx, entry, image, icon, color}

function setPicture(sha) {
  editing.image = sha || "";
  if (editing.mode === "sound") {  // uploaded or removed by hand: never replaced automatically
    editing.art = { image_auto: false, image_origin: "manual", image_src: "" };
    if ($("#b-art-choices")) $("#b-art-choices").hidden = true;
    if (typeof renderArt === "function") renderArt();
  }
  $("#b-pic-remove").hidden = !editing.image;
  renderPreview();
}

function renderPreview() {
  const box = $("#b-preview");
  const label = $("#b-label").value || "Label";
  let pic;
  if (editing.image) pic = h("img", { src: pictureUrl(editing.image), alt: "" });
  else if (editing.mode === "tile") pic = tileImg(editing.src, label);
  else pic = h("span", { class: "glyph", text: editing.icon || "–" });
  setKids(box, pic, h("span", { class: "lb", text: label }));
  box.style.borderColor = editing.color || "";
}

// "PandaDeck" button kind: things this app does (sent to it as agent:<cmd>).
const PD_ACTIONS = [
  ["agent:settings", "Open PandaDeck settings", "settings"],
  ["agent:mic", "Mute / unmute the microphone", "mic_off"],
];
const pdAction = (a) => PD_ACTIONS.find((x) => x[0] === a);

function kindNow() { return (document.querySelector("input[name=bkind]:checked") || {}).value || "keys"; }
function tkindNow() { return (document.querySelector("input[name=btkind]:checked") || {}).value || "file"; }

function showKindFields() {
  const soundMode = editing && editing.mode === "sound";
  const tile = (editing && editing.mode === "tile") || soundMode;
  const k = kindNow();
  $("#b-kind-f").hidden = tile;
  $("#b-sound-f").hidden = !(soundMode || (!tile && k === "sound"));
  $("#b-sound").hidden = soundMode;
  $("#b-keys-f").hidden = tile || k !== "keys";
  $("#b-media-f").hidden = tile || k !== "media";
  $("#b-app-f").hidden = tile || k !== "app";
  $("#b-pd-f").hidden = tile || k !== "pd";
  $("#b-target-f").hidden = tile || k !== "target";
  $("#b-agent-f").hidden = tile || k !== "agent";
  $("#b-icons-f").hidden = tile && !soundMode;
  const tk = tkindNow();
  $("#b-tbrowse").hidden = tk === "url";
  $("#b-tvalue").placeholder = tk === "url" ? "https://" : tk === "file" ? "Full path of the file" : "Full path of the folder";
  $("#b-twarn").hidden = !(k === "target" && tk === "file" && runsCode($("#b-tvalue").value));
}

function fillCommonFields(label, color) {
  $("#b-label").value = label || "";
  setKids($("#b-colors"), ["", ...COLORS].map((c) => {
    const s = h("button", { type: "button", class: "swatch" + ((editing.color || "") === c ? " on" : ""), title: c || "default", "aria-label": c || "default colour",
      onclick: (e) => { editing.color = c; for (const x of $("#b-colors").children) x.classList.toggle("on", x === e.currentTarget); renderPreview(); } });
    s.style.background = c || "linear-gradient(135deg, #2e2c28 45%, #45423b 55%)";
    return s;
  }));
  $("#b-err").textContent = "";
  $("#b-pic-remove").hidden = !editing.image;
  if (typeof aiReset === "function") aiReset();
}

function editButton(pageId, idx) {
  const page = deckCfg().custom.find((p) => p.id === pageId);
  const isMacro = pageId === MACRO_PAGE;
  const b = page && idx >= 0 ? clone(page.buttons[idx]) : { label: "", icon: isMacro ? "bolt" : "bolt", color: "", image: "", action: "keys:" };
  editing = { mode: "button", pageId, idx, image: b.image || "", icon: b.icon || "", color: b.color || "" };
  $("#b-title").textContent = isMacro ? (idx >= 0 ? "Macro" : "New macro") : "Button";
  let kind = pdAction(b.action) ? "pd" : b.action.startsWith("agent:sound:") ? "sound" : (b.action.split(":")[0] || "keys");
  if (!["keys", "media", "app", "target", "agent", "pd", "sound"].includes(kind)) kind = "agent";
  if (isMacro && idx < 0) kind = "keys";
  for (const r of document.querySelectorAll("input[name=bkind]")) r.checked = r.value === kind;
  $("#b-keys").value = kind === "keys" ? b.action.slice(5) : "";
  setKids($("#b-media"), ["playpause", "next", "prev", "mute", "volup", "voldown", "stop"].map((m) => h("option", { value: m, text: m, selected: b.action === "media:" + m })));
  const appId = kind === "app" ? b.action.slice(4) : "";
  setKids($("#b-app"), h("option", { value: "", text: S.apps.length ? "Pick an app…" : "No apps found on this computer" }),
    ...S.apps.map((a) => h("option", { value: a.id, text: a.name, selected: a.id === appId })));
  const t = kind === "target" ? (deckCfg().targets || {})[b.action.slice(7)] : null;
  editing.tid = t ? b.action.slice(7) : "";
  for (const r of document.querySelectorAll("input[name=btkind]")) r.checked = r.value === (t ? t.kind : "file");
  $("#b-tvalue").value = t ? (t.url || t.path) : "";
  $("#b-agent").value = kind === "agent" ? b.action : "";
  setKids($("#b-pd"), ...PD_ACTIONS.map(([a, text]) => h("option", { value: a, text, selected: a === b.action })));
  soundFillForButton(b.action);
  setKids($("#b-icons"), ...["", ...(S.meta.icons || [])].map((n) => h("button", {
    type: "button", class: (editing.icon || "") === n ? "on" : "", text: n || "none",
    onclick: (e) => { editing.icon = n; for (const x of $("#b-icons").children) x.classList.toggle("on", x === e.currentTarget); renderPreview(); },
  })));
  setKids($("#b-fkeys"), Array.from({ length: 12 }, (_, i) => `F${13 + i}`).map((f) => h("button", { type: "button", class: "chip pick", text: f,
    onclick: () => { $("#b-keys").value = f; if (!$("#b-label").value) $("#b-label").value = f; renderPreview(); } })));
  fillCommonFields(b.label, b.color);
  $("#b-delete").hidden = idx < 0;
  $("#b-delete").textContent = isMacro ? "Delete macro" : "Delete button";
  showKindFields();
  renderPreview();
  $("#dlg-button").showModal();
}

function editTile(entry) {
  const info = entryInfo(entry);
  if (!info) return;
  const ov = (deckCfg().app_overrides || {})[entry] || {};
  editing = { mode: "tile", entry, image: ov.image || "", color: ov.color || "", src: info.target ? "/api/targets/icon?id=" + encodeURIComponent(entry.slice(7)) : "/api/apps/icon?id=" + encodeURIComponent(entry), def: info.def };
  $("#b-title").textContent = info.target ? (info.target.kind === "url" ? "Website tile" : info.target.kind === "folder" ? "Folder tile" : "File tile") : "App tile";
  fillCommonFields(ov.label || info.def, ov.color);
  $("#b-label").placeholder = info.def;
  $("#b-delete").hidden = false;
  $("#b-delete").textContent = "Remove from page";
  showKindFields();
  renderPreview();
  $("#dlg-button").showModal();
}

function wireButtonDialog() {
  const pdDefaults = () => {
    const p = kindNow() === "pd" && pdAction($("#b-pd").value);
    if (!p) return;
    if (!$("#b-label").value) $("#b-label").value = p[0] === "agent:settings" ? "Settings" : "Mic";
    if (!editing.icon || editing.icon === "bolt") {  // still the new-button default
      editing.icon = p[2];
      for (const x of $("#b-icons").children) x.classList.toggle("on", x.textContent === p[2]);
    }
    renderPreview();
  };
  for (const r of document.querySelectorAll("input[name=bkind], input[name=btkind]")) r.addEventListener("change", showKindFields);
  for (const r of document.querySelectorAll("input[name=bkind]")) r.addEventListener("change", pdDefaults);
  for (const r of document.querySelectorAll("input[name=bkind]")) r.addEventListener("change", () => {
    if (kindNow() === "sound") $("#b-sound").dispatchEvent(new Event("change"));
  });
  $("#b-pd").addEventListener("change", pdDefaults);
  $("#b-tvalue").addEventListener("input", showKindFields);
  $("#b-label").addEventListener("input", () => renderPreview());
  wireSoundFields();
  if (typeof wireAiIcons === "function") wireAiIcons();
  $("#b-app").addEventListener("change", () => {
    const a = appById($("#b-app").value);
    if (a && !$("#b-label").value) { $("#b-label").value = (a.short || a.name).slice(0, 19); renderPreview(); }
  });
  $("#b-tbrowse").addEventListener("click", async () => {
    const t = await browse(tkindNow());
    if (t) { $("#b-tvalue").value = t.path; if (!$("#b-label").value) $("#b-label").value = targetLabel(t).slice(0, 19); showKindFields(); renderPreview(); }
  });
  $("#b-record").addEventListener("click", () => {
    if (rec.on) { stopRecording(false); return; }
    rec.on = true; rec.held = []; rec.down = new Set();
    $("#b-keys").value = "Press the keys…";
    $("#b-record").textContent = "Stop";
    $("#b-record").classList.add("recording");
  });
  document.addEventListener("keydown", onRecordKey, true);
  document.addEventListener("keyup", onRecordKey, true);
  $("#dlg-button").addEventListener("cancel", (e) => { if (rec.on) e.preventDefault(); });
  $("#dlg-button").addEventListener("close", () => stopRecording(false));
  $("#b-pic-btn").addEventListener("click", () => $("#b-pic-file").click());
  $("#b-pic-file").addEventListener("change", async () => {
    const f = $("#b-pic-file").files[0];
    $("#b-pic-file").value = "";
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { $("#b-err").textContent = "Pictures can be at most 2 MB."; return; }
    try { setPicture((await api("/api/icons", { raw: await f.arrayBuffer() })).id); $("#b-err").textContent = ""; }
    catch (e) { $("#b-err").textContent = e.message; }
  });
  $("#b-pic-remove").addEventListener("click", () => setPicture(""));
  $("#b-delete").addEventListener("click", async () => {
    try {
      if (editing.mode === "tile") {
        await deckTry({ apps: appsEntries().filter((x) => x !== editing.entry) });
      } else {
        const c = clone(deckCfg().custom);
        const page = c.find((p) => p.id === editing.pageId);
        page.buttons.splice(editing.idx, 1);
        await deckTry({ custom: c });
      }
      $("#dlg-button").close();
    } catch (e) { $("#b-err").textContent = e.message; }
  });
  $("#btn-form").addEventListener("submit", async (e) => {
    if (e.submitter && e.submitter.value !== "save") return;
    e.preventDefault();
    stopRecording(false);
    try {
      if (editing.mode === "tile") await saveTile();
      else if (editing.mode === "sound") await saveSound();
      else await saveButton();
      $("#dlg-button").close();
    } catch (err) { $("#b-err").textContent = err.message; }
  });
}

async function saveTile() {
  const label = $("#b-label").value.trim();
  const ov = { ...(deckCfg().app_overrides || {}) };
  ov[editing.entry] = { label: label === editing.def ? "" : label, image: editing.image, color: editing.color };
  await deckTry({ apps: appsEntries(), app_overrides: ov });
}

async function saveButton() {
  const k = kindNow();
  let action = "";
  const targets = { ...(deckCfg().targets || {}) };
  let sounds = deckCfg().sounds || {};
  if (k === "keys") action = "keys:" + $("#b-keys").value.trim().replace(/\s*\+\s*/g, "+").replace(/…$/, "");
  else if (k === "media") action = "media:" + $("#b-media").value;
  else if (k === "app") action = $("#b-app").value ? "app:" + $("#b-app").value : "";
  else if (k === "agent") action = $("#b-agent").value.trim();
  else if (k === "pd") action = $("#b-pd").value;
  else if (k === "sound") ({ action, sounds } = soundActionForSave($("#b-label").value.trim()));
  else {
    const tk = tkindNow();
    let v = $("#b-tvalue").value.trim().replace(/^"(.*)"$/, "$1");
    if (!v) throw new Error(tk === "url" ? "Type the website address." : "Browse or paste the full path.");
    if (tk === "url" && !/^https?:\/\//i.test(v)) v = "https://" + v;
    const tid = editing.tid || newId("t");
    targets[tid] = tk === "url" ? { kind: "url", url: v, label: "" } : { kind: tk, path: v, label: "" };
    action = "target:" + tid;
  }
  if (!action || action === "keys:") throw new Error("Tell the button what to do.");
  const btn = { label: $("#b-label").value.trim(), icon: editing.icon, color: editing.color, image: editing.image, action };
  const custom = clone(deckCfg().custom);
  let pages = deckCfg().pages.slice();
  let page = custom.find((p) => p.id === editing.pageId);
  if (!page) {  // the first macro creates the MACROS page and puts it on the deck
    page = { id: editing.pageId, name: "MACROS", color: "#34D399", layout: "keys", buttons: [] };
    custom.push(page);
    const at = pages.indexOf("macros");
    if (at >= 0) pages[at] = page.id; else if (pages.length < MAX_TABS) pages.push(page.id);
    else toast("Saved, but the MACROS tab isn't on the deck: it already has 6 tabs.", "err");
  }
  if (editing.idx >= 0) page.buttons[editing.idx] = btn; else page.buttons.push(btn);
  await deckTry({ custom, pages, targets, sounds });
}
