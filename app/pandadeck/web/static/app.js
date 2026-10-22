/* PandaDeck settings page. Vanilla JS, no build step, no external requests.
 * Every API call carries X-PandaDeck: 1 (the server refuses calls without it).
 * Text from the OS (app names, track titles) is only ever set via textContent. */
"use strict";

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const S = {
  settings: null, secrets: {}, meta: {}, status: null,
  apps: [], appsReady: false, suggested: [], appsError: "",
  release: null, fwStaged: null, install: { host: "", ok: null, source: "latest", staged: null, info: null },
  token: "", ssids: [], ssidsFetched: false, offline: false,
};

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const ICON_PATHS = {
  usb: "M12 3v13M9 6l3-3 3 3M7 11v2a3 3 0 0 0 3 3h4a3 3 0 0 0 3-3v-2M7 9h0M17 9h0M12 16v3m-2 2h4",
  wifi: "M2.5 9a14 14 0 0 1 19 0M6 12.5a9 9 0 0 1 12 0M9.5 16a4 4 0 0 1 5 0M12 19.5h0",
  off: "M4 4l16 16M9.5 16a4 4 0 0 1 5 0M6 12.5a9 9 0 0 1 4.5-2.3M2.5 9a14 14 0 0 1 5-3M14.5 6a14 14 0 0 1 7 3M12 19.5h0",
  check: "M5 12.5l4.5 4.5L19 7.5",
  alert: "M12 8v5M12 16.5h0M10.3 3.9L2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
  grip: "M9 6h0M15 6h0M9 12h0M15 12h0M9 18h0M15 18h0",
  up: "M6 15l6-6 6 6", down: "M6 9l6 6 6-6",
  x: "M6 6l12 12M18 6L6 18", plus: "M12 5v14M5 12h14",
  refresh: "M20 11a8 8 0 1 0-2.3 6.3M20 4v7h-7",
  upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
  copy: "M9 9h10v10H9zM5 15V5h10",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  chip: "M8 8h8v8H8zM4 10h2M4 14h2M18 10h2M18 14h2M10 4v2M14 4v2M10 18v2M14 18v2",
  server: "M4 5h16v6H4zM4 13h16v6H4zM8 8h0M8 16h0",
  spark: "M12 3l1.8 5.4L19 10l-5.2 1.6L12 17l-1.8-5.4L5 10l5.2-1.6z",
  music: "M9 18V6l11-2v12M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM20 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
  power: "M12 3v8M6.3 7a8 8 0 1 0 11.4 0",
  bolt: "M13 3L5 14h6l-1 7 8-11h-6z",
  pencil: "M4 20h4L19 9l-4-4L4 16zM14 6l4 4",
  play: "M8 5v14l11-7z", stop: "M7 7h10v10H7z",
  heart: "M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z",
  left: "M15 6l-6 6 6 6", right: "M9 6l6 6-6 6",
  web: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c3 3.5 3 14.5 0 18M12 3c-3 3.5-3 14.5 0 18",
};
function icon(name, cls) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "i" + (cls ? " " + cls : ""));
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", ICON_PATHS[name] || "");
  svg.append(p);
  return svg;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined || opts.raw ? "POST" : "GET"),
                 headers: { "X-PandaDeck": "1" }, cache: "no-store" };
  if (opts.raw) { init.body = opts.raw; init.headers["Content-Type"] = "application/octet-stream"; }
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  if (!r.ok) {
    const err = new Error((data && data.error) || `Request failed (${r.status})`);
    err.status = r.status;
    throw err;
  }
  return data;
}

function toast(text, kind = "ok") {
  const t = h("div", { class: "toast " + kind }, icon(kind === "err" ? "alert" : "check"), h("span", { text }));
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "err" ? 7000 : 3500);
}

function confirmBox(title, text, okLabel = "OK", danger = false) {
  const d = $("#dlg-confirm");
  $("#confirm-title").textContent = title;
  $("#confirm-text").textContent = text;
  const ok = $("#confirm-ok");
  ok.textContent = okLabel;
  ok.className = danger ? "danger" : "primary";
  d.returnValue = "";
  d.showModal();
  return new Promise((res) => d.addEventListener("close", () => res(d.returnValue === "ok"), { once: true }));
}

async function save(patch, okText) {
  try {
    const r = await api("/api/settings", { body: patch });
    S.settings = r.settings;
    if (okText) toast(okText);
  } catch (e) {
    toast(e.message, "err");
  }
  buildAll();
}

