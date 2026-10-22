# litetox proxy

Wialon IPS прокси для GSM-модулей **ALTOX** (WBUS-5 GPS, GSM-5 и др.), управляющих автономными отопителями Webasto/Eberspächer. Это серверная часть интеграции Home Assistant [**Litetox Proxy**](https://github.com/nightrus/ha-litetox), без него интеграция не работает.

![Схема взаимодействия](https://raw.githubusercontent.com/nightrus/ha-litetox/main/docs/scheme.png)

Модуль подключается по GPRS только к прокси. Прокси:
- разбирает кадры модуля (`#B#`/`#D#`/`#SD#` позиции и телеметрия, `#W#` статус отопителя, `#U#` баланс/USSD и настройки, `#C#` соты GSM, `#E#` диагностика Webasto), отвечает модулю подтверждениями и хранит всё в MariaDB;
- держит **зеркальную сессию** с ALTOX SERVER 2.0 (по умолчанию `d2.altox.app:20332`): копия всех кадров уходит туда, а команды из официального приложения ALTOX передаются модулю. Поэтому приложение ALTOX продолжает работать;
- отдаёт состояние и принимает команды через HTTP API (для Home Assistant, скриптов, curl).

Принимаются только IMEI, зарегистрированные через API: модуль с чужим IMEI получает `#AL#0`, и соединение закрывается.

## Запуск

Нужны Docker и docker compose.

```sh
git clone https://github.com/nightrus/litetox.git && cd litetox
cp .env.example .env      # задайте API_TOKEN, DB_PASSWORD, DB_ROOT_PASSWORD и DOMAIN
docker compose up -d --build
```

В составе три контейнера: `litetox` (прокси), `litetox-db` (MariaDB) и `litetox-caddy` (HTTPS).

- TCP `20013` (Wialon IPS) должен быть открыт в интернет: на него подключается модуль.
- HTTP API наружу не публикуется. Его отдаёт Caddy по `https://<DOMAIN>` с автоматическим сертификатом Let's Encrypt. Для этого A-запись домена должна указывать на сервер, а порты 80 и 443 должны быть открыты. Этот адрес указывается в Home Assistant.
- MariaDB настроена под маленький VPS (`mariadb/low-mem.cnf`, ~70 МБ).

### Свой reverse proxy вместо Caddy

Если на сервере уже есть reverse proxy (Caddy, nginx, Traefik), встроенный Caddy можно отключить, а прокси подключить к общей docker-сети. Пример `docker-compose.override.yml` (compose подхватывает его автоматически):

```yaml
services:
  caddy:
    profiles: [disabled]   # не запускать встроенный Caddy
```

После этого подключите свой reverse proxy к сети `litetox-web` (имя задаётся `WEB_NETWORK`) как к внешней (`external: true`) и проксируйте домен на `http://litetox:20080`. Можно и наоборот: подключить `litetox` к уже существующей сети вашего reverse proxy, добавив её в `networks` сервиса в том же override.

### Настройка модуля

Адрес прокси задаётся модулю SMS-командой, например:

```
IPADDR1:<ip прокси>,IPPORT1:20013,TRACKING1
```

ALTOX-сервер модулю указывать не нужно: на него сессию зеркалит прокси. IMEI модуля можно узнать SMS-командой `ID1`.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `API_TOKEN` | — | Bearer-токен для HTTP API (обязательно) |
| `DB_PASSWORD`, `DB_ROOT_PASSWORD` | — | пароли MariaDB |
| `ALTOX_UPSTREAM` | `d2.altox.app:20332` | ALTOX SERVER 2.0 по умолчанию (`host:port`, пусто = без зеркала); для каждого IMEI можно переопределить из Home Assistant |
| `DOMAIN` | — | домен HTTPS API для встроенного Caddy |
| `TCP_PORT` | `20013` | внешний порт для модулей |
| `WEB_NETWORK` | `litetox-web` | имя docker-сети, общей с reverse proxy |
| `STATUS_POLL` | `600` | раз во сколько секунд запрашивать `cmd_STATUS` |
| `USSD_POLL` | `86400` | раз во сколько секунд запрашивать баланс (`cmd_ussd`) |
| `RAW_RETENTION_DAYS` | `30` | сколько хранить журнал сырых кадров |
| `DEFAULT_IMEI` | пусто | устройство для старых `/state` и `/cmd` без IMEI |

## HTTP API

Все запросы, кроме `/health`, требуют заголовок `Authorization: Bearer <API_TOKEN>`.

| Метод | Путь | Назначение |
|---|---|---|
| GET | `/health` | версия, без авторизации |
| GET | `/devices` | список IMEI |
| POST | `/devices` | регистрация/включение IMEI: `{"imei", "name", "upstream"}`; `upstream` — `"host:port"` ALTOX-сервера, `""` — зеркало выключено, `null` — значение `ALTOX_UPSTREAM` |
| DELETE | `/devices/{imei}` | отключить IMEI |
| GET | `/state/{imei}` | текущее состояние (в т.ч. `altox_upstream`, `altox_connected`) |
| POST | `/cmd/{imei}` | `{"command": "STATUS"}` → id команды (префикс `cmd_` добавляется сам; 409, если модуль не на связи) |
| GET | `/cmd/{imei}/{id}` | статус команды и ответ модуля |
| GET | `/history/{imei}?type=positions\|heater\|balance\|commands` | история (`limit`, `since`) |

Пример:

```sh
curl -H "Authorization: Bearer $API_TOKEN" -X POST https://litetox.example.com/cmd/<imei> \
     -d '{"command": "START:30"}'
```

Команды модуля описаны в инструкции ALTOX: `STATUS`, `START`, `START:30`, `STOP`, `ussd`, `gsminfo`, `READ`, `ACTIVATE`/`DEACTIVATE` и др. Между `ussd` и следующей командой прокси сам выдерживает паузу: активная USSD-сессия модуля «съедает» следующую команду.
