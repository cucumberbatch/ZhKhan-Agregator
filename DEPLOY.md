# Развёртывание

## Что сервису нужно от хостинга

| Требование | Почему |
|---|---|
| Постоянный диск | SQLite (`maptool.db`) — единственное хранилище |
| ≥ 1 ГБ RAM | пик Chromium с OCR-моделью — 416 МБ, плюс система |
| Процесс 24/7 | фоновое обновление в 3:00 и long polling бота |
| Исходящий HTTPS | qportal.kz, Nominatim, elicense.kz, api.telegram.org |
| Входящий HTTP(S) | сайт; для Telegram — **не нужен** (используется long polling) |

Отдельно: белый IP не требуется, домен не обязателен.

## Почему не Cloud Run

Формально Cloud Run подходит, но с тремя оговорками, каждая из которых
ломает что-то существенное:

1. **Файловая система эфемерна.** SQLite живёт в слое контейнера и
   исчезает при каждом пересоздании инстанса. Спасает только том
   Cloud Storage, смонтированный через FUSE, — а это медленно,
   требует настройки блокировок файлов и ломается, если инстансов
   больше одного: два писателя в один файл по сети.
2. **Фоновое задание в 3:00 требует always-on CPU.** На бесплатном
   тарифе Cloud Run это недоступно, а на платном тариф стоит отдельных
   денег: инстанс с `min-instances=1` и постоянно выделенным CPU
   считается круглосуточно, даже когда nobody не заходит.
3. **Холодный старт.** Chromium поднимается ~130 секунд. Первый
   запрос после простоя либо превысит таймаут, либо заставит
   пользователя ждать.

Поэтому ниже основной путь — обычная виртуальная машина. Там
диск настоящий, процесс живёт всегда, а приложение запускается
почти без изменений.

---

## Вариант A. Google Compute Engine, e2-micro (бесплатно)

Единственный бесплатный вариант в Google: 1 vCPU, 1 ГБ RAM,
30 ГБ постоянного диска. Бесплатные регионы — только США
(`us-west1`, `us-central1`, `us-east1`), отсюда пинг до
Казахстана, но для нашей нагрузки это не критично.

### 1. Проект и биллинг

```bash
gcloud projects create zhkhan --name="ZhKhan"
gcloud billing projects link zhkhan --billing-account=ВАШ_БИЛЛИНГ_ID
gcloud config set project zhkhan
gcloud services enable compute.googleapis.com
```

Нужен платёжный аккаунт с картой: без привязки биллинга free tier
не работает. Деньги при этом не списываются — следите за бюджетными
алертами, а не за балансом.

### 2. Машина

```bash
gcloud compute instances create zhkhan-vm \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-standard \
  --no-boot-disk-auto-delete \
  --scopes=https://www.googleapis.com/auth/cloud-platform
```

`--no-boot-disk-auto-delete` — чтобы случайное удаление инстанса
не унесло базу вместе с ним. Данные лежат на загрузочном диске
(`/opt/zhkhan/data`), отдельный том не нужен.

### 3. Открыть порты

```bash
gcloud compute firewall-rules create zhkhan-allow-http \
  --allow=tcp:80,tcp:443 --network=default --source-ranges=0.0.0.0/0
```

### 4. Развернуть приложение

```bash
gcloud compute ssh zhkhan-vm --zone=us-central1-a
```

На машине:

```bash
curl -sSL https://raw.githubusercontent.com/cucumberbatch/ZhKhan-Agregator/main/deploy/setup_vm.sh | bash
```

Скрипт поставит Docker, создаст swap, соберёт образ, попросит вписать
токен бота в `/opt/zhkhan/.env` и запустит контейнер. Второй запуск
скрипта (после заполнения `.env`) доделает остальное.

### 5. HTTPS

Caddy ставится скриптом и сам берёт сертификат Let's Encrypt:

```bash
echo 'ваш-домен { reverse_proxy 127.0.0.1:8080 }' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Без домена: откройте `http://<внешний-IP>:8080`, задав при старте
`BIND=0.0.0.0`. Работать будет, но без шифрования.

### 6. Проверка

```bash
curl http://127.0.0.1:8080/api/telegram
docker logs -f zhkhan
```

---

## Вариант B. Oracle Cloud Always Free (бесплатно, но с запасом)

12 ГБ RAM вместо 1 ГБ, 200 ГБ диска, ARM-процессор. Регион при
регистрации выбирается навсегда — ставьте Франкфурт или Цюрих.

