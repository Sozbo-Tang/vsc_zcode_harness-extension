"""Zcode-Py agent core（Python 手搓 Agent Harness，docs/PLAN.md §3.1）。

M1：协议 + stdio 服务 + 多厂商流式 Provider（anthropic/openai/gemini）+
车钟档位映射 + 性能采样 + 会话转录。
M2：工具系统（read/glob/grep/todo/write/edit/bash/webfetch/websearch）+
权限引擎（危险命令黑名单最高优先级）+ 隐私护栏 + SSRF 校验 + git 检查点/回滚。
bash 执行核心为装配缝（docs/BASH_BACKEND_SPEC.md）。
"""

__version__ = "0.3.0"
