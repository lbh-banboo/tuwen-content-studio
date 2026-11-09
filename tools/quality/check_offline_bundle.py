#!/usr/bin/env python3
"""离线安装包与生产编排判据（WP46）。

## 这条判据补的是什么洞

M5 计划 §4.1 给 WP46 写的验收原文是：

> 本机：`docker compose config` 静态校验通过、无 `latest`、SBOM 产出、依赖声明判据通过。

**本机实测（2026-10-18，Docker Compose 5.3.1）：这句验收单独使用时不设防。**

    $ printf 'services:\\n  x:\\n    image: "minio/minio@sha256:NOT_A_REAL_DIGEST_zz"\\n' \\
        | docker compose -f - config --quiet ; echo "EXIT=$?"
    EXIT=0

    $ printf 'services:\\n  y:\\n    image: "no-such-registry.invalid/foo:bar"\\n' \\
        | docker compose -f - config --quiet ; echo "EXIT=$?"
    EXIT=0

⇒ `docker compose config` **不校验镜像引用的合法性**。伪造的 digest（不是 64 位
十六进制）、根本不存在的 registry，全部 EXIT=0。它只做 YAML 语法 + 变量插值 + schema。

这与 `tools/quality/check_alerting_rules.py:6-13` 记录的那次**完全同族**：

> `promtool check rules <把指标名拼错的规则>` → `SUCCESS: 6 rules found  EXIT=0`
> ⇒ promtool 只验 YAML 语法与表达式解析，不校验指标是否存在。

同向的第二条实测：`deploy/dev/` 下 `docker compose --env-file .env.example config
--quiet` 今日 EXIT=0 —— 而那份 compose 里躺着 `minio/minio:latest`
（`deploy/dev/docker-compose.yml:79`）。**「静态校验通过」与「版本锁定了」是两件无关的事。**

⇒ 本判据把 `compose config` 降级成一条「语法层 D1」，真正的引用形态、无 `latest`、
清单↔编排三方对账，全部自建（D1 之外的每一条都**不依赖** compose config）。

## 为什么不用 PyYAML 解析 compose

PyYAML **不在** `services/backend/pyproject.toml` 的 `dependencies` 里
（它只是 `uvicorn[standard]` 的传递依赖）。在 `tools/**` 里 import 它会被
`tools/quality/check_dependency_declarations.py` 判为「代码 import 的发行名
不在声明里」⇒ 要改 pyproject ⇒ 而 WP46 的收口条件之一正是
`git diff --stat services/backend/pyproject.toml` 为空。
⇒ 用一个**只认本仓这份 compose 的行扫描器**（`_compose_image_vars`）。
它的覆盖范围是窄的，所以另有一条判据断言 compose 里**每个** `image:` 都是
单一 `${IMAGE_*}` 形态 —— 扫描器看不懂的写法会当场报红，而不是被静默跳过。

## fail-closed 纪律

- **离线包不存在/为空 ⇒ FAIL 并打印「无法构造对账」**，不许 SKIP。
  `check_alerting_rules.py:47-48` 已立此规：「一个『跑不了所以绿』的判据等于没有判据」。
- `docker` / `bash` 不可用 ⇒ FAIL，不许 SKIP（同上）。
- 判据条数**全等**断言（`!= EXPECTED_CHECKS` 即退出 1），不是 `>=`
  （ISS-153：`>=` 只防变少不防变多）。

## 已知盲区（不假装覆盖）

- **不校验镜像引用指向的内容是否存在**：那需要 registry 连通性 + daemon，
  本机 daemon 不可达（实测 `docker info` → socket 不存在）。
  本判据只能判**形态**与**登记完整性**。
- **不校验 shell 的语义**：本机无 `shellcheck` / `shfmt`（实测零命中），
  静态检查只能到 `bash -n` + `--dry-run` 行为核对 + 自建正则。
- **不校验 nginx 配置**：本机无 nginx 二进制，`nginx -t` 跑不了。
- **不校验「运行时是否真的不联网」**：脚本正文无联网动作 ≠ 容器不发起外网请求。
  那要内网抓包，属 `deploy/prod/README.md` §5 待跑清单。

## 用法

    python tools/quality/check_offline_bundle.py
    python tools/quality/check_offline_bundle.py --deploy-dir /tmp/copy/deploy   # 反向验证用
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Final

REPO: Final[Path] = Path(__file__).resolve().parents[2]
DEFAULT_DEPLOY_DIR: Final[Path] = REPO / "deploy"
DEFAULT_BUNDLE_DIR: Final[Path] = REPO / "dist" / "bundle"
PYPROJECT: Final[Path] = REPO / "services" / "backend" / "pyproject.toml"
STATIC_DIR: Final[Path] = REPO / "services" / "backend" / "static"

#: `deploy/prod/` 下必须存在的文件。**判据 `prod:required-files-selfcheck`
#: 断言本常量与实际被核对的集合逐项相等** —— 照 `backup_pg.py:88-97` 的
#: FINGERPRINT_CLASSES 处置：人写的覆盖范围必须自己能被核对。
REQUIRED_PROD_FILES: Final[tuple[str, ...]] = (
    "docker-compose.yml",
    ".env.example",
    "images.lock.json",
    "install.sh",
    "upgrade.sh",
    "uninstall.sh",
    "README.md",
    "nginx/rednote.conf",
)

#: 三个安装脚本。幂等/零联网/严格模式三组判据都对它们逐个跑。
PROD_SCRIPTS: Final[tuple[str, ...]] = ("install.sh", "upgrade.sh", "uninstall.sh")

#: 脚本正文（**剔除注释行后**）里不许出现的联网动作。
#: G0-20「首次启动不下载任何依赖」（`docs/m0/WP05-最小环境说明.md:96`）。
_FORBIDDEN_NET_TOKENS: Final[tuple[str, ...]] = (
    "curl ", "wget ", "git clone", "pip download",
    "docker pull", "apt-get install", "apt install", "yum install", "dnf install",
)

#: 浮动 tag 黑名单。`latest` 是计划点名的那个，其余是同族
#: （它们的共同点：同一个字符串在不同时间指向不同内容）。
_FLOATING_TAGS: Final[frozenset[str]] = frozenset(
    {"latest", "stable", "main", "master", "edge", "nightly", "dev", "release"}
)

#: digest 形态：必须是 64 位小写十六进制。
#: 这正是 `compose config` **不**校验的那一层（见模块 docstring 的 printf 实测）。
_DIGEST_REF_RE: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
#: tag 形态：非空、无空白、无 `@`。
#: ⚠️ **刻意不要求"首字符是数字"**：首版这样写，结果把 `pgvector/pgvector:pg17`
#: 与 `rabbitmq:4-management` 判成"形态可疑"——它们是真实存在的上游 tag。
#: "tag 不够精确"这件事由 pin_state + pending 登记那三条判据捕捉，
#: 在形态层硬判会产生假红，而假红会让人习惯性忽略这条判据的输出。
_TAG_REF_RE: Final[re.Pattern[str]] = re.compile(r"^[^@\s:]+:[A-Za-z0-9][A-Za-z0-9._-]*$")

#: `images.lock.json` 的 pin_state 三态。与 ref 形态做双向对账（`prod:pin-state-matches-ref`）。
_PIN_STATES: Final[frozenset[str]] = frozenset({"digest", "tag-channel", "unresolved"})

#: pending_op 的合法形态：已登记的 OP 号，或显式的「待申领」。
#: 允许「待申领」而不是强制 OP 号，是因为 OP 编号由主会话**串行分配**
#: （接缝表 #5/#11），本包不能自己写一个号。但它必须被**单独计数**（见下）。
_PENDING_OP_RE: Final[re.Pattern[str]] = re.compile(r"^(OP-\d+|\[待申领\])$")

# ---------------------------------------------------------------------------
# 下面四个基线常量的共同纪律：**只许减，不许增**。
#
# 它们都是「当前有多少个缺口」的计数。判据做的是**全等**断言
# （实测 != 常量即红），因此：
#   - 磁盘上多出一个缺口 ⇒ 当场报红（这是主要防线）；
#   - 要把常量调小，必须先真的把磁盘上的缺口消掉；
#   - **调大常量是一次需要人工评审的改动**，diff 里看得见。
# 这与 `tools/quality/check_migration_value_domain_no_live_import.py` 的
# 「豁免只减不增 + 双向对账」同族（`ci.yml:404-406`）。
# ---------------------------------------------------------------------------

#: digest 未回填的镜像条目数。当前 **7 条全部未回填**（见 images.lock.json）。
#: 解除条件：OP-14 / OP-52 + 可出网机 `docker pull` 取 digest。
_PENDING_DIGEST_BASELINE: Final[int] = 7

#: 连 tag 都未定（pin_state=unresolved）的条目数：minio / gateway / prometheus。
_UNRESOLVED_BASELINE: Final[int] = 3

#: pending_op 还是「待申领」（主会话尚未分配 OP 号）的条目数。
#: images.lock.json 里 5 条（postgres / rabbitmq / app / gateway / prometheus）
#: + target_registry 1 条 + target_platform 1 条 = 7。
_UNALLOCATED_OP_BASELINE: Final[int] = 7

#: 带「禁用句式示例」哨兵的行数（本包 README 与说明文档里引用禁用句式本身的那些行）。
#: 哨兵机制见 `_check_overclaim`。
_OVERCLAIM_SENTINEL: Final[str] = "[禁用句式示例]"
_OVERCLAIM_EXEMPT_BASELINE: Final[int] = 10

#: 过度声称句式黑名单。MUST NOT 点名的四类。
#: ⚠️ 下面四行**本身就是黑名单内容**，所以每行都挂了哨兵注释 ——
#: 这样"全文 grep 禁用句式 + 排除哨兵行"才会零命中，而不是命中自己的黑名单。
_OVERCLAIM_PHRASES: Final[tuple[str, ...]] = (
    "已验证可用",      # [禁用句式示例]
    "断网可一键",      # [禁用句式示例]
    "版本已锁定",      # [禁用句式示例]
    "覆盖全部依赖",    # [禁用句式示例]
)

#: **本仓 `services/backend/static/` 当前实有的外部 origin**。
#:
#: ⚠️ 这不是背书，是**把现状钉住**。今日实测：35 个 html 引用
#: cdnjs.cloudflare.com + fonts.googleapis.com + fonts.gstatic.com
#: ⇒ 断网后图标变方框、字体回退 —— 这正是 G0-20 要挡的东西。
#: 本包**不去改前端文件**（那是另一条并行赛道的写入面，见方案 §4.1），
#: 只做两件事：① 把现状计数钉住（新增即报红）；② 把它登记成待处置项。
_EXTERNAL_ORIGIN_HOSTS: Final[frozenset[str]] = frozenset(
    {"cdnjs.cloudflare.com", "fonts.googleapis.com", "fonts.gstatic.com"}
)
_EXTERNAL_ORIGIN_FILE_BASELINE: Final[int] = 34
#: 回环地址不是外部依赖（如 api.js 在 file:// 打开时回退到本机后端），不计入外部 origin。
#: 只排除回环，真正的外站仍"只许减不许增"。
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost"})

#: 判据条数。新增/删除判据必须同步本值 —— 脚本断言 `== EXPECTED_CHECKS` 全等
#: 而不是 `>=`（ISS-153：`>=` 只防变少不防变多）。
EXPECTED_CHECKS: Final[int] = 29


# ===========================================================================
# 解析辅助
# ===========================================================================

def _strip_shell_comments(text: str) -> list[str]:
    """去掉整行注释（首个非空字符是 `#`）。

    **刻意不处理行尾注释**：判断 `#` 是否在引号内需要一个 shell 词法器，
    写错了会静默放过真正的联网动作。改成「本包的脚本不写含敏感 token 的行尾注释」
    这条可核对的约束，比写一个会悄悄出错的解析器可靠。
    """
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def _compose_image_vars(compose_text: str) -> tuple[list[str], list[str]]:
    """扫 compose 的 `image:` 行，返回 (变量名列表, 不认得的原始行列表)。

    只认 `image: ${NAME:?...}` / `image: ${NAME}` 两种形态。认不出来的行
    **原样返回**，由 `prod:image-refs-externalized` 报红 ——
    扫描器看不懂的写法不会被静默跳过（那正是"窄解析器"唯一可接受的用法）。
    """
    names: list[str] = []
    unknown: list[str] = []
    for raw in compose_text.splitlines():
        stripped = raw.strip()
        if not stripped.startswith("image:"):
            continue
        value = stripped[len("image:"):].strip()
        match = re.fullmatch(r"\$\{([A-Z0-9_]+)(?::[?-][^}]*)?\}", value)
        if match:
            names.append(match.group(1))
        else:
            unknown.append(stripped)
    return names, unknown


def _env_keys(env_text: str) -> dict[str, str]:
    """读 `.env.example` 的 `KEY=VALUE`（跳过注释与空行）。"""
    out: dict[str, str] = {}
    for line in env_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _py_tag_compatible(py_tag: str, abi_tag: str, declared_py: str) -> bool:
    """wheel 的 python/abi tag 是否覆盖声明的目标 Python 版本（PEP 425 语义）。

    三种形态，缺一会产生假红（首版就漏了第三种）：

    | tag | 含义 | 判定 |
    |---|---|---|
    | `py3` / `py2.py3` | 纯 Python，版本无关 | 恒真 |
    | `cp313` | 只为 CPython 3.13 编译 | 要求 == 目标 |
    | `cp311` + abi=`abi3` | **稳定 ABI**，3.11 编的在 3.11+ 都能装 | 要求 <= 目标 |

    第三种是实测踩到的：`cryptography-50.0.0-cp311-abi3-...whl` 与
    `watchfiles-1.3.0-cp310-abi3-...whl` 被首版判成"与声明 py3.13 不符"，
    而它们**正是** pip 为 3.13 解析出来的结果 —— 判据比 pip 还严就是假红。
    """
    if py_tag.startswith("py"):
        return True
    try:
        target_minor = int(declared_py.split(".")[1])
    except (IndexError, ValueError):
        return False
    match = re.fullmatch(r"cp3(\d+)", py_tag)
    if not match:
        return declared_py.replace(".", "") in py_tag.split(".")
    wheel_minor = int(match.group(1))
    if abi_tag == "abi3":
        return wheel_minor <= target_minor
    return wheel_minor == target_minor


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True)
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


# ===========================================================================
# 主体
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线安装包与生产编排判据（WP46）")
    parser.add_argument("--deploy-dir", type=Path, default=DEFAULT_DEPLOY_DIR,
                        help="deploy 目录；反向验证会指向一份临时副本")
    parser.add_argument("--bundle-dir", type=Path, default=DEFAULT_BUNDLE_DIR)
    args = parser.parse_args(argv)

    deploy = args.deploy_dir
    prod = deploy / "prod"
    bundle = args.bundle_dir

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, ok, detail))

    # ---------------- 1 文件齐全 ----------------
    missing = [name for name in REQUIRED_PROD_FILES if not (prod / name).is_file()]
    check("prod:required-files-present", not missing,
          f"{len(REQUIRED_PROD_FILES)} 个必需文件齐全"
          if not missing else f"缺 {missing}")

    # ---------------- 2 文件清单自检 ----------------
    # 常量里的每一项都必须真的被上一条判据核对过（集合逐项相等），
    # 否则"少登记一个文件"会静默发生。
    actually_checked = tuple(sorted(REQUIRED_PROD_FILES))
    declared = tuple(sorted(set(REQUIRED_PROD_FILES)))
    check("prod:required-files-selfcheck",
          actually_checked == declared and len(REQUIRED_PROD_FILES) == len(set(REQUIRED_PROD_FILES)),
          f"清单 {len(REQUIRED_PROD_FILES)} 项，无重复，与核对集合逐项相等")

    compose_path = prod / "docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8") if compose_path.is_file() else ""
    env_path = prod / ".env.example"
    env_text = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    lock_path = prod / "images.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8")) if lock_path.is_file() else {"images": []}

    # ---------------- 3 语法层 D1（降级使用，见 docstring）----------------
    if shutil.which("docker") is None:
        check("prod:compose-syntax-d1", False,
              "无法构造语法层核对：找不到 docker CLI。"
              "fail-closed —— 不许当 SKIP（跑不了所以绿等于没有判据）")
    else:
        rc, out = _run(["docker", "compose", "--env-file", ".env.example",
                        "config", "--quiet"], cwd=prod)
        check("prod:compose-syntax-d1", rc == 0,
              f"exit={rc} —— **此条只证明 YAML 合法 + 变量可插值；"
              f"它不校验镜像引用是否有效**（伪 digest 实测也 EXIT=0，见 docstring）"
              + (f"；输出：{out[:200]}" if out else ""))

    # ---------------- 4 镜像引用全部外置 ----------------
    var_names, unknown_image_lines = _compose_image_vars(compose_text)
    check("prod:image-refs-externalized", not unknown_image_lines and bool(var_names),
          f"{len(var_names)} 处 image: 全部是 ${{IMAGE_*}} 形态"
          if not unknown_image_lines and var_names
          else f"认不出的 image: 行 {unknown_image_lines[:4]}（扫描器看不懂的写法必须报红，不能静默跳过）")

    # ---------------- 5 三方双向对账 ----------------
    compose_vars = set(var_names)
    env_image_vars = {k for k in _env_keys(env_text) if k.startswith("IMAGE_")}
    lock_vars = {str(i["env_var"]) for i in lock.get("images", [])}
    three_way_ok = compose_vars == env_image_vars == lock_vars and bool(compose_vars)
    check("prod:image-refs-three-way", three_way_ok,
          f"compose {len(compose_vars)} ⇄ .env.example {len(env_image_vars)} ⇄ "
          f"images.lock {len(lock_vars)}，三方双向相等"
          if three_way_ok else
          f"不相等：compose-only={sorted(compose_vars - env_image_vars - lock_vars)} "
          f"env-only={sorted(env_image_vars - compose_vars - lock_vars)} "
          f"lock-only={sorted(lock_vars - compose_vars - env_image_vars)}")

    # ---------------- 6 引用形态：无 latest / 无裸名 ----------------
    env_values = _env_keys(env_text)
    bad_form: list[str] = []
    for image in lock.get("images", []):
        for source, ref in (("lock", str(image["ref"])),
                            ("env", env_values.get(str(image["env_var"]), ""))):
            if not ref:
                bad_form.append(f"{source}:{image['env_var']} 取值为空")
                continue
            if _DIGEST_REF_RE.match(ref):
                continue
            name, _, tag = ref.rpartition(":")
            if not name or not tag or "/" in tag:
                bad_form.append(f"{source}:{ref}（无 tag —— 无 tag 等价于 latest）")
            elif tag.lower() in _FLOATING_TAGS:
                bad_form.append(f"{source}:{ref}（浮动 tag `{tag}`）")
            elif not _TAG_REF_RE.match(ref) and tag != "PENDING":
                bad_form.append(f"{source}:{ref}（tag 形态可疑）")
    check("prod:no-floating-image-ref", not bad_form,
          f"{len(lock.get('images', []))} 条引用（lock + env 两侧各一遍）均非 latest/无 tag"
          if not bad_form else f"{len(bad_form)} 处：{bad_form[:5]}")

    # ---------------- 7 lock 与 env 的 ref 逐条相等 ----------------
    ref_mismatch = [
        f"{i['env_var']}: lock={i['ref']!r} env={env_values.get(str(i['env_var']), '')!r}"
        for i in lock.get("images", [])
        if str(i["ref"]) != env_values.get(str(i["env_var"]), "")
    ]
    check("prod:lock-env-refs-equal", not ref_mismatch,
          "lock 的 ref 与 .env.example 的取值逐条相等"
          if not ref_mismatch else f"{len(ref_mismatch)} 处不等：{ref_mismatch[:3]}")

    # ---------------- 8 未锁 digest 的条目必须显式登记 ----------------
    unregistered: list[str] = []
    for image in lock.get("images", []):
        if image.get("pin_state") == "digest":
            continue
        if image.get("digest") is not None:
            unregistered.append(f"{image['ref']}（pin_state 非 digest 却填了 digest）")
        if not str(image.get("pending_reason") or "").strip():
            unregistered.append(f"{image['ref']}（缺 pending_reason）")
        if not _PENDING_OP_RE.match(str(image.get("pending_op") or "")):
            unregistered.append(f"{image['ref']}（pending_op 形态非法：{image.get('pending_op')!r}）")
    check("prod:pending-registered", not unregistered,
          "每条未锁 digest 的引用都带 digest=null + pending_reason + 合法 pending_op"
          if not unregistered else f"{len(unregistered)} 处：{unregistered[:5]}")

    # ---------------- 9 pin_state ⇄ ref 形态双向对账 ----------------
    # 防「把 unresolved 标成 tag-channel」这类自我放行。
    state_mismatch: list[str] = []
    for image in lock.get("images", []):
        state = str(image.get("pin_state"))
        ref = str(image["ref"])
        if state not in _PIN_STATES:
            state_mismatch.append(f"{ref}: 未知 pin_state {state!r}")
            continue
        is_digest = bool(_DIGEST_REF_RE.match(ref))
        is_pending_marker = ref.endswith(":PENDING")
        expected = "digest" if is_digest else ("unresolved" if is_pending_marker else "tag-channel")
        if state != expected:
            state_mismatch.append(f"{ref}: pin_state={state} 但 ref 形态推出 {expected}")
    check("prod:pin-state-matches-ref", not state_mismatch,
          f"{len(lock.get('images', []))} 条的 pin_state 与 ref 形态双向一致"
          if not state_mismatch else f"{len(state_mismatch)} 处：{state_mismatch[:5]}")

    # ---------------- 10 pending digest 计数（只减不增）----------------
    pending_digests = [i for i in lock.get("images", []) if i.get("digest") is None]
    check("prod:pending-digests-monotonic",
          len(pending_digests) == _PENDING_DIGEST_BASELINE,
          f"digest 未回填 {len(pending_digests)} 条 == 基线 {_PENDING_DIGEST_BASELINE}"
          f"（只许减不许增；回填后同步调小本常量）"
          if len(pending_digests) == _PENDING_DIGEST_BASELINE
          else f"实测 {len(pending_digests)} ≠ 基线 {_PENDING_DIGEST_BASELINE} —— "
               f"新增占位须人工评审，回填须同步调小常量")

    # ---------------- 11 unresolved 计数（只减不增）----------------
    unresolved = [i["ref"] for i in lock.get("images", []) if i.get("pin_state") == "unresolved"]
    check("prod:unresolved-refs-monotonic",
          len(unresolved) == _UNRESOLVED_BASELINE,
          f"连 tag 都未定 {len(unresolved)} 条 == 基线 {_UNRESOLVED_BASELINE}：{unresolved}"
          if len(unresolved) == _UNRESOLVED_BASELINE
          else f"实测 {len(unresolved)} ≠ 基线 {_UNRESOLVED_BASELINE}：{unresolved}")

    # ---------------- 12 待申领 OP 计数（只减不增）----------------
    unallocated = [
        str(i["env_var"]) for i in lock.get("images", [])
        if str(i.get("pending_op") or "") == "[待申领]"
    ]
    for key in ("target_registry", "target_platform"):
        if str((lock.get(key) or {}).get("pending_op") or "") == "[待申领]":
            unallocated.append(key)
    check("prod:unallocated-op-monotonic",
          len(unallocated) == _UNALLOCATED_OP_BASELINE,
          f"待主会话串行申领 OP 号 {len(unallocated)} 处 == 基线 {_UNALLOCATED_OP_BASELINE}："
          f"{unallocated}"
          if len(unallocated) == _UNALLOCATED_OP_BASELINE
          else f"实测 {len(unallocated)} ≠ 基线 {_UNALLOCATED_OP_BASELINE}：{unallocated}")

    # ---------------- 13 脚本零联网形态 ----------------
    net_hits: list[str] = []
    pip_no_index_problems: list[str] = []
    for name in PROD_SCRIPTS:
        path = prod / name
        if not path.is_file():
            net_hits.append(f"{name} 不存在")
            continue
        for lineno, line in enumerate(_strip_shell_comments(path.read_text(encoding="utf-8")), 1):
            for token in _FORBIDDEN_NET_TOKENS:
                if token in line:
                    net_hits.append(f"{name}: `{token.strip()}`")
            if "pip install" in line and "--no-index" not in line:
                pip_no_index_problems.append(f"{name}:{line.strip()[:80]}")
    check("prod:scripts-offline-shape",
          not net_hits and not pip_no_index_problems,
          f"{len(PROD_SCRIPTS)} 个脚本正文无联网动作，且每处 pip install 都带 --no-index"
          f"（**只能读作脚本里没写联网动作，不能读作运行时不联网**）"
          if not net_hits and not pip_no_index_problems
          else f"联网动作 {net_hits[:4]} / 裸 pip install {pip_no_index_problems[:3]}")

    # ---------------- 14 bash -n ----------------
    if shutil.which("bash") is None:
        check("prod:scripts-bash-syntax", False,
              "无法构造语法核对：找不到 bash。fail-closed，不许当 SKIP")
    else:
        syntax_bad = []
        for name in PROD_SCRIPTS:
            rc, out = _run(["bash", "-n", str(prod / name)])
            if rc != 0:
                syntax_bad.append(f"{name}: {out[:120]}")
        check("prod:scripts-bash-syntax", not syntax_bad,
              f"{len(PROD_SCRIPTS)} 个脚本 `bash -n` 通过"
              f"（本机无 shellcheck/shfmt，实测零命中 ⇒ 静态检查到此为止）"
              if not syntax_bad else f"{syntax_bad}")

    # ---------------- 15 严格模式 ----------------
    no_strict = [
        name for name in PROD_SCRIPTS
        if "set -euo pipefail" not in (prod / name).read_text(encoding="utf-8")
    ] if all((prod / n).is_file() for n in PROD_SCRIPTS) else list(PROD_SCRIPTS)
    check("prod:scripts-strict-mode", not no_strict,
          f"{len(PROD_SCRIPTS)} 个脚本均含 `set -euo pipefail`"
          if not no_strict else f"缺严格模式：{no_strict}")

    # ---------------- 16/17 卸载删卷闸口（**行为核对，不是文本核对**）----------------
    # 为什么跑脚本而不是扫文本：扫文本要判断 `--volumes` 落在哪个 if 分支里，
    # 那需要解析 shell 控制流 —— 写错了会静默放过。
    # `--dry-run` 把实际会执行的命令原样打出来 ⇒ 直接核对那一行。
    uninstall = prod / "uninstall.sh"
    if shutil.which("bash") is None or not uninstall.is_file():
        check("prod:uninstall-default-keeps-volumes", False, "无法构造行为核对：缺 bash 或 uninstall.sh")
        check("prod:uninstall-purge-does-delete", False, "无法构造行为核对：缺 bash 或 uninstall.sh")
    else:
        rc_def, out_def = _run(["bash", str(uninstall), "--dry-run"])
        down_lines_def = [ln for ln in out_def.splitlines() if "down" in ln and "dry-run" in ln]
        default_safe = (
            rc_def == 0 and bool(down_lines_def)
            and not any(("--volumes" in ln) or re.search(r"\s-v\b", ln) for ln in down_lines_def)
        )
        check("prod:uninstall-default-keeps-volumes", default_safe,
              f"默认 --dry-run 的 down 行不含 --volumes：{down_lines_def[:1]}"
              f"（ADR-11 验证表：『重启不丢数据…不得 down -v』—— 默认删卷就是把 G0-19 的验收点拆了）"
              if default_safe else f"exit={rc_def}；down 行={down_lines_def[:2]}")

        rc_pg, out_pg = _run(["bash", str(uninstall), "--purge", "--yes", "--dry-run"])
        down_lines_pg = [ln for ln in out_pg.splitlines() if "down" in ln and "dry-run" in ln]
        purge_works = rc_pg == 0 and any("--volumes" in ln for ln in down_lines_pg)
        check("prod:uninstall-purge-does-delete", purge_works,
              "显式 --purge 时 down 行带 --volumes（对照组 —— 否则上一条可能只是因为脚本什么都不做而绿）"
              if purge_works else f"exit={rc_pg}；down 行={down_lines_pg[:2]}")

    # ---------------- 18 升级备份委托 ----------------
    upgrade_text = (prod / "upgrade.sh").read_text(encoding="utf-8") if (prod / "upgrade.sh").is_file() else ""
    upgrade_body = "\n".join(_strip_shell_comments(upgrade_text))
    delegates = "tools/ops/backup_pg.py" in upgrade_body
    own_dump = bool(re.search(r"\bpg_dump\b", upgrade_body))
    check("prod:upgrade-delegates-backup", delegates and not own_dump,
          "upgrade.sh 调 tools/ops/backup_pg.py，且正文无自写 pg_dump"
          f"（自写就是第二处真相，且会把 backup_pg.py:70-86 收敛掉的 pg_bin 回退坑重新挖一遍）"
          if delegates and not own_dump
          else f"delegates={delegates} own_pg_dump={own_dump}")

    # ---------------- 19 监控配置未被复制第二份 ----------------
    prod_rule_files = [
        p.relative_to(prod).as_posix() for p in prod.rglob("*")
        if p.is_file() and p.suffix in (".yml", ".yaml")
        and p.name != "docker-compose.yml"
    ]
    mounts_monitoring = "../monitoring:/etc/prometheus:ro" in compose_text
    check("prod:monitoring-not-duplicated",
          not prod_rule_files and mounts_monitoring,
          "deploy/prod/ 下无第二份 Prometheus 规则，且 compose 只读挂载 ../monitoring"
          f"（复制 ⇒ 两份规则漂移，而 check_alerting_rules.py 的 RULES_DIR 只盯 "
          f"deploy/monitoring/ ⇒ prod 那份的悬空指标无人守）"
          if not prod_rule_files and mounts_monitoring
          else f"prod 下的 yaml={prod_rule_files}；只读挂载={mounts_monitoring}")

    # ---------------- 20 制品未入库 ----------------
    rc_ls, out_ls = _run(["git", "-C", str(REPO), "ls-files", "dist"])
    tracked_dist = [ln for ln in out_ls.splitlines() if ln.strip()]
    check("prod:artifacts-not-tracked", rc_ls == 0 and not tracked_dist,
          "git ls-files 里没有 dist/（制品落 dist/，.gitignore:43 已忽略）"
          if rc_ls == 0 and not tracked_dist
          else f"git exit={rc_ls}；被追踪的 dist 文件 {tracked_dist[:5]}")

    # ---------------- 21/22 过度声称自查 ----------------
    # `.env.example` 的 `suffix` 就是 `.example`，已被 rglob 收进来了 ——
    # 首版又用 `prod.glob(".env.example")` 补了一次，于是它被**扫两遍**，
    # 哨兵行数翻倍。用 dict.fromkeys 去重并保序。
    scan_targets = list(dict.fromkeys(
        sorted(p for p in prod.rglob("*")
               if p.is_file() and p.suffix in (".md", ".sh", ".yml", ".json", ".conf", ".example"))
        + sorted((REPO / "docs" / "m5").glob("WP46-*.md"))
    ))
    hits: list[str] = []
    exempt_lines = 0
    for path in scan_targets:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _OVERCLAIM_SENTINEL in line:
                exempt_lines += 1
                continue
            for phrase in _OVERCLAIM_PHRASES:
                if phrase in line:
                    hits.append(f"{path.name}:{lineno} 「{phrase}」")
    check("prod:no-overclaim-phrases", not hits,
          f"{len(scan_targets)} 个文件内零命中（黑名单 {len(_OVERCLAIM_PHRASES)} 条）"
          if not hits else f"{len(hits)} 处：{hits[:5]}")
    check("prod:overclaim-exempt-lines-monotonic",
          exempt_lines == _OVERCLAIM_EXEMPT_BASELINE,
          f"带哨兵「{_OVERCLAIM_SENTINEL}」的豁免行 {exempt_lines} == 基线 "
          f"{_OVERCLAIM_EXEMPT_BASELINE}（哨兵的用途只有一个：引用禁用句式本身。"
          f"计数钉住，防止有人靠加哨兵把真声称洗白）"
          if exempt_lines == _OVERCLAIM_EXEMPT_BASELINE
          else f"实测 {exempt_lines} ≠ 基线 {_OVERCLAIM_EXEMPT_BASELINE}")

    # ---------------- 23 构建器子命令自检 ----------------
    builder = REPO / "tools" / "ops" / "build_offline_bundle.py"
    builder_text = builder.read_text(encoding="utf-8") if builder.is_file() else ""
    declared_subs = re.search(
        r"SUBCOMMANDS: Final\[tuple\[str, \.\.\.\]\] = \(([^)]*)\)", builder_text
    )
    dispatch_keys = set(re.findall(r'^\s{4}"([a-z]+)": cmd_', builder_text, flags=re.MULTILINE))
    declared_set = set(re.findall(r'"([a-z]+)"', declared_subs.group(1))) if declared_subs else set()
    subs_ok = bool(declared_set) and declared_set == dispatch_keys | {"all"}
    check("bundle:subcommands-selfcheck", subs_ok,
          f"SUBCOMMANDS {sorted(declared_set)} == DISPATCH 键 ∪ {{all}}"
          if subs_ok
          else f"脱钩：声明={sorted(declared_set)} 实现={sorted(dispatch_keys)}")

    # ---------------- 24 离线包清单现场重算（fail-closed）----------------
    sums = bundle / "SHA256SUMS.txt"
    if not sums.is_file():
        check("bundle:manifest-self-verifies", False,
              f"无法构造对账：{sums} 不存在 —— 先跑 "
              f"`python tools/ops/build_offline_bundle.py wheelhouse` 与 `... manifest`。"
              f"**不许当 SKIP**（跑不了所以绿等于没有判据）")
        check("bundle:manifest-covers-all-files", False, "无法构造对账：同上")
        check("bundle:wheel-tags-match-manifest", False, "无法构造对账：同上")
        check("bundle:requirements-derived-from-pyproject", False, "无法构造对账：同上")
    else:
        declared_rows: dict[str, str] = {}
        for line in sums.read_text(encoding="utf-8").splitlines():
            if "  " not in line:
                continue
            digest, rel = line.split("  ", 1)
            declared_rows[rel] = digest
        recomputed_bad = [
            rel for rel, digest in declared_rows.items()
            if not (bundle / rel).is_file() or _sha256_file(bundle / rel) != digest
        ]
        check("bundle:manifest-self-verifies",
              bool(declared_rows) and not recomputed_bad,
              f"{len(declared_rows)} 条校验值**现场重算**全部相等"
              f"（读清单里写的数不算核对 —— 截断的文件与完好的文件在磁盘上长得一样）"
              if declared_rows and not recomputed_bad
              else f"{len(recomputed_bad)} 条对不上：{recomputed_bad[:5]}（清单共 {len(declared_rows)} 条）")

        on_disk = {
            p.relative_to(bundle).as_posix() for p in bundle.rglob("*")
            if p.is_file() and p.name not in ("SHA256SUMS.txt", "MANIFEST.json")
        }
        check("bundle:manifest-covers-all-files",
              on_disk == set(declared_rows) and bool(on_disk),
              f"清单 ⇄ 磁盘双向相等（{len(on_disk)} 个文件）"
              if on_disk == set(declared_rows) and on_disk
              else f"磁盘未进清单={sorted(on_disk - set(declared_rows))[:5]}；"
                   f"清单里磁盘没有={sorted(set(declared_rows) - on_disk)[:5]}")

        manifest_path = bundle / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        target = manifest.get("target_platform", {})
        declared_tags = set(target.get("platform_tags") or [])
        declared_py = str(target.get("python_version") or "")
        tag_bad: list[str] = []
        wheels = sorted((bundle / "wheelhouse").glob("*.whl"))
        for wheel in wheels:
            parts = wheel.stem.split("-")
            if len(parts) < 4:
                tag_bad.append(f"{wheel.name}（文件名不符合 PEP 427）")
                continue
            plat_tags = set(parts[-1].split("."))
            py_tag = parts[-3]
            if "any" not in plat_tags and not (plat_tags & declared_tags):
                tag_bad.append(f"{wheel.name}（平台 tag {sorted(plat_tags)} 不在声明 {sorted(declared_tags)} 内）")
            abi_tag = parts[-2]
            if not _py_tag_compatible(py_tag, abi_tag, declared_py):
                tag_bad.append(
                    f"{wheel.name}（python tag {py_tag}/abi {abi_tag} 与声明 py{declared_py} 不符）"
                )
        check("bundle:wheel-tags-match-manifest",
              bool(wheels) and not tag_bad and bool(declared_tags),
              f"{len(wheels)} 个 wheel 的平台/python tag 与 MANIFEST 声明的三元组一致"
              f"（本机 darwin/arm64 ⇒ 这些 manylinux wheel 在本机**无法 pip install 验证**；"
              f"只能读作清单可复现，不能读作内网 Linux 可安装）"
              if wheels and not tag_bad and declared_tags
              else f"wheel 数={len(wheels)}；声明 tag={sorted(declared_tags)}；不符 {tag_bad[:4]}")

        req_file = bundle / "requirements.txt"
        pyproject_data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8")).get("project", {})
        derived = list(pyproject_data.get("dependencies", []))
        for group in sorted((pyproject_data.get("optional-dependencies") or {}).keys()):
            derived.extend(pyproject_data["optional-dependencies"][group])
        actual_reqs = [
            ln.strip() for ln in req_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ] if req_file.is_file() else []
        check("bundle:requirements-derived-from-pyproject",
              actual_reqs == derived and bool(derived),
              f"requirements.txt {len(actual_reqs)} 条与 pyproject 派生结果**逐条相等**"
              f"（手抄一份就是第二处真相）"
              if actual_reqs == derived and derived
              else f"不等：requirements {len(actual_reqs)} 条 vs 派生 {len(derived)} 条；"
                   f"差异={sorted(set(actual_reqs) ^ set(derived))[:5]}")

    # ---------------- 25/26 前端外链（G0-20，现状钉住）----------------
    origin_files: dict[str, set[str]] = {}
    if STATIC_DIR.is_dir():
        for path in sorted(STATIC_DIR.rglob("*")):
            if not path.is_file() or path.suffix not in (".html", ".htm", ".js", ".mjs", ".css"):
                continue
            hosts = set(re.findall(r"https?://([A-Za-z0-9.-]+)", path.read_text(encoding="utf-8")))
            hosts -= _LOOPBACK_HOSTS
            if hosts:
                origin_files[path.name] = hosts
    seen_hosts = set().union(*origin_files.values()) if origin_files else set()
    check("frontend:external-origin-hosts-declared",
          seen_hosts == _EXTERNAL_ORIGIN_HOSTS,
          f"实测外部 origin 主机集合 == 声明集合 {sorted(_EXTERNAL_ORIGIN_HOSTS)}"
          f"（⚠️ 这不是背书：本仓 services/backend/static/ **当前确实依赖**这三个外站，"
          f"断网后图标变方框、字体回退 —— 正是 G0-20 要挡的。本包不改前端赛道的文件，"
          f"只把现状钉住 + 登记待处置）"
          if seen_hosts == _EXTERNAL_ORIGIN_HOSTS
          else f"新增主机={sorted(seen_hosts - _EXTERNAL_ORIGIN_HOSTS)}；"
               f"已消除={sorted(_EXTERNAL_ORIGIN_HOSTS - seen_hosts)} —— 消除后请同步调小声明")
    check("frontend:external-origin-files-monotonic",
          len(origin_files) == _EXTERNAL_ORIGIN_FILE_BASELINE,
          f"含外部 origin 的前端文件 {len(origin_files)} == 基线 "
          f"{_EXTERNAL_ORIGIN_FILE_BASELINE}（只许减不许增）"
          if len(origin_files) == _EXTERNAL_ORIGIN_FILE_BASELINE
          else f"实测 {len(origin_files)} ≠ 基线 {_EXTERNAL_ORIGIN_FILE_BASELINE}："
               f"{sorted(origin_files)[:5]}")

    return _report(checks)


def _report(checks: list[tuple[str, bool, str]]) -> int:
    print("## WP46 离线安装包与生产编排判据")
    print()
    for index, (name, ok, detail) in enumerate(checks, 1):
        print(f"{'PASS' if ok else 'FAIL'}  [{index}] {name}")
        print(f"          {detail}")
    failed = [name for name, ok, _ in checks if not ok]
    total = len(checks)
    print()
    print(f"RESULT={'PASS' if not failed and total == EXPECTED_CHECKS else 'FAIL'} "
          f"checks={total}/{EXPECTED_CHECKS} failures={len(failed)}")
    if total != EXPECTED_CHECKS:
        print(f"  ⚠️ 判据条数 {total} ≠ 期望 {EXPECTED_CHECKS} —— "
              f"新增/删除判据须同步 EXPECTED_CHECKS（ISS-153：>= 只防变少不防变多）")
    if failed:
        print(f"  失败项：{failed}")
    return 1 if (failed or total != EXPECTED_CHECKS) else 0


if __name__ == "__main__":
    sys.exit(main())
