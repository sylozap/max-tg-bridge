# Настройка и эксплуатация MAX ↔ Telegram Bridge

Инструкция дополняет [описание проекта](../README.md). Команды выполняются из корня репозитория, если не указано другое.

## Создание ботов и связывание чатов

1. В Telegram откройте [@BotFather](https://t.me/BotFather), выполните `/newbot` и сохраните токен в `TELEGRAM_BOT_TOKEN`. Выполните `/setprivacy`, выберите бота и нажмите **Disable**. Если бот уже в группе, добавьте его повторно после смены privacy mode. Назначьте его администратором группы. Для удаления копий выдайте право **Удаление сообщений**. [Настройки Telegram-ботов](https://core.telegram.org/bots/features#botfather)
2. На [платформе MAX для партнёров](https://business.max.ru/) создайте и верифицируйте профиль юрлица, ИП или самозанятого. В разделе **Чат-боты → Создать** заполните карточку и дождитесь модерации. Откройте меню бота **⋮ → Настройки** и скопируйте токен в `MAX_BOT_TOKEN`. Добавьте бота в тестовую группу и назначьте администратором. Нужны права `read_all_messages` и `write` для чтения всех сообщений, редактирования и удаления. [Создание и модерация](https://dev.max.ru/docs/chatbots/bots-create/create) · [Права бота в MAX](https://dev.max.ru/docs/chatbots/bots-coding/prepare)
3. Если на ботах уже настроены webhook или запущен другой опрос, отключите подписки и остановите прежний процесс. Для Telegram используется `deleteWebhook`, для MAX — `DELETE /subscriptions` с URL прежней подписки. [Telegram deleteWebhook](https://core.telegram.org/bots/api#deletewebhook) · [Удаление подписки MAX](https://dev.max.ru/docs-api/methods/DELETE/subscriptions)
4. Запустите мост. Выполните `/id` в обоих групповых чатах. По желанию выполните `/id me` там же, чтобы бот вывел также числовой ID отправителя. Добавьте нужные ID пользователей в `TELEGRAM_ADMIN_IDS` и `MAX_ADMIN_IDS`, затем примените настройки через `docker compose up -d --force-recreate` или `sudo systemctl restart max-tg-bridge`. Для управляющих команд в Telegram отправляйте их от своего аккаунта, если в группе разрешён анонимный режим администратора.
5. Администратор выполняет `/link <chat_id другого мессенджера>` в одном из двух чатов, сохраняя знак ID. Бот проверит тип обоих чатов и права. Отправьте пробные сообщения в обе стороны и проверьте `/status`.

| Команда | Доступ и действие |
| --- | --- |
| `/start`, `/help` | Всем. Справка |
| `/id`, `/id me` | Всем. ID чата и, по запросу, ваш ID |
| `/status` | Администраторы. Состояние, два размера очереди, счётчики, время и последняя ошибка |
| `/pause`, `/resume` | Администраторы. Пауза и продолжение для обоих направлений текущей пары |
| `/link <chat_id>`, `/unlink` | Администраторы. Связать чаты или снять связь |
| `/pairs` | Администраторы. Список пар |

Во время паузы входящие события отбрасываются. Операция, уже отправленная в API, может завершиться. Возобновление действует на новые события. `/unlink` удаляет связь и соответствия сообщений этой пары. Уже опубликованные сообщения остаются в чатах.

## Конфигурация

Скопируйте `.env.example` в `.env` и ограничьте права файла. При systemd используется `/etc/max-tg-bridge.env`. Пустые необязательные значения используют значения по умолчанию.

| Переменная | Значение |
| --- | --- |
| `TELEGRAM_BOT_TOKEN`, `MAX_BOT_TOKEN` | Обязательные токены |
| `TELEGRAM_ADMIN_IDS`, `MAX_ADMIN_IDS` | Числовые ID пользователей через запятую. Пустой список отключает администрирование в соответствующем мессенджере |
| `DATABASE_PATH` | `data/bridge.db` для локального запуска, `/data/bridge.db` в Compose, `/var/lib/max-tg-bridge/bridge.db` в systemd |
| `LOG_LEVEL`, `DEBUG` | `INFO` и `false`. `DEBUG=true` добавляет технические логи без токенов, текста и URL вложений |
| `AUTHOR_TEMPLATE` | `{author} ({source}):`. Оба поля обязательны. Сам заголовок оформляется жирным |
| `IGNORE_OTHER_BOTS` | `true`. Собственные сообщения моста отбрасываются независимо от настройки |
| `IGNORE_SERVICE_EVENTS` | `true`. При `false` служебные события, которые получает бот, зеркалируются краткими текстовыми пометками |
| `MAX_API_BASE_URL` | `https://platform-api2.max.ru` по актуальным инструкциям MAX. `https://platform-api.max.ru` можно указать явно для старой интеграции |
| `MAX_CA_FILE` | Путь к дополнительному доверенному PEM для MAX. Системное хранилище продолжает использоваться |
| `MAX_MARKUP_UTF16` | `false`. По умолчанию смещения MAX интерпретируются как позиции Unicode. Если на целевом API emoji выявят смещения в UTF-16, задайте `true` |

MAX требует добавить сертификат Минцифры в список доверенных для нового домена. Используйте утверждённый сертификат из доверенного источника. В Compose положите PEM в каталог `certs` и задайте `MAX_CA_FILE=/certs/имя.pem`. Для systemd задайте абсолютный путь к файлу, доступному пользователю службы. [Обзор MAX Bot API](https://dev.max.ru/docs-api/)

## Запуск на VPS

Инструкция рассчитана на VPS с Ubuntu 24.04 LTS или Debian 12/13, доступом по SSH и пользователем с `sudo`. Выполните общую подготовку и выберите один способ запуска.

| Способ | Что установить на сервере | Где хранятся настройки и база |
| --- | --- | --- |
| [Docker Compose](#docker-compose) | Docker Engine, Buildx и Compose | `.env` в репозитории и именованный том `bridge-data` |
| [systemd](#systemd) | Системный Python 3.11+ и uv | `/etc/max-tg-bridge.env` и `/var/lib/max-tg-bridge/bridge.db` |

Для одного набора токенов запускайте один экземпляр моста. Остановите локальный экземпляр перед переходом на VPS. Если боты раньше работали через webhook, снимите подписки по инструкции из раздела [создания ботов](#создание-ботов-и-связывание-чатов). Для работы нужны DNS и исходящий HTTPS к API Telegram, MAX и серверам медиа. Для установки также потребуется доступ к GitHub, репозиторию пакетов выбранной ОС, PyPI и, в случае Docker, к реестрам образов. Домен, входящие HTTP-порты и обратный прокси мосту не нужны.

### Подготовка сервера

Подключитесь по SSH. Замените `vps_user` и `vps.example.com` на логин и IP-адрес или имя своего сервера.

```bash
ssh vps_user@vps.example.com
```

На сервере проверьте дистрибутив, архитектуру и свободное место. Установите общие инструменты.

```bash
cat /etc/os-release
uname -m
df -h .
sudo apt update
sudo apt install -y ca-certificates curl git nano openssl
```

Скачайте код. Для закрытого репозитория понадобится настроенный доступ к GitHub на VPS. Если репозиторий уже есть на сервере, откройте его каталог и проверьте `git status --short` перед обновлением.

```bash
git clone https://github.com/sylozap/max-tg-bridge.git
cd max-tg-bridge
git status --short
```

Дальше выполняйте команды из корня этого репозитория. Подготовьте токены и добавьте ботов в тестовые группы. В Telegram назначьте бота администратором, а для теста удаления копий выдайте право **«Удаление сообщений»**. В MAX назначьте бота администратором с правами **«Читать все сообщения»** и **«Редактировать и удалять сообщения»**.

### Сертификат для API MAX

Для `platform-api2.max.ru` мосту нужен доверенный корневой сертификат Минцифры. Скачайте PEM с Госуслуг и проверьте срок действия перед установкой в `certs/`. Мост загрузит его через `MAX_CA_FILE` вместе с системными сертификатами. [Документация MAX](https://dev.max.ru/docs-api/) · [Сертификаты на Госуслугах](https://www.gosuslugi.ru/crt)

```bash
(
    set -e
    max_ca_download=$(mktemp)
    trap 'rm -f "$max_ca_download"' EXIT
    curl -fsSL --proto '=https' --max-time 30 -o "$max_ca_download" \
        https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt
    openssl x509 -in "$max_ca_download" -noout -subject -issuer -enddate -checkend 0
    install -d -m 755 certs
    install -m 644 "$max_ca_download" certs/max-russian-trusted-root-ca.pem
)
```

В выводе OpenSSL ищите `Russian Trusted Root CA`. Проверьте сеть с VPS до запуска сервиса. Запросы ниже выполняются без токенов. Для MAX ожидается `HTTP 401` с `TLS 0`, то есть TLS проверен, а API запрашивает авторизацию. Для Telegram достаточно получить HTTP-ответ, например `302`.

```bash
curl --noproxy '*' --cacert certs/max-russian-trusted-root-ca.pem \
    -sS -o /dev/null -w 'MAX: HTTP %{http_code}, TLS %{ssl_verify_result}\n' \
    --connect-timeout 10 --max-time 15 https://platform-api2.max.ru/me
curl --noproxy '*' -sS -o /dev/null -w 'Telegram: HTTP %{http_code}\n' \
    --connect-timeout 10 --max-time 15 https://api.telegram.org/
```

Если видите `Could not resolve host`, проверьте DNS. При тайм-ауте проверьте исходящий доступ к TCP 443. При ошибке проверки сертификата проверьте время на сервере (`date -u`), срок действия PEM и результат запроса с `--cacert`.

### Docker Compose

#### Установка и проверка Docker

Если Docker уже установлен, проверьте демон, Buildx и Compose. Команда Compose пишется через пробел. Python и uv устанавливаются внутри образа при сборке.

```bash
sudo docker version
sudo docker buildx version
sudo docker compose version
```

На чистом VPS установите Docker из официального apt-репозитория. Если на сервере есть пакеты `docker.io`, `docker-compose`, `docker-compose-v2`, `podman-docker`, `containerd` или `runc`, сначала проверьте конфликты по инструкции для своего дистрибутива. Удаление пакетов на сервере с другими контейнерами может повлиять на их работу. [Установка на Ubuntu](https://docs.docker.com/engine/install/ubuntu/) · [Установка на Debian](https://docs.docker.com/engine/install/debian/)

```bash
(
    set -e
    . /etc/os-release
    case "$ID" in
        ubuntu) docker_suite=${UBUNTU_CODENAME:-$VERSION_CODENAME} ;;
        debian) docker_suite=$VERSION_CODENAME ;;
        *) echo 'Эти команды рассчитаны на Ubuntu или Debian' >&2; exit 1 ;;
    esac
    sudo install -d -m 755 /etc/apt/keyrings
    sudo curl -fsSL "https://download.docker.com/linux/$ID/gpg" \
        -o /etc/apt/keyrings/docker.asc
    sudo chmod 644 /etc/apt/keyrings/docker.asc
    sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $docker_suite
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
    sudo apt update
    sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    sudo systemctl enable --now docker
)

sudo docker version
sudo docker buildx version
sudo docker compose version
sudo docker run --rm hello-world
```

`docker version` должен показать секции `Client` и `Server`. Запуск `hello-world` проверяет скачивание образа и работу контейнера.

#### Настройка и запуск Compose

Создайте `.env` при первом запуске. При повторном выполнении существующий файл используется с его текущим содержимым.

```bash
if [ ! -e .env ]; then
    install -m 600 .env.example .env
fi
chmod 600 .env
nano .env
```

Заполните `TELEGRAM_BOT_TOKEN` и `MAX_BOT_TOKEN`. Задайте `MAX_CA_FILE=/certs/max-russian-trusted-root-ca.pem` — это путь **внутри контейнера**. `DATABASE_PATH` оставьте пустым для стандартного `/data/bridge.db`. `TELEGRAM_ADMIN_IDS` и `MAX_ADMIN_IDS` можно оставить пустыми до получения личных ID через `/id me`.

```bash
sudo docker compose config --quiet
sudo docker compose build bridge
sudo docker compose run --rm --no-deps bridge python -m app --check-config
sudo docker compose run --rm --no-deps bridge sh -ec 'test -r "$MAX_CA_FILE" && test -w /data'
sudo docker compose up -d bridge
sudo docker compose ps
sudo docker compose logs --tail=50 -f bridge
```

После `Настройки корректны` и успешной проверки прав дождитесь `bridge_started` в журнале. `Ctrl+C` закрывает просмотр журнала, а контейнер продолжает работу. Compose перезапускает сервис после сбоев и использует именованный том `bridge-data` для SQLite и временных медиа.

### systemd

#### Установка Python и uv

Установите системный Python и убедитесь, что его версия 3.11 или новее. Если `uv --version` уже работает, переходите к настройке службы. Для первой установки uv выполните второй блок под своим SSH-пользователем. [Установка uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
sudo apt install -y python3
/usr/bin/python3 --version
```

```bash
(
    set -e
    uv_installer=$(mktemp)
    trap 'rm -f "$uv_installer"' EXIT
    curl -fsSL https://astral.sh/uv/install.sh -o "$uv_installer"
    sh "$uv_installer"
)
export PATH="$HOME/.local/bin:$PATH"
command -v uv
uv --version
```

Если после установки команда недоступна в новом SSH-сеансе, добавьте `~/.local/bin` в `PATH` или повторите `export` из примера.

#### Установка и запуск службы

Создайте пользователя службы и каталог для её окружения. Сервис использует системный Python и копию пакета, установленную в `/opt/max-tg-bridge/.venv`. Это позволяет ему читать приложение при включённом в unit-файле `ProtectHome=true`.

```bash
if ! id max-tg-bridge >/dev/null 2>&1; then
    sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin max-tg-bridge
fi
sudo install -d -m 755 -o "$(id -un)" -g "$(id -gn)" /opt/max-tg-bridge
UV_PROJECT_ENVIRONMENT=/opt/max-tg-bridge/.venv UV_LINK_MODE=copy \
    uv sync --locked --no-dev --no-editable --python /usr/bin/python3 --no-managed-python
chmod -R a+rX /opt/max-tg-bridge/.venv

sudo install -d -m 755 /etc/max-tg-bridge
sudo install -m 644 certs/max-russian-trusted-root-ca.pem /etc/max-tg-bridge/max-russian-trusted-root-ca.pem
if ! sudo test -e /etc/max-tg-bridge.env; then
    sudo install -m 600 .env.example /etc/max-tg-bridge.env
fi
sudoedit /etc/max-tg-bridge.env
```

Заполните `TELEGRAM_BOT_TOKEN`, `MAX_BOT_TOKEN` и `MAX_CA_FILE=/etc/max-tg-bridge/max-russian-trusted-root-ca.pem`. Оставьте `DATABASE_PATH` пустым, чтобы systemd использовал `/var/lib/max-tg-bridge/bridge.db`. Списки администраторов заполните после первого запуска, если личные ID ещё неизвестны. Проверьте, что пользователь службы может прочитать PEM, а приложение загружает конфигурацию через systemd.

```bash
sudo -u max-tg-bridge test -r /etc/max-tg-bridge/max-russian-trusted-root-ca.pem
sudo systemd-run --wait --pipe --collect \
    -p User=max-tg-bridge -p ProtectHome=true \
    -p WorkingDirectory=/opt/max-tg-bridge \
    -p EnvironmentFile=/etc/max-tg-bridge.env \
    /opt/max-tg-bridge/.venv/bin/python -m app --check-config

sudo install -m 644 deploy/max-tg-bridge.service /etc/systemd/system/max-tg-bridge.service
sudo systemd-analyze verify /etc/systemd/system/max-tg-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now max-tg-bridge
sudo systemctl status --no-pager max-tg-bridge
sudo journalctl -u max-tg-bridge -n 50 -f
```

Дождитесь `bridge_started`. `enable --now` запускает службу сейчас и включает запуск после перезагрузки VPS. `Ctrl+C` закрывает просмотр журнала.

### Связывание групп и проверка

1. Отправьте `/id me` в каждой тестовой группе от своего аккаунта. Из ответа запишите отдельно **ID чата** и **«Ваш ID»**. В Telegram используйте свой аккаунт, если в группе разрешена анонимная отправка от имени администратора.
2. Добавьте значения из строки **«Ваш ID»** в `TELEGRAM_ADMIN_IDS` и `MAX_ADMIN_IDS` соответствующего файла настроек. Несколько ID разделяйте запятыми. Если достаточно управления из Telegram, можно заполнить только `TELEGRAM_ADMIN_IDS`.
3. Примените настройки. Для Compose выполните `sudo docker compose up -d --force-recreate bridge`, для systemd — `sudo systemctl restart max-tg-bridge`. Дождитесь нового `bridge_started`.
4. В группе Telegram отправьте `/link -123456789`, подставив **ID чата MAX** из ответа бота с его исходным знаком. Одного `/link` достаточно для обоих направлений. Если связываете из MAX, подставьте ID группы Telegram и выполняйте команду с аккаунта из `MAX_ADMIN_IDS`. После ответа «Чаты связаны» проверьте `/status`.
5. Отправьте по сообщению с emoji и ссылкой из каждой группы. Проверьте, что копии появились по одному разу, имя автора видно, а ссылка открывается. Ответьте на копию, отредактируйте исходный текст и отправьте фото с подписью или альбом.
6. Удалите свежее исходное сообщение в MAX и проверьте, что копия исчезла в Telegram. Telegram Bot API не присылает события о ручном удалении в обычной группе, поэтому для обратного направления копию в MAX можно удалить вручную. [События Telegram Bot API](https://core.telegram.org/bots/api#update)
7. Проверьте `/pause` и `/resume` с новыми сообщениями, затем перезапустите выбранный сервис. `/status` должен показать ту же пару, а новые сообщения должны пройти в обе стороны.

`--check-config` проверяет окружение и формат токена Telegram без сетевых запросов. `bridge_started` означает, что оба API ответили на запрос профиля бота. Ответы на `/id me` и тестовые сообщения проверяют опрос и отправку в группах.

### Журналы и типичные ошибки

Последние 100 строк журнала можно посмотреть командой `sudo docker compose logs --tail=100 bridge` или `sudo journalctl -u max-tg-bridge -n 100 --no-pager`. Для временной диагностики задайте `DEBUG=true` в файле настроек и примените его как после добавления ID.

| Симптом или код | Что проверить |
| --- | --- |
| `max_network` | Повторите запрос с `--cacert` из раздела про сертификат. Проверьте путь `MAX_CA_FILE` и его читаемость внутри контейнера или от имени пользователя службы. |
| `telegram_network` | Проверьте доступ к `api.telegram.org` с VPS. При Docker проверьте DNS и исходящий HTTPS также из контейнера. |
| `max_unauthorized`, `telegram_unauthorized` | Проверьте токен нужного бота в активном файле настроек и примените настройки. |
| `telegram_poll_conflict` | Остановите другой экземпляр с этим токеном, включая локальный мост. Снимите webhook, если он был настроен. |
| `bridge_already_running` | Найдите процесс, который уже использует этот файл базы, и оставьте один экземпляр. |
| `PermissionError`, `FileNotFoundError` | Проверьте наличие и права на PEM и доступ на запись в каталог базы. Для Compose используйте `/data`, для systemd — каталог `/var/lib/max-tg-bridge`. |
| Бот отвечает на `/id me`, но отклоняет `/link` | Проверьте личный ID в списке администраторов, знак ID второго чата и права обоих ботов в группах. |
| `/status` показывает ошибку опроса или доставки | Найдите в журнале соответствующие `platform` и `code`. Если текст доходит, а медиа пропускаются, проверьте исходящий доступ к файловым серверам и [лимиты вложений](#лимиты-и-деградация). |

### Остановка, обновление и резервная копия

Для остановки и повторного запуска используйте `sudo docker compose stop bridge` и `sudo docker compose up -d bridge` либо `sudo systemctl stop max-tg-bridge` и `sudo systemctl start max-tg-bridge`. Оба варианта дают мосту время завершить обработку при остановке.

Перед обновлением или переносом сохраните SQLite, конфигурацию и PEM. Примеры используют стандартные пути к базе. Архив создаётся при остановленном мосте, чтобы SQLite и возможные файлы журнала попали в одну согласованную копию. Храните резервные копии конфигурации в закрытом каталоге, поскольку в них есть токены.

**Docker Compose**

```bash
(
    set -eo pipefail
    umask 077
    bridge_backup=$(mktemp -d "$HOME/max-tg-bridge-backup.XXXXXX")
    sudo docker compose stop bridge
    sudo docker compose run --rm --no-deps -T --entrypoint tar bridge \
        -C /data -cf - . > "$bridge_backup/bridge-data.tar"
    tar -tf "$bridge_backup/bridge-data.tar" | grep -Fx './bridge.db'
    cp .env certs/max-russian-trusted-root-ca.pem "$bridge_backup/"
    printf 'Резервная копия: %s\n' "$bridge_backup"
)
```

После успешной копии выполните `git pull --ff-only` и `sudo docker compose up -d --build bridge`. Для возобновления без обновления выполните `sudo docker compose up -d bridge`. При обновлении сохраняйте имя проекта Compose и именованный том `bridge-data`. Команда `docker compose down -v` удаляет этот том вместе с базой.

**systemd**

```bash
(
    set -eo pipefail
    umask 077
    bridge_backup=$(mktemp -d "$HOME/max-tg-bridge-backup.XXXXXX")
    sudo systemctl stop max-tg-bridge
    sudo tar -C /var/lib/max-tg-bridge -cf - . > "$bridge_backup/bridge-data.tar"
    tar -tf "$bridge_backup/bridge-data.tar" | grep -Fx './bridge.db'
    sudo cat /etc/max-tg-bridge.env > "$bridge_backup/max-tg-bridge.env"
    cp /etc/max-tg-bridge/max-russian-trusted-root-ca.pem "$bridge_backup/"
    printf 'Резервная копия: %s\n' "$bridge_backup"
)
```

Для обновления на остановленной службе выполните команды ниже. `--reinstall-package` устанавливает свежий код моста, даже если версия пакета в `pyproject.toml` осталась прежней.

```bash
git pull --ff-only && \
    UV_PROJECT_ENVIRONMENT=/opt/max-tg-bridge/.venv UV_LINK_MODE=copy \
    uv sync --locked --no-dev --no-editable --python /usr/bin/python3 --no-managed-python \
    --reinstall-package max-tg-bridge && \
    chmod -R a+rX /opt/max-tg-bridge/.venv && \
    sudo install -m 644 deploy/max-tg-bridge.service /etc/systemd/system/max-tg-bridge.service && \
    sudo systemctl daemon-reload && \
    sudo systemctl start max-tg-bridge
```

Для возобновления без обновления выполните `sudo systemctl start max-tg-bridge`. После обновления проверьте новый `bridge_started`, `/status` и пересылку в обе стороны.

### Перенос базы на новый VPS

Остановите прежний экземпляр. Скопируйте архив `bridge-data.tar`, подготовьте новый VPS по инструкции выше до первого запуска и заполните его файл настроек. Для токенов и списков администраторов используйте сохранённую конфигурацию, а путь `MAX_CA_FILE` укажите для нового способа запуска. Для переноса из локального запуска со стандартным путём к базе создайте архив в домашнем каталоге после остановки моста.

```bash
(umask 077; tar -C data -cf "$HOME/bridge-data.tar" .)
```

Подставьте путь к своему архиву. Команды восстанавливают данные в пустой каталог и выполняют распаковку от имени пользователя выбранного сервиса.

**Docker Compose**

```bash
bridge_archive=/путь/к/bridge-data.tar
sudo docker compose run --rm --no-deps -T --entrypoint sh bridge -ec '
    if [ -n "$(ls -A /data)" ]; then
        echo "Каталог /data уже содержит данные" >&2
        exit 1
    fi
    tar --no-same-owner -C /data -xf -
' < "$bridge_archive"
```

**systemd**

```bash
bridge_archive=/путь/к/bridge-data.tar
sudo install -d -m 700 -o max-tg-bridge -g max-tg-bridge /var/lib/max-tg-bridge
sudo -u max-tg-bridge sh -ec '
    if [ -n "$(ls -A /var/lib/max-tg-bridge)" ]; then
        echo "Каталог /var/lib/max-tg-bridge уже содержит данные" >&2
        exit 1
    fi
    tar --no-same-owner -C /var/lib/max-tg-bridge -xf -
' < "$bridge_archive"
```

После восстановления запустите мост на новом VPS и проверьте `/pairs`, `/status` и тестовую пересылку. Существующие пары загрузятся из восстановленной базы.

### Локальная проверка

Для ручного запуска из терминала установите uv, как описано выше. Подготовьте `.env` с токенами и задайте `MAX_CA_FILE=certs/max-russian-trusted-root-ca.pem`, если сертификат скачан по инструкции. `DATABASE_PATH` можно оставить пустым для `data/bridge.db`. Выполните из корня проекта.

```bash
uv sync --locked --extra test
.venv/bin/python -m app --check-config
.venv/bin/python -m pytest -q
.venv/bin/python -m app
```

Тесты используют локальную SQLite, тестовые адаптеры и подставной HTTP-транспорт. Для проверки реальных групп выполните шаги из [раздела связывания](#связывание-групп-и-проверка). Локальный процесс останавливается через `Ctrl+C`.

## Как работают адаптеры

- Telegram использует `getUpdates` в aiogram 3 и передаёт исходящие entities со смещениями UTF-16. MAX использует тонкий клиент на httpx, чтобы явно контролировать `marker`, заголовок `Authorization` и многошаговую обработку медиа. Контрактные HTTP-запросы MAX собраны в `app/adapters/max.py`. [Telegram Bot API](https://core.telegram.org/bots/api) · [MAX Bot API](https://dev.max.ru/docs-api/)
- MAX возвращает `message_created`, `message_edited`, `message_removed` и служебные события. Мост посылает HTML в `POST /messages` и использует `PUT /messages`, `DELETE /messages` и `link.type=reply`. Команды регистрируются через `PATCH /me/commands`. Входящие slash-команды считываются из сообщений, кнопка запуска обрабатывается через `bot_started`. [События](https://dev.max.ru/docs-api/objects/Update) · [Отправка](https://dev.max.ru/docs-api/methods/POST/messages/) · [Правки](https://dev.max.ru/docs-api/methods/PUT/messages) · [Удаление](https://dev.max.ru/docs-api/methods/DELETE/messages) · [Команды](https://dev.max.ru/docs-api/methods/PATCH/me/commands/)
- Загрузка в MAX идёт через `POST /uploads`, затем через multipart-поле `data` на выданный URL. Для изображений токен берётся из `photos`, для файлов — из ответа загрузки, для аудио и видео используется выданный при получении URL токен после подтверждения загрузки. Подготовленные токены повторно используются при `attachment.not.ready`. При скачивании видео возможен запрос `GET /videos/{videoToken}`. API-токен остаётся внутри клиента MAX API. Загрузчик проверяет хост URL и каждой переадресации. [Подготовка загрузки](https://dev.max.ru/docs-api/methods/POST/uploads/) · [Примеры ответов](https://dev.max.ru/docs-api/use-cases/sending-messages/media/) · [Ссылки видео](https://dev.max.ru/docs-api/methods/GET/videos/-videoToken-)
- Ядро принимает нормализованный пакет, направляет его в отдельную FIFO-очередь каждой пары и направления и ждёт результата обработки перед сохранением курсора. Сетевые ошибки и 429 повторяются с задержкой в том же слоте. `Retry-After` MAX и `retry_after` Telegram задают минимальное ожидание. После исчерпания попыток окончательная ошибка попадает в журнал и `/status`, очередь продолжает работу. Для добавления webhook можно подать нормализованный пакет в то же ядро.
- Ответ на исходник или на копию использует таблицу соответствий в обе стороны. Без доступного соответствия добавляется цитата. При пересылке указывается исходный автор, если мессенджер раскрыл его. `@username` сохраняет видимое написание за счёт невидимого разделителя, а упоминания по ID остаются обычным текстом. Это исключает нежелательные упоминания в другом чате. Безопасные HTTP(S)-ссылки сохраняются вместе с текстом ссылки.

## Лимиты и деградация

| Направление или API | Ограничения |
| --- | --- |
| Telegram → MAX | Облачный Telegram Bot API выдаёт через `getFile` файлы до 20 МБ |
| MAX → Telegram | Multipart в облачном Telegram Bot API до 10 МБ для фото и до 50 МБ для файлов. Крупные фото до 50 МБ отправляются документом |
| MAX Bot API | Изображения до 50 МБ и 7680×7680. Видео до 250 МБ. Аудио до 256 МБ и 60 минут. Файлы до 4 ГБ. Изображения и видео можно объединить, до 12 вложений. Документы отправляются отдельными сообщениями |
| Текст | Telegram до 4096 символов, подпись до 1024. MAX до 4000. Длинные тексты автоматически режутся по границам с повторным применением разметки |

Итоговый лимит определяется самым строгим этапом передачи. Мост проверяет известный размер и число байт при скачивании. [Файлы Telegram](https://core.telegram.org/bots/api#sending-files) · [Лимиты MAX](https://dev.max.ru/docs-api/methods/POST/uploads/)

- Фото, видео, документы, аудио, геолокации и доступные карточки контактов передаются как вложения. Telegram-голосовые и видеокружки становятся аудио и видео в MAX. При возможности GIF передаётся как анимация. Статические WebP-стикеры Telegram в MAX передаются файлом, видеостикеры — видео. Для анимированных TGS-стикеров и опросов используется текстовая замена. Для недоступного файла, ссылки CDN, просроченного URL или ограничения формата мост добавляет пометку с причиной. Документы, контакты, координаты и вложения без API-подписи могут потребовать отдельной части рядом с заголовком.
- Telegram-спойлеры зеркалируются открытым текстом в MAX, где HTML-спойлер не задокументирован. Обычные и кодовые блоки, ссылки, цитаты и стили переносятся по возможностям целевой платформы. Документация MAX описывает смещения разметки в символах без примера с emoji. Значение `MAX_MARKUP_UTF16` позволяет переключить интерпретацию. [Форматирование MAX](https://dev.max.ru/docs-api/use-cases/sending-messages/text-formatting/)
- Альбомы Telegram собираются по `media_group_id`. Основные фото и видео уходят одним сообщением MAX. Часть альбома, пришедшую после окна сборки, мост отправляет отдельным сообщением. При редактировании единственной подписи без замены вложений обновляется копия альбома. Для сложной правки альбома мост отправляет пояснение к копии. Некоторые виды вложений Telegram нельзя заменить in-place. В этом случае мост пытается пересоздать часть.
- В группах Telegram Bot API не сообщает об удалении исходника. При удалении в MAX мост удаляет копию в Telegram, пока это допускает `deleteMessage` (обычно до 48 часов после отправки). [События Telegram](https://core.telegram.org/bots/api#update) · [Ограничения удаления](https://core.telegram.org/bots/api#deletemessage)
- Порядок сохраняется внутри каждого направления пары. При почти одновременных сообщениях суммарный порядок в двух чатах может отличаться. Telegram хранит непрочитанные обновления до 24 часов, MAX ограничивает историю Long Polling. При первом запуске без `marker` MAX отдаёт только последнее доступное обновление. [Telegram Updates](https://core.telegram.org/bots/api#getting-updates) · [MAX Long Polling](https://dev.max.ru/docs-api/methods/GET/updates/)
- При перезапуске обработанные события и зафиксированные копии подавляются по техническим ID. Возможен дубль, если API принял отправку, но процесс завершился до записи ID или ответ потерялся в сети. Атомарной операции между SQLite и внешними Bot API нет. Часть с окончательной ошибкой доставки учитывается в `/status`.
