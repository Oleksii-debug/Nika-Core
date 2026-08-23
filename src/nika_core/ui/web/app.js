(() => {
  "use strict";

  const statusNode = document.getElementById("app-status");
  const activityLog = document.getElementById("activity-log");
  const keymapBody = document.getElementById("keymap-body");
  const keymapJson = document.getElementById("keymap-json");
  const commandInput = document.getElementById("command-input");
  const tasksList = document.getElementById("tasks-list");
  const approvalsList = document.getElementById("approvals-list");
  const agentsList = document.getElementById("agents-list");
  const workspacesList = document.getElementById("workspaces-list");
  const tasksEmpty = document.getElementById("tasks-empty");
  const approvalsEmpty = document.getElementById("approvals-empty");
  const agentsEmpty = document.getElementById("agents-empty");
  const workspacesEmpty = document.getElementById("workspaces-empty");
  let actions = [];
  let actionsReady = false;
  let bridgeInitializationStarted = false;

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
    return String(binding || "").split("+").map((part) => part.trim().toLowerCase()).filter(Boolean).join("+");
  }

  function focusElementById(focusId) {
    if (!focusId) return false;
    const element = document.getElementById(focusId);
    if (!(element instanceof HTMLElement)) return false;
    element.focus({ preventScroll: false });
    return document.activeElement === element;
  }

  function renderItems(list, emptyNode, items, formatter) {
    list.replaceChildren();
    emptyNode.hidden = items.length > 0;
    for (const item of items) {
      const row = document.createElement("li");
      row.textContent = formatter(item);
      list.appendChild(row);
    }
  }

  function detail(parent, label, value) {
    if (value === null || value === undefined || value === "") return;
    const line = document.createElement("p");
    const name = document.createElement("strong");
    name.textContent = `${label}: `;
    line.append(name, document.createTextNode(String(value)));
    parent.appendChild(line);
  }

  function renderApprovals(items) {
    approvalsList.replaceChildren();
    approvalsEmpty.hidden = items.length > 0;
    for (const item of items) {
      const row = document.createElement("li");
      const heading = document.createElement("h3");
      const isReview = item.request_type === "review";
      heading.textContent = isReview
        ? `Перевірка ${item.subject_kind} — ${item.purpose}`
        : `${item.action_id} — ризик ${item.risk}`;
      row.appendChild(heading);
      detail(row, "Причина", item.reason);
      if (isReview) {
        detail(row, "Проєкт", item.project_id);
        detail(row, "Мета", item.purpose);
        detail(row, "Ресурс", item.resource_id);
        for (const binding of item.bindings || []) {
          if (Array.isArray(binding) && binding.length === 2) {
            detail(row, `Прив'язка ${binding[0]}`, binding[1]);
          }
        }
      } else {
        detail(row, "Інструмент", item.tool_id);
        detail(row, "Ціль", item.target);
        detail(row, "Шлях запису", item.write_path);
        if (Number(item.write_bytes || 0) > 0) detail(row, "Байтів запису", item.write_bytes);
        detail(row, "Мережевий вузол", item.network_host);
        detail(row, "Виконуваний файл", item.executable);
      }
      detail(row, "Строк дії запиту до", item.expires_at);

      const controls = document.createElement("div");
      controls.className = "actions";
      const approve = document.createElement("button");
      approve.type = "button";
      approve.textContent = isReview ? "Схвалити цей точний предмет" : "Підтвердити цю точну дію";
      approve.dataset.actionId = isReview ? "approval.review.approve" : "approval.action.approve";
      approve.dataset.approvalRequestId = item.request_id;
      approve.setAttribute(
        "aria-label",
        isReview
          ? `Схвалити точну перевірку ${item.purpose} для проєкту ${item.project_id}`
          : `Підтвердити точну дію ${item.action_id}: ${item.target}`,
      );
      const deny = document.createElement("button");
      deny.type = "button";
      deny.textContent = "Відхилити";
      deny.dataset.actionId = isReview ? "approval.review.deny" : "approval.action.deny";
      deny.dataset.approvalRequestId = item.request_id;
      deny.setAttribute(
        "aria-label",
        isReview
          ? `Відхилити перевірку ${item.purpose} для проєкту ${item.project_id}`
          : `Відхилити дію ${item.action_id}: ${item.target}`,
      );
      controls.append(approve, deny);
      row.appendChild(controls);
      approvalsList.appendChild(row);
    }
  }

  async function refreshState() {
    if (!globalThis.pywebview?.api?.get_state) return false;
    const response = await globalThis.pywebview.api.get_state();
    if (!response.ok) {
      appendLog(response.message || "Не вдалося отримати стан програми.");
      return false;
    }
    const state = response.state || {};
    renderItems(tasksList, tasksEmpty, state.tasks || [], (item) => `${item.command || "Без назви"} — ${item.state}`);
    renderApprovals(state.pending_approvals || []);
    renderItems(agentsList, agentsEmpty, state.agents || [], (item) => `${item.name} — ${item.goal}`);
    renderItems(workspacesList, workspacesEmpty, state.workspaces || [], (item) => `${item.name} — ${item.description || "Без опису"}`);
    return true;
  }

  async function dispatch(actionId, trigger = null) {
    if (!globalThis.pywebview?.api?.dispatch) {
      announce("Міст Nika ще не готовий.", true);
      return;
    }
    const payload = {};
    if (actionId === "task.create") payload.command = commandInput.value.trim();
    if (actionId.startsWith("approval.")) {
      const approvalRequestId = trigger?.dataset?.approvalRequestId;
      if (!approvalRequestId) {
        announce("Запит на людське рішення не визначено; дію відхилено.", true);
        return;
      }
      payload.request_id = approvalRequestId;
    }
    const result = await globalThis.pywebview.api.dispatch({ request_id: requestId(), action_id: actionId, payload });
    const failed = result.status === "failed" || result.status === "rejected";
    announce(result.message || (result.status === "completed" ? "Виконано." : result.status), failed);
    appendLog(result.message);
    await refreshState();
    const focusId = result.focus_id || trigger?.dataset?.focusTarget;
    if (focusId) focusElementById(focusId);
    else trigger?.focus?.();
  }

  async function refreshKeymap() {
    if (!globalThis.pywebview?.api?.list_actions) {
      actionsReady = false;
      return false;
    }
    actions = await globalThis.pywebview.api.list_actions();
    keymapBody.replaceChildren();
    for (const action of actions) {
      const row = document.createElement("tr");
      const labelCell = document.createElement("th");
      labelCell.scope = "row";
      labelCell.textContent = action.label;
      const bindingCell = document.createElement("td");
      const controlCell = document.createElement("td");
      if (action.scope === "explicit") {
        bindingCell.textContent = "Тільки явна кнопка";
        controlCell.textContent = "Глобальна комбінація вимкнена";
      } else {
        const input = document.createElement("input");
        input.type = "text";
        input.value = action.binding || "";
        input.dataset.actionId = action.action_id;
        input.setAttribute("aria-label", `Комбінація для ${action.label}`);
        bindingCell.appendChild(input);
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
      }
      row.append(labelCell, bindingCell, controlCell);
      keymapBody.appendChild(row);
    }
    actionsReady = true;
    return true;
  }

  async function initializeBridge() {
    if (bridgeInitializationStarted) return;
    bridgeInitializationStarted = true;
    announce("Завантаження команд Nika Core…");
    try {
      const ready = await refreshKeymap();
      if (!ready) throw new Error("Action Registry bridge unavailable");
      await refreshState();
      document.documentElement.dataset.nikaReady = "true";
      announce("Nika Core готова до роботи.");
    } catch (error) {
      actionsReady = false;
      bridgeInitializationStarted = false;
      document.documentElement.dataset.nikaReady = "false";
      announce("Не вдалося завантажити команди Nika Core.", true);
      appendLog(error instanceof Error ? error.message : String(error));
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
    if (isEditable(event.target) && event.ctrlKey && !event.altKey && !event.metaKey && reservedEditingKeys.has(event.key.toLowerCase())) return;
    if (!actionsReady) return;
    const pressed = eventBinding(event);
    if (!pressed) return;
    const action = actions.find((candidate) => candidate.scope === "app" && normalizedBinding(candidate.binding) === pressed);
    if (!action) return;
    event.preventDefault();
    void dispatch(action.action_id, event.target instanceof HTMLElement ? event.target : null);
  });

  window.addEventListener("pywebviewready", () => { void initializeBridge(); });
  if (globalThis.pywebview?.api) void initializeBridge();
})();
