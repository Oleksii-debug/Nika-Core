"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync(process.argv[2], "utf8");
const declarationStart = source.indexOf("  const taskMutationActions = new Set(");
const declarationEnd = source.indexOf("  let bridgeInitializationStarted = false;", declarationStart);
const dispatchResponseStart = source.indexOf("  function validDispatchResponse(");
const dispatchResponseEnd = source.indexOf("  function requestId() {", dispatchResponseStart);
const dispatchStart = source.indexOf("  async function dispatch(actionId, trigger = null) {");
const dispatchEnd = source.indexOf("  async function refreshKeymap() {", dispatchStart);
const stateRefreshStart = source.indexOf("  async function refreshState(");
const stateRefreshEnd = source.indexOf("  async function dispatch(", stateRefreshStart);
const pollingStart = source.indexOf("  function startStatePolling() {");
const pollingEnd = source.indexOf("  async function initializeBridge() {", pollingStart);
assert(declarationStart >= 0 && declarationEnd > declarationStart);
assert(dispatchResponseStart >= 0 && dispatchResponseEnd > dispatchResponseStart);
assert(dispatchStart >= 0 && dispatchEnd > dispatchStart);
assert(stateRefreshStart >= 0 && stateRefreshEnd > stateRefreshStart);
assert(pollingStart >= 0 && pollingEnd > pollingStart);

const factory = new Function("context", `
  const {
    globalThis, announce, appendLog, requestId, commandInput, sourceInputs, refreshState,
    reportStateUnavailable, document, focusElementById, dispatchAutostart, refreshKeymap,
    validKeymapResponse,
  } = context;
  let sourceRevision = 5;
  let sourceDirty = true;
  let actionsReady = true;
  ${source.slice(declarationStart, declarationEnd)}
  ${source.slice(dispatchResponseStart, dispatchResponseEnd)}
  ${source.slice(dispatchStart, dispatchEnd)}
  return {
    dispatch, mutateKeymap, getSourceDirty: () => sourceDirty,
    getActionsReady: () => actionsReady,
  };
`);

