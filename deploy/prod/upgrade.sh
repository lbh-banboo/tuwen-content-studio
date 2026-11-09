#!/usr/bin/env bash
# rednote-production 内网升级（WP46 ①）
#
# ============================================================================
# 这个脚本能读作什么、不能读作什么
# ============================================================================
# 本机**从未完整执行过**（Docker daemon 不可达）。本机只验到：`bash -n` 通过、
# `--dry-run` 逻辑路径可走通、正文无联网动作。
# ⇒ **不能**读作「升级已验证」。真机升级 + 回滚归内网窗口（README §5 第 6 项）。
#
# ============================================================================
# 为什么备份这一步调 tools/ops/backup_pg.py 而不是自己写一行 pg_dump
# ============================================================================
# 自己写 `pg_dump -Fc` 是**第二处真相**：
#   - backup_pg.py 除 dump 之外还产出 manifest（6 类结构指纹 + 行数快照 + sha256），
#     而升级前备份恰恰最需要这个 —— 升级失败要回滚时，手上得有「回滚到哪个状态」
#     的可机读描述，而不只是一个二进制文件；
#   - 它的 `_pg_bin()` 已经把「本机 Homebrew 路径 vs CI apt 路径」的回退收敛成**一份**
#     （见该文件 :70-86 的 docstring：首版写死 Homebrew 路径，本机通、CI 必红）。
#     再写一份 pg_dump 调用就是把那个坑重新挖一遍。
#
# 升级顺序的理由（顺序即安全）：
#   1 停 worker（停新任务）—— 先停消费者，否则迁移期间仍在写库
#   2 备份（含 manifest）—— 失败即中止，**没有备份不许升级**
#   3 载入新镜像 tar  —— 纯本地动作，失败不影响在跑的服务
#   4 迁移 upgrade head
#   5 换镜像重建服务
#   6 就绪核验
# 回滚点在第 2 步之后：任何一步失败，手上都有第 2 步的备份 + manifest。
#
# 用法：
#   ./upgrade.sh --bundle /media/rednote-offline-<新版本ts> --backup-dir /srv/backup
#   ./upgrade.sh --bundle <dir> --backup-dir <dir> --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

BUNDLE=""
BACKUP_DIR=""
ENV_FILE="${SCRIPT_DIR}/.env"
DRY_RUN=0

usage() {
  cat <<'USAGE'
用法: upgrade.sh --bundle <新版本离线包> --backup-dir <备份落点> [--env-file <path>] [--dry-run]

  --bundle <dir>      新版本离线包目录
  --backup-dir <dir>  升级前备份的落点（调 tools/ops/backup_pg.py）
  --env-file <path>   默认 deploy/prod/.env
  --dry-run           只打印步骤，不产生副作用
USAGE
}

log()  { printf '[upgrade] %s\n' "$*"; }
fail() { printf '[upgrade] 失败: %s\n' "$*" >&2; exit 1; }

# 与 install.sh 同口径：fail() 管参数错误（dry-run 也停），
# gate() 管环境/介质缺口（dry-run 记一笔继续走，否则在无 daemon 的机器上
# 连逻辑路径都走不了，「脚本没问题」就成了无证据的话）。
gate() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[upgrade] ⚠️ dry-run：真实升级会在此中止 —— %s\n' "$*" >&2
  else
    fail "$*"
  fi
}

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)     BUNDLE="${2:?--bundle 需要一个目录}"; shift 2 ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir 需要一个目录}"; shift 2 ;;
    --env-file)   ENV_FILE="${2:?--env-file 需要一个路径}"; shift 2 ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *)            usage; fail "未知参数: $1" ;;
  esac
done

[[ -n "${BUNDLE}" ]] || { usage; fail "--bundle 必填"; }
[[ -n "${BACKUP_DIR}" ]] || { usage; fail "--backup-dir 必填（没有备份不许升级）"; }

