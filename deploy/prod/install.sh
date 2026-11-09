#!/usr/bin/env bash
# rednote-production 内网**离线**安装（WP46 ①）
#
# ============================================================================
# 这个脚本能读作什么、不能读作什么
# ============================================================================
# 本脚本在本机**从未完整执行过**：Docker daemon 不可达（实测
# `docker info` => `dial unix /Users/<user>/.orbstack/run/docker.sock: no such file`），
# 因此 `docker load` / `docker compose up -d` 这两步**一步都没跑过**。
# 本机只验到：`bash -n` 语法通过、`--dry-run` 逻辑路径可走通、
# 脚本正文里没有联网动作（判据 `prod:scripts-offline-shape`）。
#
# ⇒ **不能**读作「断网可一键安装」[禁用句式示例]。离线安装的验证归内网窗口（README §5 第 2 项）。
#
# ============================================================================
# 为什么是 shell 而不是 Python
# ============================================================================
# 这是**内网第一步**跑的东西：那一刻 wheelhouse 还没装、虚拟环境还不存在，
# 能依赖的只有目标机自带的 bash + docker。用 Python 写会制造一个
# 「要先有 Python 环境才能装 Python 环境」的循环。
#
# ============================================================================
# 零联网纪律（G0-20）
# ============================================================================
# 本脚本正文**不允许**出现任何外网动作：
#   - 不下载（判据扫 `curl` / `wget` / `git clone` / `pip download`）
#   - `pip install` **必须**带 `--no-index`（否则默认回落 PyPI ⇒ 断网时卡死重试）
#   - 不 `docker pull`（镜像只能来自离线包的 tar）
# ⚠️ 这只能读作「脚本里没写联网动作」，**不能**读作「运行时不联网」——
#    容器自己是否发起外网请求，要内网抓包才算验过。
#
# 用法：
#   ./install.sh --bundle /media/rednote-offline-20261018T120000
#   ./install.sh --bundle <dir> --dry-run      # 只打印顺序，不执行任何副作用
#
# 幂等：重复执行安全（docker load 覆盖同 tag、alembic upgrade head 到头即空操作、
#       compose up -d 对已健康的服务不重建）。**真机幂等属内网待跑**（README §5 第 6 项）。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

BUNDLE=""
ENV_FILE="${SCRIPT_DIR}/.env"
DRY_RUN=0
COMPOSE_PROFILES_ARG=()

usage() {
  cat <<'USAGE'
用法: install.sh --bundle <离线包目录> [--env-file <path>] [--with-auth] [--dry-run]

  --bundle <dir>     离线包目录（含 SHA256SUMS.txt / MANIFEST.json / images/ / wheelhouse/）
  --env-file <path>  compose 用的 env 文件，默认 deploy/prod/.env
  --with-auth        同时拉起 Keycloak（compose profile=auth，默认不启动）
  --dry-run          只打印将要执行的步骤，不产生任何副作用
USAGE
}

log()  { printf '[install] %s\n' "$*"; }
fail() { printf '[install] 失败: %s\n' "$*" >&2; exit 1; }

# 「真实安装必须中止，dry-run 只记一笔继续走」的那类闸口。
# 与 fail() 分开是刻意的：fail() 用于**参数错误**（dry-run 也该立刻停），
# gate() 用于**环境/介质缺口**（dry-run 的全部意义就是在缺环境的机器上走一遍）。
gate() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[install] ⚠️ dry-run：真实安装会在此中止 —— %s\n' "$*" >&2
  else
    fail "$*"
  fi
}

# 所有副作用都经这里 —— --dry-run 的「不执行」是在**一处**实现的。
# 分散在各步里写 `if dry; then echo; else run; fi` 必然漏一处，
# 而漏掉的那一处在 --dry-run 下会真的执行（最坏的那种 bug）。
run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bundle)   BUNDLE="${2:?--bundle 需要一个目录}"; shift 2 ;;
    --env-file) ENV_FILE="${2:?--env-file 需要一个路径}"; shift 2 ;;
    --with-auth) COMPOSE_PROFILES_ARG=(--profile auth); shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  usage; exit 0 ;;
    *)          usage; fail "未知参数: $1" ;;
  esac