function setKids(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
  return el;
}
const clone = (o) => JSON.parse(JSON.stringify(o));
const pct = (v) => (typeof v === "number" && v >= 0 ? Math.round(v) : null);
function fmtDur(s) {
  if (typeof s !== "number" || s < 0) return "";
  const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d} d ${hh} h`;
  if (hh) return `${hh} h ${m} m`;
  return `${m} m`;
}

// ---------------------------------------------------------------- controls
function toggle(checked, onChange, opts = {}) {
  const input = h("input", { type: "checkbox", checked: !!checked, disabled: !!opts.disabled, "aria-label": opts.label || "" });
  input.addEventListener("change", () => onChange(input.checked, input));
  return h("label", { class: "switch", title: opts.title || "" }, input, h("span"));
}
function seg(name, options, current, onChange, opts = {}) {
  return h("div", { class: "seg", role: "radiogroup", "aria-label": opts.label || name },
    options.map(([value, label, disabled]) => {
      const input = h("input", { type: "radio", name, value, checked: value === current, disabled: !!disabled });
      input.addEventListener("change", () => input.checked && onChange(value));
      return h("label", {}, input, h("span", { text: label }));
    }));
}
function row(title, desc, ctl) {
  return h("div", { class: "row" },
    h("div", { class: "label" }, h("b", { text: title }), desc ? (desc instanceof Node ? desc : h("span", { text: desc })) : null),
    h("div", { class: "ctl" }, ctl));
}
const pill = (kind, text) => h("span", { class: "pill " + (kind || "") }, h("i", { class: "dot " + (kind || "") }), text);
function bar(frac, cls) {
  const i = h("i");
  i.style.width = Math.max(0, Math.min(1, frac || 0)) * 100 + "%";
  return h("div", { class: "bar " + (cls || "") }, i);
}
function cardHead(title, ic, lead, right) {
  return h("div", { class: "card-head" },
    h("div", {}, h("h3", {}, ic ? icon(ic) : null, title), lead ? h("p", { class: "lead", text: lead }) : null),
    right || null);
}
function copyBox(text) {
  return h("div", { class: "copybox" }, h("code", { text }),
    h("button", { class: "ghost small", type: "button", onclick: async () => {
      try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (_) { toast("Select the text and copy it", "err"); }
    } }, icon("copy"), "Copy"));
}
/* Icons load a few at a time, and only once visible: the app renders them one by one,
   and hundreds of parallel requests would only queue up (or exhaust sockets). */
const iconQueue = { active: 0, waiting: [], max: 4 };
function pumpIcons() {
  while (iconQueue.active < iconQueue.max && iconQueue.waiting.length) {
    const img = iconQueue.waiting.shift();
    if (!img.isConnected) continue;
    iconQueue.active++;
    const done = () => { iconQueue.active--; pumpIcons(); };
    img.addEventListener("load", done, { once: true });
    img.addEventListener("error", done, { once: true });
    img.src = img.dataset.src;
  }
}
const iconSeen = new IntersectionObserver((entries) => {
  for (const e of entries) if (e.isIntersecting) { iconSeen.unobserve(e.target); iconQueue.waiting.push(e.target); }
  pumpIcons();
}, { rootMargin: "200px" });
function lazyImg(src, fallback, cls) {
  const img = h("img", { alt: "", class: cls || "", dataset: { src } });
  img.addEventListener("error", () => img.replaceWith(fallback));
  iconSeen.observe(img);
  return img;
}
function appImg(app, cls) {
  const ph = h("span", { class: "ph", text: (app.short || app.name || "?").slice(0, 1).toUpperCase() });
  if (!app.id) return ph;
  return lazyImg("/api/apps/icon?id=" + encodeURIComponent(app.id), ph, cls);
}
function keepFocus(card) {
  const a = document.activeElement;
  return a && card.contains(a) && /INPUT|SELECT|TEXTAREA/.test(a.tagName);
}

// ---------------------------------------------------------------- 01 Panel
const PAGE_INFO = {
  ai: ["AI", "Claude Code & Codex sessions, approvals and usage", "#D97757", "AI"],
  deck: ["DECK", "Your apps and shortcut buttons", "#F2B14B", "Deck"],
  music: ["MUSIC", "Now playing, with cover and big controls", "#C084FC", "Music"],
  hud: ["HUD", "CPU, memory, GPU, network, and your homelab", "#6FA8DC", "HUD"],
};
const pageInfo = (id) => PAGE_INFO[id] || (S.meta.page_info || {})[id] || [id.toUpperCase(), "", "#888888", id];

function devicePreview(st) {
  const p = st.panel;
  const on = p.state === "connected";
  const tabs = h("div", { class: "tabs" }, (S.settings.pages || []).map((id, i) =>
    h("b", { class: i === 0 ? "on" : "", text: pageInfo(id)[0] })));
  const chosen = (S.settings.deck.apps.length ? S.settings.deck.apps : S.suggested).slice(0, 12);
  const byId = Object.fromEntries(S.apps.map((a) => [a.id, a]));
  const tiles = h("div", { class: "tiles" });
  for (let i = 0; i < 12; i++) {
    const a = byId[chosen[i]];
    tiles.append(a ? h("i", {}, appImg(a)) : h("i", { class: "c" + ((i % 4) + 1) }));
  }
  const badge = on ? h("span", { class: "link-badge ok" }, icon(p.kind === "wifi" ? "wifi" : "usb"), p.kind === "wifi" ? "WI-FI" : "USB")
                   : h("span", { class: "link-badge" }, icon("off"), p.state === "off" ? "OFF" : "SEARCHING");
  return h("div", { class: "device" }, h("div", { class: "screen" + (on ? "" : " off") }, tabs, tiles), badge);
}

function renderHero() {
  const card = $("#panel-hero");
  const st = S.status;
  if (!st) return;
  const p = st.panel;
  const text = h("div", { class: "hero-text" });
  if (p.state === "connected") {
    const wifi = p.wifi || {};
    text.append(
      h("div", { class: "inline" }, pill("ok", p.kind === "wifi" ? "Connected over Wi-Fi" : "Connected over USB"),
        st.demo ? h("span", { class: "tag clay", text: "demo" }) : null, p.busy ? pill("warn", "Updating") : null),
      worksLine(),
      h("h3", { text: (p.settings && p.settings.name) || "PandaDeck", style: { marginTop: "12px" } }),
      h("dl", { class: "facts" },
        h("dt", { text: "Firmware" }), h("dd", { text: p.fw || "unknown" }),
        h("dt", { text: "Connection" }), h("dd", { text: p.kind === "wifi" ? `Wi-Fi, ${p.where}` : `USB cable (${p.where})` }),
        h("dt", { text: "Wi-Fi" }), h("dd", { text: wifi.connected ? `${wifi.ssid || "connected"}${wifi.ip ? " · " + wifi.ip : ""}` : (wifi.configured ? "set up, not connected" : "not set up") }),
        h("dt", { text: "Pages" }), h("dd", { text: (S.settings.pages || []).map((x) => pageInfo(x)[0]).join(" · ") })),
      h("div", { class: "inline" }, h("button", { type: "button", class: "ghost small", onclick: reconnect }, icon("refresh"), "Reconnect")));
  } else if (p.usb_paused > 0) {
    text.append(pill("warn", "Bootloader mode"), h("h3", { text: "The USB port is free for flashing", style: { marginTop: "12px" } }),
      h("p", { class: "hint", text: `PandaDeck stays off the panel's USB port for ${Math.ceil(p.usb_paused / 60)} more minutes so esptool can use it. Unplug and replug the panel to leave bootloader mode.`, style: { marginTop: "8px" } }),
      h("div", { class: "inline", style: { marginTop: "12px" } }, h("button", { type: "button", class: "small", onclick: reconnect }, icon("refresh"), "Done flashing: connect again")));
  } else if (p.state === "off") {
    text.append(pill("", "Panel link off"), h("h3", { text: "Not talking to a panel", style: { marginTop: "12px" } }),
      h("p", { class: "hint", text: "The app was started with --no-panel. Everything else works; restart the app normally to connect.", style: { marginTop: "8px" } }));
  } else {
    text.append(
      h("div", { class: "inline" }, h("span", { class: "pill warn" }, h("i", { class: "dot busy" }), "Looking for your panel…")),
      worksLine(),
      h("h3", { text: "Plug the panel into this computer", style: { marginTop: "12px" } }),
      h("ol", { class: "checklist" },
        h("li", {}, h("span", {}, h("b", { text: "Use a USB-C data cable. " }), "Many cables only carry power: if your computer doesn't notice the panel, try another one.")),
        h("li", {}, h("span", {}, h("b", { text: "Plug it straight into the computer, " }), "not through a monitor or a passive hub.")),
        h("li", {}, h("span", {}, h("b", { text: "Close other programs " }), "that may hold the port (Arduino serial monitor, a second PandaDeck).")),
        st.os === "linux" ? h("li", {}, h("span", {}, h("b", { text: "Linux: " }), "allow serial ports with ", h("code", { text: "sudo usermod -aG dialout $USER" }), ", then log out and in.")) : null,
        h("li", {}, h("span", {}, h("b", { text: "No PandaDeck firmware yet? " }), "Use ", h("a", { href: "#install-card", onclick: () => showInstall(true) }, "Install PandaDeck on a panel"), " below."))),
      p.error ? h("p", { class: "hint", text: "Last attempt: " + p.error, style: { marginTop: "12px" } }) : null);
  }
  setKids(card, h("div", { class: "hero" }, devicePreview(st), text), p.state === "off" ? null : worksPanel());
}

async function reconnect() {
  try { await api("/api/panel/reconnect", { body: {} }); toast("Reconnecting…"); } catch (e) { toast(e.message, "err"); }
}

// ---- install wizard (stock Panda Touch -> PandaDeck)
let installForced = false;
function showInstall(force) {
  installForced = force;
  buildInstall();
  $("#install-card").hidden = false;
  if (force) $("#install-card").scrollIntoView({ behavior: "smooth", block: "start" });
}

function jobView(target) {
  const j = S.status && S.status.job;
  const box = h("div", { class: "jobview", dataset: { target } });
  if (!j || j.target !== target || j.state === "idle") return box;
  const busy = ["checking", "downloading", "uploading"].includes(j.state);
  box.append(h("div", { class: "meter", style: { marginTop: "14px" } },
    h("div", { class: "top" }, h("span", { text: j.message || (busy ? "Working…" : "") }), h("b", { text: busy ? Math.round((j.progress || 0) * 100) + "%" : "" })),
    bar(j.state === "done" ? 1 : j.progress, j.state === "error" ? "amber" : j.state === "done" ? "green" : "")));
  if (j.state === "error") box.append(h("p", { class: "note err", text: j.message, style: { marginTop: "10px" } }));
  if (j.state === "done") box.append(h("p", { class: "note ok", text: j.message, style: { marginTop: "10px" } }));
  return box;
}

