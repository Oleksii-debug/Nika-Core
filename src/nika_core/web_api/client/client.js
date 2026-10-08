(() => {
  "use strict";
  const form = document.getElementById("inspect-form");
  const field = document.getElementById("task-id");
  const button = document.getElementById("inspect-submit");
  const status = document.getElementById("app-status");
  const details = document.getElementById("task-details");
  const resultId = document.getElementById("result-id");
  const resultState = document.getElementById("result-state");
  let pending = false;

  function report(message) {
    status.textContent = message;
    status.focus();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (pending) return;
    const taskId = field.value;
    // The server is the sole source of tenant/workspace/permission authority.
    // Never send identity, entitlement, local credentials or user-provided tokens.
    if (!taskId || taskId !== taskId.trim() ||
        taskId.length > 120 || /\s|[\x00-\x1f\x7f]/u.test(taskId)) {
      details.hidden = true;
      report("Перевірте ідентифікатор завдання.");
      return;
    }
    if (typeof crypto.randomUUID !== "function") {
      details.hidden = true;
      report("Безпечний ідентифікатор запиту недоступний.");
      return;
    }
    pending = true;
    button.disabled = true;
    form.setAttribute("aria-busy", "true");
    details.hidden = true;
    status.textContent = "Отримання стану завдання.";
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("/v1/commands", {
        method: "POST",
        mode: "same-origin",
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: crypto.randomUUID(),
          action_id: "task.inspect",
          payload: { task_id: taskId }
        }),
        signal: controller.signal
      });
      const outcome = await response.json();
      if (!response.ok || !outcome || typeof outcome !== "object" ||
          outcome.status !== "completed" || outcome.code !== "ok" ||
          !outcome.data || outcome.data.task_id !== taskId ||
          typeof outcome.data.state !== "string" ||
          !/^[A-Z_]{1,40}$/u.test(outcome.data.state)) {
        report("Стан недоступний або немає дозволу. Перевірте авторизацію й повторіть вручну.");
        return;
      }
      resultId.textContent = taskId;
      resultState.textContent = outcome.data.state;
      details.hidden = false;
      report("Стан завдання отримано.");
    } catch (_error) {
      // Even a read-only request is not silently retried after a network fault.
      report("Сервер недоступний або запит перервано. Повторіть вручну.");
    } finally {
      clearTimeout(timeout);
      pending = false;
      button.disabled = false;
      form.removeAttribute("aria-busy");
    }
  });
})();
