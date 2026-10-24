/**
 * SessionsView 会话视图（M1 空壳 + 列表骨架；M4 持久化/恢复/分叉/并行落地，R16）。
 * 数据源：sessions_list 事件 → sessions 切片（宿主接入后自动填充）。
 */
import { useState } from 'react';
import { useStore } from '../store/store';

function fmtAgo(ts: number): string {
  const d = Date.now() - ts;
  if (d < 60_000) return '刚刚';
  if (d < 3600_000) return `${Math.floor(d / 60_000)}分钟前`;
  if (d < 86_400_000) return `${Math.floor(d / 3600_000)}小时前`;
  return new Date(ts).toLocaleDateString();
}

export function SessionsView(): React.JSX.Element {
  const sessions = useStore((s) => s.sessions);
  const activeSessionKey = useStore((s) => s.activeSessionKey);
  const newSession = useStore((s) => s.newSession);
  const [q, setQ] = useState('');

  const filtered = sessions.filter((s) =>
    (s.title ?? '').toLowerCase().includes(q.toLowerCase()),
  );
  return (
    <div className="view">
      <div className="view-head">
        <span>会话</span>
        <button className="btn" onClick={newSession}>
          ＋
        </button>
      </div>
      <input className="in grow" placeholder="🔍 搜索历史消息…" value={q} onChange={(e) => setQ(e.target.value)} />
      <div className="sessionlist">
        {filtered.length === 0 ? (
          <div className="muted empty-note">
            暂无历史会话 —— 会话持久化与恢复/分叉/并行多开在 M4 接入（R16）。
          </div>
        ) : (
          filtered.map((s) => (
            <div key={s.sessionId} className={`session${s.sessionId === activeSessionKey ? ' sel' : ''}`}>
              <span>{s.sessionId === activeSessionKey ? '●' : '○'}</span>
              <span className="session-title">{s.title ?? s.sessionId}</span>
              {s.updatedAt && <span className="muted">{fmtAgo(Date.parse(s.updatedAt) || Date.now())}</span>}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