done

[[ -n "${BUNDLE}" ]] || { usage; fail "--bundle 必填"; }

# ---------------------------------------------------------------------------
# 步骤 0：前置检查。**全部前置一次性查完再报**，不是查一条退一条 ——
#   运维在断网机房里，一次跑出全部缺口比来回五次有用得多。
# ---------------------------------------------------------------------------
log "步骤 0/6 前置检查"
problems=()
command -v docker >/dev/null 2>&1 || problems+=("找不到 docker")
docker compose version >/dev/null 2>&1 || problems+=("docker compose 插件不可用")
# daemon 可达性：`docker info` 不可达时退出码非 0（本机实测即如此）
docker info >/dev/null 2>&1 || problems+=("Docker daemon 不可达（socket 未起或无权限）")
[[ -d "${BUNDLE}" ]] || problems+=("离线包目录不存在: ${BUNDLE}")
[[ -f "${BUNDLE}/SHA256SUMS.txt" ]] || problems+=("离线包缺 SHA256SUMS.txt")
[[ -f "${BUNDLE}/MANIFEST.json" ]] || problems+=("离线包缺 MANIFEST.json")
[[ -f "${ENV_FILE}" ]] || problems+=("env 文件不存在: ${ENV_FILE}（先 cp .env.example .env 并回填 IMAGE_*）")

