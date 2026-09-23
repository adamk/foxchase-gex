#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_DIR="${SCRIPT_DIR}/production-ui"
TARGET_ROOT="/opt/foxchase-gex"
BACKUP_ROOT="${TARGET_ROOT}/ui-backups"
SERVICE="foxchase-gex.service"
SITE_URL="https://gex.foxchasetrading.com"
API_ROOT="${SITE_URL}/api/gex"
SOURCE_MANIFEST="${SOURCE_DIR}/SHA256SUMS"
BASELINE_MANIFEST="${SOURCE_DIR}/production-baseline.SHA256SUMS"

SOURCE_TEMPLATE="${SOURCE_DIR}/index.html"
SOURCE_SCRIPT="${SOURCE_DIR}/app.js"
SOURCE_STYLES="${SOURCE_DIR}/style.css"
TARGET_TEMPLATE="${TARGET_ROOT}/static/templates/index.html"
TARGET_SCRIPT="${TARGET_ROOT}/static/js/app.js"
TARGET_STYLES="${TARGET_ROOT}/static/css/style.css"

DRY_RUN=0
RESTART=0
case "${1:-}" in
  "") ;;
  --dry-run) DRY_RUN=1 ;;
  --restart) RESTART=1 ;;
  *) echo "usage: $0 [--dry-run|--restart]" >&2; exit 2 ;;
esac

die() {
  echo "ERROR: $*" >&2
  exit 1
}

validate_source() {
  [[ -f "${SOURCE_TEMPLATE}" ]] || die "missing ${SOURCE_TEMPLATE}"
  [[ -f "${SOURCE_SCRIPT}" ]] || die "missing ${SOURCE_SCRIPT}"
  [[ -f "${SOURCE_STYLES}" ]] || die "missing ${SOURCE_STYLES}"
  [[ -f "${SOURCE_MANIFEST}" ]] || die "missing ${SOURCE_MANIFEST}"
  (cd "${SOURCE_DIR}" && sha256sum -c "${SOURCE_MANIFEST}") || die "source hash validation failed"
}

print_mapping() {
  echo "${SOURCE_TEMPLATE} -> ${TARGET_TEMPLATE}"
  echo "${SOURCE_SCRIPT} -> ${TARGET_SCRIPT}"
  echo "${SOURCE_STYLES} -> ${TARGET_STYLES}"
}

preserve_install() {
  local source_path="$1"
  local target_path="$2"
  local owner_uid
  local group_gid
  local mode
  owner_uid="$(stat -c '%u' "${target_path}")"
  group_gid="$(stat -c '%g' "${target_path}")"
  mode="$(stat -c '%a' "${target_path}")"
  install -o "${owner_uid}" -g "${group_gid}" -m "${mode}" "${source_path}" "${target_path}"
}

restore_backup() {
  local backup_dir="$1"
  preserve_install "${backup_dir}/index.html" "${TARGET_TEMPLATE}"
  preserve_install "${backup_dir}/app.js" "${TARGET_SCRIPT}"
  preserve_install "${backup_dir}/style.css" "${TARGET_STYLES}"
}

health_check() {
  curl --fail --silent --show-error --output /dev/null --max-time 20 "${SITE_URL}"
  curl --fail --silent --show-error --output /dev/null --max-time 20 "${API_ROOT}/NDX"
  curl --fail --silent --show-error --output /dev/null --max-time 20 "${API_ROOT}/SPX"
}

validate_source
if (( DRY_RUN )); then
  echo "DRY RUN: no production changes"
  print_mapping
  exit 0
fi

[[ "$(id -u)" -eq 0 ]] || die "run deployment as root"
[[ -d "${TARGET_ROOT}" ]] || die "missing production root ${TARGET_ROOT}"
[[ -f "${TARGET_TEMPLATE}" ]] || die "missing ${TARGET_TEMPLATE}"
[[ -f "${TARGET_SCRIPT}" ]] || die "missing ${TARGET_SCRIPT}"
[[ -f "${TARGET_STYLES}" ]] || die "missing ${TARGET_STYLES}"
[[ "$(systemctl is-active "${SERVICE}")" == "active" ]] || die "${SERVICE} is not active"

# Drift gate: only exact recorded production UI baseline may be replaced.
sha256sum -c "${BASELINE_MANIFEST}" || die "production UI baseline drifted"

install -d -o root -g root -m 0750 "${BACKUP_ROOT}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${BACKUP_ROOT}/${timestamp}"
[[ ! -e "${backup_dir}" ]] || die "backup path already exists: ${backup_dir}"
install -d -o root -g root -m 0750 "${backup_dir}"

cp -a "${TARGET_TEMPLATE}" "${backup_dir}/index.html"
cp -a "${TARGET_SCRIPT}" "${backup_dir}/app.js"
cp -a "${TARGET_STYLES}" "${backup_dir}/style.css"
sha256sum "${backup_dir}/index.html" "${backup_dir}/app.js" "${backup_dir}/style.css" > "${backup_dir}/SHA256SUMS"
chmod 0640 "${backup_dir}/SHA256SUMS"
sha256sum -c "${backup_dir}/SHA256SUMS" || die "rollback backup verification failed"

rollback_dir="${backup_dir}"
restarted=0
rollback_on_error() {
  local status="$?"
  trap - ERR
  echo "Deployment failed; restoring ${rollback_dir}" >&2
  restore_backup "${rollback_dir}" || true
  if (( restarted )); then
    systemctl restart "${SERVICE}" || true
  fi
  exit "${status}"
}
trap rollback_on_error ERR

preserve_install "${SOURCE_TEMPLATE}" "${TARGET_TEMPLATE}"
preserve_install "${SOURCE_SCRIPT}" "${TARGET_SCRIPT}"
preserve_install "${SOURCE_STYLES}" "${TARGET_STYLES}"

cmp -s "${SOURCE_TEMPLATE}" "${TARGET_TEMPLATE}"
cmp -s "${SOURCE_SCRIPT}" "${TARGET_SCRIPT}"
cmp -s "${SOURCE_STYLES}" "${TARGET_STYLES}"

if (( RESTART )); then
  restarted=1
  systemctl restart "${SERVICE}"
fi
systemctl is-active --quiet "${SERVICE}"
health_check

trap - ERR
echo "Deployment PASS"
echo "Backup: ${backup_dir}"
print_mapping
