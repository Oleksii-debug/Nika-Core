(() => {
  "use strict";

  const statusNode = document.getElementById("app-status");
  const activityLog = document.getElementById("activity-log");
  const keymapBody = document.getElementById("keymap-body");
  const keymapJson = document.getElementById("keymap-json");
  const commandInput = document.getElementById("command-input");
  const tasksList = document.getElementById("tasks-list");
  const agentsList = document.getElementById("agents-list");
  const workspacesList = document.getElementById("workspaces-list");
  const tasksEmpty = document.getElementById("tasks-empty");
  const agentsEmpty = document.getElementById("agents-empty");
  const workspacesEmpty = document.getElementById("workspaces-empty");
  const productProjectEmpty = document.getElementById("product-project-empty");
  const productProjectSummary = document.getElementById("product-project-summary");
  const productProjectFields = Object.freeze({
    title: document.getElementById("product-project-title"),
    project_id: document.getElementById("product-project-id"),
    goal: document.getElementById("product-project-goal"),
    state: document.getElementById("product-project-state"),
    spec_version: document.getElementById("product-project-spec-version"),
    blocker_count: document.getElementById("product-project-blocker-count"),
    status_count: document.getElementById("product-project-status-count"),
    decision_count: document.getElementById("product-project-decision-count"),
  });
  let actions = [];
  let actionsReady = false;
  let bridgeInitializationStarted = false;

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
    if (target.matches("input, textarea, select")) return true;
    return target instanceof HTMLElement && target.isContentEditable;
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

  function keymapControlId(actionId, control) {
    return `keymap-${control}-${encodeURIComponent(String(actionId))}`;
  }

  function keymapAccessibleActionLabel(action) {
    return `${action.label} (${action.action_id})`;
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

  function validProductProject(project) {
    if (!project || typeof project !== "object" || Array.isArray(project)) return false;
    const stringFields = ["title", "project_id", "goal", "state"];
    if (stringFields.some((field) => typeof project[field] !== "string" || !project[field].trim())) {
      return false;
    }
    if (!Number.isInteger(project.spec_version) || project.spec_version < 1) return false;
    const countFields = ["blocker_count", "status_count", "decision_count"];
    return countFields.every((field) => Number.isInteger(project[field]) && project[field] >= 0);
  }

  function clearProductProjectFields() {
    for (const node of Object.values(productProjectFields)) node.textContent = "";
  }

  function renderProductProjectUnavailable(message) {
    productProjectEmpty.textContent = message || "Стан поточного ProductProject недоступний.";
    productProjectEmpty.hidden = false;
    productProjectSummary.hidden = true;
    clearProductProjectFields();
  }

  function renderProductProject(project) {
    if (project == null) {
      productProjectEmpty.textContent = "Поточний ProductProject не вибрано.";
      productProjectEmpty.hidden = false;
      productProjectSummary.hidden = true;
      clearProductProjectFields();
      return true;
    }
    if (!validProductProject(project)) {
      renderProductProjectUnavailable(
        "Стан поточного ProductProject недоступний або пошкоджений.",
      );
      appendLog("Некоректний bounded ProductProject state відхилено інтерфейсом.");
      return false;
    }
    for (const [field, node] of Object.entries(productProjectFields)) {
      node.textContent = String(project[field]);
    }
    productProjectEmpty.hidden = true;
    productProjectSummary.hidden = false;
    return true;
  }

  async function refreshState() {
    if (!globalThis.pywebview?.api?.get_state) {
      renderProductProjectUnavailable("Стан поточного ProductProject недоступний.");
      return false;
    }
    let response;
    try {
      response = await globalThis.pywebview.api.get_state();
    } catch (error) {
      renderProductProjectUnavailable("Стан поточного ProductProject недоступний.");
      appendLog(error instanceof Error ? error.message : String(error));
      return false;
    }
    if (!response.ok) {
      renderProductProjectUnavailable("Стан поточного ProductProject недоступний.");
      appendLog(response.message || "Не вдалося отримати стан програми.");
      return false;
    }
    const state = response.state || {};
    renderItems(tasksList, tasksEmpty, state.tasks || [], (item) => `${item.command || "Без назви"} — ${item.state}`);
    renderItems(agentsList, agentsEmpty, state.agents || [], (item) => `${item.name} — ${item.goal}`);
    renderItems(workspacesList, workspacesEmpty, state.workspaces || [], (item) => `${item.name} — ${item.description || "Без опису"}`);
    renderProductProject(state.product_project ?? null);
    return true;
  }

  async function dispatch(actionId, trigger = null) {
    if (!globalThis.pywebview?.api?.dispatch) {
      announce("Міст Nika ще не готовий.", true);
      return;
    }
    const payload = {};
    if (actionId === "task.create") payload.command = commandInput.value.trim();
    const result = await globalThis.pywebview.api.dispatch({ request_id: requestId(), action_id: actionId, payload });
    const failed = result.status === "failed" || result.status === "rejected";
    announce(result.message || (result.status === "completed" ? "Виконано." : result.status), failed);
    appendLog(result.message);
    await refreshState();
    const focusId = result.focus_id || (failed ? trigger?.dataset?.errorFocusTarget : trigger?.dataset?.focusTarget);
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
      const accessibleActionLabel = keymapAccessibleActionLabel(action);
      const row = document.createElement("tr");
      const labelCell = document.createElement("th");
      labelCell.scope = "row";
      labelCell.textContent = action.label;
      const bindingCell = document.createElement("td");
      const input = document.createElement("input");
      input.type = "text";
      input.id = keymapControlId(action.action_id, "binding");
      input.value = action.binding || "";
      input.dataset.actionId = action.action_id;
      input.setAttribute("aria-label", `Комбінація для ${accessibleActionLabel}`);
      bindingCell.appendChild(input);
      const controlCell = document.createElement("td");
      const save = document.createElement("button");
      const saveFocusId = keymapControlId(action.action_id, "save");
      save.type = "button";
      save.id = saveFocusId;
      save.textContent = action.may_be_unbound ? "Зберегти / очистити" : "Зберегти";
      save.setAttribute(
        "aria-label",
        action.may_be_unbound
          ? `Зберегти або очистити комбінацію для ${accessibleActionLabel}`
          : `Зберегти комбінацію для ${accessibleActionLabel}`,
      );
      save.addEventListener("click", async () => {
        const response = await globalThis.pywebview.api.set_binding(action.action_id, input.value.trim() || null);
        announce(response.message, !response.ok);
        if (response.ok) {
          await refreshKeymap();
          focusElementById(saveFocusId);
        } else input.focus();
      });
      const restore = document.createElement("button");
      const restoreFocusId = keymapControlId(action.action_id, "restore");
      restore.type = "button";
      restore.id = restoreFocusId;
      restore.textContent = "За замовчуванням";
      restore.setAttribute(
        "aria-label",
        `Відновити комбінацію за замовчуванням для ${accessibleActionLabel}`,
      );
      restore.addEventListener("click", async () => {
        const response = await globalThis.pywebview.api.restore_default(action.action_id);
        announce(response.message, !response.ok);
        if (response.ok) {
          await refreshKeymap();
          focusElementById(restoreFocusId);
        }
      });
      controlCell.append(save, document.createTextNode(" "), restore);
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
      const stateReady = await refreshState();
      if (!stateReady) throw new Error("Desktop state bridge unavailable");
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
    if (isEditable(event.target)) return;
    if (!actionsReady) return;
    const pressed = eventBinding(event);
    if (!pressed) return;
    const action = actions.find((candidate) => normalizedBinding(candidate.binding) === pressed);
    if (!action) return;
    event.preventDefault();
    void dispatch(action.action_id, event.target instanceof HTMLElement ? event.target : null);
  });

  window.addEventListener("pywebviewready", () => { void initializeBridge(); });
  if (globalThis.pywebview?.api) void initializeBridge();
})();
