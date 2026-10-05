"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync(process.argv[2], "utf8");
const declarationStart = source.indexOf("  const taskMutationActions = new Set(");
const declarationEnd = source.indexOf("  let bridgeInitializationStarted = false;", declarationStart);
const dispatchStart = source.indexOf("  async function dispatch(actionId, trigger = null) {");
const dispatchEnd = source.indexOf("  async function refreshKeymap() {", dispatchStart);
assert(declarationStart >= 0 && declarationEnd > declarationStart);
assert(dispatchStart >= 0 && dispatchEnd > dispatchStart);

const factory = new Function("context", `
  const {
    globalThis, announce, appendLog, requestId, commandInput, sourceInputs, refreshState,
    reportStateUnavailable, document, focusElementById, dispatchAutostart, refreshKeymap,
  } = context;
  let sourceRevision = 5;
  let sourceDirty = true;
  let actionsReady = true;
  ${source.slice(declarationStart, declarationEnd)}
  ${source.slice(dispatchStart, dispatchEnd)}
  return {
    dispatch, mutateKeymap, getSourceDirty: () => sourceDirty,
    getActionsReady: () => actionsReady,
  };
`);

async function main() {
  let nextId = 0;
  let bridge = null;
  let stateRead = async () => true;
  let stateReads = 0;
  let focusCount = 0;
  let keymapReads = 0;
  let keymapReady = true;
  const requests = [];
  const messages = [];
  const logs = [];
  const focusIds = [];
  const trigger = {
    dataset: {focusTarget: "tasks-heading", errorFocusTarget: "command-input"},
    focus: () => { focusCount += 1; },
  };
  const context = {
    globalThis: {pywebview: {api: {dispatch: (request) => {
      requests.push(request);
      return bridge(request);
    }}}},
    announce: (message, assertive) => messages.push([message, assertive]),
    appendLog: (message) => logs.push(message),
    requestId: () => `req-${++nextId}`,
    commandInput: {value: "  Створити завдання  "},
    sourceInputs: {root: {value: "C:\\\\Українська папка"}, source_a: {value: "а.txt"}, source_b: {value: "б.txt"}},
    refreshState: async () => {stateReads += 1; return stateRead();},
    refreshKeymap: async () => {keymapReads += 1; return keymapReady;},
    reportStateUnavailable: () => messages.push(["Стан недоступний", true]),
    document: {documentElement: {dataset: {nikaReady: "true"}}},
    focusElementById: (id) => {focusIds.push(id); return true;},
    dispatchAutostart: async () => {throw Error("autostart is a separate command boundary");},
  };
  const ui = factory(context);
  let completeFirst;
  bridge = () => new Promise((resolve) => {completeFirst = resolve;});
  const first = ui.dispatch("task.create", trigger);
  await Promise.resolve();
  await ui.dispatch("task.create", trigger);
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 1, "duplicate task and parallel control must not dispatch");
  assert(messages.at(-1)[0].includes("Попередню команду"));
  console.log("PASS: blocked task control gives an accessible pending explanation");
  assert.equal(requests[0].payload.command, "Створити завдання");
  assert.equal(requests[0].request_id, "req-1");
  completeFirst({status: "completed", message: "Створено.", focus_id: null});
  await first;
  assert.equal(stateReads, 1);
  assert.deepEqual(focusIds, ["tasks-heading"]);
  console.log("PASS: duplicate and cross-command single-flight");

  bridge = async () => ({status: "completed", message: "Призупинено."});
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].request_id, "req-2");
  console.log("PASS: command unlocks after acknowledged completion");

  let releaseSource;
  bridge = () => new Promise((resolve) => {releaseSource = resolve;});
  const sourceSave = ui.dispatch("team.sources.configure", trigger);
  await Promise.resolve();
  await ui.dispatch("team.sources.configure", trigger);
  assert.equal(requests.length, 3);
  assert.equal(requests[2].payload.revision, 5);
  assert.equal(requests[2].payload.source_a, "а.txt");
  assert.equal(ui.getSourceDirty(), true);
  releaseSource({status: "completed", message: "Збережено."});
  await sourceSave;
  assert.equal(ui.getSourceDirty(), false);
  console.log("PASS: settings single-flight retains revision and dirty state until ACK");

  bridge = async () => {throw Error("SECRET_CONNECTION_DETAIL");};
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 4);
  assert.equal(stateReads, 3, "no state replay or blind retry on uncertain dispatch");
  assert(messages.at(-1)[0].includes("Немає підтвердження"));
  assert(!JSON.stringify(messages).includes("SECRET_CONNECTION_DETAIL"));
  assert(focusCount > 0, "keyboard focus restored on uncertain command");
  console.log("PASS: transport failure redacted; no blind retry");

  bridge = async () => ({status: "unexpected", message: "false success"});
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 5, "lock cleared after transport failure");
  assert(messages.at(-1)[0].includes("непідтверджений"));
  assert(!logs.includes("false success"), "malformed acknowledgement is not trusted");
  console.log("PASS: malformed acknowledgement fails closed");

  bridge = async () => ({status: "completed", message: "Записано."});
  stateRead = async () => false;
  await ui.dispatch("task.create", trigger);
  assert.equal(context.document.documentElement.dataset.nikaReady, "false");
  assert(messages.at(-1)[0].includes("Дію підтверджено"));
  assert(logs.includes("Записано."));
  console.log("PASS: confirmed effect distinguished from stale projection");
  let finishKeymap;
  const keymapInput = {focus: () => {focusCount += 1;}};
  const saveKeymap = ui.mutateKeymap(
    () => new Promise((resolve) => {finishKeymap = resolve;}),
    "keymap-save",
    keymapInput,
  );
  await ui.mutateKeymap(() => {throw Error("duplicate mutation");}, null, keymapInput);
  assert.equal(keymapReads, 0, "keymap must not refresh before its first ACK");
  assert(messages.at(-1)[0].includes("Зміна карти клавіш ще виконується"));
  console.log("PASS: blocked keymap mutation gives an accessible pending explanation");
  finishKeymap({ok: true, message: "Клавіші збережено."});
  await saveKeymap;
  assert.equal(keymapReads, 1);
  assert.equal(focusIds.at(-1), "keymap-save");
  console.log("PASS: keymap mutations single-flight and focus after ACK");

  await ui.mutateKeymap(
    async () => {throw Error("PRIVATE_KEYMAP_TRANSPORT");}, null, keymapInput,
  );
  assert(messages.at(-1)[0].includes("Немає підтвердження зміни клавіш"));
  assert(!JSON.stringify(messages).includes("PRIVATE_KEYMAP_TRANSPORT"));
  assert.equal(keymapReads, 1);
  await ui.mutateKeymap(async () => ({ok: "true", message: "bad"}), null, keymapInput);
  assert(messages.at(-1)[0].includes("непідтверджену зміну"));
  console.log("PASS: keymap failures and malformed ACKs fail closed without retry");

  keymapReady = false;
  await ui.mutateKeymap(async () => ({ok: true, message: "Прийнято."}), "keymap-save");
  assert.equal(ui.getActionsReady(), false, "unknown refreshed bindings cannot remain hotkey-ready");
  assert(messages.at(-1)[0].includes("Зміну підтверджено"));
  assert(logs.includes("Прийнято."));
  console.log("PASS: confirmed keymap write with failed refresh disables stale hotkeys");

  const logFunctionsStart = source.indexOf("  function announce(message, assertive = false) {");
  const logFunctionsEnd = source.indexOf("  function requestId() {", logFunctionsStart);
  const reportStart = source.indexOf("  function reportStateUnavailable() {");
  const reportEnd = source.indexOf("  function renderProductProject(project) {", reportStart);
  assert(logFunctionsStart >= 0 && logFunctionsEnd > logFunctionsStart);
  assert(reportStart >= 0 && reportEnd > reportStart);
  assert(source.includes(
    "    stateUnavailableReported = false;\\n    if (announceTeamTransitions && teamRender.changed) {".replace(
      "\\\\n", "\\n",
    ),
  ), "healthy state must rearm outage reporting");

  const entries = [];
  let listLabel = "";
  let alerts = 0;
  const activityLog = {
    get lastElementChild() {return entries.at(-1);},
    get firstElementChild() {return entries[0];},
    get childElementCount() {return entries.length;},
    appendChild(item) {
      item.remove = () => {entries.splice(entries.indexOf(item), 1);};
      entries.push(item);
    },
    setAttribute(name, value) {if (name === "aria-label") listLabel = value;},
  };
  const statusNode = {
    textContent: "",
    setAttribute() {},
  };
  const logFactory = new Function("context", `
    const {
      document, statusNode, activityLog, renderProductProjectUnavailable,
      renderTeamTaskUnavailable, productProjectUnavailableMessage,
    } = context;
    let stateUnavailableReported = false;
    const maxActivityItems = 200;
    ${source.slice(logFunctionsStart, logFunctionsEnd)}
    ${source.slice(reportStart, reportEnd)}
    return {
      appendLog, reportStateUnavailable, reset: () => {stateUnavailableReported = false;},
    };
  `);
  const uiLog = logFactory({
    document: {createElement: () => ({textContent: ""})},
    statusNode, activityLog,
    renderProductProjectUnavailable: () => {},
    renderTeamTaskUnavailable: () => {},
    productProjectUnavailableMessage: "Стан недоступний",
  });
  uiLog.appendLog("same message");
  uiLog.appendLog("same message");
  assert.equal(entries.length, 1, "immediate duplicate must not grow the log");
  for (let i = 0; i < 205; i += 1) uiLog.appendLog(`entry-${i}`);
  assert.equal(entries.length, 200);
  assert.equal(entries[0].textContent, "entry-5");
  assert.equal(entries.at(-1).textContent, "entry-204");
  assert(listLabel.includes("останні 200"), "log truncation must be accessible");
  console.log("PASS: transient UI log bounded, deduplicated and labeled");

  statusNode.setAttribute = () => {alerts += 1;};
  uiLog.reportStateUnavailable();
  uiLog.reportStateUnavailable();
  assert.equal(alerts, 1, "same outage must announce once");
  uiLog.reset();
  uiLog.reportStateUnavailable();
  assert.equal(alerts, 2, "fresh outage after recovery must announce again");
  console.log("PASS: backend outage announced once per episode and rearmed on recovery");
}
main().catch((error) => {console.error(error); process.exitCode = 1;});
