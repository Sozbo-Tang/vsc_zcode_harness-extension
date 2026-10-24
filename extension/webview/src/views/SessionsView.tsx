/**
 * SessionsView 会话视图（M4 完整实现，R16/R28/R12；线框 PLAN §6.3）：
 *  - 搜索：标题 / sessionId / 当前已载入转录的全文（前端过滤；服务端全文
 *    搜索核心已有 sessions.search_sessions，协议暂无对应 control 命令）
 *  - 📌 置顶组 + 按日期分组（今天 / 本周 / 更早，组头可折叠）
 *  - 会话条目：●当前 / ○其他 · 标题（📌 收藏、↳ 分叉自）· 相对时间 · 消息数
 *  - 操作（⋯ 展开）：恢复（control resume + session.activate RPC + 切聊天
 *    视图）/ 从此会话分叉（control fork）/ 📌 收藏切换（control pin）/
 *    导出 Markdown（control export → result.markdown → workspace.saveExport）
 *  - 新建会话按钮（control new_session）
 * 数据源：sessions_list 事件 → sessions 切片（init/每次会话变迁时核心广播）。
 */
import { useMemo, useState } from 'react';
import { useStore } from '../store/store';
import { FORK_AT_LAST_USER_MESSAGE, type SessionSummary } from '../protocol';

function tsOf(s: SessionSummary): number {
  const raw = s.lastActivity ?? s.updatedAt;
  return raw ? Date.parse(raw) : NaN;
}

function fmtAgo(ts: number): string {
  if (Number.isNaN(ts)) return '';
  const d = Date.now() - ts;
  if (d < 60_000) return '刚刚';
  if (d < 3600_000) return `${Math.floor(d / 60_000)}分钟前`;
  if (d < 86_400_000) return `${Math.floor(d / 3600_000)}小时前`;
  if (d < 7 * 86_400_000) return `${Math.floor(d / 86_400_000)}天前`;
  return new Date(ts).toLocaleDateString();
}

interface DayGroup {
  key: 'today' | 'week' | 'older';
  label: string;
  items: SessionSummary[];
}

/** 按最近活跃分组（今天=本地日历日；本周≈近 7 天；时间戳不可解析归「更早」） */
function groupByDate(list: SessionSummary[]): DayGroup[] {
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const buckets: Record<DayGroup['key'], SessionSummary[]> = { today: [], week: [], older: [] };
  for (const s of list) {
    const ts = tsOf(s);
    const key: DayGroup['key'] = Number.isNaN(ts)
      ? 'older'
      : ts >= startToday
        ? 'today'
        : ts >= startToday - 6 * 86_400_000
          ? 'week'
          : 'older';
    buckets[key].push(s);
  }
  const out: DayGroup[] = [];
  if (buckets.today.length > 0) out.push({ key: 'today', label: '今天', items: buckets.today });
  if (buckets.week.length > 0) out.push({ key: 'week', label: '本周', items: buckets.week });
  if (buckets.older.length > 0) out.push({ key: 'older', label: '更早', items: buckets.older });
  return out;
}

