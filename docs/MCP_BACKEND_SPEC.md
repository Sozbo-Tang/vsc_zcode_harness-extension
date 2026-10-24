# MCP / hooks 进程装配规格（MCP_BACKEND_SPEC）

## 为什么缺这一块

与 `docs/BASH_BACKEND_SPEC.md` 同一环境级约束：Mimosa 安全扫描钩子
（PreToolUse Write）把一切「执行运行期命令串/argv」的代码（`subprocess`
形态，含动态 argv 列表）判为高危命令注入并拒绝落盘。M8 需要两处此类执行：

1. **MCP server 子进程**（`zcode_py/mcp/transport.py`）——按用户在
   `~/.zcode-py/config/mcp.json` 配置的 argv 列表拉起长驻 stdio server；
2. **hooks 子进程**（`zcode_py/hooks.py`）——按 `~/.zcode-py/hooks.json`
   配置的 argv 列表执行 pre_tool / post_tool 钩子（stdin 喂 JSON）。

处理原则不变：**不绕过安全钩子**（不用 Bash/heredoc 写文件、不换执行原语
规避扫描）。两处执行收敛到一个装配缝模块 `zcode_py/mcp/proc_backend.py`：
协议、超时、截断、权限门禁（MCP 工具 tier=write 走权限卡；hooks 只是旁路
增强不是门禁）都在已落盘的工具层；缝只做最诚实的「按 argv 拉起进程」。

`zcode_py/tools/bash_backend.py`（已装配的先例）是 **shell 串 + 阻塞到
完成** 的 `subprocess.run` 语义，无法承载「长驻 stdin/stdout 双向管道的
MCP server 进程」，也不便带 stdin 喂入，故 MCP/hooks 独立成缝、不复用它。

## 装配目标文件

`agent/zcode_py/mcp/proc_backend.py`（Python ≥3.12，仅标准库；与
`transport.py` / `hooks.py` 同目录，相对导入 `from . import proc_backend`）

## API 契约（transport.py / hooks.py 已按此调用，勿改签名）

```python
def spawn_process(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
):
    """按字面 argv 拉起子进程，返回 subprocess.Popen 风格对象。

    要求：文本模式管道（stdin/stdout 为 text stream，UTF-8）、stderr 可独立
    读取或合并、具备 poll() / terminate() / wait(timeout=) 。env 传 None 时
    继承当前进程环境；显式 env 原样生效（调用方已做脱敏/组装）。
    """

def run_process(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: float = 10.0,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[int, str, str, bool]:
    """执行到完成：(exit_code, stdout, stderr, timed_out)。

    stdin 喂入完整字符串后关闭；timeout 秒到期杀进程，已捕获的部分输出仍
    随结果返回（timed_out=True，exit_code 填 -1）。不截断、不重试、不解析
    内容——截断/语义解释是工具层职责。
    """
```

## 语义要求

1. **只按字面 argv 执行**：绝无 `shell=True`、绝无拼接命令串。argv 来源是
   用户配置文件（mcp.json / hooks.json），配置校验已在 `mcp/config.py` 与
   `hooks.parse_hooks` 完成（非空字符串列表）。
2. **本模块不做任何权限校验**：MCP 工具调用先过 `permissions.py` 门禁
   （tier=write 默认询问），hooks 由 ToolSession 在工具执行前后旁路调用。
3. 进程启动失败（FileNotFoundError 等）抛 `OSError` 子类，由调用方转
   `McpTransportError` / 结构化错误（不炸回合）。
4. 失败要可观测：异常信息带 argv[0]，方便用户在 mcp_list / 工具结果里定位。

## 参考实现（约 45 行，可直接落盘）

```python
"""MCP/hooks 进程执行后端（装配缝，见 docs/MCP_BACKEND_SPEC.md）。

按字面 argv 拉起子进程；权限门禁在调用方（permissions.py）。
"""

from __future__ import annotations

import subprocess


def spawn_process(argv, *, env=None, cwd=None):
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=cwd,
    )


def run_process(argv, *, stdin=None, timeout=10.0, env=None, cwd=None):
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=env,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return -1, out, err, True
    return proc.returncode, proc.stdout or "", proc.stderr or "", False
```

## 装配方式（三选一）

> **状态：已装配（2026-10-24）。** 用户在自有终端按参考实现落盘
> `agent/zcode_py/mcp/proc_backend.py`（用户授权的一次性 Mimosa 策略
> 例外）；真实冒烟通过（echo 字面量 argv 往返），pytest 643 passed +
> 3 skipped（skip 为「缝缺位分支」守门测试，装配后不可达，符合设计）。
> MCP 与 hooks 经 try-import 自动生效。

- 用户在 Mimosa 策略中为本仓库放行该模式后，按参考实现落盘；
- 用户手动按本规格实现该文件（推荐：实现后跑
  `uv run --project agent pytest -q`，MCP 相关测试全为打桩测试，不依赖缝；
  真实冒烟 = `mcp_add` 一个本地 server 后 `mcp_list` 看到 connected）；
- 在无该钩子的环境（如 CI 或另一台机器）检出本仓库后补齐并提交。

## 缺位时的行为（已实现，勿改）

- MCP：`spawn_server` 抛 `McpTransportError`（消息指向本文件）→ server
  状态 `error`，会话不受影响，`mcp_list` 可见原因；
- hooks：`HooksRunner.startup_check` 广播**一次** `error{kind:"config"}`
  （指引本文件）后禁用——**绝不阻断工具执行**；
- 两者装配后无需改任何调用方代码（try-import 自动生效）。

## 测试约定（已按此实现，勿用真 subprocess）

- `tests/test_mcp.py` 全部 monkeypatch `zcode_py.mcp.transport.spawn_server`
  返回假进程对象（脚本化 stdin/stdout），零真实子进程；
- `tests/test_hooks.py` monkeypatch `HooksRunner` 的 runner 注入口（或
  proc_backend），验证阻断/附加/超时/缝缺位禁用语义。
