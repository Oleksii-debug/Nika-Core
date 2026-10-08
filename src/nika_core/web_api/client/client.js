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
  const MAX_RESPONSE_BYTES = 80 * 1024;

  // Bound response allocation before decoding JSON. Browser input and even
  // misconfigured server responses are not an authority for memory budgets.
  async function readBoundedJson(response, signal) {
    const contentType = response.headers.get("content-type");
    if (typeof contentType !== "string" ||
        !/^application\/json(?:\s*;\s*charset=utf-8)?$/iu.test(contentType.trim())) {
      throw new Error("unexpected response content type");
    }
    const lengthHeader = response.headers.get("content-length");
    if (lengthHeader !== null &&
        (!/^(?:0|[1-9][0-9]{0,5})$/u.test(lengthHeader) ||
         Number(lengthHeader) > MAX_RESPONSE_BYTES)) {
      throw new Error("invalid response length");
    }
    if (!response.body || typeof response.body.getReader !== "function") {
      throw new Error("streaming response unavailable");
    }
    const reader = response.body.getReader();
    // Some fetch/stream implementations may ignore abort during a pending
    // reader.read(). Race the read against the same request deadline signal
    // so keyboard users can recover without an indefinitely busy form.
    let onAbort;
    const aborted = new Promise((_, reject) => {
      onAbort = () => reject(new Error("request deadline exceeded"));
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    });
    const parts = [];
    let total = 0;
    try {
      while (true) {
        const item = await Promise.race([reader.read(), aborted]);
        if (item.done) break;
        if (!(item.value instanceof Uint8Array)) {
          throw new Error("invalid response chunk");
        }
        total += item.value.byteLength;
        if (total > MAX_RESPONSE_BYTES) {
          throw new Error("response body exceeds limit");
        }
        parts.push(item.value);
      }
    } catch (error) {
      // Cancellation is best effort: waiting for a broken reader.cancel()
      // would reintroduce the same permanent busy-state failure.
      try {
        const cancellation = reader.cancel();
        if (cancellation && typeof cancellation.catch === "function") {
          cancellation.catch(() => {});
        }
      } catch (_ignored) {}
      throw error;
    } finally {
      signal.removeEventListener("abort", onAbort);
      reader.releaseLock();
    }
    if (lengthHeader !== null && Number(lengthHeader) !== total) {
      throw new Error("response length mismatch");
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const part of parts) {
      bytes.set(part, offset);
      offset += part.byteLength;
    }
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  }

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
    // Core TaskQueue generates ASCII task identities. Reject all Unicode
    // lookalikes and format characters, not just the common bidi controls.
    if (!taskId || taskId !== taskId.trim() ||
        taskId.length > 120 || /[^\x21-\x7e]/u.test(taskId)) {
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
      const requestId = crypto.randomUUID();
      const response = await fetch("/v1/commands", {
        method: "POST",
        mode: "same-origin",
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          request_id: requestId,
          action_id: "task.inspect",
          payload: { task_id: taskId }
        }),
        signal: controller.signal
      });
      if (!response.ok) {
        report("Стан недоступний або немає дозволу. Перевірте авторизацію й повторіть вручну.");
        return;
      }
      const outcome = await readBoundedJson(response, controller.signal);
      // Some transports can resolve after abort; never publish a late result.
      if (controller.signal.aborted) {
        throw new Error("request deadline exceeded");
      }
      if (!outcome || typeof outcome !== "object" ||
          outcome.request_id !== requestId ||
          outcome.status !== "completed" || outcome.code !== "ok" ||
          !outcome.data || outcome.data.task_id !== taskId ||
          typeof outcome.data.state !== "string" ||
          !/^[A-Z_]{1,40}$/u.test(outcome.data.state)) {
        report("Стан недоступний або немає дозволу. Перевірте авторизацію й повторіть вручну.");
        return;
      }
      // The form can be edited while a request is awaiting a response.
      // Never present the result of an older input as the new selection.
      if (field.value !== taskId) {
        report("Ідентифікатор завдання змінився. Перевірте його ще раз.");
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
