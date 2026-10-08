"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync(process.argv[2], "utf8");
assert(source.includes("let autostartUncertain = false;"));
const viewStart = source.indexOf("  function renderAutostart(");
const viewEnd = source.indexOf("  autostartInput?.addEventListener", viewStart);
const dispatchStart = source.indexOf("  async function dispatchAutostart(");
const dispatchEnd = source.indexOf("  function renderSourceSetup(", dispatchStart);
assert(viewStart > 0 && viewEnd > viewStart && dispatchStart > 0 && dispatchEnd > dispatchStart);

const factory = new Function("ctx",
  "const {globalThis, announce, appendLog, requestId, autostartInput, autostartSave, " +
  "autostartStatus, refreshState, focusElementById}=ctx;" +
  "let autostartDirty=true, autostartPending=false, autostartUncertain=false, autostartGeneration=0;" +
  source.slice(viewStart, viewEnd) + source.slice(dispatchStart, dispatchEnd) +
  "return {dispatchAutostart, renderAutostart, locked:()=>autostartUncertain, pending:()=>autostartPending, dirty:()=>autostartDirty};"
);

function caseWithBridge(handler) {
  const requests = [];
  const announced = [];
  const input = {checked: true, disabled: false, focus() {}};
  const save = {disabled: false};
  const status = {textContent: ""};
  const trigger = {disabled: false, focus() {}};
  let sequence = 0;
  let reads = 0;
  let ui;
  const context = {
    globalThis: {pywebview: {api: {dispatch: (request) => {
      requests.push(request);
      return handler(request);
    }}}},
    announce: (message, assertive) => announced.push([message, assertive]),
    appendLog: () => {},
    requestId: () => "autostart-" + ++sequence,
    autostartInput: input,
    autostartSave: save,
    autostartStatus: status,
    refreshState: async () => {
      reads += 1;
      ui.renderAutostart({schema_version: 1, state: "disabled", can_change: true});
      return true;
    },
    focusElementById: () => {},
  };
  ui = factory(context);
  return {ui, requests, announced, input, save, status, trigger, reads:()=>reads};
}

async function run() {
  const action = "settings.autostart.configure";
  const completed = (req) => ({request_id: req.request_id, status:"completed", message:"Готово"});
  let test = caseWithBridge(completed);
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.requests.length, 1);
  assert.equal(test.requests[0].request_id, "autostart-1");
  assert.equal(test.ui.locked(), false);
  assert.equal(test.save.disabled, false);
  assert.equal(test.ui.dirty(), false, "confirmed write clears edited state");
  console.log("PASS: matching autostart ACK permits normal keyboard operation");

  test = caseWithBridge(() => ({request_id:"foreign-request",status:"completed",message:"forged"}));
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.ui.locked(), true);
  assert.equal(test.save.disabled, true);
  assert.equal(test.ui.dirty(), true, "foreign ACK must preserve user intent");
  assert.equal(test.input.checked, true, "refresh must not overwrite uncertain edit");
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.requests.length, 1);
  console.log("PASS: foreign success ACK cannot unlock a second Windows write");

  test = caseWithBridge(() => ({status:"completed", message:"missing correlation"}));
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.ui.locked(), true);
  assert.equal(test.save.disabled, true);
  assert.equal(test.ui.dirty(), true, "missing ACK cannot clear pending edit");
  console.log("PASS: missing correlation fails closed");

  test = caseWithBridge((req) => ({request_id:req.request_id,status:"failed",message:"backend fault"}));
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.ui.locked(), true);
  assert.equal(test.ui.dirty(), true, "possibly committed failure preserves edit");
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.requests.length, 1);
  assert(test.announced.some(([message]) => message.includes("Повтор заблоковано")));
  console.log("PASS: potentially committed failed write does not retry");

  let rejectedOnce = true;
  test = caseWithBridge((req) => {
    if (rejectedOnce) {
      rejectedOnce = false;
      return {request_id:req.request_id,status:"rejected",message:"Rejected"};
    }
    return completed(req);
  });
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.ui.locked(), false);
  assert.equal(test.ui.dirty(), true, "rejected write keeps editable intent");
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.requests.length, 2);
  assert.equal(test.ui.locked(), false);
  console.log("PASS: pre-effect input rejection remains retryable");

  let resolveWrite;
  test = caseWithBridge((req) => req.action_id === action
    ? new Promise(resolve => {resolveWrite = () => resolve(completed(req));})
    : Promise.resolve(completed(req)));
  const pending = test.ui.dispatchAutostart(action, test.trigger);
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.requests.length, 1);
  assert.equal(test.ui.pending(), true);
  resolveWrite();
  await pending;
  assert.equal(test.ui.pending(), false);
  console.log("PASS: concurrent button/keyboard activation is single-flight");

  test = caseWithBridge((req) => req.action_id === action
    ? Promise.reject(new Error("private operating-system detail"))
    : Promise.resolve(completed(req)));
  await test.ui.dispatchAutostart(action, test.trigger);
  assert.equal(test.ui.locked(), true);
  await test.ui.dispatchAutostart("settings.autostart.refresh", test.trigger);
  assert.equal(test.requests.length, 2);
  assert.equal(test.ui.locked(), true);
  assert.equal(test.save.disabled, true);
  assert.equal(test.ui.dirty(), true, "transport uncertainty preserves edit");
  assert(!JSON.stringify(test.announced).includes("private operating-system detail"));
  console.log("PASS: transport failure is secret-free; read-only refresh preserves write lock");

  test = caseWithBridge(completed);
  test.ui.renderAutostart({schema_version: 1, state: "error", can_change: false});
  assert.equal(test.ui.dirty(), true, "failed read must not erase a pending keyboard edit");
  assert.equal(test.input.disabled, true);
  test.ui.renderAutostart({schema_version: 1, state: "disabled", can_change: true});
  assert.equal(test.input.checked, true, "recovered read must keep unsaved user intent");
  assert.equal(test.input.disabled, false);
  console.log("PASS: temporary OS read failure and recovery preserve unsaved keyboard choice");
}

if (require.main === module) {
  run().catch(error => { console.error(error); process.exitCode = 1; });
}
module.exports = {run};
