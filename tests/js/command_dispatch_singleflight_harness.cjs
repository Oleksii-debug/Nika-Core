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
    getForegroundStateRefreshPending: () => foregroundStateRefreshPending,
  };
`);

async function main() {
  let nextId = 0;
  let bridge = null;
  let stateRead = async () => true;
  let stateReads = 0;
  const stateReadOptions = [];
  let focusCount = 0;
  let keymapReads = 0;
  let keymapReady = true;
  const requests = [];
  const messages = [];
  const logs = [];
  const focusIds = [];
  const canonicalAck = (request, response) => ({
    request_id: request.request_id,
    focus_id: null,
    ...response,
  });
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
    refreshState: async (options = {}) => {
      stateReads += 1;
      stateReadOptions.push(options);
      return stateRead(options);
    },
    refreshKeymap: async () => {keymapReads += 1; return keymapReady;},
    reportStateUnavailable: () => messages.push(["Стан недоступний", true]),
    document: {documentElement: {dataset: {nikaReady: "true"}}},
    focusElementById: (id) => {focusIds.push(id); return true;},
    dispatchAutostart: async () => {throw Error("autostart is a separate command boundary");},
  };
  let ui = factory(context);
  let completeFirst;
  bridge = (request) => new Promise((resolve) => {
    completeFirst = (response) => resolve(canonicalAck(request, response));
  });
  const first = ui.dispatch("task.create", trigger);
  await Promise.resolve();
  assert.equal(ui.getForegroundStateRefreshPending(), 1, "foreground dispatch must block polling");
  await ui.dispatch("task.create", trigger);
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 1, "duplicate task and parallel control must not dispatch");
  assert(messages.at(-1)[0].includes("Попередню команду"));
  console.log("PASS: blocked task control gives an accessible pending explanation");
  assert.equal(requests[0].payload.command, "Створити завдання");
  assert.equal(requests[0].request_id, "req-1");
  completeFirst({status: "completed", message: "Створено.", focus_id: null});
  await first;
  assert.equal(ui.getForegroundStateRefreshPending(), 0, "foreground fence must release after reconciliation");
  assert.equal(stateReads, 1);
  assert.deepEqual(focusIds, ["tasks-heading"]);
  console.log("PASS: duplicate and cross-command single-flight");

  bridge = async (request) => canonicalAck(request, {status: "completed", message: "Призупинено."});
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 2);
  assert.equal(requests[1].request_id, "req-2");
  console.log("PASS: command unlocks after acknowledged completion");

  bridge = async (request) => canonicalAck(
    request, {status: "accepted", message: "Завдання прийнято.", focus_id: "tasks-heading"},
  );
  stateRead = async () => true;
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 3);
  assert.equal(requests[2].request_id, "req-3");
  assert(messages.some(([message]) => message === "Завдання прийнято."));
  assert.equal(stateReads, 3, "accepted task must reconcile current state");
  console.log("PASS: accepted task acknowledgement is trusted and reconciled");

  let releaseSource;
  bridge = (request) => new Promise((resolve) => {
    releaseSource = (response) => resolve(canonicalAck(request, response));
  });
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

  bridge = async (request) => canonicalAck(request, {status: "completed", message: "Збережено."});
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
  assert.equal(ui.getForegroundStateRefreshPending(), 1, "uncertain reconciliation must retain foreground priority");
  assert.equal(
    context.document.documentElement.dataset.nikaReady,
    "false",
    "Uncertain durable effect must drop packaged readiness before reconciliation completes",
  );
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 6, "retry must stay blocked while reconciliation is pending");
  assert(messages.at(-1)[0].includes("Попередню команду"));
  finishStateReconcile(true);
  await uncertainDispatch;
  assert.equal(ui.getForegroundStateRefreshPending(), 0, "foreground priority must release even when durable retry remains fenced");
  assert(messages.at(-1)[0].includes("Повтор заблоковано до перезапуску"));
  assert.equal(stateReadOptions.at(-1).requireCurrentGeneration, true);
  assert(!JSON.stringify(messages).includes("SECRET_CONNECTION_DETAIL"));
  assert(focusCount > 0, "keyboard focus restored after uncertain reconciliation");
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 6, "current-generation state reread must not authorize a fresh durable task mutation");
  assert(messages.at(-1)[0].includes("Попередню команду"));
  console.log("PASS: transport-uncertain durable task remains locked after current-generation reread");
  ui = factory(context);

  stateRead = async () => true;
  bridge = async (request) => canonicalAck(request, {status: "unexpected", message: "false success"});
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, 7, "malformed acknowledgement must not blind-retry");
  assert(messages.at(-1)[0].includes("Повтор заблоковано до перезапуску"));
  assert(!logs.includes("false success"), "malformed acknowledgement message is not trusted");
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, 7, "malformed durable acknowledgement must remain locked after current-generation reread");
  console.log("PASS: malformed durable acknowledgement remains locked after reconciliation");
  ui = factory(context);

  bridge = async (request) => canonicalAck(request, {status: "accepted", message: "Записано."});
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

  const requestsBeforeStrictFailure = requests.length;
  bridge = async () => {throw Error("UNCERTAIN_AFTER_READY_STATE");};
  stateRead = async () => false;
  await ui.dispatch("task.create", trigger);
  assert.equal(stateReadOptions.at(-1).requireCurrentGeneration, true);
  let blockedAfterStrictFailureCalled = false;
  bridge = async (request) => {
    blockedAfterStrictFailureCalled = true;
    return canonicalAck(request, {status: "completed", message: "must stay blocked"});
  };
  await ui.dispatch("task.pause", trigger);
  assert.equal(blockedAfterStrictFailureCalled, false);
  assert.equal(requests.length, requestsBeforeStrictFailure + 1);
  assert(messages.at(-1)[0].includes("Попередню команду"));
  assert(!JSON.stringify(messages).includes("UNCERTAIN_AFTER_READY_STATE"));
  console.log("PASS: failed current-generation reconciliation retains task-control lock");

  ui = factory(context);
  stateRead = async () => true;
  const beforeWrongRequestId = requests.length;
  bridge = async (request) => ({
    ...canonicalAck(request, {status: "completed", message: "НЕ ДОВІРЯТИ"}),
    request_id: "wrong-request-id",
  });
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, beforeWrongRequestId + 1);
  assert(!logs.includes("НЕ ДОВІРЯТИ"), "wrong request-id acknowledgement message is not trusted");
  await ui.dispatch("task.pause", trigger);
  assert.equal(
    requests.length,
    beforeWrongRequestId + 1,
    "wrong request-id durable acknowledgement must retain retry fence",
  );
  console.log("PASS: wrong request-id acknowledgement is uncertain and permanently fenced");

  ui = factory(context);
  const beforeBadMessage = requests.length;
  bridge = async (request) => canonicalAck(
    request, {status: "completed", message: {private: "BAD_MESSAGE_CANARY"}},
  );
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, beforeBadMessage + 1);
  assert(!JSON.stringify(messages).includes("BAD_MESSAGE_CANARY"));
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, beforeBadMessage + 1);
  console.log("PASS: non-text acknowledgement message is rejected before presentation");

  ui = factory(context);
  const beforeBadFocus = requests.length;
  bridge = async (request) => canonicalAck(
    request, {status: "completed", message: "НЕ ДОВІРЯТИ ФОКУСУ", focus_id: {id: "tasks-heading"}},
  );
  await ui.dispatch("task.create", trigger);
  assert.equal(requests.length, beforeBadFocus + 1);
  assert(!logs.includes("НЕ ДОВІРЯТИ ФОКУСУ"));
  await ui.dispatch("task.pause", trigger);
  assert.equal(requests.length, beforeBadFocus + 1);
  console.log("PASS: non-text acknowledgement focus target is rejected before use");

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

  const pollStart = source.indexOf("  function startStatePolling() {");
  const pollEnd = source.indexOf("  async function initializeBridge()", pollStart);
  assert(pollStart >= 0 && pollEnd > pollStart);
  const pollFactory = new Function("context", `
    const {window, document, refreshState} = context;
    let statePollHandle = null;
    let statePollPending = false;
    let foregroundStateRefreshPending = 0;
    ${source.slice(pollStart, pollEnd)}
    return {
      startStatePolling,
      setForegroundPending: (value) => { foregroundStateRefreshPending = value; },
      getPollPending: () => statePollPending,
    };
  `);
  let pollTick = null;
  let pollReads = 0;
  let finishPoll = null;
  let pollRead = () => new Promise((resolve) => { finishPoll = resolve; });
  const pollDocument = {hidden: false, documentElement: {dataset: {nikaReady: "true"}}};
  const polling = pollFactory({
    window: {
      setInterval: (callback, milliseconds) => {
        assert.equal(milliseconds, 1500);
        pollTick = callback;
        return 1;
      },
    },
    document: pollDocument,
    refreshState: async () => {
      pollReads += 1;
      return pollRead();
    },
  });
  polling.startStatePolling();
  assert.equal(typeof pollTick, "function");
  polling.setForegroundPending(1);
  await pollTick();
  assert.equal(pollReads, 0, "background poll must yield to foreground reconciliation");
  polling.setForegroundPending(0);
  const slowPoll = pollTick();
  await Promise.resolve();
  assert.equal(pollReads, 1);
  assert.equal(polling.getPollPending(), true);
  await pollTick();
  assert.equal(pollReads, 1, "slow background state reads must remain single-flight");
  finishPoll(true);
  await slowPoll;
  assert.equal(polling.getPollPending(), false);
  assert.equal(pollDocument.documentElement.dataset.nikaReady, "true");
  pollRead = async () => false;
  await pollTick();
  assert.equal(pollReads, 2, "polling must resume after the prior read settles");
  assert.equal(pollDocument.documentElement.dataset.nikaReady, "false");
  console.log("PASS: background state polling is single-flight and yields to foreground reconciliation");

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