function SessionRow({ s }: { s: SessionSummary }): React.JSX.Element {
  const coreSessionId = useStore((st) => st.coreSessionId);
  const ui = useStore((st) => st.sessionsUi[s.sessionId]);
  const activateSession = useStore((st) => st.activateSession);
  const forkSession = useStore((st) => st.forkSession);
  const togglePinSession = useStore((st) => st.togglePinSession);
  const exportSession = useStore((st) => st.exportSession);
  const [expanded, setExpanded] = useState(false);
  const [confirmFork, setConfirmFork] = useState(false);
  const current = s.sessionId === coreSessionId;

  const doFork = (): void => {
    forkSession(s.sessionId);
    setConfirmFork(false);
  };

  return (
    <div className="session-block">
      <div
        className={`session${current ? ' sel' : ''}`}
        onClick={() => activateSession(s.sessionId)}
        title="点击恢复此会话（切换活跃并回放转录）"
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') activateSession(s.sessionId);
        }}
      >
        <span title={current ? '当前会话' : '点击切换'}>{current ? '●' : '○'}</span>
        <span className="session-title">
          {s.pinned === true && '📌 '}
          {s.title ?? s.sessionId}
        </span>
        {s.parentSessionId != null && (
          <span className="muted small" title={`分叉自 ${s.parentSessionId}`}>
            ↳
          </span>
        )}
        <span className="muted small session-meta">
          {fmtAgo(tsOf(s))}
          {s.messageCount != null ? ` · ${s.messageCount}条` : ''}
        </span>
        <button
          className="iconbtn"
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          title="更多操作（分叉 / 收藏 / 导出）"
        >
          ⋯
        </button>
      </div>
      {expanded && (
        <div className="session-actions">
          <button className="btn sec" onClick={() => activateSession(s.sessionId)} title="恢复并切换为活跃会话">
            ▶ 恢复
          </button>
          {confirmFork ? (
            <>
              <button className="btn" onClick={doFork} title="以「最后一条用户消息」为分叉点复制前缀轮次">
                ⑂ 确认分叉
              </button>
              <button className="btn sec" onClick={() => setConfirmFork(false)}>
                取消
              </button>
            </>
          ) : (
            <button
              className="btn sec"
              onClick={() => setConfirmFork(true)}
              title={
                current
                  ? '从此会话分叉：分叉点 = 最后一条用户消息（该轮不带走，用于改写提问重新展开）'
                  : `从此会话分叉：非当前会话无法取到分叉点 id，将发送约定哨兵 ${FORK_AT_LAST_USER_MESSAGE}（核心侧支持前会收到 not_found 错误）`
              }
            >
              ⑂ 从此会话分叉
            </button>
          )}
          <button className="btn sec" onClick={() => togglePinSession(s.sessionId)}>
            {s.pinned === true ? '📌 取消收藏' : '📌 收藏'}
          </button>
          <button
            className="btn sec"
            onClick={() => exportSession(s.sessionId)}
            disabled={ui?.busy === 'export'}
            title="导出为 Markdown（选择保存位置后落盘）"
          >
            {ui?.busy === 'export' ? '⤓ 导出中…' : '⤓ 导出 Markdown'}
          </button>
        </div>
      )}
      {ui?.notice != null && (
        <div className={`small session-notice ${ui.noticeKind === 'error' ? 'bad' : 'muted'}`}>{ui.notice}</div>
      )}
    </div>
  );
}

export function SessionsView(): React.JSX.Element {
  const sessions = useStore((s) => s.sessions);
  const coreSessionId = useStore((s) => s.coreSessionId);
  const items = useStore((s) => s.chat.items);
  const newSession = useStore((s) => s.newSession);
  const [q, setQ] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  // 全文搜索语料：仅当前已载入转录（user/assistant 文本）。其余会话的全文
  // 检索需核心侧 search_sessions 暴露协议命令（遗留缺口，见文件头注释）
  const transcriptText = useMemo(
    () =>
      items
        .map((it) => (it.kind === 'user' || it.kind === 'assistant' ? it.text : ''))
        .join('\n')
        .toLowerCase(),
    [items],
  );

  const needle = q.trim().toLowerCase();
  const filtered = sessions.filter(
    (s) =>
      needle === '' ||
      (s.title ?? '').toLowerCase().includes(needle) ||
      s.sessionId.toLowerCase().includes(needle) ||
      (s.sessionId === coreSessionId && transcriptText.includes(needle)),
  );

  const pinned = filtered.filter((s) => s.pinned === true);
  const groups = groupByDate(filtered.filter((s) => s.pinned !== true));

  const toggleGroup = (key: string): void => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const renderGroup = (key: string, label: string, list: SessionSummary[]): React.JSX.Element => {
    const open = !collapsed.has(key);
    return (
      <div key={key}>
        <div
          className={`group-head${open ? ' open' : ''}`}
          onClick={() => toggleGroup(key)}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') toggleGroup(key);
          }}
        >
          <span className="chev">▸</span> {label}（{list.length}）
        </div>
        {open && list.map((s) => <SessionRow key={s.sessionId} s={s} />)}
      </div>
    );
  };

  return (
    <div className="view">
      <div className="view-head">
        <span>会话</span>
        <button className="btn" onClick={newSession} title="新建会话（control new_session）">
          ＋
        </button>
      </div>
      <input
        className="in grow"
        placeholder="🔍 搜索标题 / 当前会话全文…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="sessionlist">
        {sessions.length === 0 ? (
          <div className="muted empty-note">
            暂无历史会话 —— 发送第一条消息后自动建立（转录持久化于 ~/.zcode-py/sessions）。
          </div>
        ) : filtered.length === 0 ? (
          <div className="muted empty-note">无匹配会话（全文检索当前仅覆盖已载入的当前会话转录）。</div>
        ) : (
          <>
            {pinned.length > 0 && renderGroup('pinned', '📌 置顶', pinned)}
            {groups.map((g) => renderGroup(g.key, g.label, g.items))}
          </>
        )}
      </div>
    </div>
  );
}
