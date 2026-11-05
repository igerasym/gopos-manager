#!/bin/bash
# The Frame Manager (cafe-manager) auto-deploy: poll origin/main, build in a separate checkout,
# swap the image, health-check, auto-rollback, Telegram alert to the owner.
# Installed on the EC2 host at ~/bin/cafe-auto-deploy.sh, run every 5 min by systemd user timer
# cafe-autodeploy.timer. Touches ONLY the `cafe` service — never CatchMyAction or its postgres.
set -uo pipefail

APP=$HOME/cafe-manager             # live checkout (+ untracked .env and data/, both mounted into the container)
BUILD=$HOME/cafe-build             # separate checkout used only for building
IMAGE=cafe-manager-cafe            # image compose uses for the `cafe` service
CONTAINER=cafe-manager-cafe-1
LOG=$HOME/cafe-deploy.log
FAILED=$HOME/.cafe-deploy-failed-sha

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

# Telegram to the FIRST id in TELEGRAM_CHAT_ID only (the owner), via the app's own sender
notify() {
  docker exec "$CONTAINER" python -c \
    "import sys; from app.telegram_bot import send_message, CHAT_IDS; CHAT_IDS and send_message(sys.argv[1], CHAT_IDS[0])" \
    "$1" >/dev/null 2>&1 || log "telegram notify failed"
}

# Same lock as the CatchMyAction deploy: the 2 GB host can't run two docker builds at once
exec 9>"$HOME/.deploy.lock"
flock -n 9 || exit 0

cd "$APP" || exit 1
git fetch -q origin main || { log "fetch failed"; exit 1; }
LIVE=$(git rev-parse HEAD)
TARGET=$(git rev-parse origin/main)
[ "$LIVE" = "$TARGET" ] && exit 0
[ -f "$FAILED" ] && [ "$(cat "$FAILED")" = "$TARGET" ] && exit 0   # already failed this commit; wait for a new one

SHORT_LIVE=${LIVE:0:7}; SHORT_T=${TARGET:0:7}
SUBJECT=$(git log -1 --format=%s "$TARGET" | head -c 120 | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
log "deploy start $SHORT_LIVE -> $SHORT_T"

# 1. Build the candidate image from a separate checkout (live tree and container untouched on failure)
[ -d "$BUILD/.git" ] || git clone -q https://github.com/igerasym/gopos-manager.git "$BUILD"
git -C "$BUILD" fetch -q origin main && git -C "$BUILD" checkout -q --detach "$TARGET" || { log "checkout failed"; exit 1; }
if ! docker build -q -t "$IMAGE:candidate" "$BUILD" >> "$HOME/cafe-deploy-build.log" 2>&1; then
  log "BUILD FAILED for $SHORT_T — prod stays on $SHORT_LIVE"
  echo "$TARGET" > "$FAILED"
  notify "⚠️ <b>Cafe deploy failed</b> (build) — $SHORT_T: $SUBJECT. Prod stays on $SHORT_LIVE."
  exit 1
fi

# 2. Swap: keep a rollback tag, point latest at the candidate, restart cafe only
docker tag "$IMAGE:latest" "$IMAGE:rollback-$SHORT_LIVE"
docker tag "$IMAGE:candidate" "$IMAGE:latest"
git merge --ff-only -q "$TARGET" || log "ff-merge failed (local changes on host?)"
docker compose up -d --no-deps --no-build cafe >> "$HOME/cafe-deploy-build.log" 2>&1

# 3. Health check (login redirect 302 or 200 = app is up), auto-rollback on failure
ok=0
for i in $(seq 1 30); do
  h=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ || true)
  case "$h" in 200|302|303|307) ok=1; break ;; esac
  sleep 3
done
if [ "$ok" != 1 ]; then
  log "HEALTH FAILED for $SHORT_T (http=$h) — rolling back to $SHORT_LIVE"
  docker tag "$IMAGE:rollback-$SHORT_LIVE" "$IMAGE:latest"
  git checkout -q -B main "$LIVE"
  docker compose up -d --no-deps --no-build cafe >> "$HOME/cafe-deploy-build.log" 2>&1
  echo "$TARGET" > "$FAILED"
  notify "⚠️ <b>Cafe deploy failed</b> (health, http=$h) — $SHORT_T: $SUBJECT. Rolled back to $SHORT_LIVE."
  exit 1
fi
log "deploy OK $SHORT_T"
rm -f "$FAILED"
notify "🚀 <b>Cafe deployed</b> $SHORT_T: $SUBJECT"

# 4. Housekeeping: keep the 2 newest rollback tags (disk is tight), drop dangling layers
docker images "$IMAGE" --format '{{.Tag}} {{.CreatedAt}}' | grep '^rollback-' | sort -k2,3 -r | tail -n +3 | awk '{print $1}' \
  | while read -r t; do docker rmi "$IMAGE:$t" >/dev/null 2>&1; done
docker image prune -f >/dev/null 2>&1
