/* PandaDeck settings page: "✨ Generate with AI" in the button editor. The user's
 * own Codex (real pictures, from its image generation) or Claude Code (simple
 * vector icons, drawn to PNG by the server) on this computer makes 3 options;
 * picking one makes it the button's picture (the user's own: never replaced
 * automatically).
 * Uses app.js (h, api, S, toast) and deck.js (editing, setPicture, kindNow, ...). */
"use strict";

const AI_PREFS = "pandadeck.aiIcons";
const aiEngines = () => ((S.meta && S.meta.ai_icons && S.meta.ai_icons.engines) || []);
const aiWhat = (e) => (e.kind === "picture" ? "pictures" : "simple icons");
const aiEta = (e) => (e.kind === "picture" ? "about 40–90 s" : "about 20–60 s");
let aiBusy = false;

function aiPrefs() {
  try { return JSON.parse(localStorage.getItem(AI_PREFS) || "{}") || {}; } catch (_) { return {}; }
}
function aiSavePrefs(p) {
  try { localStorage.setItem(AI_PREFS, JSON.stringify(p)); } catch (_) { /* private mode: not remembered */ }
}

/* What the picture is about, from whatever the editor shows. */
function aiInfo() {
  const label = $("#b-label").value.trim() || $("#b-label").placeholder || "";
  // the button's picture: Codex's reference for what the subject looks like (the server never uses an AI one)
  const info = { label, color: editing.color || "", image: editing.image || "",
                 image_origin: (editing.art && editing.art.image_origin) || "" };
  if (editing.mode === "sound") {
    const s = soundInfo(editing.soundId) || {};
    Object.assign(info, { kind: "sound", sound: s.defLabel && s.defLabel !== label ? s.defLabel : label, link: editing.link || "" });
  } else if (editing.mode === "tile") {
    Object.assign(info, { kind: editing.entry && editing.entry.startsWith("target:") ? "target" : "app", app: editing.def || label });
  } else {
    const k = kindNow();
    info.kind = editing.pageId === MACRO_PAGE ? "macro" : k;
    if (k === "app") { const a = appById($("#b-app").value); if (a) info.app = a.name; }
    if (k === "sound") { const s = soundInfo($("#b-sound").value); if (s) info.sound = s.label; }
  }
  return info;
}

/* Called whenever the editor opens (deck.js fillCommonFields). */
function aiReset() {
  const btn = $("#b-ai-btn");
  if (!btn) return;
  btn.hidden = !aiEngines().length;
  $("#b-ai").hidden = true;
  setKids($("#b-ai-previews"));
  $("#b-ai-msg").textContent = "";
}

function aiOpen() {
  const box = $("#b-ai");
  if (!box.hidden) { box.hidden = true; return; }
  const prefs = aiPrefs();
  const engines = aiEngines();
  const pick = engines.some((e) => e.id === prefs.engine) ? prefs.engine : (engines[0] || {}).id;
  setKids($("#b-ai-engines"), engines.length > 1
    ? seg("aieng", engines.map((e) => [e.id, `${e.name} · ${aiWhat(e)}`]), pick,
      (v) => aiSavePrefs({ ...aiPrefs(), engine: v }), { label: "Which AI" })
    : h("span", { class: "tag", text: engines[0] ? `${engines[0].name} · ${aiWhat(engines[0])}` : "" }));
  $("#b-ai-style").value = prefs.style || "";
  $("#b-ai-msg").className = "hint";
  $("#b-ai-msg").textContent = "Uses your own " + engines.map((e) => e.name).join(" or ") + " on this computer."
    + (engines.some((e) => e.kind === "picture")
      ? " Codex looks the subject up on the web first, and works from the button's current picture when it has one."
      : "");
  box.hidden = false;
}

function aiEngineNow() {
  const r = document.querySelector("input[name=aieng]:checked");
  return r ? r.value : (aiEngines()[0] || {}).id;
}

async function aiGenerate() {
  if (aiBusy) return;
  const engine = aiEngineNow();
  const eng = aiEngines().find((e) => e.id === engine) || { name: "AI" };
  const name = eng.name;
  const style = $("#b-ai-style").value.trim();
  aiSavePrefs({ ...aiPrefs(), engine, style });
  const msg = $("#b-ai-msg");
  msg.className = "hint";
  msg.textContent = `Asking ${name} for 3 ${aiWhat(eng)}… (${aiEta(eng)})`;
  aiBusy = true;
  $("#b-ai-go").disabled = true;
  setKids($("#b-ai-previews"));
  try {
    let st = await api("/api/ai/icons", { body: { engine, info: aiInfo(), style } });
    const t0 = Date.now();
    while (st.state === "running") {
      const secs = Math.round((Date.now() - t0) / 1000);
      msg.textContent = st.stage === "drawing" ? "Drawing the pictures…"
        : `${name} is making 3 ${aiWhat(eng)}… ${secs} s (${aiEta(eng)})`;
      await new Promise((r) => setTimeout(r, 600));
      st = await api("/api/ai/icons");
    }
    if (st.state !== "done") throw new Error(st.error || "No pictures came back.");
    setKids($("#b-ai-previews"), ...st.previews.map((p) => h("button", { type: "button", class: "art-choice ai",
      title: "Use this picture", onclick: () => aiPick(p.cid) }, h("img", { src: p.thumb, alt: "" }))),
      h("button", { type: "button", class: "small ghost", onclick: aiGenerate }, "Again"));
    msg.textContent = "Pick one, or press Again for new ideas.";
  } catch (e) {
    msg.className = "hint err-text";
    msg.textContent = e.message;
  } finally {
    aiBusy = false;
    $("#b-ai-go").disabled = false;
  }
}

async function aiPick(cid) {
  try {
    const r = await api("/api/ai/icons/choose", { body: { cid } });
    setPicture(r.image);                       // the user's own picture from now on
    if (editing.mode === "sound") editing.art = { image_auto: false, image_origin: "ai", image_src: "" };
    if (typeof renderArt === "function" && editing.mode === "sound") renderArt("Picture: made with AI (yours)");
    $("#b-ai").hidden = true;
  } catch (e) {
    $("#b-ai-msg").className = "hint err-text";
    $("#b-ai-msg").textContent = e.message;
  }
}

function wireAiIcons() {
  if (!$("#b-ai-btn")) return;
  $("#b-ai-btn").addEventListener("click", aiOpen);
  $("#b-ai-go").addEventListener("click", aiGenerate);
  $("#b-ai-style").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); aiGenerate(); } });
}
