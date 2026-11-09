#!/usr/bin/env bash
# rednote-production 内网卸载（WP46 ①）
#
# ============================================================================
# 这个脚本最重要的一行是它**默认不做**的那件事
# ============================================================================
# 默认行为是 `docker compose down`（**不带 -v**）⇒ 容器与网络删掉，**数据卷留着**。
# 删卷必须显式 `--purge`，而且 --purge 还要再过一道二次确认。
#
# 理由是硬依据而不是谨慎：ADR-11 的验证表明文写着
#   「重启不丢数据…**不得 `down -v`**」
# 也就是说 G0-19（数据持久化）的验收点，靠的正是「卷不被顺手删掉」。
# 一个默认 `down -v` 的卸载脚本等于把那条验收点拆了 —— 而且是在
# 「运维想把环境清干净重装一次」这种最常见、最无防备的场景下拆的。
#
# ⇒ 本脚本**默认不可逆的动作一个都不做**。
#
# ============================================================================
# 本机验证边界
# ============================================================================
# 本机**从未执行过**（Docker daemon 不可达）。只验到 `bash -n` 通过、
# `--dry-run` 逻辑路径可走通、正文无联网动作。
# ⇒ **不能**读作「卸载已验证」。
#
# 用法：
#   ./uninstall.sh                      # down（保留卷与镜像）
#   ./uninstall.sh --purge              # down + 删卷（会二次确认）
#   ./uninstall.sh --purge --yes        # 跳过二次确认（自动化场景）
#   ./uninstall.sh --purge --also-images  # 连镜像一起删
#   ./uninstall.sh --dry-run            # 只打印

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE="${SCRIPT_DIR}/.env"
PURGE=0
ALSO_IMAGES=0
ASSUME_YES=0
DRY_RUN=0

usage() {
  cat <<'USAGE'
用法: uninstall.sh [--purge] [--also-images] [--yes] [--env-file <path>] [--dry-run]

  (默认)         docker compose down —— 删容器与网络，**保留数据卷**
  --purge        额外删除数据卷（不可逆；需二次确认或 --yes）
  --also-images  额外删除本编排引用的镜像（仅与 --purge 同用）
  --yes          跳过二次确认
  --env-file     默认 deploy/prod/.env
  --dry-run      只打印

⚠️ ADR-11 验证表：「重启不丢数据…不得 down -v」。--purge 会让 G0-19 的
   数据持久化验收点失去依据，仅在确认要重建环境时使用。
USAGE
}

log()  { printf '[uninstall] %s\n' "$*"; }
fail() { printf '[uninstall] 失败: %s\n' "$*" >&2; exit 1; }

# 与 install.sh / upgrade.sh 同口径。
gate() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[uninstall] ⚠️ dry-run：真实卸载会在此中止 —— %s\n' "$*" >&2
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
    --purge)       PURGE=1; shift ;;
    --also-images) ALSO_IMAGES=1; shift ;;
    --yes)         ASSUME_YES=1; shift ;;
    --env-file)    ENV_FILE="${2:?--env-file 需要一个路径}"; shift 2 ;;
    --dry-run)     DRY_RUN=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             usage; fail "未知参数: $1" ;;
  esac
done

if [[ "${ALSO_IMAGES}" -eq 1 && "${PURGE}" -eq 0 ]]; then
  fail "--also-images 只能与 --purge 同用（单独删镜像留着卷没有意义，而且会让下次 up 拉不到镜像）"
fi

command -v docker >/dev/null 2>&1 || gate "找不到 docker"
[[ -f "${ENV_FILE}" ]] || gate "env 文件不存在: ${ENV_FILE}（compose 需要它解析 IMAGE_* 才能知道要删什么）"

# ---------------------------------------------------------------------------
# 不可逆动作的二次确认。
#   为什么不是 `read -p "确定吗?[y/N]"` 这种一按回车就过的形式：
#   运维在机房里连敲 Enter 是常态。必须让他**打出一个具体的词**。
# ---------------------------------------------------------------------------
if [[ "${PURGE}" -eq 1 && "${ASSUME_YES}" -eq 0 && "${DRY_RUN}" -eq 0 ]]; then
  printf '即将删除以下数据卷（不可逆）：\n'
  docker compose --env-file "${ENV_FILE}" config --volumes || true
  printf '确认请完整输入 DELETE-VOLUMES ：'
  read -r answer
  [[ "${answer}" == "DELETE-VOLUMES" ]] || fail "未确认，已中止（什么都没删）"
fi

log "步骤 1/3 停止并移除容器与网络"
if [[ "${PURGE}" -eq 1 ]]; then
  # 只有这一条分支里出现 -v。整个脚本里 `--volumes` 只此一处。
  log "  含 --volumes（数据卷将被删除）"
  run docker compose --env-file "${ENV_FILE}" --profile auth down --volumes --remove-orphans
else
  log "  不含 --volumes（数据卷保留 —— ADR-11「不得 down -v」）"
  run docker compose --env-file "${ENV_FILE}" --profile auth down --remove-orphans
fi

log "步骤 2/3 镜像处置"
if [[ "${ALSO_IMAGES}" -eq 1 ]]; then
  log "  删除本编排引用的镜像"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] docker compose --env-file %s down --rmi all\n' "${ENV_FILE}"
  else
    docker compose --env-file "${ENV_FILE}" --profile auth down --rmi all --remove-orphans || \
      log "  （部分镜像被其它编排引用，未删除 —— 这是正常的，不是失败）"
  fi
else
  log "  保留镜像（下次安装无需重新载入 tar）"
fi

log "步骤 3/3 残留核对"
if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '[dry-run] docker compose ps -a / docker volume ls 核对残留\n'
  log "dry-run 结束：以上 3 步均未执行（无任何副作用）"
  exit 0
fi
log "  剩余容器："
docker compose --env-file "${ENV_FILE}" ps -a || true
log "  剩余卷（名字以 rednote-prod- 开头的）："
docker volume ls --filter 'name=rednote-prod-' || true

if [[ "${PURGE}" -eq 0 ]]; then
  log "卸载完成。**数据卷仍在**，重新 install.sh 即可恢复到卸载前的数据状态。"
else
  log "卸载完成（含删卷）。数据已不可恢复 —— 恢复只能靠 tools/ops/backup_pg.py 的备份。"
fi