if [[ ${#problems[@]} -gt 0 ]]; then
  # --dry-run 下前置不通过**不中止**：dry-run 什么都不执行，它的用途正是
  # 「在还没有 daemon / 还没有介质的机器上先把逻辑路径走一遍」。
  # 若这里也 exit 1，那么在本机（daemon 不可达）就连 --dry-run 都跑不了，
  # 于是「脚本逻辑没问题」这件事会变成一句无证据的话。
  # ⚠️ 代价必须说清：dry-run 走通 **只能**读作「参数解析与步骤顺序没问题」，
  #    不能读作「这台机器装得上」—— 前置缺口原样列在下面。
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[install] ⚠️ dry-run：前置检查有 %d 项不通过，真实安装时会在此中止：\n' "${#problems[@]}" >&2
    printf '  - %s\n' "${problems[@]}" >&2
  else
    printf '[install] 前置检查未通过（%d 项）：\n' "${#problems[@]}" >&2
    printf '  - %s\n' "${problems[@]}" >&2
    exit 1
  fi
else
  log "  前置检查通过"
fi

# ---------------------------------------------------------------------------
# 步骤 1：校验介质完整性。
#   为什么必须先校验再 load：U 盘/刻盘拷贝出来的截断文件与完好文件在磁盘上
#   长得一样（backup_pg.py docstring 的同一条教训）。
#   sha256sum（GNU）与 shasum -a 256（BSD/macOS）二者取其一 —— 目标机发行版未定
#   ⇒ 不赌哪一个存在。
# ---------------------------------------------------------------------------
log "步骤 1/6 校验 SHA256SUMS.txt"
if command -v sha256sum >/dev/null 2>&1; then
  run bash -c "cd '${BUNDLE}' && sha256sum --check --quiet SHA256SUMS.txt"
elif command -v shasum >/dev/null 2>&1; then
  run bash -c "cd '${BUNDLE}' && shasum -a 256 --check --quiet SHA256SUMS.txt"
else
  gate "目标机既无 sha256sum 也无 shasum —— 无法校验介质完整性（不校验就装等于没有校验）"
fi
[[ "${DRY_RUN}" -eq 1 ]] || log "  校验通过"

# ---------------------------------------------------------------------------
# 步骤 2：docker load 镜像。
#   ⚠️ 本机从未执行（无 daemon）。
#   不用 `docker pull`：那是联网动作，断网必失败且会重试到超时。
# ---------------------------------------------------------------------------
log "步骤 2/6 载入镜像 tar"
shopt -s nullglob
image_tars=("${BUNDLE}"/images/*.tar)
shopt -u nullglob
if [[ ${#image_tars[@]} -eq 0 ]]; then
  gate "离线包 images/ 下没有 .tar —— 空目录与『镜像已就位』同形"
fi
for tar in "${image_tars[@]+"${image_tars[@]}"}"; do
  log "  load $(basename "${tar}")"
  run docker load --input "${tar}"
done

# ---------------------------------------------------------------------------
# 步骤 3：安装 Python 依赖（离线 wheelhouse）。
#   --no-index 是**硬要求**：没有它 pip 会回落到公网索引，断网时表现为长时间卡住
#   而不是明确失败（`docs/m3/工程债-依赖声明一致性说明.md` 记过同族：
#   缺 cryptography 时 alembic upgrade head 直接 ModuleNotFoundError 崩掉）。
#   requirements.txt 由 build_offline_bundle.py 从 pyproject 派生，不手抄。
# ---------------------------------------------------------------------------
log "步骤 3/6 安装 Python 依赖（离线）"
[[ -d "${BUNDLE}/wheelhouse" ]] || gate "离线包缺 wheelhouse/"
[[ -f "${BUNDLE}/requirements.txt" ]] || gate "离线包缺 requirements.txt（应由 build_offline_bundle.py 派生）"
run python3 -m pip install --no-index --find-links "${BUNDLE}/wheelhouse" -r "${BUNDLE}/requirements.txt"

# ---------------------------------------------------------------------------
# 步骤 4：起数据面，等健康。
#   先起数据面再跑迁移：alembic 连不上库会红在一个与迁移无关的错误上。
# ---------------------------------------------------------------------------
log "步骤 4/6 启动数据面并等待健康"
run docker compose --env-file "${ENV_FILE}" "${COMPOSE_PROFILES_ARG[@]+"${COMPOSE_PROFILES_ARG[@]}"}" up -d postgres rabbitmq minio

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

if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '[dry-run] 等待 postgres / rabbitmq / minio 变 healthy（最长 300s/个）\n'
else
  for svc in postgres rabbitmq minio; do
    log "  等待 ${svc}"
    wait_healthy "${svc}" || fail "${svc} 在 300s 内未变 healthy —— 看 \`docker compose logs ${svc}\`"
  done
fi

# ---------------------------------------------------------------------------
# 步骤 5：数据库迁移。
#   命令与 .github/workflows/ci.yml 的迁移步骤同口径（同一条 alembic 调用），
#   **不另写一套** —— 两套迁移入口就是两处真相。
#   `upgrade head` 幂等：已到头时是空操作。
# ---------------------------------------------------------------------------
log "步骤 5/6 数据库迁移 alembic upgrade head"
run python3 -m alembic -c "${REPO_ROOT}/migrations/alembic.ini" upgrade head

# ---------------------------------------------------------------------------
# 步骤 6：起应用面与网关。
# ---------------------------------------------------------------------------
log "步骤 6/6 启动应用面与网关"
run docker compose --env-file "${ENV_FILE}" "${COMPOSE_PROFILES_ARG[@]+"${COMPOSE_PROFILES_ARG[@]}"}" up -d

if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '[dry-run] 等待 api-1 / api-2 变 healthy\n'
  log "dry-run 结束：以上 6 步均未执行（无任何副作用）"
  exit 0
fi

for svc in api-1 api-2; do
  log "  等待 ${svc}"
  wait_healthy "${svc}" || fail "${svc} 在 300s 内未变 healthy —— 看 \`docker compose logs ${svc}\`"
done

log "安装完成。核对清单："
log "  docker compose --env-file '${ENV_FILE}' ps"
log "  （/readyz 的回执由 api 容器 healthcheck 给出，见上面的等待步骤）"
log "⚠️ 以下几项本脚本**不**验证，必须人工按 README §5 内网待跑清单核对："
log "   - 断网下是否真的零外网访问（需抓包）"
log "   - /metrics 是否仅运维面可达（OP-191，需从外网实测一次）"
log "   - RabbitMQ 是否 3 节点 quorum（本编排是单节点）"
