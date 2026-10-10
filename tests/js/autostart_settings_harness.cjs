"use strict";
const fs = require("node:fs");
const assert = require("node:assert/strict");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const listeners = {};
let poll;
class Element {}
class HTMLElement extends Element {
  constructor(id = "") {
    super(); this.id = id; this.textContent = ""; this.dataset = {};
    this.children = []; this.attributes = {}; this.listeners = {};
    this.hidden = false; this.disabled = false; this.checked = false; this.value = "";
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  replaceChildren(...items) { this.children = items; }
  appendChild(item) { this.children.push(item); return item; }
  append(...items) { this.children.push(...items); }
  focus() { if (!this.disabled) document.activeElement = this; }
  matches() { return this.id === "autostart-enabled"; }
  closest() { return this.dataset.actionId ? this : null; }
}
global.Element = Element;
global.HTMLElement = HTMLElement;
const elements = {};
const element = (id) => elements[id] ??= new HTMLElement(id);
global.document = {
  activeElement: null, hidden: false, documentElement: element("documentElement"),
  getElementById: element, createElement: () => new HTMLElement(),
  createTextNode: (text) => Object.assign(new HTMLElement(), { textContent: text }),
  addEventListener: (name, fn) => { listeners[name] = fn; },
};
global.window = {
  addEventListener() {}, setInterval(fn) { poll = fn; return 1; }, clearInterval() {},
};
const input = element("autostart-enabled");
const save = element("autostart-save");
save.dataset.actionId = "settings.autostart.configure";
const reload = element("autostart-reload");
reload.dataset.actionId = "settings.autostart.refresh";
let current = "disabled";
let badSnapshot = null;
let dispatchMode = "success";
let releaseWrite;
let delayedRead;
let failRead = false;
const calls = [];
function snapshot(state = current) {
  return { ok: true, state: { tasks: [], agents: [], workspaces: [],
    autostart: badSnapshot ?? { schema_version: 1, state,
      can_change: ["enabled", "disabled", "stale"].includes(state), message: "PRIVATE_REGISTRY_CANARY" },
    startup_recovery: {
      schema_version: 1, status: "ready",
      auto_resume_count: 0, manual_resume_count: 0, approval_count: 0,
      uncertain_count: 0, blocked_count: 0, resume_failed_count: 0,
    },
  } };
}
global.pywebview = { api: {
  list_actions: async () => [],
  get_state: async () => {
    if (failRead) throw new Error("PRIVATE_REGISTRY_CANARY");
    if (delayedRead) { const read = delayedRead; delayedRead = null; return read; }
    return snapshot();
  },
  dispatch: async (command) => {
    calls.push(command);
    if (command.action_id.endsWith("refresh")) return { status: "completed", message: "Перечитано." };
    if (dispatchMode === "reject") return { status: "failed", message: "Зміну не підтверджено." };
    if (dispatchMode === "disconnect") throw new Error("PRIVATE_REGISTRY_CANARY");
    if (dispatchMode === "pending") await new Promise((resolve) => { releaseWrite = resolve; });
    current = command.payload.enabled ? "enabled" : "disabled";
    return { status: "completed", message: "Налаштування автозапуску збережено." };
  },
} };
eval(fs.readFileSync(process.argv[2], "utf8"));
const click = (target) => listeners.click({ target });
function choose(checked) { input.checked = checked; input.listeners.change(); }
const stateText = () => element("autostart-status").textContent;

(async () => {
  await tick(); await tick();
  assert.equal(input.checked, false);
  assert.equal(input.disabled, false);
  assert.match(stateText(), /Автозапуск вимкнено/);
  input.focus();
  let prevented = false;
  listeners.keydown({ target: input, key: " ", preventDefault() { prevented = true; } });
  assert.equal(prevented, false, "Native checkbox Space must not be intercepted");
  choose(true);
  await poll();
  assert.equal(input.checked, true, "Polling must preserve an explicitly labelled unsaved choice");
  assert.match(stateText(), /ще не збережено/);
  assert.equal(calls.length, 0, "Changing the checkbox alone must not persist");
  let oldRead;
  delayedRead = new Promise((resolve) => { oldRead = resolve; });
  const oldPoll = poll();
  dispatchMode = "pending";
  click(save); click(save);
  assert.equal(input.disabled, true);
  assert.equal(calls.length, 1, "Pending duplicate activation must not dispatch twice");
  releaseWrite(); await tick(); await tick();
  assert.deepEqual(calls[0].payload, { enabled: true });
  assert.equal(input.checked, true);
  assert.equal(document.activeElement, input);
  oldRead(snapshot("disabled")); await oldPoll; await tick();
  assert.equal(input.checked, true, "An older read must not replace the write acknowledgement");
  assert.match(stateText(), /Автозапуск увімкнено/);
  dispatchMode = "reject";
  choose(false); click(save); await tick(); await tick();
  assert.match(stateText(), /Автозапуск увімкнено/);
  assert.match(stateText(), /ще не збережено/);
  click(reload); await tick(); await tick();
  assert.equal(input.checked, true);
  assert.doesNotMatch(stateText(), /ще не збережено/);
  dispatchMode = "disconnect";
  choose(false); const beforeDisconnect = calls.length;
  click(save); await tick(); await tick();
  assert.equal(calls.length, beforeDisconnect + 1, "No blind retry after unknown write outcome");
  assert.match(element("app-status").textContent, /Немає підтвердження/);
  assert.match(stateText(), /Автозапуск увімкнено/);
  dispatchMode = "success";
  click(save); await tick(); await tick();
  assert.equal(input.checked, false);
  assert.match(stateText(), /Автозапуск вимкнено/);
  current = "stale"; await poll();
  assert.equal(input.disabled, false);
  assert.match(stateText(), /застарілий/);
  for (const broken of [
    { schema_version: 2, state: "enabled", can_change: true },
    { schema_version: 1, state: "enabled", can_change: false },
    { schema_version: 1, state: "PRIVATE_REGISTRY_CANARY", can_change: true },
  ]) {
    badSnapshot = broken; await poll();
    assert.equal(input.disabled, true);
    assert.equal(input.checked, false);
    assert.match(stateText(), /Не вдалося прочитати/);
  }
  badSnapshot = null;
  current = "unavailable"; await poll();
  assert.equal(input.disabled, true);
  assert.match(stateText(), /лише у зібраному застосунку Windows/);
  current = "enabled"; await poll();
  const priorFocus = document.activeElement;
  failRead = true; await poll();
  assert.equal(input.disabled, true);
  assert.equal(document.activeElement, priorFocus, "Polling must never move focus");
  assert.equal(JSON.stringify(Object.values(elements).map((e) => e.textContent)).includes("PRIVATE_REGISTRY_CANARY"), false);
  console.log("PASS: autostart renderer, native Space, draft, acknowledgement, failure, read race, focus, no retry");
})().catch((error) => { console.error(error); process.exitCode = 1; });
