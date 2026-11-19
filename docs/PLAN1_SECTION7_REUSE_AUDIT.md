# План 1: аудит і повторне використання розділу 7

7А DONE: завершено незалежний аудит/reuse. 7Б і 7В OPEN; весь розділ 7 не закрито.

| Acceptance scope | Чинна authority і перевірка | Межа доказу |
| --- | --- | --- |
| Start/deadline/recurrence/missed occurrence | APSchedulerAdapter + DurableRecurrenceService; #994; UTC, coalesce-one, terminal task, restart/replay, exact binding, bounded lifecycle CAS | #1873 підключив ordinary scheduled task; frozen EXE та extracted ZIP підтвердили один запуск без replay |
| Wait/backoff/retry/timeout | Чинні coordinator/retry/batch cursor/connectivity wait; збережені UTC floors та asyncio monotonic elapsed budgets | 182 timing/recovery tests; NTP/server sync не заявлено і не випливає з вимоги monotonic/UTC evidence |
| Scheduler concurrency | #884 transaction guards + #1107 typed snapshots + #994 lifecycle CAS, persisted-only activation, shutdown fence | Незалежно 218 tests і початкове відтворення cancellation через два незалежні stores/services; це не фізичний power-loss тест |
| Resources/budgets/FIFO | #357 ResourceManager: SQLite authority, process generation, dead-owner recovery, validated telemetry, fairness | Реальний packaged ordinary fresh start/resume/cancel використовує manager; інші execution owners не отримують автоматичної сертифікації |
| Named profiles | #536 normal/economy/night_batch/low_memory policy та canonical budget projection | #1873 підключив selection/persistence та GENERAL admission; heavy/owner-active та глобальна координація інших workloads ще OPEN |
| Packaged Windows/accessibility | Exact #1873 і #1013 Core/M11/M12 + Windows extracted ZIP, native UIA/focus та actual runtime proof | Hosted automation, не human NVDA; hardware/power/throughput benchmarks не заявлено |

Результати та точні main/PR/SHA/artifact зафіксовано в MULTI_PLAN_CLOSURE_STATE.md.
Прийняті механізми не переписуються; наступна робота — лише підтверджені production composition gaps.

HUMAN_TESTED=false; NVDA_VERIFIED=false; PRODUCTION_RELEASE_READY=false.

## Повний початковий scope 7.2 / 7.3 — збережено, не звужено

Первісний розділ 7 у legacy50plan вимагає practical CPU/RAM/GPU/disk/network/
model/token/cost/process/browser concurrency budgets, normal/economy/heavy/
low-memory/owner-active profiles та contention/starvation/queue pressure/
cancellation/hibernate-wake/reboot/preemption/endurance.
Ordinary Windows-компонент #1873 не закриває весь цей перелік.

| Вимога | Canonical reuse / чинний стан | Відкрита робота 7Б |
| --- | --- | --- |
| CPU/RAM/available memory | #357/#1873, ResourceManager + PsutilResourceObserver; actual ordinary start/resume | Не переносити цей доказ автоматично на всі execution owners |
| Disk/GPU/process RSS ceilings | Resource-only slice старого #357 `962d9e37d18bcac1741920e73a1c183409d255c4` та strict #986 `7335a08c25dd526936da687c83e184d7e4d1df91`; поточний snapshot спостерігає RSS без ceiling | Адаптувати лише потрібні optional fields і migration v2; не імпортувати mixed Memory/scheduler schema |
| Network/process/disk-write budgets | security.ExecutionBudget/Ledger: max_network_calls/max_process_launches/max_write_bytes | In-memory tool counters не є durable product-wide bandwidth/process concurrency authority |
| Model admission | ModelResourcePolicy CPU/RAM/floor у Foundry preflight; parallel provider batch #837 `cde4a45a179fd381224f71feb2becbcb6199a1f9` ще не інтегрований | Звести provider/model admission з чинним durable manager |
| Token budgets | ModelUsage спостерігає optional token counts | Немає aggregate durable token ledger/output-token policy; спостереження не є лімітом |
| Cost budgets | Model cost ledger не знайдено | Не використовувати сторонній paper-trader ledger; потрібен належний model-usage budget contract |
| Browser concurrency | #536 CHROMIUM conflict policy; injected MediaResourceCoordinator має process-local active classes | Немає machine-wide durable browser reservation |
| Heavy / owner-active profiles | Наявні normal/economy/night_batch/low_memory; night_batch не доводить owner-active | Реальний owner-active/heavy policy ще потрібен |
| Owner presence / owner return | #824→#855/#856/#857/#858/#1503; strict UTC/persistence successors #1467/#1468 | Перевірити/reuse stack; зараз не production authority у main |
| Active pause / preemption | Canonical prerequisite #654 `89fc2cb54f60919ab7ccd175d00fd332cd0f49cb`; owner-return #858 `0a869dbdcc418c27b9655507ba2646b8231a4b9d` | Чинний DesktopBackend відхиляє unsupported active pause; не заявляти cooperative preemption |
| Contention/starvation/endurance | FIFO/restart/cancel tests проходять | Це не quantified starvation/endurance campaign |
| Hibernate/wake / reboot/target hardware | Hosted autostart/UIA — реальний component proof | Фізичний hardware campaign та human NVDA — окремий final Plan7; відсутні software gaps не списуються на hardware |

Незалежний ресурсний аудит виконав 124 поточні resource/profile/desktop/
recovery/Foundry tests. Результат підтверджує ці компоненти, а не missing rows.
7А audit/reuse DONE залишається чинним; 7Б/7В OPEN, independent count 19/30,
legacy aggregate 6/10. Наступний конкретний slice: disk/GPU/process-RSS
optional budgets, migration і збереження advanced limits при зміні профілю.
