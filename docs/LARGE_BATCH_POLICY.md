# Nika Core — Large Coherent Batch Policy

Дата фіксації: 2026-08-17.

## Головний принцип
Щогодинний автономний запуск не є одним мікротикетом. Один запуск має виконувати максимально великий безпечний когерентний інженерний пакет, який реально рухає продукт до acceptance gate.

Не зупинятися після одного файла, однієї функції, одного тесту чи одного дрібного commit, якщо в межах того самого циклу можна надійно виконати більший пов’язаний блок.

Аналогія користувача: не 10 сторінок за цикл, а 2–3 книги, якщо це один логічний завершуваний пакет.

## Що може входити в один великий batch
- повторний reuse-аудит офіційної документації й готових бібліотек;
- уточнення/ADR архітектурного рішення;
- кілька взаємопов’язаних модулів;
- schema/migration;
- adapter/port implementation;
- unit/integration tests;
- error/recovery paths;
- документація;
- оновлення статусу та evidence.

## Природні межі batch
Зупинитися дозволено, коли досягнута одна з реальних меж:
1. завершений subsystem slice;
2. закритий acceptance gate;
3. integration boundary, де потрібен окремий proof/runner;
4. реальний blocker або зовнішня залежність;
5. ризик/невизначеність, що вимагає окремого architecture selection gate;
6. небезпечна дія, яка потребує human approval.

Не створювати штучних пауз лише тому, що наступний scheduled run буде через годину.

## Архітектурний принцип
Програму нарощуємо, а не регулярно переписуємо. Nika domain залежить від стабільних versioned ports/contracts, а frameworks/providers/UI shells працюють через adapters. Базова форма: modular monolith + ports-and-adapters/hexagonal architecture + dependency inversion + plugin/workspace SDK + migrations/backward-compatible evolution.

Новий функціонал бажано додавати модулем, plugin, adapter або новою реалізацією стабільного interface. Великі framework choices проходять proof/selection gate до того, як їх типи проникнуть у domain API.

## Метрика успіху циклу
Не кількість комітів і не кількість файлів. Метрика — закриті acceptance gates, зелений evidence, зменшення ризику та ваговий прогрес milestone.
