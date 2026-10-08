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
    [good, {...good, action_id: "bad\\nidentity"}],
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
}

if (require.main === module) {
  run(fs.readFileSync(process.argv[2], "utf8"))
    .catch((error) => {console.error(error); process.exitCode = 1;});
}
module.exports = {run};
