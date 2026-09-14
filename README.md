# gitlab-migrate

CLI для переноса репозиториев между двумя инстансами GitLab.

Скрипт умеет:

1. По списку `group/project` найти проекты в **исходном** GitLab
2. Выгрузить по каждому проекту:
   - метаданные проекта
   - CI/CD variables
   - labels
   - protected branches / tags
   - webhooks и deploy keys (опционально)
   - список веток/тегов и sample коммитов
   - полный git-mirror (ветки, теги, вся история)
   - опционально официальный GitLab export-архив (issues/MR/wiki)
3. Отдельным действием загрузить этот пул в **целевой** GitLab

## Что переносится надёжно

| Данные | Способ |
|---|---|
| Исходники, ветки, теги, история коммитов | `git clone --mirror` → push `refs/heads/*` + `refs/tags/*` |
| Описание, visibility, path | GitLab API create project |
| CI/CD variables | GitLab API `/variables` |
| Labels | GitLab API |
| Protected branches/tags | GitLab API |
| Issues / MR / Wiki / snippets | через `--with-bundle` + `--prefer-bundle` |

## Ограничения

- **Members / permissions** не копируются автоматически (роли на другом инстансе другие).
- **Container Registry / Packages / Releases assets** этим CLI не переносятся.
- **Runners** не мигрируются.
- Значения **masked** переменных доступны API только при достаточном уровне прав; на целевом GitLab masked-формат может отличаться — тогда переменная создаётся без `masked`.
- Token webhook’а в API часто **не возвращается** — hook создастся, но secret нужно будет проставить вручную.
- Для создания групп/проектов на целевом GitLab нужны права Maintainer/Owner (или admin).

## Требования

- [uv](https://docs.astral.sh/uv/)
- Python 3.9+ (uv установит сам при необходимости)
- `git` в PATH
- Personal Access Token на обоих GitLab с scopes примерно:
  - source: `read_api`, `read_repository`
  - destination: `api`, `write_repository`
  - для variables на source желательно роль Maintainer+

## Установка

Если `uv` ещё нет:

```powershell
# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex
```

Далее в проекте:

```powershell
cd D:\projects\gitlab-migrate
uv sync
```

`uv sync` создаст `.venv`, поставит зависимости из `pyproject.toml` / `uv.lock` и установит CLI `gitlab-migrate`.

Проверка:

```powershell
uv run gitlab-migrate --help
```

Либо активировать окружение и вызывать напрямую:

```powershell
.\.venv\Scripts\Activate.ps1
gitlab-migrate --help
```

## Подготовка списка репозиториев

Скопируйте пример и заполните полными путями:

```powershell
copy repos.example.txt repos.txt
```

Формат `repos.txt`:

```text
# комментарии допустимы
my-group/frontend
my-group/backend
other-group/subgroup/service-a
```

## Dry-run (что найдено и что будет перенесено)

Без изменений на диске / в целевом GitLab — только lookup и план.

### Export dry-run

Ищет проекты в source по `repos.txt`, читает inventory (ветки, теги, variables, labels…) и печатает, что было бы выгружено:

```powershell
uv run gitlab-migrate export `
  --repos-file repos.txt `
  --dry-run `
  --skip-missing
```

### Import dry-run

Смотрит локальный пул `export_data`, проверяет есть ли уже проект на destination, и печатает план импорта:

```powershell
uv run gitlab-migrate import `
  --in export_data `
  --path-prefix migrated `
  --dry-run
```

### Полный migrate dry-run

Сразу source → destination: что найдено на исходном, куда ляжет на целевом, существует ли target, какие шаги выполнятся:

```powershell
uv run gitlab-migrate migrate `
  --src-url https://gitlab-source.example.com `
  --src-token glpat-xxx `
  --dst-url https://gitlab-dest.example.com `
  --dst-token glpat-yyy `
  --repos-file repos.txt `
  --path-prefix migrated `
  --dry-run `
  --skip-missing
```

В конце всегда будет блок `DRY-RUN: ...` и JSON-отчёт (`would_export` / `would_import` / `would_migrate`).

## Быстрый старт (два шага)

### 1) Export из первого GitLab

```powershell
$env:SRC_GITLAB_URL="https://gitlab-source.example.com"
$env:SRC_GITLAB_TOKEN="glpat-xxx"

uv run gitlab-migrate export `
  --repos-file repos.txt `
  --out export_data `
  --skip-missing
```

Полезно добавить официальный bundle (issues/MR/wiki):

```powershell
uv run gitlab-migrate export `
  --url $env:SRC_GITLAB_URL `
  --token $env:SRC_GITLAB_TOKEN `
  --repos-file repos.txt `
  --out export_data `
  --with-bundle `
  --skip-missing
```

Результат в `export_data/`:

```text
export_data/
  group__project/
    metadata.json      # метаданные + variables + branches/tags/...
    repo.git/          # bare mirror репозитория
    gitlab_export.tar.gz   # если --with-bundle
  export_summary.json
```

### 2) Import во второй GitLab

```powershell
$env:DST_GITLAB_URL="https://gitlab-dest.example.com"
$env:DST_GITLAB_TOKEN="glpat-yyy"

uv run gitlab-migrate import `
  --in export_data `
  --path-prefix migrated
```

`--path-prefix migrated` положит проекты в `migrated/group/project` на целевом инстансе. Если префикс не нужен — просто не указывайте флаг.

Если экспортировали bundle и хотите импортировать через официальный Import API:

```powershell
uv run gitlab-migrate import `
  --in export_data `
  --prefer-bundle `
  --with-hooks
```

## Одной командой

```powershell
uv run gitlab-migrate migrate `
  --src-url https://gitlab-source.example.com `
  --src-token glpat-xxx `
  --dst-url https://gitlab-dest.example.com `
  --dst-token glpat-yyy `
  --repos-file repos.txt `
  --work-dir export_data `
  --skip-missing
```

## Проверка доступа

```powershell
uv run gitlab-migrate whoami --url https://gitlab-source.example.com --token glpat-xxx
uv run gitlab-migrate whoami --url https://gitlab-dest.example.com --token glpat-yyy
```

Если используется self-signed сертификат:

```powershell
uv run gitlab-migrate export ... --insecure
```

## Полезные флаги

**export**

- `--dry-run` — только найти проекты и показать план выгрузки
- `--no-git` — только метаданные, без clone
- `--with-bundle` — скачать official project export
- `--skip-missing` — не падать, если репо из списка нет

**import**

- `--dry-run` — показать план импорта и статус target-проектов
- `--no-git` / `--no-variables` / `--no-labels` / `--no-protections`
- `--with-hooks` — импорт webhooks
- `--with-deploy-keys` — импорт deploy keys
- `--prefer-bundle` — если есть `gitlab_export.tar.gz`, импортировать им
- `--path-prefix NAME` — добавить namespace-префикс на целевом GitLab

**migrate**

- `--dry-run` — полный план source → destination без выгрузки и загрузки

## Рекомендуемый workflow

1. `uv sync`
2. `uv run gitlab-migrate whoami` на обоих инстансах
3. `uv run gitlab-migrate migrate --dry-run` — проверить, что найдено
4. Export 1–2 тестовых репозиториев
5. `uv run gitlab-migrate import --dry-run` по локальному пулу
6. Import в тестовую группу на целевом GitLab (`--path-prefix test-migration`)
7. Сверить ветки/теги/variables
8. Прогнать полный список

## Права токена (кратко)

На source GitLab токен должен читать проекты и (желательно) variables.  
На destination — создавать группы/проекты, пушить код и писать variables.