function buildInstall() {
  const card = $("#install-card");
  const I = S.install;
  const addr = h("input", { type: "text", placeholder: "10.0.0.57", value: I.host, class: "mono", autocomplete: "off", spellcheck: false });
  addr.addEventListener("input", () => { I.host = addr.value.trim(); I.ok = null; });
  const checkBtn = h("button", { type: "button", onclick: async () => {
    try {
      const r = await api("/api/install/check", { body: { host: addr.value } });
      I.host = r.host; I.ok = r.updater;
      if (!r.updater) toast(`No updater found at ${r.host}. Check the address on the panel.`, "err");
    } catch (e) { toast(e.message, "err"); I.ok = false; }
    buildInstall();
  } }, "Check");
  const file = h("input", { type: "file", accept: ".bin,application/octet-stream", hidden: true });
  file.addEventListener("change", async () => {
    const f = file.files[0];
    if (!f) return;
    try {
      const r = await api("/api/firmware/file", { raw: await f.arrayBuffer() });
      I.staged = r.staged; I.info = r.info; I.source = "file";
    } catch (e) { toast(e.message, "err"); }
    buildInstall();
  });
  const info = I.info;
  const step2 = h("div", { class: "body" }, h("b", { text: "Choose the firmware" }),
    seg("isrc", [["latest", "Latest release"], ["file", "From a file"]], I.source, (v) => { I.source = v; buildInstall(); }),
    I.source === "file" ? h("div", { class: "inline" },
      h("button", { type: "button", onclick: () => file.click() }, icon("upload"), info ? "Pick another file" : "Pick a .bin file"), file,
      info ? h("span", { class: "hint", text: `${info.project || "firmware"} ${info.version || ""} · ${Math.round(info.size / 1024)} KB` }) : null) :
      h("p", { class: "hint", text: "The app downloads pandadeck-firmware.bin from the project's latest release." }),
    I.source === "file" && info && !info.pandadeck ? h("p", { class: "note warn", text: "This doesn't look like PandaDeck firmware. The panel will run whatever you install." }) : null);
  const canGo = I.ok === true && (I.source === "latest" || I.staged);
  const job = S.status && S.status.job;
  const running = job && job.target === "install" && ["checking", "downloading", "uploading"].includes(job.state);
  setKids(card,
    cardHead("Install PandaDeck on a panel", "bolt", "For a Panda Touch still running its stock firmware, on the same Wi-Fi as this computer. Takes about a minute.",
      installForced ? h("button", { type: "button", class: "ghost small", onclick: () => { installForced = false; card.hidden = S.status.panel.state !== "searching"; } }, icon("x")) : null),
    h("div", { class: "steps" },
      h("div", { class: "step" + (I.ok ? " done" : "") }, h("span", { class: "n", text: I.ok ? "✓" : "1" }),
        h("div", { class: "body" }, h("b", { text: "Find the panel's address" }),
          h("p", { class: "hint", text: "On the Panda Touch open Settings → Wi-Fi (sometimes called WLAN). The address looks like 10.0.0.57 (four numbers with dots)." }),
          h("div", { class: "inline" }, addr, checkBtn),
          I.ok === true ? pill("ok", `Firmware updater found at ${I.host}`) : I.ok === false ? pill("err", "No updater at that address") : null)),
      h("div", { class: "step" }, h("span", { class: "n", text: "2" }), step2),
      h("div", { class: "step" }, h("span", { class: "n", text: "3" }),
        h("div", { class: "body" }, h("b", { text: "Install" }),
          h("p", { class: "hint", text: "Don't unplug the panel while it uploads. It restarts by itself into PandaDeck; then plug it into this computer with a USB-C data cable." }),
          h("div", { class: "inline" }, h("button", { type: "button", class: "primary", disabled: !canGo || running, onclick: async () => {
            try { await api("/api/install/start", { body: { host: I.host, source: I.source, staged: I.staged } }); toast("Uploading…"); }
            catch (e) { toast(e.message, "err"); }
          } }, icon("upload"), "Install PandaDeck")),
          jobView("install")))));
}

// ---- display
function buildDisplay() {
  const card = $("#display-card");
  if (keepFocus(card) || card.dataset.dragging === "1") return;
  const p = S.status ? S.status.panel : { state: "searching", settings: {} };
  const on = p.state === "connected";
  const ps = p.settings || {};
  const bright = h("input", { type: "range", min: 5, max: 100, step: 1, value: ps.brightness ?? 100, disabled: !on, "aria-label": "Brightness" });
  const fill = () => bright.style.setProperty("--fill", ((bright.value - 5) / 95) * 100 + "%");
  fill();
  const val = h("b", { class: "mono", text: (ps.brightness ?? "–") + "%" });
  bright.addEventListener("input", () => { fill(); val.textContent = bright.value + "%"; card.dataset.dragging = "1"; });
  bright.addEventListener("change", async () => { card.dataset.dragging = ""; await panelConfig({ brightness: +bright.value }); });
  setKids(card,
    cardHead("Display", null, on ? "Changes apply on the panel right away." : "Connect the panel to change these."),
    h("div", { class: "rows" },
      row("Brightness", null, h("div", { class: "inline" }, bright, val)),
      row("Flip screen", "Rotate the picture 180° (also: hold the clock in the top-right corner of the panel for 2 s).",
        toggle(ps.flip, (v) => panelConfig({ flip: v }), { disabled: !on, label: "Flip screen" })),
      row("Flip touch", "Use this if the picture is right but touches land in the wrong place.",
        toggle(ps.tflip, (v) => panelConfig({ tflip: v }), { disabled: !on, label: "Flip touch" }))));
}

async function panelConfig(payload) {
  try { await api("/api/panel/config", { body: payload }); await refresh(); }
  catch (e) { toast(e.message, "err"); buildDisplay(); }
}

// ---- Wi-Fi
function buildWifi() {
  const card = $("#wifi-card");
  if (keepFocus(card)) return;
  const p = S.status ? S.status.panel : { state: "searching", wifi: {} };
  const usb = p.state === "connected" && p.kind === "usb";
  const w = p.wifi || {};
  if (usb && !S.ssidsFetched) {
    S.ssidsFetched = true;
    api("/api/wifi/ssids").then((r) => { S.ssids = r.ssids || []; buildWifi(); }).catch(() => {});
  }
  const ssid = h("input", { type: "text", placeholder: "Network name", list: "ssid-list", autocomplete: "off", value: w.ssid || S.ssids[0] || "" });
  const pass = h("input", { type: "password", placeholder: "Password", autocomplete: "new-password" });
  const showPw = h("button", { type: "button", class: "icon ghost", title: "Show password", onclick: () => { pass.type = pass.type === "password" ? "text" : "password"; } }, icon("eye"));
  const status = p.state !== "connected" ? pill("", "Panel not connected") :
    w.connected ? pill("ok", `On ${w.ssid || "Wi-Fi"}${w.ip ? " · " + w.ip : ""}`) :
    w.configured ? pill("warn", "Set up, but not connected") : pill("", "Not set up");
  setKids(card,
    cardHead("Wi-Fi", "wifi", "Optional: for updates without the cable, or using the panel away from this computer.", status),
    h("datalist", { id: "ssid-list" }, S.ssids.map((s) => h("option", { value: s }))),
    h("div", { class: "stack" },
      h("label", { class: "field" }, h("span", { text: "Network" }), ssid),
      h("label", { class: "field" }, h("span", { text: "Password" }), h("div", { class: "inline" }, pass, showPw)),
      h("div", { class: "inline" },
        h("button", { type: "button", class: "primary", disabled: !usb, onclick: async () => {
          try { await api("/api/panel/wifi", { body: { ssid: ssid.value.trim(), pass: pass.value } }); pass.value = ""; toast("Sent to the panel. It joins the network in a few seconds."); }
          catch (e) { toast(e.message, "err"); }
        } }, "Connect"),
        w.configured ? h("button", { type: "button", class: "ghost", disabled: !usb, onclick: async () => {
          if (!(await confirmBox("Forget Wi-Fi?", "The panel will only work over the USB cable.", "Forget"))) return;
          try { await api("/api/panel/wifi", { body: { ssid: "", pass: "" } }); toast("Wi-Fi forgotten"); } catch (e) { toast(e.message, "err"); }
        } }, "Forget") : null),
      usb ? h("p", { class: "hint", text: "The password goes over the USB cable and is stored on the panel only." })
          : h("p", { class: "hint", text: "Plug the panel in with USB to send Wi-Fi details." })));
}

