(() => {
  "use strict";

  const status = document.getElementById("app-status");
  const activity = document.getElementById("activity-log");
  const commandInput = document.getElementById("command-input");

  function announce(message) {
    status.textContent = message;
  }

  function log(message) {
    const item = document.createElement("li");
    item.textContent = message;
    activity.prepend(item);
  }

  async function invoke(actionId, payload = {}) {
    const requestId = crypto.randomUUID();
    announce(`Виконується: ${actionId}`);

    if (!window.pywebview?.api?.dispatch) {
      const message = `Bridge недоступний: ${actionId}`;
      announce(message);
      log(message);
      return;
    }

    try {
      const result = await window.pywebview.api.dispatch({
        request_id: requestId,
        action_id: actionId,
        payload,
      });
      const message = result?.message || `Статус: ${result?.status || "невідомо"}`;
      announce(message);
      log(message);
    } catch (error) {
      const message = `Помилка виконання ${actionId}: ${String(error)}`;
      announce(message);
      log(message);
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action-id]");
    if (!button) return;

    const actionId = button.dataset.actionId;
    const payload = actionId === "task.create"
      ? { command: commandInput.value.trim() }
      : {};
    invoke(actionId, payload);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.activeElement !== commandInput) {
      invoke("agent.stop");
    }
  });
})();