const stateFactory = new Function("context", `
  const {
    globalThis, renderAutostart, reportStateUnavailable, renderSourceSetup, renderItems,
    tasksList, tasksEmpty, agentsList, agentsEmpty, workspacesList, workspacesEmpty,
    renderProductProject, renderTeamTask, announce, document, window,
  } = context;
  let autostartGeneration = 0;
  let stateReadGeneration = 0;
  let statePollHandle = null;
  let stateUnavailableReported = false;
  const inFlightActions = new Set();
  ${source.slice(stateRefreshStart, stateRefreshEnd)}
  ${source.slice(pollingStart, pollingEnd)}
  return {refreshState, startStatePolling, inFlightActions};
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
    globalThis: {pywebview: {api: {dispatch: async (request) => {
      requests.push(request);
      const response = await bridge(request);
      if (
        response
        && typeof response === "object"
        && !Array.isArray(response)
        && !Object.hasOwn(response, "request_id")
        && ["accepted", "completed", "failed", "rejected"].includes(response.status)
      ) {
        return {...response, request_id: request.request_id};
      }
      return response;
    }}}},
    announce: (message, assertive) => messages.push([message, assertive]),
    appendLog: (message) => logs.push(message),
    requestId: () => `req-${++nextId}`,
    commandInput: {value: "  Створити завдання  "},
    sourceInputs: {root: {value: "C:\\\\Українська папка"}, source_a: {value: "а.txt"}, source_b: {value: "б.txt"}},
    refreshState: async () => {stateReads += 1; return stateRead();},
    refreshKeymap: async () => {keymapReads += 1; return keymapReady;},
    reportStateUnavailable: () => messages.push(["Стан недоступний", true]),
    validKeymapResponse: (response, requireData = false) => Boolean(
      response
      && typeof response === "object"
      && !Array.isArray(response)
      && typeof response.ok === "boolean"
      && typeof response.message === "string"
      && (!requireData || !response.ok || typeof response.data === "string")
    ),
    document: {documentElement: {dataset: {nikaReady: "true"}}},
    focusElementById: (id) => {focusIds.push(id); return true;},
    dispatchAutostart: async () => {throw Error("autostart is a separate command boundary");},
  };
  let ui = factory(context);
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

  bridge = async () => ({status: "accepted", message: "Завдання прийнято.", focus_id: "tasks-heading"});
  stateRead = async () => true;
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 3);
  assert.equal(requests[2].request_id, "req-3");
  assert(messages.some(([message]) => message === "Завдання прийнято."));
  assert.equal(stateReads, 3, "accepted task must reconcile current state");
  console.log("PASS: accepted task acknowledgement is trusted and reconciled");

  let releaseSource;
  bridge = () => new Promise((resolve) => {releaseSource = resolve;});
  const sourceSave = ui.dispatch("team.sources.configure", trigger);
  await Promise.resolve();
  await ui.dispatch("team.sources.configure", trigger);
  assert.equal(requests.length, 4);
  assert.equal(requests[3].payload.revision, 5);
  assert.equal(requests[3].payload.source_a, "а.txt");
  assert.equal(ui.getSourceDirty(), true);
  releaseSource({status: "accepted", message: "Зміну прийнято."});
  await sourceSave;
  assert.equal(ui.getSourceDirty(), true, "accepted is not a completed source write");
  console.log("PASS: accepted source acknowledgement keeps the dirty revision");

  bridge = async () => ({status: "completed", message: "Збережено."});
  await ui.dispatch("team.sources.configure", trigger);
  assert.equal(requests.length, 5);
  assert.equal(ui.getSourceDirty(), false);
  console.log("PASS: completed source acknowledgement clears the dirty revision");

  let finishStateReconcile;
  stateRead = () => new Promise((resolve) => {finishStateReconcile = resolve;});
  bridge = async () => {throw Error("SECRET_CONNECTION_DETAIL");};
  const uncertainDispatch = ui.dispatch("task.create", trigger);
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(requests.length, 6);
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 6, "retry must stay blocked while reconciliation is pending");
  assert(messages.at(-1)[0].includes("Попередню команду"));
  finishStateReconcile(true);
  await uncertainDispatch;
  assert(messages.at(-1)[0].includes("Повтор заблоковано до перезапуску"));
  assert(!JSON.stringify(messages).includes("SECRET_CONNECTION_DETAIL"));
  assert(focusCount > 0, "keyboard focus restored after uncertain reconciliation");
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 6, "generic state reread must not authorize a fresh durable task mutation");
  assert(messages.at(-1)[0].includes("Попередню команду"));
  console.log("PASS: transport-uncertain durable task remains locked after successful state reread");
  ui = factory(context);

  stateRead = async () => true;
  bridge = async () => ({status: "unexpected", message: "false success"});
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 7, "malformed acknowledgement must not blind-retry");
  assert(messages.at(-1)[0].includes("Повтор заблоковано до перезапуску"));
  assert(!logs.includes("false success"), "malformed acknowledgement message is not trusted");
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 7, "malformed durable acknowledgement must remain locked after generic reread");
  console.log("PASS: malformed durable acknowledgement remains locked after reconciliation");
  ui = factory(context);

  bridge = async () => ({status: "accepted", message: "Записано."});
  stateRead = async () => false;
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 8);
  assert.equal(context.document.documentElement.dataset.nikaReady, "false");
  assert(messages.at(-1)[0].includes("Повтор заблоковано до перезапуску"));
  assert(logs.includes("Записано."));
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 8, "confirmed accepted effect with stale projection must not mint a new request");
  assert(messages.at(-1)[0].includes("Попередню команду"));
  console.log("PASS: confirmed accepted effect with stale projection retains durable mutation lock");
  ui = factory(context);

  stateRead = async () => true;
  bridge = async () => ({
    request_id: "stale-request",
    status: "completed",
    message: "STALE_RESPONSE_MUST_NOT_BE_TRUSTED",
  });
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 9);
  assert(messages.at(-1)[0].includes("Повтор заблоковано до перезапуску"));
  assert(!logs.includes("STALE_RESPONSE_MUST_NOT_BE_TRUSTED"));
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 9, "mismatched acknowledgement must retain the durable lock");
  console.log("PASS: mismatched command request ID is reconciled and never trusted");

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

  let finishKeymapReconcile;
  keymapReady = new Promise((resolve) => {finishKeymapReconcile = resolve;});
  const uncertainKeymap = ui.mutateKeymap(
    async () => {throw Error("PRIVATE_KEYMAP_TRANSPORT");}, null, keymapInput,
  );
  await Promise.resolve();
  await Promise.resolve();
  let duplicateKeymapCalled = false;
  await ui.mutateKeymap(async () => {
    duplicateKeymapCalled = true;
    return {ok: true, message: "duplicate"};
  }, null, keymapInput);
  assert.equal(duplicateKeymapCalled, false);
  assert(messages.at(-1)[0].includes("Зміна карти клавіш ще виконується"));
  finishKeymapReconcile(true);
  await uncertainKeymap;
  assert.equal(keymapReads, 2);
  assert(logs.some((message) => message.includes("Немає підтвердження зміни клавіш")));
  assert(!JSON.stringify(messages).includes("PRIVATE_KEYMAP_TRANSPORT"));
  assert(messages.at(-1)[0].includes("Карту клавіш перечитано"));
  console.log("PASS: uncertain keymap mutation remains locked until exact map reread");

  keymapReady = true;
  await ui.mutateKeymap(async () => ({ok: "true", message: "bad"}), null, keymapInput);
  assert.equal(keymapReads, 3);
  assert(logs.some((message) => message.includes("непідтверджену зміну")));
  assert(messages.at(-1)[0].includes("Карту клавіш перечитано"));
  console.log("PASS: malformed keymap acknowledgement reconciles before retry");

  keymapReady = false;
  await ui.mutateKeymap(async () => ({ok: true, message: "Прийнято."}), "keymap-save");
  assert.equal(ui.getActionsReady(), false, "unknown refreshed bindings cannot remain hotkey-ready");
  assert(messages.at(-1)[0].includes("Повтор змін заблоковано"));
  assert(logs.includes("Прийнято."));
  let postFailureMutationCalled = false;
  await ui.mutateKeymap(async () => {
    postFailureMutationCalled = true;
    return {ok: true, message: "should not run"};
  }, null, keymapInput);
  assert.equal(postFailureMutationCalled, false);
  console.log("PASS: confirmed keymap write with failed reread disables hotkeys and retains lock");

  const stateWaiters = [];
  const stateMarkers = [];
  let stateOutages = 0;
  let stateApiCalls = 0;
  let pollingCallback = null;
  let stateReader = () => new Promise((resolve, reject) => {
    stateWaiters.push({resolve, reject});
  });
  const stateDocument = {
    hidden: false,
    documentElement: {dataset: {nikaReady: "true"}},
  };
  const stateUi = stateFactory({
    globalThis: {pywebview: {api: {get_state: () => {
      stateApiCalls += 1;
      return stateReader();
    }}}},
    renderAutostart: () => {},
    reportStateUnavailable: () => {stateOutages += 1;},
    renderSourceSetup: (selection) => {
      if (selection?.marker) stateMarkers.push(selection.marker);
    },
    renderItems: () => {},
    tasksList: {replaceChildren() {}}, tasksEmpty: {},
    agentsList: {replaceChildren() {}}, agentsEmpty: {},
    workspacesList: {replaceChildren() {}}, workspacesEmpty: {},
    renderProductProject: () => true,
    renderTeamTask: () => ({ok: true, changed: false}),
    announce: () => {},
    document: stateDocument,
    window: {setInterval: (callback) => {pollingCallback = callback; return 1;}},
  });

  const staleStateRead = stateUi.refreshState();
  const currentStateRead = stateUi.refreshState();
  stateWaiters[1].resolve({
    ok: true,
    state: {v01_sources: {marker: "current"}, tasks: [], agents: [], workspaces: []},
  });
  assert.equal(await currentStateRead, true);
  stateWaiters[0].resolve({
    ok: true,
    state: {v01_sources: {marker: "stale"}, tasks: [], agents: [], workspaces: []},
  });
  assert.equal(await staleStateRead, null);
  assert.deepEqual(stateMarkers, ["current"]);
  console.log("PASS: stale overlapping state response cannot overwrite the current projection");

  const staleFailure = stateUi.refreshState();
  const currentAfterFailure = stateUi.refreshState();
  stateWaiters[3].resolve({
    ok: true,
    state: {
      v01_sources: {marker: "current-after-failure"}, tasks: [], agents: [], workspaces: [],
    },
  });
  assert.equal(await currentAfterFailure, true);
  stateWaiters[2].reject(new Error("STALE_PRIVATE_TRANSPORT_DETAIL"));
  assert.equal(await staleFailure, null);
  assert.equal(stateOutages, 0, "superseded transport failure must not announce a false outage");
  assert.deepEqual(stateMarkers, ["current", "current-after-failure"]);
  console.log("PASS: stale overlapping state failure cannot downgrade a newer healthy projection");

  const outagesBeforeMalformed = stateOutages;
  stateReader = async () => ({
    ok: true,
    state: {tasks: {}, agents: [], workspaces: []},
  });
  assert.equal(await stateUi.refreshState(), false);
  assert.equal(stateOutages, outagesBeforeMalformed + 1);
  console.log("PASS: malformed summary collections fail closed without renderer exceptions");

  stateReader = async () => ({
    ok: true,
    state: {v01_sources: {marker: "polled"}, tasks: [], agents: [], workspaces: []},
  });
  stateUi.startStatePolling();
  assert.equal(typeof pollingCallback, "function");
  const callsBeforeLock = stateApiCalls;
  stateUi.inFlightActions.add("task-control");
  await pollingCallback();
  assert.equal(stateApiCalls, callsBeforeLock, "poll must pause during an in-flight mutation");
  stateUi.inFlightActions.delete("task-control");
  await pollingCallback();
  assert.equal(stateApiCalls, callsBeforeLock + 1);
  assert.equal(stateDocument.documentElement.dataset.nikaReady, "true");
  console.log("PASS: background polling pauses across in-flight command mutations");

  const logFunctionsStart = source.indexOf("  function announce(message, assertive = false) {");
  const logFunctionsEnd = source.indexOf("  function requestId() {", logFunctionsStart);
  const reportStart = source.indexOf("  function reportStateUnavailable() {");
  const reportEnd = source.indexOf("  function renderProductProject(project) {", reportStart);
  assert(logFunctionsStart >= 0 && logFunctionsEnd > logFunctionsStart);
  assert(reportStart >= 0 && reportEnd > reportStart);
  const recoveryReset = source.indexOf(
    "    stateUnavailableReported = false;", source.indexOf("  async function refreshState("),
  );
  assert(recoveryReset > 0, "healthy state must rearm outage reporting");

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
      renderTeamTaskUnavailable, renderStartupRecovery, renderModelSettings,
      productProjectUnavailableMessage,
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
    renderStartupRecovery: () => {},
    renderModelSettings: () => {},
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
