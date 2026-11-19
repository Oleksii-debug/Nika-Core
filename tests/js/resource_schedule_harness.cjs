"use strict";
const fs = require("node:fs");
const assert = require("node:assert/strict");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const drain = async () => { await tick(); await tick(); await tick(); };
const listeners = {};
const unhandled = [];
process.on("unhandledRejection", (error) => { unhandled.push(error); });
let poll;
class Element {}
class HTMLElement extends Element {
  constructor(id = "", tag = "div") {
    super(); this.id = id; this.tagName = tag.toUpperCase(); this.textContent = "";
    this.dataset = {}; this.children = []; this.attributes = {}; this.listeners = {};
    this.hidden = false; this.disabled = false; this.checked = false; this.value = "";
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  replaceChildren(...items) { this.children = []; if (this.tagName === "SELECT") this.value = ""; this.append(...items); }
  appendChild(item) {
    this.children.push(item);
    if (this.tagName === "SELECT" && this.children.length === 1) this.value = item.value;
    return item;
  }
  append(...items) { for (const item of items) this.appendChild(item); }
  focus() { if (!this.disabled) document.activeElement = this; }
  matches(selector) { return selector.split(",").some((tag) => tag.trim().toUpperCase() === this.tagName); }
  closest() { return this.dataset.actionId ? this : null; }
}
global.Element = Element;
global.HTMLElement = HTMLElement;
const elements = {};
const nativeControls = {"resource-profile": "select", "schedule-task": "select", "command-input": "textarea",
  "schedule-delay": "input", "schedule-retry": "input", "schedule-window": "input"};
const element = (id) => elements[id] ??= new HTMLElement(id, nativeControls[id] ?? "div");
global.document = {
  activeElement: null, hidden: false, documentElement: element("documentElement"),
  getElementById: element, createElement: (tag) => new HTMLElement("", tag),
  createTextNode: (text) => Object.assign(new HTMLElement(), {textContent: text}),
  addEventListener: (name, fn) => { listeners[name] = fn; },
};
global.window = {addEventListener() {}, setInterval(fn) { poll = fn; return 1; }, clearInterval() {}};
const button = (id, action, errorFocus) => {
  const node = element(id); node.tagName = "BUTTON"; node.dataset.actionId = action;
  node.dataset.errorFocusTarget = errorFocus;
  return node;
};
const profile = element("resource-profile");
const profileSave = button("resource-profile-save", "settings.resources.configure", "resource-profile");
const create = button("schedule-create", "schedule.create", "schedule-delay");
const scheduleButtons = Object.fromEntries(["pause", "resume", "cancel"].map((verb) => [verb,
  button(`schedule-${verb}`, `schedule.${verb}`, "schedule-task")]));
const firstTask = "11111111-1111-4111-8111-111111111111";
const selectedTask = "22222222-2222-4222-8222-222222222222";
let selection = {profile: "normal", revision: 7};
let schedules = [{task_id: firstTask, status: "active", next_due_at: "2026-11-19T12:00:00+00:00"},
  {task_id: selectedTask, status: "paused", next_due_at: null}];
let readOverride;
let failRead = false;
let dispatchMode = "success";
let releaseWrite;
const calls = [];
const snapshot = (current = selection) => ({ok: true, state: {tasks: [], agents: [], workspaces: [],
  autostart: {schema_version: 1, state: "disabled", can_change: true},
  resource_profile: current, schedules}});
global.pywebview = {api: {
  list_actions: async () => [],
  get_state: async () => {
    if (failRead) throw new Error("PRIVATE_STATE_CANARY");
    if (readOverride) { const read = readOverride; readOverride = null; return read; }
    return snapshot();
  },
  dispatch: async (command) => {
    calls.push(command);
    if (dispatchMode === "disconnect") throw new Error("PRIVATE_WRITE_CANARY");
    if (dispatchMode === "reject") return {status: "rejected", message: "Зміни не підтверджено."};
    if (dispatchMode === "pending") await new Promise((resolve) => { releaseWrite = resolve; });
    if (command.action_id === "settings.resources.configure") {
      selection = {profile: command.payload.profile, revision: selection.revision + 1};
      return {status: "completed", message: "Профіль ресурсів збережено."};
    }
    return {status: "completed", message: "Розклад оновлено.", focus_id: "schedule-heading"};
  },
}};
eval(fs.readFileSync(process.argv[2], "utf8"));
const click = (target) => listeners.click({target});
const choose = (value) => { profile.value = value; profile.listeners.change(); };
const profileStatus = () => element("resource-profile-status").textContent;

(async () => {
  await drain();
  assert.equal(profile.disabled, false);
  assert.equal(profile.value, "normal");
  assert.match(profileStatus(), /Звичайний/);
  assert.equal(element("schedule-task").children.filter((option) => option.value).length, 2);
  assert.equal(element("schedule-task").value, "", "A schedule must be chosen explicitly before control actions");
  profile.focus();
  let prevented = false;
  listeners.keydown({target: profile, key: " ", preventDefault() { prevented = true; }});
  assert.equal(prevented, false, "Native select keyboard handling must remain available");
  choose("economy");
  await poll();
  assert.equal(profile.value, "economy", "Polling must preserve an unsaved explicit choice");
  assert.equal(calls.length, 0, "Changing a profile alone must not persist");
  assert.match(profileStatus(), /ще не збережено/);

  let oldRead;
  readOverride = new Promise((resolve) => { oldRead = resolve; });
  const oldPoll = poll();
  dispatchMode = "pending";
  click(profileSave); click(profileSave);
  assert.equal(profile.disabled, true);
  assert.equal(profileSave.disabled, true);
  assert.equal(calls.length, 1, "Pending save must not send duplicate writes");
  assert.deepEqual(calls[0].payload, {profile: "economy", revision: 7});
  releaseWrite(); await drain();
  assert.equal(profile.value, "economy");
  assert.equal(document.activeElement, profile);
  oldRead(snapshot({profile: "normal", revision: 7})); await oldPoll; await drain();
  assert.equal(profile.value, "economy", "Stale read must not overwrite an acknowledged choice");

  let draftRead;
  readOverride = new Promise((resolve) => { draftRead = resolve; });
  const draftPoll = poll();
  choose("low_memory");
  draftRead(snapshot({profile: "normal", revision: 7})); await draftPoll; await drain();
  assert.equal(profile.value, "low_memory", "Stale response must not overwrite a newer edit");
  dispatchMode = "reject";
  click(profileSave); await drain();
  assert.deepEqual(calls.at(-1).payload, {profile: "low_memory", revision: 8});
  assert.equal(profile.value, "economy", "Rejected write must show the durable saved selection");
  assert.equal(profile.disabled, false);
  assert.equal(document.activeElement, profile);
  assert.match(element("app-status").textContent, /не підтверджено/);

  choose("night_batch"); dispatchMode = "disconnect";
  const beforeUnknownWrite = calls.length;
  click(profileSave); await drain();
  assert.equal(calls.length, beforeUnknownWrite + 1, "Unknown write outcome must not cause automatic retry");
  assert.equal(profile.value, "economy");
  assert.equal(profile.disabled, false);
  assert.equal(document.activeElement, profile);
  assert.equal(unhandled.length, 0, "Profile transport failure must be handled");

  failRead = true; await poll();
  assert.equal(profile.disabled, true);
  assert.equal(profileSave.disabled, true);
  const beforeDisabledSave = calls.length;
  click(profileSave); await drain();
  assert.equal(calls.length, beforeDisabledSave);
  failRead = false; await poll();
  assert.equal(profile.disabled, false);
  for (const broken of [{profile: "PRIVATE_STATE_CANARY", revision: 9}, {profile: "normal", revision: 1.5},
    {profile: "economy", revision: Number.MAX_SAFE_INTEGER + 1}]) {
    selection = broken; await poll();
    assert.equal(profile.disabled, true);
  }
  selection = {profile: "economy", revision: 8}; await poll();

  dispatchMode = "success";
  element("command-input").value = "  Порівняй два джерела  ";
  element("schedule-delay").value = "30";
  element("schedule-retry").value = "12";
  element("schedule-window").value = "600";
  click(create); await drain();
  assert.equal(calls.at(-1).action_id, "schedule.create");
  assert.deepEqual(calls.at(-1).payload, {command: "Порівняй два джерела", delay_seconds: 30,
    retry_interval_seconds: 12, start_window_seconds: 600});
  assert.equal(document.activeElement, element("schedule-heading"));
  element("schedule-task").value = selectedTask;
  await poll();
  assert.equal(element("schedule-task").value, selectedTask, "Polling preserves an existing selected schedule");
  for (const verb of ["pause", "resume", "cancel"]) {
    click(scheduleButtons[verb]); await drain();
    assert.equal(calls.at(-1).action_id, `schedule.${verb}`);
    assert.deepEqual(calls.at(-1).payload, {task_id: selectedTask});
  }
  dispatchMode = "reject";
  click(create); await drain();
  assert.equal(document.activeElement, element("schedule-delay"), "Validation rejection returns to the labelled duration control");
  assert.match(element("app-status").textContent, /не підтверджено/);
  schedules = [{task_id: "not-a-task", status: "active", next_due_at: null}]; await poll();
  assert.equal(element("schedule-task").disabled, true);
  assert.equal(element("schedule-task").children.length, 0);

  dispatchMode = "disconnect";
  const beforeScheduleDisconnect = calls.length;
  const previousAnnouncement = element("app-status").textContent;
  click(create); await drain();
  assert.equal(calls.length, beforeScheduleDisconnect + 1);
  assert.equal(unhandled.length, 0, "Schedule transport failure must produce controlled feedback, not an unhandled rejection");
  assert.notEqual(element("app-status").textContent, previousAnnouncement, "Unknown write outcome needs a new user-facing announcement");
  assert.equal(element("app-status").attributes["aria-live"], "assertive");
  assert.equal(document.activeElement, element("schedule-delay"));
  assert.equal(JSON.stringify(Object.values(elements).map((node) => node.textContent)).includes("PRIVATE_"), false);
  console.log("PASS: resource/schedule renderer, typed payloads, selection, drafts, read races, failures, focus, native keyboard");
})().catch((error) => { console.error(error); process.exitCode = 1; });
