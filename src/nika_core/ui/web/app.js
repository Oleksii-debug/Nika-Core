(() => {
  "use strict";

  const statusNode = document.getElementById("app-status");
  const activityLog = document.getElementById("activity-log");
  const keymapBody = document.getElementById("keymap-body");
  const keymapJson = document.getElementById("keymap-json");
  const commandInput = document.getElementById("command-input");
  let actions = [];

  const reservedEditingKeys = new Set(["a", "c", "x", "v", "z", "y"]);

  function announce(message, assertive = false) {
    statusNode.setAttribute("aria-live", assertive ? "assertive" : "polite");
    statusNode.textContent = message || "Готово.";
  }

  function appendLog(message) {
    if (!message) return;
    const item = document.createElement("li");
    item.textContent = message;
    activityLog.appendChild(item);
  }

  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function isEditable(target) {
    if (!(target instanceof Element)) return false;
    return target.matches("input, textarea, select, [contenteditable='true']");
  }

  function eventBinding(event) {
    const parts = [];
    if (event.ctrlKey) parts.push("ctrl");
    if (event.altKey) parts.push("alt");
    if (event.shiftKey) parts.push("shift");
    if (event.metaKey) parts.push("win");
    const key = event.key.toLowerCase();
    if (["control", "alt", "shift", "meta"].includes(key)) return null;
    parts.push(key);
    return parts.join("+");
  }

  function normalizedBinding(binding) {
    return String(binding || "")
      .split("+")
      .map((part) => part.trim().toLowerCase())
      .filter(Boolean)
      .join("+");
  }

  async function dispatch(actionId, trigger = null) {
    if (!globalThis.pywebview?.api?.dispatch) {
      announce("Міст Nika ще не готовий.", true);
      return;
    }
    const payload = {};
    if (actionId === "task.create") payload.command = commandInput.value.trim();
    const result = await globalThis.pywebview.api.dispatch({
      request_id: requestId(),
      action_id: actionId,
      payload,
    });
    announce(result.message || (result.status === "completed" ? "Виконано." : result.status), result.status === "failed");
    appendLog(result.message);
    const focusId = result.focus_id || trigger?.dataset?.focusTarget;
    if (focusId) document.getElementById(focusId)?.focus();
    else trigger?.focus?.();
  }

  async function refreshKeymap() {
    if (!globalThis.pywebview?.api?.list_actions) return;
    actions = await globalThis.pywebview.api.list_actions();
    keymapBody.replaceChildren();
    for (const action of actions) {
      const row = document.createElement("tr");
      const labelCell = document.createElement("th");
      labelCell.scope = "row";
      labelCell.textContent = action.label;

      const bindingCell = document.createElement("td");
      const input = document.createElement("input");
      input.type = "text";
      input.value = action.binding || "";
      input.dataset.actionId = action.action_id;
      input.setAttribute("aria-label", `Комбінація для ${action.label}`);
      bindingCell.appendChild(input);

      const controlCell = document.createElement("td");
      const save = document.createElement("button");
      save.type = "button";
      save.textContent = action.may_be_unbound ? "Зберегти / очистити" : "Зберегти";
      save.addEventListener("click", async () => {
        const response = await globalThis.pywebview.api.set_binding(action.action_id, input.value.trim() || null);
        announce(response.message, !response.ok);
        if (response.ok) await refreshKeymap();
        else input.focus();
      });
      const restore = document.createElement("button");
      restore.type = "button";
      restore.textContent = "За замовчуванням";
      restore.addEventListener("click", async () => {
        const response = await globalThis.pywebview.api.restore_default(action.action_id);
        announce(response.message, !response.ok);
        if (response.ok) await refreshKeymap();
      });
      controlCell.append(save, document.createTextNode(" "), restore);
      row.append(labelCell, bindingCell, controlCell);
      keymapBody.appendChild(row);
    }
  }

  document.getElementById("keymap-export").addEventListener("click", async () => {
    const response = await globalThis.pywebview.api.export_keymap();
    announce(response.message, !response.ok);
    if (response.ok) {
      keymapJson.value = response.data;
      keymapJson.focus();
    }
  });

  document.getElementById("keymap-import").addEventListener("click", async () => {
    const response = await globalThis.pywebview.api.import_keymap(keymapJson.value);
    announce(response.message, !response.ok);
    if (response.ok) await refreshKeymap();
    else keymapJson.focus();
  });

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest?.("[data-action-id]");
    if (!trigger) return;
    void dispatch(trigger.dataset.actionId, trigger);
  });

  document.addEventListener("keydown", (event) => {
    if (isEditable(event.target) && event.ctrlKey && !event.altKey && !event.metaKey && reservedEditingKeys.has(event.key.toLowerCase())) {
      return;
    }
    const pressed = eventBinding(event);
    if (!pressed) return;
    const action = actions.find((candidate) => normalizedBinding(candidate.binding) === pressed);
    if (!action) return;
    event.preventDefault();
    void dispatch(action.action_id, event.target instanceof HTMLElement ? event.target : null);
  });

  document.addEventListener("pywebviewready", () => {
    announce("Nika Core готова до роботи.");
    void refreshKeymap();
  });
})();
