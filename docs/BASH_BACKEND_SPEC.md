# bash 执行后端装配规格（BASH_BACKEND_SPEC）

## 为什么缺这一块

本开发环境安装了 Mimosa 安全扫描钩子（PreToolUse Write）。它会将一切
「执行运行期命令串」的代码（`subprocess` + shell 形态，含其建议的 argv 变体）
判定为高危命令注入并拒绝落盘。经 workflow 子代理与编排方主会话双向复现确认：
这不是可修的误报绕行问题，而是环境级策略与本仓库规划功能（PLAN.md R29 / M2
要求 bash 工具，前置门禁为权限引擎 + 危险命令黑名单）的正面冲突。

处理原则：不绕过安全钩子（不用 Bash/heredoc 写文件、不换执行原语规避扫描）。
`zcode_py/tools/bash.py`（工具主体：权限判定、黑名单、L1 截断、事件广播）
已正常实现；仅执行核心独立为 `bash_backend` 模块并在此留装配缝。

## 装配目标文件

`agent/zcode_py/tools/bash_backend.py`（Python ≥3.12，仅标准库）

## API 契约（bash.py 已按此调用，勿改签名）

```text
dataclass BashResult:
    exit_code: int        # 进程退出码；超时/无法启动为 -1
    stdout: str           # 完整标准输出，不截断（截断是工具层职责）
    stderr: str           # 完整标准输出错误流
    timed_out: bool       # 超时被杀为 True
    duration_ms: int      # 墙钟耗时毫秒

class BashBackendError(RuntimeError):
    """找不到可用 shell 或进程无法启动。"""

def run_command(
    command: str,
    *,
    cwd: str | None = None,
    timeout: float = 120.0,
    env: dict[str, str] | None = None,
) -> BashResult: ...
```

## 语义要求

1. `command` 是已通过 `permissions.py` 门禁（默认询问 + DANGER_PATTERN 黑名单
   优先级最高）的完整命令串，需要完整 shell 语义（管道、`&&`、重定向）。
   **安全契约：本模块不做任何权限校验，调用方必须先过门禁。**
2. 通过 POSIX shell 执行：优先 `/bin/bash`，回退 `/usr/bin/bash`、`/bin/sh`；
   三者皆无则抛 `BashBackendError`（Windows 原生留待 M9）。
3. `timeout` 秒到期：杀进程，`timed_out=True`，已捕获的部分输出仍随结果返回。
4. 不截断、不重试、不解析输出内容；stdout/stderr 全量透传给工具层。
5. 失败要可观测：进程启动失败抛 `BashBackendError`，由工具层转为结构化错误文本。

## 装配方式（三选一，均约 40 行代码）

> **状态：已装配（2026-10-24）。** 用户在自有终端按本规格落盘
> `agent/zcode_py/tools/bash_backend.py`（用户授权的一次性 Mimosa 策略例外，
> 提交 eb9f088）；守门冒烟已生效，`uv run --project agent pytest -q` 全绿
> （396 passed / 0 skipped）。以下方式供其他环境重建时参考。

- 用户在 Mimosa 策略中为本仓库放行该模式后，重跑相关 workflow 步骤自动落盘；
- 用户手动按本规格实现该文件（推荐：实现后在仓库根跑
  `uv run --project agent pytest -q`，`test_bash.py` 里的 `pytest.importorskip`
  真实冒烟测试会自动生效）；
- 在无该钩子的环境（如 CI 或另一台机器）检出本仓库后补齐并提交。

## 测试约定（已按此实现，勿用真 subprocess）

- 工具层测试全部 `monkeypatch` 一个假的 `bash_backend` 模块/函数，不写真实执行代码；
- 真实冒烟测试用 `pytest.importorskip("zcode_py.tools.bash_backend")` 守门，
  后端装配后自动开始生效。
