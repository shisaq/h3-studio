#!/bin/bash
# Keep h3d running in the background (starts at login, restarts if it crashes).
#   scripts/install-launchagent.sh            install and start
#   scripts/install-launchagent.sh uninstall  stop and remove
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="local.h3d"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOGDIR="${HOME}/Library/Logs/h3d"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
  rm -f "${PLIST}"
  echo "Removed ${LABEL}"
  exit 0
fi

PYTHON="${PYTHON:-$(command -v python3)}"
mkdir -p "${LOGDIR}" "$(dirname "${PLIST}")"
sed -e "s|__LABEL__|${LABEL}|g" -e "s|__PYTHON__|${PYTHON}|g" -e "s|__ROOT__|${ROOT}|g" -e "s|__LOGDIR__|${LOGDIR}|g" \
  "${ROOT}/launchd/h3d.plist.template" > "${PLIST}"
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${DOMAIN}" "${PLIST}"
echo "Installed ${LABEL} (python: ${PYTHON})"
echo "Log: ${LOGDIR}/h3d.log"
