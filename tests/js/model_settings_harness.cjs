"use strict";
const fs = require("node:fs");
const assert = require("node:assert/strict");
const tick = () => new Promise((resolve) => setImmediate(resolve));

const listeners = {};
let poll;
class Element {}
class HTMLElement extends Element {
  constructor(id = "", tagName = "DIV") {
    super();
    this.id = id;
    this.tagName = tagName;
    this.type = "";
    this.textContent = "";
    this.dataset = {};
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.isContentEditable = false;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  replaceChildren(...items) { this.children = items; }
  appendChild(item) { this.children.push(item); return item; }
  append(...items) { this.children.push(...items); }
  focus() { if (!this.disabled) document.activeElement = this; }
  matches(selector) {
    if (selector === "input, textarea, select") {
      return ["INPUT", "TEXTAREA", "SELECT"].includes(this.tagName);
    }
    return false;
  }
  closest() { return this.dataset.actionId ? this : null; }
}
global.Element = Element;
global.HTMLElement = HTMLElement;

const elements = {};
function element(id, tag = "DIV") {
  if (!elements[id]) elements[id] = new HTMLElement(id, tag);
  return elements[id];
}
global.document = {
  activeElement: null,
  hidden: false,
  documentElement: element("documentElement", "HTML"),
  getElementById: (id) => element(id),
  createElement: (tag) => new HTMLElement("", String(tag).toUpperCase()),
  createTextNode: (text) => Object.assign(new HTMLElement(), { textContent: text }),
  addEventListener: (name, fn) => { listeners[name] = fn; },
};
global.window = {
  addEventListener() {},
  setInterval(fn) { poll = fn; return 1; },
  clearInterval() {},
};

const tags = {
  "model-route-kind": "SELECT",
  "model-provider": "INPUT",
  "model-name": "INPUT",
  "model-base-url": "INPUT",
  "model-credential-ref": "INPUT",
  "model-private-data": "INPUT",
  "model-timeout": "INPUT",
  "model-save": "BUTTON",
  "model-reload": "BUTTON",
  "autostart-enabled": "INPUT",
  "autostart-save": "BUTTON",
  "command-input": "TEXTAREA",
  "source-root": "INPUT",
  "source-a": "INPUT",
  "source-b": "INPUT",
  "keymap-json": "TEXTAREA",
};
for (const [id, tag] of Object.entries(tags)) {
  elements[id] = new HTMLElement(id, tag);
}
element("model-private-data").type = "checkbox";
element("autostart-enabled").type = "checkbox";
element("model-save").dataset.actionId = "settings.model.configure";
element("model-reload").dataset.actionId = "settings.model.refresh";
element("model-reload").dataset.errorFocusTarget = "model-settings-heading";
element("autostart-save").dataset.actionId = "settings.autostart.configure";

let currentModel = { status: "missing", revision: 0 };
let currentRecovery = {
  schema_version: 1,
  status: "ready",
  auto_resume_count: 0,
  manual_resume_count: 0,
  approval_count: 0,
  uncertain_count: 0,
  blocked_count: 0,
  resume_failed_count: 0,
};
let dispatchMode = "success";
let failRead = false;
const calls = [];

function safeModelSnapshot(payload) {
  return {
    status: "ready",
    revision: payload.revision + 1,
    route_kind: payload.route_kind,
    provider_id: payload.provider_id,
    provider_kind: payload.route_kind === "ollama" ? "local" : "cloud",
    model: payload.model,
    base_url: payload.base_url,
    timeout_seconds: payload.timeout_seconds,
    private_data_allowed: payload.route_kind === "ollama" ? true : payload.private_data_allowed,
    credential_configured: payload.credential_ref !== null,
  };
}

function snapshot() {
  return {
    ok: true,
    state: {
      tasks: [],
      agents: [],
      workspaces: [],
      autostart: { schema_version: 1, state: "disabled", can_change: true, message: "ok" },
      startup_recovery: currentRecovery,
      v01_sources: { status: "missing", revision: 0, root: "", source_a: "", source_b: "" },
      v01_model_settings: currentModel,
      product_project: null,
      v01_team_task: null,
    },
  };
}

global.pywebview = { api: {
  list_actions: async () => [],
  get_state: async () => {
    if (failRead) throw new Error("PRIVATE_MODEL_CANARY");
    return snapshot();
  },
  dispatch: async (command) => {
    calls.push(command);
    if (dispatchMode === "disconnect") throw new Error("PRIVATE_MODEL_CANARY");
    if (command.action_id === "settings.model.refresh") {
      return { status: "completed", message: "Збережені налаштування моделі перечитано.", focus_id: "model-route-kind" };
    }
    if (command.action_id === "settings.model.configure") {
      currentModel = safeModelSnapshot(command.payload);
      return { status: "completed", message: "Модель збережено для нових завдань.", focus_id: "command-input" };
    }
    return { status: "completed", message: "ok" };
  },
  export_keymap: async () => ({ ok: true, data: "{}", message: "ok" }),
  import_keymap: async () => ({ ok: true, message: "ok" }),
} };

eval(fs.readFileSync(process.argv[2], "utf8"));

function fire(target, name) {
  const fn = target.listeners[name];
  if (fn) fn({ target });
}
function click(target) {
  fire(target, "click");
  if (listeners.click) listeners.click({ target });
}
const route = element("model-route-kind");
const provider = element("model-provider");
const model = element("model-name");
const baseUrl = element("model-base-url");
const credential = element("model-credential-ref");
const privateData = element("model-private-data");
const timeout = element("model-timeout");
const save = element("model-save");
const reload = element("model-reload");
const status = element("model-settings-status");

(async () => {
  await tick(); await tick(); await tick();
  assert.equal(route.value, "ollama");
  assert.equal(provider.value, "ollama");
  assert.equal(provider.disabled, true);
  assert.equal(credential.disabled, true);
  assert.equal(baseUrl.value, "http://localhost:11434");
  assert.equal(timeout.value, "60");
  assert.equal(privateData.checked, true);
  assert.match(status.textContent, /ще не вибрано/);
  assert.match(element("recovery-status").textContent, /Перевірку відновлення завершено/);
  assert.equal(element("recovery-summary").hidden, false);
  assert.equal(element("recovery-auto-count").textContent, "0");
  assert.equal(element("recovery-uncertain-count").textContent, "0");

  model.focus();
  const recoveryFocus = document.activeElement;
  currentRecovery = {
    ...currentRecovery,
    status: "recovering",
    auto_resume_count: 1,
  };
  await poll();
  assert.equal(document.activeElement, recoveryFocus, "Recovery polling must not steal focus");
  assert.match(element("recovery-status").textContent, /crash-left/);
  assert.equal(element("recovery-auto-count").textContent, "1");

  currentRecovery = {
    ...currentRecovery,
    status: "attention",
    auto_resume_count: 0,
    uncertain_count: 1,
  };
  await poll();
  assert.equal(document.activeElement, recoveryFocus, "Attention state must not steal focus");
  assert.match(element("recovery-status").textContent, /невизначена або заблокована/i);
  assert.equal(element("recovery-uncertain-count").textContent, "1");
  assert.match(element("app-status").textContent, /невизначена або заблокована/i);

  currentRecovery = {
    ...currentRecovery,
    status: "ready",
    uncertain_count: 0,
  };
  await poll();
  assert.match(element("recovery-status").textContent, /Перевірку відновлення завершено/);

  model.focus();
  let prevented = false;
  listeners.keydown({
    target: model, key: "a", ctrlKey: true, altKey: false, shiftKey: false, metaKey: false,
    preventDefault() { prevented = true; },
  });
  assert.equal(prevented, false, "Standard editing keys in model input must not be intercepted");

  model.value = "qwen3:8b";
  fire(model, "input");
  const focusedBeforePoll = document.activeElement;
  await poll();
  assert.equal(model.value, "qwen3:8b", "Polling must preserve an unsaved model draft");
  assert.equal(document.activeElement, focusedBeforePoll, "Polling must not steal focus");
  assert.match(status.textContent, /ще не збережено/);

  click(save);
  await tick(); await tick(); await tick();
  const localCall = calls.find((call) => call.action_id === "settings.model.configure");
  assert.ok(localCall);
  assert.deepEqual(localCall.payload, {
    revision: 0,
    route_kind: "ollama",
    provider_id: "ollama",
    model: "qwen3:8b",
    base_url: "http://localhost:11434",
    credential_ref: null,
    private_data_allowed: true,
    timeout_seconds: 60,
  });
  assert.equal(document.activeElement, element("command-input"));
  assert.match(status.textContent, /ollama, qwen3:8b/);

  route.value = "openai_compatible";
  fire(route, "change");
  assert.equal(provider.disabled, false);
  assert.equal(credential.disabled, false);
  assert.equal(provider.value, "");
  assert.equal(baseUrl.value, "");
  provider.value = "lab-api";
  fire(provider, "input");
  model.value = "model-x";
  fire(model, "input");
  baseUrl.value = "https://api.example.test/v1";
  fire(baseUrl, "input");
  credential.value = "env:NIKA_TEST_API_KEY";
  fire(credential, "input");
  privateData.checked = false;
  fire(privateData, "change");
  timeout.value = "45";
  fire(timeout, "input");
  click(save);
  await tick(); await tick(); await tick();

  const apiCalls = calls.filter((call) => call.action_id === "settings.model.configure");
  assert.equal(apiCalls.length, 2);
  assert.deepEqual(apiCalls[1].payload, {
    revision: 1,
    route_kind: "openai_compatible",
    provider_id: "lab-api",
    model: "model-x",
    base_url: "https://api.example.test/v1",
    credential_ref: "env:NIKA_TEST_API_KEY",
    private_data_allowed: false,
    timeout_seconds: 45,
  });
  assert.equal(credential.value, "", "Credential reference must not be reflected from persisted snapshot");
  assert.match(status.textContent, /навмисно не показується/);
  assert.equal(JSON.stringify(currentModel).includes("NIKA_TEST_API_KEY"), false);

  model.focus();
  model.value = "unsaved-model";
  fire(model, "input");
  const dirtyFocus = document.activeElement;
  await poll();
  assert.equal(model.value, "unsaved-model");
  assert.equal(document.activeElement, dirtyFocus);

  currentModel = { ...currentModel, revision: currentModel.revision + 1, model: "external-model" };
  await poll();
  assert.equal(save.disabled, true, "Concurrent revision change must block stale save");
  assert.match(status.textContent, /іншому вікні/);
  click(reload);
  await tick(); await tick(); await tick();
  assert.equal(model.value, "external-model");
  assert.equal(save.disabled, false);

  currentModel = { status: "invalid" };
  await poll();
  assert.equal(save.disabled, true);
  assert.match(status.textContent, /пошкоджені або несумісні/);

  currentModel = {
    status: "ready", revision: 9, route_kind: "ollama", provider_id: "ollama",
    provider_kind: "local", model: "safe-model", base_url: "http://localhost:11434",
    timeout_seconds: 60, private_data_allowed: true, credential_configured: false,
  };
  failRead = false;
  await poll();
  model.focus();
  const beforeReadFailure = document.activeElement;
  failRead = true;
  await poll();
  assert.equal(document.activeElement, beforeReadFailure, "Read failure must not move focus");
  assert.equal(save.disabled, true);
  assert.equal(JSON.stringify(Object.values(elements).map((e) => e.textContent)).includes("PRIVATE_MODEL_CANARY"), false);

  failRead = false;
  await poll();

  currentRecovery = {
    schema_version: 1,
    status: "unknown",
    auto_resume_count: 0,
    manual_resume_count: 0,
    approval_count: 0,
    uncertain_count: 0,
    blocked_count: 0,
    resume_failed_count: 0,
  };
  const beforeInvalidRecovery = document.activeElement;
  await poll();
  assert.equal(document.activeElement, beforeInvalidRecovery, "Invalid recovery state must not steal focus");
  assert.match(element("recovery-status").textContent, /недоступний або несумісний/);
  currentRecovery = {
    schema_version: 1,
    status: "ready",
    auto_resume_count: 0,
    manual_resume_count: 0,
    approval_count: 0,
    uncertain_count: 0,
    blocked_count: 0,
    resume_failed_count: 0,
  };
  await poll();

  model.value = "no-blind-retry";
  fire(model, "input");
  dispatchMode = "disconnect";
  const beforeDisconnect = calls.length;
  click(save);
  await tick(); await tick(); await tick();
  assert.equal(calls.length, beforeDisconnect + 1, "Unknown write outcome must not be retried");
  assert.match(element("app-status").textContent, /Немає підтвердження зміни моделі/);
  assert.equal(JSON.stringify(Object.values(elements).map((e) => e.textContent)).includes("PRIVATE_MODEL_CANARY"), false);

  console.log("PASS: model settings + startup recovery renderer, draft/race, keyboard focus, safe credential reference, no blind retry");
})().catch((error) => { console.error(error); process.exitCode = 1; });
