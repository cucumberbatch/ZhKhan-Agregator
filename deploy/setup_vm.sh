#!/usr/bin/env bash
# Развёртывание ZhKhan-Agregator на Ubuntu-сервере (Compute Engine, Oracle A1
# и любой другой обычный VPS).
#
#   ssh -i KEY user@IP 'curl -sSL https://raw.githubusercontent.com/cucumberbatch/ZhKhan-Agregator/main/deploy/setup_vm.sh | bash'
#
# Скрипт идемпотентен: повторный запуск ничего не ломает.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/cucumberbatch/ZhKhan-Agregator.git}"
APP_DIR="${APP_DIR:-/opt/zhkhan}"
DATA_DIR="${DATA_DIR:-/opt/zhkhan/data}"
IMAGE="${IMAGE:-zhkhan:local}"
PORT="${PORT:-8080}"
# Порт публикации: по умолчанию только localhost, наружу — Caddy.
BIND="${BIND:-127.0.0.1}"
# Предел памяти контейнера. На бесплатной e2-micro всего 1 ГБ, и
# лимит выше хостового приведёт к падению контейнера при старте.
MEM_LIMIT="${MEM_LIMIT:-900m}"
MEM_SWAP="${MEM_SWAP:-2500m}"

log() { echo "[setup] $*"; }

# ---------------------------------------------------------------- Docker
if ! command -v docker >/dev/null 2>&1; then
  log "ставлю Docker"
  sudo apt-get update -qq
  sudo apt-get install -y -qq ca-certificates curl git
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "Docker установлен. Перелогиньтесь (exit) и запустите скрипт заново."
  exit 0
fi
sudo systemctl enable --now docker

# ---------------------------------------------------------------- swap
# На бесплатных машинах памяти впритык: пик Chromium с OCR-моделью
# доходил до 416 МБ. Подкачка не даст OOM-убийцу прибить сервис
# в момент холодного старта браузера.
if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
  log "создаю swap 2 ГБ"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# ---------------------------------------------------------------- код
log "забираю код в $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo chown "$USER" "$APP_DIR"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

# ---------------------------------------------------------------- сборка
log "собираю образ (это надолго: Chromium и OCR-модель)"
cd "$APP_DIR"
docker build -t "$IMAGE" .

# ---------------------------------------------------------------- данные
log "готовлю каталог данных $DATA_DIR"
sudo mkdir -p "$DATA_DIR"
# UID/GID пользователя appuser внутри образа — 10001. Без этого
# SQLite не сможет создать файл в смонтированном каталоге.
sudo chown -R 10001:10001 "$DATA_DIR"
sudo chmod 750 "$DATA_DIR"

# ---------------------------------------------------------------- секреты
# Токен бота и прочие переменные держим в файле с правами 600,
# вне репозитория.
ENV_FILE="$APP_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  log "создаю $ENV_FILE — впишите TELEGRAM_BOT_TOKEN и PUBLIC_URL"
  sudo tee "$ENV_FILE" >/dev/null <<'EOF'
TELEGRAM_BOT_TOKEN=
PUBLIC_URL=
REFRESH_ENABLED=1
REFRESH_HOUR=3
REFRESH_MINUTE=0
MAPTOOL_TEST=0
EOF
  sudo chmod 600 "$ENV_FILE"
  sudo chown "$USER" "$ENV_FILE"
  echo ">>> ВПИШИТЕ ТОКЕН В $ENV_FILE и запустите скрипт заново <<<"
  exit 0
fi

# ---------------------------------------------------------------- запуск
log "перезапускаю контейнер"
docker rm -f zhkhan >/dev/null 2>&1 || true
docker run -d \
  --name zhkhan \
  --restart unless-stopped \
  --memory="$MEM_LIMIT" --memory-swap="$MEM_SWAP" \
  -p "$BIND:$PORT:8080" \
  -v "$DATA_DIR:/data" \
  --env-file "$ENV_FILE" \
  -e MAPTOOL_DB=/data/maptool.db \
  -e MAPTOOL_PORT=8080 \
  -e PYTHONUNBUFFERED=1 \
  "$IMAGE" >/dev/null

log "контейнер запущен, жду готовности"
for i in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/api/telegram" >/dev/null 2>&1; then
    log "готово: http://127.0.0.1:$PORT/api/telegram отвечает"
    break
  fi
  sleep 2
done

# ---------------------------------------------------------------- HTTPS
# Caddy сам берёт сертификат Let's Encrypt и перезапускается при
# обновлении. Входящий порт 80/443 наружу — единственное, что нужно
# открыть в фаерволе.
if [ "${SETUP_CADDY:-1}" = "1" ] && ! command -v caddy >/dev/null 2>&1; then
  log "ставлю Caddy для HTTPS"
  sudo apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq caddy
fi

log "готово. Что дальше:"
echo "  1. Откройте порты 80 и 443 в фаерволе/секьюрити-группе."
echo "  2. Укажите домен в /etc/caddy/Caddyfile:"
echo "       ${DOMAIN:-ваш-домен} { reverse_proxy 127.0.0.1:$PORT }"
echo "     и выполните: sudo systemctl reload caddy"
echo "  3. Проверьте: curl http://127.0.0.1:$PORT/api/telegram"
echo "  4. Логи: docker logs -f zhkhan"