// ---- firmware
function buildFirmware() {
  const card = $("#firmware-card");
  if (keepFocus(card)) return;
  const p = S.status ? S.status.panel : { state: "searching" };
  const on = p.state === "connected";
  const rel = S.release;
  const file = h("input", { type: "file", accept: ".bin,application/octet-stream", hidden: true });
  file.addEventListener("change", async () => {
    const f = file.files[0];
    if (!f) return;
    try {
      const r = await api("/api/firmware/file", { raw: await f.arrayBuffer() });
      const info = r.info;
      const what = info.stock ? "the stock BIGTREETECH firmware" : info.pandadeck ? "PandaDeck firmware" : `"${info.project || "unknown"}" firmware`;
      const warn = info.pandadeck ? "" : " It isn't PandaDeck firmware: once installed, the panel stops talking to this app (that's expected when going back to the stock firmware).";
      if (await confirmBox("Install this firmware?", `This file looks like ${what}${info.version ? " (" + info.version + ")" : ""}, ${Math.round(info.size / 1024)} KB.${warn}`, "Install", !info.pandadeck)) {
        await api("/api/firmware/update", { body: { staged: r.staged } });
        toast("Installing…");
      }
    } catch (e) { toast(e.message, "err"); }
    file.value = "";
  });
  let relLine;
  if (!rel) relLine = h("button", { type: "button", class: "small", onclick: checkRelease }, icon("refresh"), "Check for updates");
  else if (!rel.ok) relLine = h("span", { class: "hint", text: rel.error || "Couldn't check" });
  else if (rel.newer) relLine = h("div", { class: "inline" }, pill("warn", `Version ${rel.version} is available`),
    h("button", { type: "button", class: "primary small", disabled: !on, onclick: async () => {
      try { await api("/api/firmware/update", { body: { source: "latest" } }); toast("Downloading the update…"); } catch (e) { toast(e.message, "err"); }
    } }, "Update"));
  else relLine = pill("ok", `Up to date (latest is ${rel.version || "?"})`);
  const host = h("input", { type: "text", class: "mono", placeholder: "pandadeck.local or 10.0.0.50", value: S.settings.panel.host || "" });
  host.addEventListener("change", () => save({ panel: { host: host.value.trim() } }, "Saved"));
  const tokenBox = S.token ? copyBox(S.token) : h("button", { type: "button", class: "small", disabled: !(p.has_token), onclick: async () => {
    try { S.token = (await api("/api/panel/token", { body: {} })).token; buildFirmware(); } catch (e) { toast(e.message, "err"); }
  } }, icon("eye"), "Show token");
  setKids(card,
    cardHead("Firmware", "chip", "Updates install over USB (or Wi-Fi) and the panel restarts by itself in about 20 seconds."),
    h("div", { class: "rows" },
      row("Installed", null, h("span", { class: "mono", text: on ? p.fw || "unknown" : "–" })),
      row("Latest release", null, relLine),
      row("Install from file", "Any panel firmware .bin, including the stock BIGTREETECH firmware to go back.",
        h("div", {}, h("button", { type: "button", disabled: !on, onclick: () => file.click() }, icon("upload"), "Choose file…"), file))),
    jobView("panel"),
    h("details", { class: "more" }, h("summary", { text: "Advanced" }),
      h("div", { class: "inner" },
        row("Connection", "Auto uses USB when plugged in, else Wi-Fi.",
          seg("link", [["auto", "Auto"], ["usb", "USB only"], ["wifi", "Wi-Fi only"]], S.settings.panel.link, (v) => save({ panel: { link: v } }))),
        h("label", { class: "field" }, h("span", { text: "Panel address for Wi-Fi (empty = automatic)" }), host),
        row("Panel token", "The panel's own password for Wi-Fi requests (scripts, browser updates).", tokenBox),
        row("Restart into bootloader", "For flashing with esptool over USB.", h("button", { type: "button", class: "small", disabled: !on, onclick: async () => {
          if (!(await confirmBox("Restart into the bootloader?", "The panel restarts into the ESP32-S3's download mode so esptool can flash it over USB. The screen stays dark until you flash it or unplug and replug it.", "Restart"))) return;
          try { await api("/api/panel/bootloader", { body: {} }); toast("Panel is in bootloader mode"); } catch (e) { toast(e.message, "err"); }
        } }, "Restart…")),
        row("Reset panel settings", "Forgets Wi-Fi, pages, brightness and flip on the panel.", h("button", { type: "button", class: "danger small", disabled: !on, onclick: async () => {
          if (!(await confirmBox("Reset the panel's settings?", "The panel forgets its Wi-Fi network, pages, brightness and orientation. The app sends your pages again right after.", "Reset", true))) return;
          try { await api("/api/panel/reset", { body: {} }); toast("Panel settings reset"); } catch (e) { toast(e.message, "err"); }
        } }, "Reset…")),
        row("Another panel", "Put PandaDeck on a Panda Touch that still runs its stock firmware.",
          h("button", { type: "button", class: "small", onclick: () => showInstall(true) }, "Install PandaDeck on a panel")))));
}

let releaseAsked = false;
async function checkRelease() {
  releaseAsked = true;
  try { S.release = await api("/api/firmware/latest?force=1"); } catch (e) { S.release = { ok: false, error: e.message }; }
  buildFirmware();
}

// ---------------------------------------------------------------- 02 Pages
function pageOrder() {
  const ids = S.meta.page_ids || ["ai", "deck", "music", "hud"];
  const on = S.settings.pages.filter((p) => ids.includes(p));
  return [...on, ...ids.filter((p) => !on.includes(p))];
}

