/* PandaDeck settings page: what works, depending on how the panel is connected
 * (status.panel.mode: usb | wifi | usb_other | none). One line in the panel
 * card, a "What works how?" comparison that expands in place, the side-rail
 * pill, and the deck section's USB hints. Uses h/icon/setKids/S from app.js. */
"use strict";

const WORK_MODES = [
  { mode: "usb", ic: "usb", title: "USB cable to this computer", how: "The panel's side USB-C is plugged into this computer.", items: [
    [1, "Deck keys, media keys and macros, as a real USB keyboard: instant, in every app, even without PandaDeck running"],
    [1, "App, file and website buttons, music and its controls, HUD, AI approvals, sounds, the settings gear"],
    [1, "Fast firmware updates"]] },
  { mode: "wifi", ic: "wifi", title: "Wi-Fi only", how: "The panel runs on a charger (or another computer's power); PandaDeck reaches it over your network.", items: [
    [1, "Music, HUD, AI approvals, app / file / website buttons, sounds"],
    [0, "Keyboard shortcuts, media keys and macros: they need the USB cable to this computer"],
    [1, "Firmware updates over Wi-Fi"]] },
  { mode: "usb_other", ic: "usb", title: "USB cable to another computer", how: "The panel's USB-C goes to a different computer.", items: [
    [1, "Keys, media keys and macros work on that other computer"],
    [1, "Music, HUD, AI, app buttons and sounds still run from this one, over Wi-Fi"],
    [0, "Keys don't reach this computer"]] },
  { mode: "none", ic: "off", title: "Power only, or not connected", how: "No USB cable to this computer and no Wi-Fi link.", items: [
    [0, "Nothing reaches this computer: the panel keeps its last screen and shows “PC agent offline”"]] },
];

const WORKS_LINE = {
  usb: ["ok", "✓", "Everything works: keys, music, HUD, AI, sounds"],
  wifi: ["warn", "⚠", "Keyboard keys and macros won't reach this computer: plug the panel's side USB-C into it"],
  usb_other: ["warn", "⚠", "Keys go to the computer the panel's USB is plugged into; music, HUD, AI and sounds still work here"],
  none: ["err", "✕", "Nothing reaches this computer right now"],
};

const RAIL_TEXT = {
  usb: ["ok", "USB: everything works"], wifi: ["warn", "Wi-Fi: no keyboard keys"],
  usb_other: ["warn", "USB to another computer"], none: ["", "Panel not connected"],
};

const WORKS_KEY = "pandadeck.worksOpen";
let worksOpen = (() => { try { return localStorage.getItem(WORKS_KEY) === "1"; } catch (_) { return false; } })();

const linkMode = () => (S.status && S.status.panel && S.status.panel.mode) || "none";

function setWorksOpen(open) {
  worksOpen = open;
  try { localStorage.setItem(WORKS_KEY, open ? "1" : "0"); } catch (_) { /* private mode: just don't remember */ }
  for (const el of document.querySelectorAll(".works-more")) el.classList.toggle("open", open);
  for (const el of document.querySelectorAll(".works-toggle")) el.setAttribute("aria-expanded", String(open));
}

/* The one line under the connection pill, with its "What works how?" link. */
function worksLine() {
  const [kind, mark, text] = WORKS_LINE[linkMode()] || WORKS_LINE.none;
  return h("div", { class: "works-line " + kind },
    h("span", { class: "mark", "aria-hidden": "true", text: mark }), h("span", { text }),
    h("button", { type: "button", class: "linkish works-toggle", "aria-expanded": String(worksOpen), "aria-controls": "works-more",
      onclick: () => setWorksOpen(!worksOpen) }, "What works how?"));
}

/* The four cases side by side (collapsed by default). */
function worksPanel() {
  const now = linkMode();
  const cells = WORK_MODES.map((m) => h("div", { class: "works-cell" + (m.mode === now ? " now" : "") },
    h("div", { class: "works-head" }, icon(m.ic), h("b", { text: m.title }), m.mode === now ? h("span", { class: "tag green", text: "You are here" }) : null),
    h("p", { class: "works-how", text: m.how }),
    h("ul", { class: "works-list" }, m.items.map(([ok, text]) => h("li", { class: ok ? "yes" : "no" },
      h("span", { class: "mark", "aria-label": ok ? "works" : "doesn't work", text: ok ? "✓" : "✕" }), h("span", { text }))))));
  return h("div", { class: "works-more" + (worksOpen ? " open" : ""), id: "works-more" },
    h("div", { class: "works-clip" }, h("div", { class: "works-grid" }, cells)));
}

function buildRailStatus() {
  const box = $("#rail-status");
  if (!box) return;
  const [kind, text] = RAIL_TEXT[linkMode()] || RAIL_TEXT.none;
  setKids(box, h("a", { href: "#panel", class: "rail-pill", title: "What works right now",
    onclick: () => { setWorksOpen(true); } }, h("i", { class: "dot " + kind }), text));
}

/* Next to keyboard / macro buttons: they need the USB cable to this computer. */
function usbKeysNote() {
  const box = h("div", { class: "usb-note" });
  fillUsbNote(box);
  return box;
}
function fillUsbNote(box) {
  const mode = linkMode();
  setKids(box, h("span", { class: "tag", text: "⌨ needs the USB cable to this computer" }),
    mode !== "usb" ? h("span", { class: "hint", text: mode === "usb_other" ? " Right now the panel's USB goes to another computer: these keys go there."
      : " Right now the panel isn't connected to this computer by USB, so these keys won't reach it." }) : null);
}
function refreshUsbNotes() {
  for (const box of document.querySelectorAll(".usb-note")) fillUsbNote(box);
}
