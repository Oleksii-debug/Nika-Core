# План 1, 7Б: підключення ресурсів і розкладу до програми

Статус: IMPLEMENTED, остаточна exact-head Windows/package перевірка ще потрібна.
Аудит/reuse 7А DONE збережено. 7Б/7В та весь розділ 7 поки OPEN.

Повторно використано прийняті #357/#994/#536 та чинні TaskQueue,
TaskRuntimeCoordinator, SQLiteStore, SourceSettings і ActionRegistry.
Нових scheduler/resource/telemetry/runtime двигунів або залежностей немає.

- Чотири профілі зберігаються через revision CAS. Бюджет, вибір та аудит
  записуються однією транзакцією; помилка журналу відкочує зміну.
- Чинний ResourceManager перевіряє політику GENERAL для звичайних desktop tasks
  всередині своєї транзакції й на тому самому перевіреному snapshot.
  CPU/memory та available-memory floor справді обмежують допуск.
  FIFO, process generation, leases та recovery залишаються його authority.
- Збережений розклад допускає одну підготовлену звичайну задачу.
  Інтервал повторює лише перевірку ресурсів, а не виконану бізнес-дію.
  Початок підтверджується durable RUNNING history; невизначеність не
  вважається безпечною відмовою. Пізній/restart виклик не повторює effect.
- Другий незалежний backend не може призупинити READY-задачу, lease якої
  вже захопив перший. Це реальна before-fail гонка, збережена як регресія.
- Explicit Resume повторно активує актуальний persisted DATE intent після
  втрати тригера через помилку callback. Нові pause/cancel/definition
  залишаються авторитетними; старий snapshot не записується повторно.
- Збережені розклади читаються SQL keyset-сторінками по 50 до декодування.
  Нові звичайні задачі не приховують старі активні розклади. First/Next
  дозволяють переглянути весь набір без загального ліміту результатів.
- Інтерфейс має українські label/select/number/button, стандартний Tab,
  текстові повідомлення, відновлення фокусу та явне збереження профілю.
  JS не повторює невизначений write після transport failure.

Скасування розкладу не є командою зупинки вже активного runtime.
Пауза/зупинка активної роботи залишається чинною coordinator authority.
Крайній час у формі — крайній час допуску, а не примусове завершення роботи.
Підготовка task і створення schedule не оголошуються атомарними:
помилка виконує безпечну компенсацію; crash до створення intent не запускає orphan.

Політика не сертифікує cross-workload browser/model/transcription authority,
idle-model unloading чи виміряні power/throughput профілі Windows.
Ці owners і фізична остаточна перевірка зберігають власні gates.

Вихідний combined source разом із §8 audit/artifact backend пройшов
scripts/verify.py: 4294 passed, 0 skipped, 11 warnings; pip check/Ruff/compile PASS.
Незалежно: profile core 156 tests; host/backend/builder/JS 62 tests, включно
з власним прикладом 51 schedule та початковою двома backend гонкою.
Нові сценарії frozen EXE і WebView2/UIA перевірятимуть остаточний SHA;
цей Linux результат не підміняє їх.

HUMAN_TESTED=false; NVDA_VERIFIED=false; PRODUCTION_RELEASE_READY=false.
Секрети, приватні журнали, production DB, browser profiles і model weights не включено.
Rollback: окрема branch, прийнятий main і старі PR не видаляються.

## Ремонт фактично невдалої M11-збірки

Коміт 2cd95f2ab1e97d4116077007d2f9bbb801da7975 не пройшов M11
на Verify configuration admission before packaging: import APScheduler був
відсутній у GUI/packaging profile. Піднято вже зафіксований APScheduler 3.11.3
з agent до GUI requirements без оновлення будь-якого з 210 resolved versions.
Повторно експортовано hashes через чинний uv 0.13.6; Core/M12/build exports
не змінено. Windows transitive tzlocal 5.4.5/tzdata 2026.6 включено у повні
ліцензійні notices та незалежний module→distribution/missing-notice oracle.

Primary provenance: https://pypi.org/project/APScheduler/3.11.3/ ;
https://pypi.org/project/tzlocal/5.4.5/ ; https://pypi.org/project/tzdata/2026.6/ .
Зібрано повні installed LICENSE тексти; MIT для APScheduler/tzlocal,
Apache-2.0 та upstream data license для tzdata. Джерела/hash залишаються
у чинному uv.lock. Ремонт не додає LLM/provider stack до детермінованого GUI.

Fresh isolated hashed build+packaging installation пройшла 23 config admission
тести, які фактично впали у M11. Її реальний source CLI proof також пройшов
без agent extras. Це Linux package-profile proof; Windows exact-head gates
після зміни профілю мають бути виконані заново. Старий SHA PASS не переноситься.
