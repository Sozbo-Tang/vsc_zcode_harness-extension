"""会话持久化：每会话目录 meta.json + transcript.jsonl 追加式（PLAN §3.2）。

转录即 UI 事件流：核→壳协议帧（dir=out）与入站 user_message（dir=in）按到达
顺序追加为 ``{"ts", "dir", "event"}`` 信封行；回放 = 重放 out 帧；L4 冷存储 =
文件本身（天然可 grep）。分叉（R16）经 parentSessionId 追溯父会话。

M4 起 meta.json 额外承载列表元信息（R16/R27/R28/R11）：

- ``title``：首条用户消息前 30 字符（只定一次，不随对话漂移）；
- ``messageCount``：入站 user_message 条数（轮次计数）；
- ``pinned``：收藏标志（会话级，R28）；
- ``rootPath``：multi-root 当前根（R11，切换/恢复的依据）；
- ``inputHistory``：用户输入文本列表（上限 100，R27 ↑ 键调出）。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from ..config import home_dir

# R16：列表 title 取首条用户消息前 30 字符（够辨认、单行不溢出）
TITLE_MAX_CHARS = 30
# R27：输入历史上限（↑ 键翻阅的是最近输入，更早的没必要跨会话背走）
INPUT_HISTORY_LIMIT = 100

# 导出 Markdown 时工具卡摘要取的入参键（按常见工具选第一个命中者；
# 结果全文不进导出——导出是给人读的对话记录，不是转录副本）
_TOOL_ARG_KEYS = ("path", "command", "pattern", "query", "url", "mode")


class SessionError(Exception):
    """会话不存在 / 元数据不可读 / 分叉点非法。"""


def _now_iso(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) + (
        f".{int(ts * 1000) % 1000:03d}Z"
    )


def _now_pair() -> tuple[str, float]:
    """(人读 ISO, 高精度 epoch)。列表排序用 epoch（毫秒粒度会同刻抖动）。"""
    ts = time.time()
    return _now_iso(ts), ts


def _new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]


class SessionStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = Path(base_dir) if base_dir else home_dir() / "sessions"

    # -- 生命周期 ------------------------------------------------------------

    def create(
        self,
        *,
        workspace_roots: list | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        parent_id: str | None = None,
        title: str | None = None,
        root_path: str | None = None,
    ) -> dict:
        session_id = _new_session_id()
        updated_iso, updated_epoch = _now_pair()
        meta = {
            "sessionId": session_id,
            "createdAt": updated_iso,
            "updatedAt": updated_iso,
            "updatedAtEpoch": updated_epoch,
            "workspaceRoots": list(workspace_roots or []),
            "providerId": provider_id,
            "modelId": model_id,
            "parentSessionId": parent_id,
            "title": title,
            "turnCount": 0,
            "pinned": False,
            "messageCount": 0,
            "rootPath": root_path,
            "inputHistory": [],
        }
        directory = self.base_dir / session_id
        directory.mkdir(parents=True, exist_ok=True)
        self._write_meta(directory, meta)
        return meta

    # -- 转录 ----------------------------------------------------------------

    def _session_dir(self, session_id: str) -> Path:
        directory = self.base_dir / session_id
        if not directory.is_dir():
            raise SessionError(f"session not found: {session_id}")
        return directory

    def _append_line(self, session_id: str, envelope: dict) -> None:
        """追加一行信封帧。

        上次进程崩溃可能留下无换行的断尾行；直接追加会把新帧拼进坏行一起
        丢失，故先检测并补齐换行，保证坏行只影响它自己（read_frames 跳过）。
        """
        path = self._session_dir(session_id) / "transcript.jsonl"
        needs_newline = False
        if path.exists() and path.stat().st_size > 0:
            with path.open("rb") as fh:
                fh.seek(-1, 2)
                needs_newline = fh.read(1) != b"\n"
        with path.open("a", encoding="utf-8") as fh:
            if needs_newline:
                fh.write("\n")
            fh.write(json.dumps(envelope, ensure_ascii=False) + "\n")

    def append(self, session_id: str, frame: dict, direction: str = "out") -> None:
        """追加一帧（新时间戳信封）到 transcript.jsonl。"""
        self._append_line(
            session_id, {"ts": _now_iso(), "dir": direction, "event": frame}
        )

    def read_frames(self, session_id: str) -> list[dict]:
        """读取全部信封帧（含 in/out 与 ts）。"""
        path = self._session_dir(session_id) / "transcript.jsonl"
        if not path.exists():
            return []
        frames = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                frames.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 断尾行（进程崩溃）：跳过，保住前面的完整帧
        return frames

    # -- 元数据 --------------------------------------------------------------

    @staticmethod
    def _write_meta(directory: Path, meta: dict) -> None:
        (directory / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def get_meta(self, session_id: str) -> dict:
        path = self._session_dir(session_id) / "meta.json"
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"cannot read meta for session {session_id}: {exc}") from exc
        if not isinstance(meta, dict):
            raise SessionError(f"invalid meta for session {session_id}")
        return meta

    def update_meta(self, session_id: str, **fields) -> dict:
        meta = self.get_meta(session_id)
        meta.update(fields)
        updated_iso, updated_epoch = _now_pair()
        meta["updatedAt"] = updated_iso
        meta["updatedAtEpoch"] = updated_epoch
        self._write_meta(self._session_dir(session_id), meta)
        return meta

    # -- 列表 / 搜索（R16） ---------------------------------------------------

    def list_sessions(self) -> list[dict]:
        """全部会话摘要，按最近更新降序（epoch 高精度排序，sessionId 决胜）。

        pinned 只作标志返回，不在这里改序——「钉在顶部」是 UI 呈现策略，
        存储层保持单一 recency 排序契约。
        """
        if not self.base_dir.is_dir():
            return []
        metas = []
        for child in self.base_dir.iterdir():
            meta_path = child / "meta.json"
            if not child.is_dir() or not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(meta, dict) and meta.get("sessionId"):
                metas.append(meta)
        metas.sort(
            key=lambda m: (
                float(m.get("updatedAtEpoch") or 0.0),
                str(m.get("sessionId") or ""),
            ),
            reverse=True,
        )
        return metas

    def search_sessions(self, query: str) -> list[dict]:
        """按关键词过滤：title 命中或转录全文（user/assistant 文本）命中。

        大小写不敏感子串匹配（会话搜索是「聊过什么」，不是正则检索）；
        工具参数/结果不进搜索语料——那属于文件内容，另有 grep。空关键词
        退化为全量列表（与 sessions_list 一致）。
        """
        if not query.strip():
            return self.list_sessions()
        needle = query.strip().lower()
        matched: list[dict] = []
        for meta in self.list_sessions():
            title_hit = needle in (meta.get("title") or "").lower()
            session_id = str(meta.get("sessionId") or "")
            text_hit = session_id and any(
                needle in text.lower() for text in self._transcript_texts(session_id)
            )
            if title_hit or text_hit:
                matched.append(meta)
        return matched

    def _transcript_texts(self, session_id: str) -> list[str]:
        """可搜索文本：入站 user_message.text + 出站 message_complete.text。"""
        texts: list[str] = []
        for envelope in self.read_frames(session_id):
            event = envelope.get("event") or {}
            kind = (envelope.get("dir"), event.get("type"))
            if kind in (("in", "user_message"), ("out", "message_complete")):
                texts.append(str(event.get("text") or ""))
        return texts

    # -- 用户消息入账（R16 title / R27 历史 / 轮次计数） -----------------------

    def record_user_input(self, session_id: str, text: str) -> dict:
        """入站用户消息的元数据入账。

        - title 只由首条非空用户消息决定（前 30 字符）：后续消息不改名，
          列表标题不随对话漂移；
        - 输入历史跳过与上一条完全相同的重复（连续重发同句不占 ↑ 位），
          只记非空文本；
        - messageCount 无论文本是否为空都 +1（纯附件消息也是一轮）。
        """
        meta = self.get_meta(session_id)
        updates: dict = {"messageCount": int(meta.get("messageCount") or 0) + 1}
        if not meta.get("title") and text:
            updates["title"] = text[:TITLE_MAX_CHARS].strip()
        history = [str(t) for t in (meta.get("inputHistory") or [])]
        if text and (not history or history[-1] != text):
            history.append(text)
        updates["inputHistory"] = history[-INPUT_HISTORY_LIMIT:]
        return self.update_meta(session_id, **updates)

    def set_pinned(self, session_id: str, pinned: bool) -> dict:
        """R28：收藏标志落 meta（置顶渲染由 UI 按 pinned 决定）。"""
        return self.update_meta(session_id, pinned=bool(pinned))

    # -- 分叉（R16） ----------------------------------------------------------

    def fork(self, session_id: str, at_message_id: str) -> dict:
        """把 ``at_message_id``（转录中入站 user_message 的 messageId）之前的
        转录前缀复制到新会话，返回新会话 meta。

        前缀语义（R16「从某消息分支」）：**分叉点那一轮不带走**——新会话 =
        该用户消息之前的全部 in/out 帧（含工具卡/权限卡等 uiEvent，回放即
        完整还原分支起点的界面）。理由：分支的意义在于改写分叉点的提问
        重新展开；若连该消息及其回答一起复制，分支与父会话在该点完全
        重合，无从改写。父会话的 session_started（旧 id）剔除，换成新 id
        的首帧；其余信封原样保留（ts 不改写，忠实回放）。

        轮次计数与输入历史随前缀复制（resume 后 context_from_frames 的重建
        结果与 messageCount 保持一致）。
        """
        frames = self.read_frames(session_id)
        split_at: int | None = None
        for index, envelope in enumerate(frames):
            event = envelope.get("event") or {}
            if (
                envelope.get("dir") == "in"
                and event.get("type") == "user_message"
                and event.get("messageId") == at_message_id
            ):
                split_at = index
                break
        if split_at is None:
            raise SessionError(
                f"fork point {at_message_id!r} not found in session {session_id}"
            )
        parent_meta = self.get_meta(session_id)
        prefix = frames[:split_at]
        new_meta = self.create(
            workspace_roots=parent_meta.get("workspaceRoots"),
            provider_id=parent_meta.get("providerId"),
            model_id=parent_meta.get("modelId"),
            parent_id=session_id,
            title=parent_meta.get("title"),
            root_path=parent_meta.get("rootPath"),
        )
        new_id = new_meta["sessionId"]
        self._append_line(
            new_id,
            {"ts": _now_iso(), "dir": "out",
             "event": {"type": "session_started", "sessionId": new_id}},
        )
        user_texts: list[str] = []
        for envelope in prefix:
            event = envelope.get("event") or {}
            if event.get("type") == "session_started":
                continue  # 旧 id 首帧剔除，见 docstring
            self._append_line(new_id, envelope)
            if envelope.get("dir") == "in" and event.get("type") == "user_message":
                user_texts.append(str(event.get("text") or ""))
        return self.update_meta(
            new_id,
            messageCount=len(user_texts),
            inputHistory=[t for t in user_texts if t][-INPUT_HISTORY_LIMIT:],
        )

    # -- 导出 Markdown（R12） --------------------------------------------------

    def export_markdown(self, session_id: str) -> str:
        """转录 → 结构化 Markdown 文本。

        结构：``# title`` + 元信息行 → 逐轮「## 用户 → 本轮工具卡摘要 →
        ## 助手」。无文本的完成轮（取消/出错）显式标注，不静默丢轮——导出
        要能对照转录审计。落盘路径由 UI 决定，核心只产文本。
        """
        meta = self.get_meta(session_id)
        frames = self.read_frames(session_id)
        lines: list[str] = [f"# {meta.get('title') or session_id}", ""]
        facts: list[str] = [f"session `{session_id}`"]
        if meta.get("providerId") or meta.get("modelId"):
            facts.append(f"model `{meta.get('providerId')}/{meta.get('modelId')}`")
        if meta.get("createdAt"):
            facts.append(f"created {meta.get('createdAt')}")
        if meta.get("rootPath"):
            facts.append(f"root `{meta.get('rootPath')}`")
        lines += ["> " + " · ".join(facts), ""]
        pending_tools: list[str] = []

        def flush_tools() -> None:
            if pending_tools:
                lines.extend(["### 工具", "", *pending_tools, ""])
                pending_tools.clear()

        for envelope in frames:
            event = envelope.get("event") or {}
            etype = event.get("type")
            if envelope.get("dir") == "in" and etype == "user_message":
                flush_tools()  # 上一轮被截断没收尾时，工具卡仍归属其轮次
                lines += ["## 用户", "", str(event.get("text") or ""), ""]
            elif envelope.get("dir") == "out" and etype == "tool_call":
                if event.get("state") == "finished":
                    pending_tools.append(self._tool_line(event))
            elif envelope.get("dir") == "out" and etype == "message_complete":
                flush_tools()
                text = event.get("text")
                lines += ["## 助手", "", text if text else "_（无文本输出）_", ""]
        flush_tools()  # 尾轮未完成（进程中断）：残余工具卡不丢
        return "\n".join(lines).rstrip("\n") + "\n"

    @staticmethod
    def _tool_line(event: dict) -> str:
        args = event.get("args") or {}
        subject = ""
        for key in _TOOL_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value:
                subject = " " + value
                break
        result = event.get("result")
        outcome = "error" if isinstance(result, dict) and result.get("error") else "ok"
        elapsed = event.get("elapsedMs")
        timing = f", {elapsed}ms" if isinstance(elapsed, (int, float)) else ""
        return f"- `{event.get('name')}`{subject} ({outcome}{timing})"