function buildPages() {
  const list = $("#page-list");
  const order = pageOrder();
  const enabled = new Set(S.settings.pages);
  const commit = (newOrder, newEnabled) => save({ pages: newOrder.filter((p) => newEnabled.has(p)) });
  let dragId = null;
  setKids(list, ...order.map((id, idx) => {
    const [name, desc, color, nice] = pageInfo(id);
    const badge = h("span", { class: "pbadge", text: name.slice(0, 5) });
    badge.style.background = color + "26";
    badge.style.color = color;
    const isOn = enabled.has(id);
    const move = (d) => { const o = order.slice(); const j = idx + d; if (j < 0 || j >= o.length) return; [o[idx], o[j]] = [o[j], o[idx]]; commit(o, enabled); };
    const item = h("div", { class: "pitem" + (isOn ? "" : " off"), draggable: "true", dataset: { id } },
      h("span", { class: "grip", title: "Drag to reorder" }, icon("grip")), badge,
      h("div", { class: "txt" }, h("b", { text: nice }), h("span", { text: desc })),
      h("div", { class: "updown" },
        h("button", { type: "button", class: "icon ghost", title: "Move up", disabled: idx === 0, onclick: () => move(-1) }, icon("up")),
        h("button", { type: "button", class: "icon ghost", title: "Move down", disabled: idx === order.length - 1, onclick: () => move(1) }, icon("down"))),
      toggle(isOn, (v) => { const e = new Set(enabled); v ? e.add(id) : e.delete(id); commit(order, e); },
        { disabled: isOn && enabled.size === 1, label: `Show ${name}`, title: isOn && enabled.size === 1 ? "At least one page stays on" : "" }));
    item.addEventListener("dragstart", (e) => { dragId = id; item.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", id); });
    item.addEventListener("dragend", () => item.classList.remove("dragging"));
    item.addEventListener("dragover", (e) => { e.preventDefault(); item.classList.add("over"); });
    item.addEventListener("dragleave", () => item.classList.remove("over"));
    item.addEventListener("drop", (e) => {
      e.preventDefault(); item.classList.remove("over");
      if (!dragId || dragId === id) return;
      const o = order.filter((x) => x !== dragId);
      o.splice(o.indexOf(id) + (order.indexOf(dragId) < order.indexOf(id) ? 1 : 0), 0, dragId);
      commit(o, enabled);
    });
    return item;
  }));
}

// ---------------------------------------------------------------- 03 Deck: see deck.js

// ---------------------------------------------------------------- 04 Music
/* ❤ Like: the track's Liked Songs state, or why it isn't available. */
function likeTag(now, sp) {
  if (!sp.signed_in) return h("a", { class: "tag", href: "#music-spotify", text: "♡ Like needs Spotify sign-in" });
  if (!sp.can_like) return h("a", { class: "tag warn", href: "#music-spotify", text: "Sign in again to enable ❤ Like" });
  if (now.liked !== 0 && now.liked !== 1) return h("span", { class: "tag", text: "♡ not on Spotify", title: "No matching Spotify track (or still looking)" });
  return h("button", { type: "button", class: "small ghost like" + (now.liked ? " on" : ""), title: now.liked ? "Remove from Liked Songs" : "Add to Liked Songs",
    onclick: async () => { try { await api("/api/music/like", { body: {} }); await refresh(); } catch (e) { toast(e.message, "err"); } } },
    icon("heart"), now.liked ? "In Liked Songs" : "Like");
}

function buildNowPlaying() {
  const card = $("#np-card");
  const m = S.status ? S.status.music : null;
  if (!m) return;
  const now = m.now;
  const srcName = m.active === "spotify" ? "Spotify (signed in)" : m.active === "system" ? ({ windows: "Windows media", macos: "macOS", linux: "MPRIS" }[S.status.os] || "this computer") : "";
  let cover;
  if (now && now.art) {
    cover = h("img", { class: "cover", alt: "", src: "/api/music/cover?k=" + encodeURIComponent(now.art) });
    cover.addEventListener("error", () => cover.replaceWith(h("div", { class: "cover" }, icon("music"))));
  } else cover = h("div", { class: "cover" }, icon("music"));
  const body = now ? h("div", {},
    h("div", { class: "t", text: now.title || "Untitled" }), h("div", { class: "a", text: [now.artist, now.album].filter(Boolean).join(" — ") }),
    h("div", { class: "src" }, pill(now.playing ? "ok" : "", now.playing ? "Playing" : "Paused"),
      now.app ? h("span", { class: "tag app-src", title: "The source name and dot the panel shows" },
        h("i", { class: "appdot" + (now.playing ? "" : " paused"), style: { background: now.app_color || now.tint || "var(--faint)" } }), now.app) : null,
      srcName ? h("span", { class: "tag", text: "via " + srcName }) : null,
      likeTag(now, m.spotify),
      S.settings.music.backdrop === "artist" ? h("span", { class: "tag", text: { spotify: "artist photo: Spotify", deezer: "artist photo: Deezer", cover: "no artist photo: cover" }[m.backdrop_source] || "artist photo" }) : null,
      now.dur ? h("span", { class: "hint mono", text: `${fmtClock(now.pos)} / ${fmtClock(now.dur)}` }) : null))
    : h("div", {}, h("div", { class: "t", text: "Nothing playing right now" }),
      h("div", { class: "a", text: !m.available && !m.spotify.signed_in ? "This system's media session isn't available. Sign in with Spotify below to use the MUSIC page." : "Play something in Spotify, your browser or any player." }));
  const key = JSON.stringify([now && now.title, now && now.art, now && now.playing, now && now.app, now && now.liked, m.active, m.backdrop_source, m.spotify.can_like, m.spotify.signed_in, now && Math.floor(now.pos / 5)]);
  if (card.dataset.key === key) return;
  card.dataset.key = key;
  setKids(card, h("div", { class: "np" }, cover, body));
}
function fmtClock(s) { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }

function buildSource() {
  const card = $("#source-card");
  const mu = S.settings.music;
  const signed = S.secrets.spotify;
  setKids(card,
    cardHead("Source and background", null, null),
    h("div", { class: "rows" },
      row("Music source", signed ? "Automatic prefers Spotify when it's playing, else this computer's player." : "Spotify as a source needs the sign-in above.",
        seg("msrc", [["auto", "Automatic"], ["system", "This computer"], ["spotify", "Spotify", !signed]], mu.source, (v) => save({ music: { source: v } }))),
      row("Background", signed
        ? "A soft, blurred cover, or the artist's photo (from Spotify; Deezer's public catalog when Spotify has none)."
        : "A soft, blurred cover, or the artist's photo, looked up by artist name in Deezer's public catalog (no sign-in needed). Falls back to the cover when there's no photo.",
        seg("mbg", [["cover", "Blurred cover"], ["artist", "Artist photo"]], mu.backdrop, (v) => save({ music: { backdrop: v } })))));
}

function buildSpotify() {
  const card = $("#spotify-help");
  if (keepFocus(card)) return;
  const sp = S.status ? S.status.music.spotify : { signed_in: S.secrets.spotify };
  const redirect = S.meta.redirect_uri || "http://127.0.0.1:47321/spotify/callback";
  if (sp.signed_in) {
    setKids(card,
      cardHead("Spotify", "music", "❤ Like (Liked Songs), artist photos, reliable shuffle / repeat / seek, and control of Spotify on your other devices.",
        pill("ok", sp.name ? `Connected as ${sp.name}` : "Connected")),
      sp.error ? h("p", { class: "note warn", text: sp.error }) : null,
      !sp.can_like ? h("p", { class: "note warn", style: { marginTop: "12px" },
        text: "Sign in again to enable ❤ Like: Spotify needs your OK to read and change your Liked Songs (your sign-in is from before Like existed)." }) : null,
      h("div", { class: "inline", style: { marginTop: "12px" } },
        !sp.can_like ? h("button", { type: "button", class: "spotify", onclick: async () => {
          try { const r = await api("/api/spotify/login", { body: {} }); window.location.href = r.url; } catch (e) { toast(e.message, "err"); }
        } }, "Sign in again") : null,
        h("button", { type: "button", class: "ghost", onclick: async () => {
        try { await api("/api/spotify/logout", { body: {} }); S.secrets.spotify = false; toast("Signed out of Spotify"); await loadSettings(); } catch (e) { toast(e.message, "err"); }
      } }, "Sign out")));
    return;
  }
  const cid = h("input", { type: "text", class: "mono", placeholder: "32-character Client ID", value: S.settings.music.spotify_client_id || "", spellcheck: false, autocomplete: "off" });
  const signIn = h("button", { type: "button", class: "spotify", onclick: async () => {
    try {
      if (cid.value.trim() !== S.settings.music.spotify_client_id) {
        const r = await api("/api/settings", { body: { music: { spotify_client_id: cid.value.trim() } } });
        S.settings = r.settings;
      }
      const r = await api("/api/spotify/login", { body: {} });
      window.location.href = r.url;
    } catch (e) { toast(e.message, "err"); }
  } }, "Sign in with Spotify");
  const portNote = !redirect.includes(":47321/") ? h("p", { class: "note warn", text: "PandaDeck is using a different port than the usual 47321 on this computer, so register exactly the address above. If 47321 was only busy for a moment, restart the app to get it back." }) : null;
  setKids(card,
    cardHead("Sign in with Spotify", "music", "Optional, about two minutes. Adds the ❤ Like button (saves the song to your Liked Songs), reliable shuffle / repeat / seek on every system, control of Spotify playing on your phone or a speaker, and Spotify's own artist photos.",
      h("span", { class: "tag green", text: "recommended for Spotify users" })),
    h("ol", { class: "guide" },
      h("li", {}, h("div", {}, "Open ", h("a", { href: "https://developer.spotify.com/dashboard", target: "_blank", rel: "noopener noreferrer" }, "developer.spotify.com/dashboard"), " and log in with your Spotify account.")),
      h("li", {}, h("div", {}, h("b", { text: "Create app. " }), "Name it PandaDeck, tick ", h("b", { text: "Web API" }), ", and add this ", h("b", { text: "Redirect URI" }), " exactly:", copyBox(redirect))),
      h("li", {}, h("div", {}, "Open the app's ", h("b", { text: "Settings" }), ", copy the ", h("b", { text: "Client ID" }), " and paste it here:",
        h("div", { class: "inline", style: { marginTop: "8px" } }, cid, signIn)))),
    portNote,
    sp.last_result && sp.last_result !== "ok" ? h("p", { class: "note err", text: sp.last_result, style: { marginTop: "12px" } }) : null,
    h("p", { class: "hint", text: "Spotify asks every app to use its own Client ID. It and your sign-in stay on this computer.", style: { marginTop: "14px" } }));
}

// ---------------------------------------------------------------- 05 AI
function buildAiPage() {
  const card = $("#ai-page-card");
  const on = S.settings.pages.includes("ai");
  setKids(card, row("Show the AI page", "Live sessions, usage limits and approvals on the panel. You can reorder it in Pages.",
    toggle(on, (v) => save({ pages: v ? ["ai", ...S.settings.pages.filter((p) => p !== "ai")] : S.settings.pages.filter((p) => p !== "ai") }), { label: "Show the AI page", disabled: on && S.settings.pages.length === 1 })));
}

function hookStatus(name, st, enabled, err) {
  if (err) return pill("err", err);
  if (!enabled) return st.installed ? pill("warn", "Old PandaDeck hooks found (turn on to update, then off to remove)") : pill("", "Off");
  if (st.current) return pill("ok", "Connected");
  return pill("warn", "Turn off and on again to repair");
}

function buildHooks() {
  const card = $("#hooks-card");
  const st = S.status ? S.status.ai : null;
  const ai = S.settings.ai;
  const hk = st ? st.hooks : { claude: {}, codex: {} };
  const errs = st ? st.hook_errors : {};
  const sess = st ? st.session_list : [];
  setKids(card,
    cardHead("Live sessions and approvals", "spark", "PandaDeck adds a few hooks so your agents tell the app what they're doing. Your own settings and hooks are kept, with a backup next to the file.",
      st ? pill(st.sessions ? "ok" : "", st.sessions ? `${st.sessions} live session${st.sessions > 1 ? "s" : ""}` : "No sessions yet") : null),
    h("div", { class: "rows" },
      row("Connect Claude Code", h("span", {}, hookStatus("claude", hk.claude || {}, ai.claude_hooks, errs.claude), " ",
        hk.claude && !hk.claude.cli ? h("span", { class: "hint", text: "Claude Code CLI not found on PATH." }) : null),
        toggle(ai.claude_hooks, (v) => save({ ai: { claude_hooks: v } }, v ? "Claude Code connected. Restart open sessions." : "Hooks removed"), { label: "Connect Claude Code" })),
      row("Connect Codex", h("span", {}, hookStatus("codex", hk.codex || {}, ai.codex_hooks, errs.codex), " ",
        h("span", { class: "hint", text: "Needs a Codex CLI with hooks support." })),
        toggle(ai.codex_hooks, (v) => save({ ai: { codex_hooks: v } }, v ? "Codex connected. Restart open sessions." : "Hooks removed"), { label: "Connect Codex" }))),
    sess && sess.length ? h("div", { class: "sess" }, sess.map((s) => h("div", {},
      h("i", { class: "dot " + (s.st === "work" ? "busy" : s.st === "perm" ? "warn" : s.st === "wait" ? "ok" : "") }),
      h("b", { text: s.proj }), h("span", { class: "tag " + (s.src === "codex" ? "blue" : "clay"), text: s.src }), h("span", { text: s.act })))) : null,
    h("p", { class: "hint", text: "Only yes/no permission requests take over the panel; questions an agent asks you are answered in the terminal.", style: { marginTop: "12px" } }));
}

function meter(label, v, reset, cls) {
  const p = pct(v);
  return h("div", { class: "meter" },
    h("div", { class: "top" }, h("span", { text: label + (reset > 0 ? ` · resets in ${fmtDur(reset)}` : "") }), h("b", { text: p === null ? "–" : p + "%" })),
    bar(p === null ? 0 : p / 100, p !== null && p >= 85 ? "amber" : cls));
}

function usagePill(u) {
  if (!u || u.state === "off") return pill("", "Off");
  if (u.state === "ok") return pill("ok", "Updated " + ago(u.at));
  if (u.state === "error") return pill("err", u.error || "Error");
  return pill("", "Waiting…");
}
function ago(t) { if (!t) return ""; const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? "just now" : `${Math.round(s / 60)} min ago`; }

function buildUsage() {
  const card = $("#usage-card");
  if (keepFocus(card)) return;
  const ai = S.settings.ai;
  const st = S.status ? S.status.ai : null;
  const vals = (st && st.values) || {};
  const cl = st ? st.usage.claude : null, cx = st ? st.usage.codex : null;
  const tok = h("input", { type: "password", class: "mono", placeholder: "Paste the token from claude setup-token", autocomplete: "off" });
  const claudeBlock = h("div", { class: "stack" },
    h("div", { class: "inline" }, h("b", { text: "Claude" }), usagePill(cl)),
    ai.claude_usage !== "off" ? h("div", { class: "stack" }, meter("5-hour window", vals.s5h, vals.s5h_reset, ""), meter("Weekly", vals.week, vals.week_reset, "")) : null,
    h("div", { class: "inline" },
      h("button", { type: "button", class: ai.claude_usage === "off" ? "primary" : "", onclick: async () => {
        try {
          const r = await api("/api/claude/connect", { body: {} });
          toast(r.started ? "A terminal opened with `claude setup-token`. Authorize in the browser, then paste the token here." : r.reason, r.started ? "ok" : "err");
        } catch (e) { toast(e.message, "err"); }
      } }, "Connect Claude"),
      ai.claude_usage !== "off" ? h("button", { type: "button", class: "ghost", onclick: async () => {
        try { await api("/api/claude/token", { body: { token: "" } }); await loadSettings(); toast("Claude disconnected"); } catch (e) { toast(e.message, "err"); }
      } }, "Disconnect") : null),
    h("div", { class: "inline" }, tok, h("button", { type: "button", onclick: async () => {
      try { await api("/api/claude/token", { body: { token: tok.value.trim() } }); tok.value = ""; await loadSettings(); toast("Token saved. Checking your limits…"); } catch (e) { toast(e.message, "err"); }
    } }, "Save token")),
    h("p", { class: "hint", text: (st && st.claude_cli ? "The Claude Code CLI is installed: Connect Claude runs `claude setup-token` for you." : "Run `claude setup-token` in a terminal where Claude Code is installed and paste the token.") + " The app checks once a minute with a one-token request." }));
  const codexBlock = h("div", { class: "stack" },
    h("div", { class: "inline" }, h("b", { text: "Codex" }), usagePill(cx)),
    ai.codex_usage !== "off" ? meter("Weekly", vals.codex_week, vals.codex_reset, "blue") : null,
    h("div", { class: "inline" },
      ai.codex_usage === "off" ? h("button", { type: "button", class: "primary", onclick: async () => {
        try { const r = await api("/api/codex/connect", { body: {} }); if (r.found) { toast("Codex connected"); await loadSettings(); } else toast("No Codex login on this computer: run `codex login` first.", "err"); } catch (e) { toast(e.message, "err"); }
      } }, "Connect Codex") : h("button", { type: "button", class: "ghost", onclick: () => save({ ai: { codex_usage: "off" } }, "Codex disconnected") }, "Disconnect")),
    h("p", { class: "hint", text: st && st.codex_auth ? "Uses the login the Codex CLI saved on this computer." : "Run `codex login` first; PandaDeck reads the login it saves (~/.codex/auth.json)." }));
  const cu = h("input", { type: "url", class: "mono", placeholder: "http://usage.example:9000/usage", value: ai.claude_usage_url });
  const xu = h("input", { type: "url", class: "mono", placeholder: "http://usage.example:9000/codex-usage", value: ai.codex_usage_url });
  cu.addEventListener("change", () => save({ ai: { claude_usage_url: cu.value.trim() } }, "Saved"));
  xu.addEventListener("change", () => save({ ai: { codex_usage_url: xu.value.trim() } }, "Saved"));
  const wait = h("input", { type: "number", min: 30, max: 3600, value: ai.perm_wait_s, style: { width: "110px" } });
  wait.addEventListener("change", () => save({ ai: { perm_wait_s: +wait.value } }, "Saved"));
  setKids(card,
    cardHead("Usage meters", null, "Your Claude 5-hour and weekly windows and Codex's weekly window, with reset times."),
    h("div", { class: "grid-2" }, claudeBlock, codexBlock),
    h("details", { class: "more" }, h("summary", { text: "Advanced" }), h("div", { class: "inner" },
      row("Claude usage from", null, seg("cmode", [["off", "Off"], ["token", "Token"], ["url", "URL"]], ai.claude_usage, (v) => save({ ai: { claude_usage: v } }))),
      h("label", { class: "field" }, h("span", { text: "Claude usage URL (a usage daemon on your network)" }), cu),
      row("Codex usage from", null, seg("xmode", [["off", "Off"], ["auth", "Codex login"], ["url", "URL"]], ai.codex_usage, (v) => save({ ai: { codex_usage: v } }))),
      h("label", { class: "field" }, h("span", { text: "Codex usage URL" }), xu),
      row("Approval wait", "Seconds a permission request waits for a tap before the computer's own prompt takes over.", wait))));
}

// ---------------------------------------------------------------- 06 HUD
function buildPc(force) {
  // The live part refreshes every couple of seconds; the "PC name" field is built
  // once (and after settings changes) so typing in it is never interrupted.
  const card = $("#pc-card");
  const pc = S.status && S.status.hud.pc;
  const stat = (k, v, unit) => h("div", { class: "stat" }, h("span", { text: k }), h("b", {}, v === null || v === undefined || v < 0 ? "–" : String(v), v >= 0 ? h("small", { text: unit }) : null));
  const models = pc ? [pc.cpu_model, pc.gpu_model].filter(Boolean).join(" · ") : "";
  const live = h("div", { class: "pc-live" },
    cardHead(pc ? pc.name : "This computer", "chip", (models ? models + " — " : "") + "automatic, nothing to set up.",
      pill(pc ? "ok" : "", pc ? "Live" : "Starting…")),
    pc ? h("div", { class: "stats" },
      stat("CPU", pct(pc.cpu), "%"), stat("Memory", pct(pc.ram), "%"),
      pc.gpu >= 0 ? stat("GPU", pct(pc.gpu), "%") : stat("Disk", pct(pc.disk), "%"),
      stat("Down", pc.down >= 0 ? pc.down.toFixed(1) : -1, "Mb/s"), stat("Up", pc.up >= 0 ? pc.up.toFixed(1) : -1, "Mb/s")) : null);
  const old = card.querySelector(".pc-live");
  if (old && !force && card.querySelector(".pc-form")) { old.replaceWith(live); return; }
  const name = h("input", { type: "text", maxlength: 31, value: S.settings.hud.pc_name || "",
                            placeholder: S.meta.hostname || "This PC", "aria-label": "PC name" });
  name.addEventListener("change", () => save({ hud: { pc_name: name.value.trim() } }, "Saved"));
  setKids(card, live, h("label", { class: "field pc-form", style: { marginTop: "16px" } },
    h("span", { text: "PC name (shown on the HUD; empty = this computer's name)" }), name));
}

function buildLab() {
  const card = $("#lab-card");
  if (keepFocus(card)) return;
  const hud = S.settings.hud;
  const lab = S.status ? S.status.hud.lab : { state: "off" };
  const url = h("input", { type: "url", class: "mono", placeholder: "http://homeserver:47380", value: hud.lab_url });
  url.addEventListener("change", () => save({ hud: { lab_url: url.value.trim() } }, "Saved"));
  const name = h("input", { type: "text", placeholder: "Default: the server's own name", value: hud.lab_name, maxlength: 31 });
  name.addEventListener("change", () => save({ hud: { lab_name: name.value.trim() } }, "Saved"));
  const tok = h("input", { type: "password", class: "mono", placeholder: S.secrets.lab_token ? "•••••••• (saved)" : "Token", autocomplete: "off" });
  const st = lab.state === "ok" ? pill("ok", "Connected" + (lab.host ? ": " + lab.host : "")) : lab.state === "error" ? pill("err", lab.error) : pill("", hud.lab_url ? "Waiting…" : "Not set up");
  setKids(card,
    cardHead("Homelab", "server", "Show a home server on the HUD: CPU, memory, temperature, disk and Docker containers (hold a container on the panel to restart it). Set it up with the steps below.", st),
    h("div", { class: "stack" },
      h("label", { class: "field" }, h("span", { text: "pandadeck-lab address" }), url),
      h("label", { class: "field" }, h("span", { text: "Token" }), h("div", { class: "inline" }, tok,
        h("button", { type: "button", onclick: async () => {
          try { await api("/api/secret", { body: { name: "lab_token", value: tok.value.trim() } }); tok.value = ""; await loadSettings(); toast("Token saved"); } catch (e) { toast(e.message, "err"); }
        } }, "Save"),
        S.secrets.lab_token ? h("button", { type: "button", class: "ghost", onclick: async () => {
          try { await api("/api/secret", { body: { name: "lab_token", value: "" } }); await loadSettings(); toast("Token removed"); } catch (e) { toast(e.message, "err"); }
        } }, "Remove") : null)),
      hud.lab_url ? h("label", { class: "field" }, h("span", { text: "Server name" }), name) : null));
}

// ---------------------------------------------------------------- 07 Startup
function buildStartup() {
  const card = $("#startup-card");
  const st = S.status ? S.status.startup : { enabled: false, supported: false };
  setKids(card, h("div", { class: "rows" },
    row("Launch PandaDeck when I log in", st.supported ? null : "Not available on this system yet.",
      toggle(st.enabled, async (v) => {
        try { await api("/api/autostart", { body: { enabled: v } }); toast(v ? "PandaDeck will start with your computer" : "Removed from startup"); await refresh(); }
        catch (e) { toast(e.message, "err"); buildStartup(); }
      }, { disabled: !st.supported, label: "Launch at login" })),
    row("Settings page", null, h("code", { text: `http://127.0.0.1:${S.meta.port || location.port}/` })),
    row("Settings folder", h("span", { class: "mono", text: S.meta.config_dir || "" }),
      h("button", { type: "button", class: "small", onclick: () => api("/api/open", { body: { what: "config" } }).catch((e) => toast(e.message, "err")) }, icon("folder"), "Open")),
    row("Logs", h("span", { class: "mono", text: S.meta.log_dir || "" }),
      h("button", { type: "button", class: "small", onclick: () => api("/api/open", { body: { what: "logs" } }).catch((e) => toast(e.message, "err")) }, icon("folder"), "Open"))));
}

// ---------------------------------------------------------------- extensions
// Optional extensions (absent from most builds) list themselves at /api/extensions
// and bring their own script, which calls PandaDeck.register(name, render).
const EXT = {};
window.PandaDeck = {
  h, icon, api, save, toast, toggle, seg, row, pill, bar, cardHead, confirmBox, keepFocus,
  get settings() { return S.settings; }, get status() { return S.status; }, get meta() { return S.meta; },
  register(name, render) { EXT[name] = render; buildExtension(name); },
};

function buildExtension(name) {
  const card = $("#ext-card-" + name);
  if (!card || !EXT[name] || !S.settings) return;
  keyed("#ext-card-" + name, [S.settings.ext, S.status && (S.status.ext || {})[name]], () => {
    try { EXT[name](card); } catch (e) { setKids(card, h("p", { class: "note err", text: "This section failed to load: " + e.message })); }
  });
}

async function loadExtensions() {
  let list = [];
  try { list = await api("/api/extensions"); } catch (_) { return; }
  const core = document.querySelectorAll("main .section").length;
  list.forEach((ext, i) => {
    const num = String(core + i + 1).padStart(2, "0");
    const id = "ext-" + ext.name;
    $("#nav").append(h("a", { href: "#" + id }, h("span", { class: "num", text: num }), ext.title));
    $("#main").append(h("section", { class: "section", id },
      h("header", { class: "sec-head" }, h("span", { class: "eyebrow", text: `${num} — ${ext.title}` }), h("h2", { text: ext.title })),
      h("div", { class: "card", id: "ext-card-" + ext.name })));
    if (ext.style) document.head.append(h("link", { rel: "stylesheet", href: ext.style }));
    document.head.append(h("script", { src: ext.script, defer: true }));
  });
  navSpy();
}

// ---------------------------------------------------------------- status + nav
function keyed(sel, key, fn) {
  const el = $(sel);
  const k = JSON.stringify(key);
  if (el.dataset.k === k || keepFocus(el) || el.dataset.dragging === "1") return;  // never yank a field from under the user
  el.dataset.k = k;
  fn();
}

function buildStatusParts() {
  const st = S.status;
  if (!st) return;
  const p = st.panel;
  $("#foot-version").textContent = `PandaDeck ${st.version} · ${({ windows: "Windows", macos: "macOS", linux: "Linux" })[st.os] || st.os}`;
  const pw = $("#platform-warn");
  pw.hidden = st.platform.ok;
  if (!st.platform.ok) pw.textContent = "Some features (your apps, system media, volume, launch at login) aren't available on this system yet: " + st.platform.error;
  keyed("#panel-hero", [p.state, p.kind, p.mode, p.where, p.fw, p.wifi, p.settings, p.error, p.busy, Math.ceil((p.usb_paused || 0) / 60), S.settings.pages, S.settings.deck.apps, S.suggested.length, S.apps.length], renderHero);
  const inst = $("#install-card");
  const showInst = (p.state === "searching" && !p.usb_paused) || installForced;
  if (showInst && inst.hidden) { buildInstall(); inst.hidden = false; }
  else if (!showInst) inst.hidden = true;
  if (!inst.hidden) { const jv = $("#install-card .jobview"); if (jv) jv.replaceWith(jobView("install")); }
  keyed("#display-card", [p.state, p.settings], buildDisplay);
  keyed("#wifi-card", [p.state, p.kind, p.wifi, S.ssids], buildWifi);
  if (p.state === "connected" && !releaseAsked && !st.demo) checkRelease();  // once per page load
  keyed("#firmware-card", [p.state, p.fw, p.has_token, S.release, S.token, S.settings.panel], buildFirmware);
  const fv = $("#firmware-card .jobview");
  if (fv) fv.replaceWith(jobView("panel"));
  buildNowPlaying();
  buildPc();
  keyed("#startup-card", [st.startup, S.meta.config_dir], buildStartup);
  keyed("#hooks-card", [st.ai.hooks, st.ai.hook_errors, st.ai.session_list, st.ai.sessions, S.settings.ai], buildHooks);
  keyed("#usage-card", [st.ai.values, st.ai.usage, st.ai.claude_cli, st.ai.codex_auth, S.settings.ai], buildUsage);
  buildRailStatus();
  refreshUsbNotes();
  keyed("#sounds-card", [st.deck.picture_choices, S.settings.deck.sounds, S.settings.deck.pages], buildSounds);
  keyed("#lab-card", [st.hud.lab.state, st.hud.lab.error, st.hud.lab.host, S.settings.hud, S.secrets.lab_token], buildLab);
  refreshLabLive();
  keyed("#spotify-help", [st.music.spotify.signed_in, st.music.spotify.name, st.music.spotify.error, st.music.spotify.last_result,
    st.music.spotify.can_like, S.settings.music.spotify_client_id, S.meta.redirect_uri], buildSpotify);
  for (const [id, [kind, text]] of Object.entries(railStatus(st, p))) {
    const d = $("#nav-dot-" + id);
    if (!d) continue;
    d.className = "dot " + kind;
    d.parentElement.title = text;
    d.setAttribute("aria-label", text);
  }
  for (const name of Object.keys(EXT)) buildExtension(name);
}

/* The side rail's dots, one meaning everywhere: "ok" (green) working right now, "" (grey) fine but
 * nothing going on, "warn" (amber) needs a look, "err" (red) broken. Sections that are only settings
 * (Pages, Startup, ...) have no dot. Returns {section: [kind, hover text]}. */
function railStatus(st, p) {
  const now = st.music.now;
  const aiErr = Object.keys(st.ai.hook_errors).length || st.ai.usage.claude.state === "error" || st.ai.usage.codex.state === "error";
  const lab = st.hud.lab.state;
  const pcLive = st.hud.pc && typeof st.hud.pc.cpu === "number";
  return {
    panel: p.state === "connected" ? ["ok", "Panel: connected"]
      : p.state === "searching" ? ["warn", "Panel: looking for it…"] : ["", "Panel: not connected"],
    deck: st.deck.error ? ["warn", "Deck: " + st.deck.error]
      : st.deck.ready ? ["ok", "Deck: on the panel"] : ["", "Deck: loading your apps…"],
    music: st.music.spotify.error ? ["warn", "Music: Spotify needs a look"]
      : now && now.playing ? ["ok", "Music: playing"] : ["", now ? "Music: paused" : "Music: nothing playing right now"],
    ai: aiErr ? ["err", "AI: something needs a look"]
      : st.ai.sessions ? ["ok", `AI: ${st.ai.sessions} session${st.ai.sessions > 1 ? "s" : ""} running`]
      : ["", "AI: no sessions right now"],
    hud: lab === "error" ? ["err", "HUD: the homelab isn't answering"]
      : pcLive ? ["ok", "HUD: live"] : ["", "HUD: waiting for stats"],
  };
}

function buildAll() {
  if (!S.settings) return;
  for (const el of document.querySelectorAll("[data-k]")) delete el.dataset.k;
  buildPages(); buildStyle(); buildApps(); buildMacros(); buildSounds(); buildDeckPages(); buildCustom(); buildSource(); buildAiPage(); buildLabHelp();
  if (!keepFocus($("#pc-card"))) buildPc(true);
  if (S.status) buildStatusParts();
  else { buildSpotify(); buildHooks(); buildUsage(); buildLab(); buildFirmware(); buildDisplay(); buildWifi(); }
}

async function refresh() {
  try {
    S.status = await api("/api/status");
    if (S.offline) { S.offline = false; $("#offline").hidden = true; }
    buildStatusParts();
  } catch (_) {
    S.offline = true;
    $("#offline").hidden = false;
  }
}

async function loadSettings() {
  const r = await api("/api/settings");
  S.settings = r.settings; S.secrets = r.secrets; S.meta = r.meta;
  buildAll();
}

async function loadApps() {
  try {
    const r = await api("/api/apps");
    S.apps = r.apps; S.appsReady = r.ready; S.suggested = r.suggested || []; S.appsError = r.error || "";
    buildApps(); buildMacros(); buildDeckPages(); buildCustom();
    if (S.status) renderHero();
    if (!r.ready) setTimeout(loadApps, 2000);
  } catch (e) { setTimeout(loadApps, 4000); }
}

let spy = null;
function navSpy() {
  if (spy) spy.disconnect();
  const links = () => [...document.querySelectorAll("#nav a")];
  const obs = spy = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) {
      for (const a of links()) a.classList.toggle("on", a.getAttribute("href") === "#" + e.target.id);
    }
  }, { rootMargin: "-30% 0px -60% 0px" });
  for (const s of document.querySelectorAll("main .section")) obs.observe(s);
}

