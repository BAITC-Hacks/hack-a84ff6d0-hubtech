# Агенты / роли под задачу ЭКТ (автозаказы поставщикам)

> Кейс: ТОО «Электрокомплект» — рекомендованные заказы поставщикам.
> См. первоисточник: [relly-problem.md](./relly-problem.md)
> Источник агентов: https://github.com/msitarzewski/agency-agents (230+ промпт-персон).
> ⚠️ GIS-дивизион больше **не релевантен** — задача про прогноз спроса, а не карты.

---

## 🎯 Ядро команды под задачу

| Агент | Дивизион | Роль в проекте |
|---|---|---|
| **AI Engineer** | Engineering | Модели прогноза спроса, детекция аномалий, интеграция OpenAI/Codex |
| **Financial Analyst** | Finance | Моделирование потребности, forecasting, оценка издержек хранения/дефицита |
| **Backend Architect** | Engineering | FastAPI + пайплайн загрузки данных из 1С, формула расчёта |
| **Database Optimizer** | Engineering | Схема под историю продаж/остатки/stockout, быстрые агрегаты |
| **Frontend Developer** | Engineering | Дашборд заказов + экспорт в Excel (совместимость с 1С) |
| **Security Engineer** | Security | Обезличивание клиентских данных — жёсткое требование кейса |
| **Product Manager** | Product | Резать scope до MVP из 5 Must-have за 1 день |
| **Sprint Prioritizer** | Product | Приоритезация задач под дедлайн Demo Day |
| **Reality Checker** | Testing | Прогон по проверкам Must-have (evidence-based) перед защитой |
| **Evidence Collector** | Testing | Скриншоты/артефакты для демо и README |

## 🔗 Как роли ложатся на 5 Must-have

| Must-have | Ответственные агенты |
|---|---|
| 1. Базовый расчёт потребности по всем источникам | AI Engineer + Backend Architect |
| 2. Сезонность и устойчивый рост спроса | AI Engineer + Financial Analyst |
| 3. Компенсация упущенного спроса (stockout) | AI Engineer |
| 4. Исключение разовых крупных заказов | AI Engineer + Reality Checker (проверка) |
| 5. Список по поставщикам с обоснованием | Backend Architect + AI Engineer (LLM-обоснование) |

## 🧭 Рекомендованная связка (workflow)
```
Product Manager        → режет scope до 5 Must-have (MVP)
   ↓
Database Optimizer     → схема под 1С-данные (продажи, остатки, stockout, поставщики)
Backend Architect      → FastAPI + формула базовой потребности
AI Engineer            → прогноз спроса + сезонность + исключение аномалий
Financial Analyst      → валидация логики потребности и издержек
   ↓
Backend + AI Engineer  → группировка по поставщикам + LLM-обоснование (Codex)
Frontend Developer     → дашборд + экспорт в Excel (1С)
Security Engineer      → проверка обезличивания клиентских данных
   ↓
Reality Checker        → прогон по всем проверкам Must-have перед Demo Day (29.09)
Evidence Collector     → артефакты для защиты
```

## Как использовать
1. Клонировать репо `msitarzewski/agency-agents`.
2. Установить перечисленных агентов через скрипт репо (поддержка Claude Code / Codex / Cursor).
3. Делегировать роли: каждый агент = markdown-инструкция в `agents/`.

> 💡 Альтернатива: написать 2-3 **своих** агента под конкретный кейс ЭКТ
> (например, `demand-forecaster`, `order-explainer`, `data-privacy-guard`),
> вместо generic-персон — часто точнее под узкую задачу.

## ⚠️ Убрано из ранней версии (гипотеза «маршрутизация»)
- ~~GIS Division: Spatial Data Engineer, Web GIS Developer, GIS Consultant~~ — карты не нужны
- ~~RAG Pipeline Engineer~~ — база знаний не в центре задачи (можно вернуть, если делать справку по регламентам)
