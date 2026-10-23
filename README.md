# Zcode-Py

一个类 GitHub Copilot 的 VSCode 侧边栏插件：Agent 大脑（工具循环、权限系统、多厂商 LLM 接入、会话管理、上下文压缩、子代理、skills、MCP、工作流编排）全部用 **Python 手搓**（`agent/zcode_py/`，约 90% 代码），TypeScript 只写物理上不可避免的薄壳（`extension/`，约 10%——注册视图、拉起 Python 子进程、转发消息），两侧通过 stdio JSONL 协议通信，一个会话对应一个 Python 子进程。规划与里程碑详见 [docs/PLAN.md](docs/PLAN.md) 与 [docs/ROADMAP.md](docs/ROADMAP.md)。
