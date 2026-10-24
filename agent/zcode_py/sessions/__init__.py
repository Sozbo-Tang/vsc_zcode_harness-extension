"""会话持久化：每会话目录 meta.json + transcript.jsonl 追加式（PLAN §3.2）。

转录即 UI 事件流：核→壳协议帧（dir=out）与入站 user_message（dir=in）按到达
顺序追加为 ``{"ts", "dir", "event"}`` 信封行；回放 = 重放 out 帧；L4 冷存储 =
文件本身（天然可 grep）。分叉（R16）预留 parentSessionId。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from ..config import home_dir


class SessionError(Exception):
    """会话不存在 / 元数据不可读。"""


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

    def append(self, session_id: str, frame: dict, direction: str = "out") -> None:
        """追加一帧到 transcript.jsonl。

        上次进程崩溃可能留下无换行的断尾行；直接追加会把新帧拼进坏行一起
        丢失，故先检测并补齐换行，保证坏行只影响它自己（read_frames 跳过）。
        """
        envelope = {"ts": _now_iso(), "dir": direction, "event": frame}
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

    # -- 列表 ----------------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        """全部会话摘要，按最近更新降序（epoch 高精度排序，sessionId 决胜）。"""
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
