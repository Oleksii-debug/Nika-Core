# План 1: аудит і повторне використання розділу 7

7А DONE: завершено незалежний аудит/reuse. 7Б і 7В OPEN; весь розділ 7 не закрито.

| Acceptance scope | Чинна authority і перевірка | Межа доказу |
| --- | --- | --- |
| Start/deadline/recurrence/missed occurrence | APSchedulerAdapter + DurableRecurrenceService; #994; UTC, coalesce-one, terminal task, restart/replay, exact binding, bounded lifecycle CAS | Потрібне підключення ordinary scheduled task у packaged builder |
| Wait/backoff/retry/timeout | Чинні coordinator/retry/batch cursor/connectivity wait; збережені UTC floors та asyncio monotonic elapsed budgets | 182 timing/recovery tests; NTP/server sync не заявлено і не випливає з вимоги monotonic/UTC evidence |
| Scheduler concurrency | #884 transaction guards + #1107 typed snapshots + #994 lifecycle CAS, persisted-only activation, shutdown fence | Незалежно 218 tests і початкове відтворення cancellation через два незалежні stores/services; це не фізичний power-loss тест |
| Resources/budgets/FIFO | #357 ResourceManager: SQLite authority, process generation, dead-owner recovery, validated telemetry, fairness | Реальний packaged ordinary fresh start/resume/cancel використовує manager; інші execution owners не отримують автоматичної сертифікації |
| Named profiles | #536 normal/economy/night_batch/low_memory policy та canonical budget projection | 48 independent tests; selection/persistence та production policy admission ще не підключені |
| Packaged Windows/accessibility | Exact #536 Core/M12 + Windows final extracted ZIP manifest/notices/UIA/keyboard/autostart | Hosted automation, не human NVDA; hardware/power/throughput benchmarks не заявлено |

Результати та точні main/PR/SHA/artifact зафіксовано в MULTI_PLAN_CLOSURE_STATE.md.
Прийняті механізми не переписуються; наступна робота — лише підтверджені production composition gaps.

HUMAN_TESTED=false; NVDA_VERIFIED=false; PRODUCTION_RELEASE_READY=false.