log "步骤 0/6 前置检查"
problems=()
command -v docker >/dev/null 2>&1 || problems+=("找不到 docker")
docker info >/dev/null 2>&1 || problems+=("Docker daemon 不可达")
[[ -d "${BUNDLE}" ]] || problems+=("离线包目录不存在: ${BUNDLE}")
[[ -f "${BUNDLE}/SHA256SUMS.txt" ]] || problems+=("离线包缺 SHA256SUMS.txt")
[[ -f "${ENV_FILE}" ]] || problems+=("env 文件不存在: ${ENV_FILE}")
if [[ ${#problems[@]} -gt 0 ]]; then
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[upgrade] ⚠️ dry-run：前置检查有 %d 项不通过，真实升级时会在此中止：\n' "${#problems[@]}" >&2
    printf '  - %s\n' "${problems[@]}" >&2
  else
    printf '[upgrade] 前置检查未通过（%d 项）：\n' "${#problems[@]}" >&2
    printf '  - %s\n' "${problems[@]}" >&2
    exit 1
  fi
fi

log "步骤 1/6 停 worker（停新任务）"
# 只停 worker，不停 API：API 继续对外提供读能力，长任务不再被领取。
run docker compose --env-file "${ENV_FILE}" stop worker

log "步骤 2/6 升级前备份（tools/ops/backup_pg.py，含 manifest）"
run mkdir -p "${BACKUP_DIR}"
# DSN 从 .env 的 PG_* 拼出，与 compose 的 x-app-env 同口径。
# 这里**必须**带 --dsn：backup_pg.py 省略 --dsn 时读 REDNOTE_DATABASE_URL，
# 而内网那个变量未必指向本编排的库（赌它就是「本机口径被当成通用口径」的那类坑）。
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  set -a; . "${ENV_FILE}"; set +a
  PG_DSN="postgresql://${PG_USER:?.env 缺 PG_USER}:${PG_PASSWORD:?.env 缺 PG_PASSWORD}@127.0.0.1:5432/${PG_DATABASE:?.env 缺 PG_DATABASE}"
else
  gate "env 文件不存在，无法拼出备份 DSN: ${ENV_FILE}"
  PG_DSN="postgresql://<PG_USER>:<PG_PASSWORD>@127.0.0.1:5432/<PG_DATABASE>"
fi
run python3 "${REPO_ROOT}/tools/ops/backup_pg.py" --dsn "${PG_DSN}" --out "${BACKUP_DIR}"
if [[ "${DRY_RUN}" -eq 0 ]]; then
  log "  备份完成，manifest 落在 ${BACKUP_DIR}"
fi

log "步骤 3/6 校验并载入新镜像 tar"
if command -v sha256sum >/dev/null 2>&1; then
  run bash -c "cd '${BUNDLE}' && sha256sum --check --quiet SHA256SUMS.txt"
elif command -v shasum >/dev/null 2>&1; then
  run bash -c "cd '${BUNDLE}' && shasum -a 256 --check --quiet SHA256SUMS.txt"
else
  gate "目标机既无 sha256sum 也无 shasum —— 不在未校验的介质上升级"
fi
shopt -s nullglob
image_tars=("${BUNDLE}"/images/*.tar)
shopt -u nullglob
[[ ${#image_tars[@]} -gt 0 ]] || gate "离线包 images/ 下没有 .tar"
for tar in "${image_tars[@]+"${image_tars[@]}"}"; do
  log "  load $(basename "${tar}")"
  run docker load --input "${tar}"
done

log "步骤 4/6 迁移 alembic upgrade head"
# 迁移失败即中止，**不往下换镜像** —— 旧镜像 + 已迁移一半的库是最糟的组合。
# 回滚方式：`python -m alembic -c migrations/alembic.ini downgrade <上一 revision>`，
# 演练见 tools/quality/check_rollback_drill.py（WP47 ⑤）。
run python3 -m alembic -c "${REPO_ROOT}/migrations/alembic.ini" upgrade head

log "步骤 5/6 换镜像、重建服务"
run docker compose --env-file "${ENV_FILE}" up -d

log "步骤 6/6 就绪核验"
if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '[dry-run] 等待 api-1 / api-2 变 healthy，然后 start worker\n'
  log "dry-run 结束：以上 6 步均未执行（无任何副作用）"
  exit 0
fi

wait_healthy() {
  local service="$1" deadline=$((SECONDS + 300)) cid state
  while (( SECONDS < deadline )); do
    cid="$(docker compose --env-file "${ENV_FILE}" ps -q "${service}" 2>/dev/null || true)"
    if [[ -n "${cid}" ]]; then
      state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "${cid}" 2>/dev/null || echo unknown)"
      [[ "${state}" == "healthy" || "${state}" == "no-healthcheck" ]] && return 0
    fi
    sleep 5
  done
  return 1
}

for svc in api-1 api-2; do
  log "  等待 ${svc}"
  wait_healthy "${svc}" || fail "${svc} 未就绪 —— 回滚：按 README §4 用步骤 2 的备份恢复"
done

log "恢复 worker 消费"
docker compose --env-file "${ENV_FILE}" start worker

log "升级完成。备份保留在 ${BACKUP_DIR}（**不要立刻删**，回滚窗口未过）"
