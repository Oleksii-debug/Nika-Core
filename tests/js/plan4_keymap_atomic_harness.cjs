"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");

async function run(source) {
  const start = source.indexOf("  async function refreshKeymap() {");
  const end = source.indexOf("  function startStatePolling()", start);
  assert(start >= 0 && end > start);
  const inventory = [];
  const existing = { marker: "prior editor" };
  let displayed = [existing];
  let replacements = 0;
  const keymapBody = {
    replaceChildren(...children) { displayed = children; replacements += 1; },
  };
  const element = (tag) => ({
    tag, dataset: {}, children: [], value: "", textContent: "",
    setAttribute() {}, addEventListener() {},
    appendChild(node) { this.children.push(node); },
    append(...nodes) { this.children.push(...nodes); },
  });
  const factory = new Function("ctx",
    "const {globalThis,document,keymapBody,keymapAccessibleActionLabel,"
    + "keymapControlId,mutateKeymap}=ctx;"
    + "let actions=[{action_id:'previous'}], actionsReady=true;"
    + source.slice(start, end)
    + "return {refreshKeymap, snapshot:()=>({actions,actionsReady})};"
  );
  const ui = factory({
    globalThis: {pywebview: {api: {list_actions: async () => inventory.shift()}}},
    document: {createElement: element, createTextNode: (value) => ({value})},
    keymapBody, keymapAccessibleActionLabel: (action) => action.label,
    keymapControlId: (id, kind) => kind + "-" + id,
    mutateKeymap: async () => {},
  });
  const good = {action_id: "task.create", label: "Створити завдання",
    binding: "ctrl+n", may_be_unbound: false};
  const badCases = [
    null,
    "invalid",
    [{...good}, {...good}],
    [good, {action_id: "task.pause", label: "Пауза", binding: null}],
    [good, {...good, action_id: "bad\nidentity"}],
  ];
  for (const invalid of badCases) {
    inventory.push(invalid);
    await assert.rejects(ui.refreshKeymap());
    assert.equal(replacements, 0, "invalid projection must not replace existing DOM");
    assert.equal(displayed[0], existing);
    assert.equal(ui.snapshot().actions[0].action_id, "previous");
    assert.equal(ui.snapshot().actionsReady, false);
  }
  console.log("PASS: five invalid/duplicate action inventories preserve editor state");

  inventory.push([good]);
  assert.equal(await ui.refreshKeymap(), true);
  assert.equal(replacements, 1);
  assert.equal(displayed.length, 1);
  assert.equal(displayed[0].children[0].textContent, good.label);
  assert.equal(ui.snapshot().actions[0].action_id, good.action_id);
  assert.equal(ui.snapshot().actionsReady, true);
  console.log("PASS: complete valid inventory publishes semantic keymap controls");

  inventory.push([good, null]);
  await assert.rejects(ui.refreshKeymap());
  assert.equal(replacements, 1, "late malformed entries may not partially render");
  assert.equal(displayed.length, 1);
  console.log("PASS: failed refresh retains previously rendered keymap controls");

  // Invisible bidi overrides and line breaks can spoof the NVDA action inventory.
  for (const unsafe of ["Створити\nзавдання", "Створити\u202Eзавдання"]) {
    inventory.push([{...good, label: unsafe}]);
    await assert.rejects(ui.refreshKeymap());
    assert.equal(replacements, 1);
    assert.equal(displayed[0].children[0].textContent, good.label);
  }
  inventory.push([{...good, binding: "ctrl+n\u202E"}]);
  await assert.rejects(ui.refreshKeymap());
  assert.equal(replacements, 1);
  console.log("PASS: control and bidi text cannot spoof the NVDA keymap");
  inventory.push([good]);
  assert.equal(await ui.refreshKeymap(), true, "safe inventory must recover");
  assert.equal(replacements, 2);
  assert.equal(ui.snapshot().actionsReady, true);
  assert.equal(displayed[0].children[0].textContent, good.label);
  console.log("PASS: safe keyboard inventory restores editing after a failed refresh");

  // A delayed older bridge read must not roll back a newer accessible keymap.
  let resolveOlder;
  inventory.push(new Promise((resolve) => { resolveOlder = resolve; }));
  inventory.push([{...good, label: "Нова команда"}]);
  const older = ui.refreshKeymap();
  const newer = ui.refreshKeymap();
  assert.equal(await newer, true);
  resolveOlder([{...good, label: "Стара команда"}]);
  assert.equal(await older, null);
  assert.equal(replacements, 3);
  assert.equal(ui.snapshot().actions[0].label, "Нова команда");
  assert.equal(displayed[0].children[0].textContent, "Нова команда");
  console.log("PASS: stale success cannot roll back the newer semantic keymap");

  let rejectOlder;
  inventory.push(new Promise((_, reject) => { rejectOlder = reject; }));
  inventory.push([good]);
  const failedOld = ui.refreshKeymap();
  const recovered = ui.refreshKeymap();
  assert.equal(await recovered, true);
  rejectOlder(new Error("stale bridge internals"));
  assert.equal(await failedOld, null);
  assert.equal(ui.snapshot().actionsReady, true);
  assert.equal(ui.snapshot().actions[0].label, good.label);
  console.log("PASS: stale failure cannot invalidate a newer verified keymap");
}

if (require.main === module) {
  run(fs.readFileSync(process.argv[2], "utf8"))
    .catch((error) => {console.error(error); process.exitCode = 1;});
}
module.exports = {run};