async function init() {
  wireButtonDialog();
  navSpy();
  $("#open-config").addEventListener("click", () => api("/api/open", { body: { what: "config" } }).catch((e) => toast(e.message, "err")));
  const q = new URLSearchParams(location.search);
  const sp = q.get("spotify");
  if (sp) {
    toast(sp === "ok" ? "Signed in to Spotify" : sp === "denied" ? "Spotify sign-in was cancelled" : "Spotify sign-in failed", sp === "ok" ? "ok" : "err");
    history.replaceState(null, "", location.pathname + location.hash);
  }
  try { await loadSettings(); } catch (e) { $("#offline").hidden = false; }
  await refresh();
  loadExtensions();
  loadApps();
  setInterval(refresh, 2000);
  goToAnchor();
  window.addEventListener("hashchange", goToAnchor);
}

/* Section anchors, plus a few deep links the panel opens (settings:<section>). */
const ANCHORS = { "#music-spotify": "#spotify-help" };
function goToAnchor() {
  const sel = ANCHORS[location.hash] || location.hash;
  if (!sel || sel === "#") return;
  let t = null;
  try { t = document.querySelector(sel); } catch (_) { return; }
  if (!t) return;
  t.scrollIntoView({ block: ANCHORS[location.hash] ? "center" : "start" });
  if (ANCHORS[location.hash]) { t.classList.remove("flash"); void t.offsetWidth; t.classList.add("flash"); }
}

document.addEventListener("DOMContentLoaded", init);