```bash
# после создания инстанса A1
ssh -i ~/.ssh/<ключ> ubuntu@<ip>
curl -sSL https://raw.githubusercontent.com/cucumberbatch/ZhKhan-Agregator/main/deploy/setup_vm.sh | bash
```

Особенности:

- **ARM.** `playwright` и `ddddocr` под aarch64 есть, но `onnxruntime`
  после версии 1.23.2 потерял cp310. В `Dockerfile` тогда нужен
  пин `onnxruntime==1.23.2` либо переход на Python 3.11.
- **Oracle может остановить простаивающий инстанс** (CPU, сеть и
  память ниже 20% неделю подряд). Данные при этом сохраняются.
- Дефицит ресурсов A1 — создание приходится повторять, если
  «out of host capacity».

---

## Вариант C. Cloud Run (если всё-таки нужен)

Только если устраивает платный тариф (десятки долларов в месяц за
`min-instances=1` с постоянно выделенным CPU) и риск с SQLite.

Обязательные настройки сервиса:

```bash
gcloud run deploy zhkhan \
  --image=gcr.io/ВАШ_ПРОЕКТ/zhkhan \
  --region=asia-central1 \
  --min-instances=1 --max-instances=1 \
  --cpu=1 --memory=2Gi --timeout=3600 \
  --set-env-vars="MAPTOOL_DB=/data/maptool.db,REFRESH_ENABLED=1" \
  --add-volume=name=data,storage-source=gcs:///ВАШ-БАКЕТ/data \
  --add-volume-mount=volume=data,mount-path=/data \
  --service-account=ВАШ-СЕРВИС-АККАУНТ
```

Команды создания бакета и сервис-аккаунта:

```bash
gcloud storage buckets create gs://ВАШ-БАКЕТ --location=asia-central1
gcloud iam service-accounts create zhkhan
gcloud storage buckets add-iam-policy-binding gs://ВАШ-БАКЕТ \
  --member="serviceAccount:zhkhan@ВАШ_ПРОЕКТ.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

Ограничения, которые надо учесть:

- `--max-instances=1` обязателен: два инстанса в один том — это два
  писателя в один файл SQLite по сети.
- Том GCS работает через FUSE: файловые блокировки там работают не
  так, как на локальном диске. Для SQLite это официально
  нерекомендуемая конфигурация.
- `--timeout=3600` нужен для SSE и для холодного старта Chromium.
- Бесплатный tier Cloud Run — по объёму использования и не привязан к
  региону. Но `min-instances=1` с постоянно выделенным CPU в него не
  входит: такая конфигурация считается круглосуточно и оплачивается.
  Именно этот пункт, а не трафик, делает Cloud Run платным.

---

## После развёртывания: что настроить

| Переменная | Зачем |
|---|---|
| `TELEGRAM_BOT_TOKEN` | без неё бот не стартует; токен от @BotFather |
| `PUBLIC_URL` | адрес сайта в ссылках в сообщениях бота |
| `REFRESH_ENABLED` | `1` — ежедневное обновление (по умолчанию включено) |
| `REFRESH_HOUR` / `REFRESH_MINUTE` | время запуска по Астане, по умолчанию 3:00 |

### Первое обновление вручную

Планировщик ждёт 3:00 и молчит при первом запуске. Чтобы прогнать
сбор сразу:

```bash
curl "http://127.0.0.1:8080/api/refresh?run=1"
```

### Проверить, что бот жив

```bash
curl http://127.0.0.1:8080/api/telegram      # состояние и подписчики
curl "http://127.0.0.1:8080/api/telegram?test=1"   # тестовое сообщение
```

## Чего ждать на сервере

- **Капча elicense.kz с IP дата-центра.** С домашнего адреса капчи
  почти не бывает, с IP облака может быть чаще или могут начать
  блокировать. Это главный непроверенный риск. Проверяется так:
  после первого прогона посмотрите `docker logs zhkhan | grep -i капч`.
- **Первый обход занимает 1–2 часа.** Скорость ограничена капчей:
  примерно 45 секунд на застройщика, застройщиков около 70.
- **Холодный старт Chromium — 130 секунд.** Первый запрос после
  простоя инстанса будет долгим.
- **Telegram может быть недоступен из некоторых сетей.** С российских
  адресов `api.telegram.org` не отвечает — с сервера в США или Европе
  проблемы не будет.

## Откат

```bash
cd /opt/zhkhan && git pull --ff-only
docker build -t zhkhan:local . && docker rm -f zhkhan
bash deploy/setup_vm.sh
```

Данные в `/opt/zhkhan/data` при этом не трогаются.
