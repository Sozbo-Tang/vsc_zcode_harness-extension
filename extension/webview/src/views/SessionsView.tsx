/**
 * SessionsView 会话视图（M4 完整实现，R16/R28/R12；线框 PLAN §6.3）：
 *  - 搜索：标题 / sessionId / 当前已载入转录的全文（前端过滤；服务端全文
 *    搜索核心已有 sessions.search_sessions，协议暂无对应 control 命令）
 *  - 📌 置顶组 + 按日期分组（今天 / 本周 / 更早，组头可折叠）
 *  - 会话条目：●当前 / ○其他 · 标题（📌 收藏、↳ 分叉自）· 相对时间 · 消息数
 *  - 操作（⋯ 展开）：恢复（control resume + session.activate RPC + 切聊天
 *    视图）/ 从此会话分叉（control fork）/ 📌 收藏切换（control pin）/
 *    导出 Markdown（control export → result.markdown → workspace.saveExport）/
 *    🗑 删除（control session_delete，两步确认；当前活跃会话禁用——核侧
 *    同样拒绝，成功后 sessions_list 收敛）
 *  - 新建会话按钮（control new_session）
 * 数据源：sessions_list 事件 → sessions 切片（init/每次会话变迁时核心广播）。
 */
import { useMemo, useState } from 'react';
import { useStore } from '../store/store';
import { useT, type TranslateParams } from '../i18n';
import { FORK_AT_LAST_USER_MESSAGE, type SessionSummary } from '../protocol';

function tsOf(s: SessionSummary): number {
  const raw = s.lastActivity ?? s.updatedAt;
  return raw ? Date.parse(raw) : NaN;
}

/** 相对时间（文案经 t() 渲染期解析，R20 i18n） */
function fmtAgo(ts: number, t: (key: string, params?: TranslateParams) => string): string {
  if (Number.isNaN(ts)) return '';
  const d = Date.now() - ts;
  if (d < 60_000) return t('sessions.justNow');
  if (d < 3600_000) return t('sessions.minutesAgo', { n: Math.floor(d / 60_000) });
  if (d < 86_400_000) return t('sessions.hoursAgo', { n: Math.floor(d / 3600_000) });
  if (d < 7 * 86_400_000) return t('sessions.daysAgo', { n: Math.floor(d / 86_400_000) });
  return new Date(ts).toLocaleDateString();
}

interface DayGroup {
  key: 'today' | 'week' | 'older';
  /** 分组标签键（sessions.today / .week / .older，渲染期经 t() 解析） */
  labelKey: string;
  items: SessionSummary[];
}

/** 按最近活跃分组（今天=本地日历日；本周≈近 7 天；时间戳不可解析归「更早」） */
function groupByDate(list: SessionSummary[]): DayGroup[] {
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const buckets: Record<'today' | 'week' | 'older', SessionSummary[]> = { today: [], week: [], older: [] };
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
  if (buckets.today.length > 0) out.push({ key: 'today', labelKey: 'sessions.today', items: buckets.today });
  if (buckets.week.length > 0) out.push({ key: 'week', labelKey: 'sessions.week', items: buckets.week });
  if (buckets.older.length > 0) out.push({ key: 'older', labelKey: 'sessions.older', items: buckets.older });
  return out;
}

function SessionRow({ s }: { s: SessionSummary }): React.JSX.Element {
  const coreSessionId = useStore((st) => st.coreSessionId);
  const ui = useStore((st) => st.sessionsUi[s.sessionId]);
  const activateSession = useStore((st) => st.activateSession);
  const forkSession = useStore((st) => st.forkSession);
  const togglePinSession = useStore((st) => st.togglePinSession);
  const exportSession = useStore((st) => st.exportSession);
  const deleteSession = useStore((st) => st.deleteSession);
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  const [confirmFork, setConfirmFork] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const current = s.sessionId === coreSessionId;

  const doFork = (): void => {
    forkSession(s.sessionId);
    setConfirmFork(false);
  };

  const doDelete = (): void => {
    // 破坏性操作两步确认（同 ApiView 删除）；回执后由 sessions_list 收敛
    deleteSession(s.sessionId);
    setConfirmDelete(false);
    setExpanded(false);
  };

  return (
    <div className="session-block">
      <div
        className={`session${current ? ' sel' : ''}`}
        onClick={() => activateSession(s.sessionId)}
        title={t('sessions.rowTitle')}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') activateSession(s.sessionId);
        }}
      >
        <span title={current ? t('sessions.currentTitle') : t('sessions.switchTitle')}>{current ? '●' : '○'}</span>
        <span className="session-title">
          {s.pinned === true && '📌 '}
          {s.title ?? s.sessionId}
        </span>
        {s.parentSessionId != null && (
          <span className="muted small" title={t('sessions.forkedFrom', { id: s.parentSessionId })}>
            ↳
          </span>
        )}
        <span className="muted small session-meta">
          {fmtAgo(tsOf(s), t)}
          {s.messageCount != null ? t('sessions.messagesSuffix', { n: s.messageCount }) : ''}
        </span>
        <button
          className="iconbtn"
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          title={t('sessions.moreActions')}
        >
          ⋯
        </button>
      </div>
      {expanded && (
        <div className="session-actions">
          <button className="btn sec" onClick={() => activateSession(s.sessionId)} title={t('sessions.resumeTitle')}>
            {t('sessions.resume')}
          </button>
          {confirmFork ? (
            <>
              <button className="btn" onClick={doFork} title={t('sessions.confirmForkTitle')}>
                {t('sessions.confirmFork')}
              </button>
              <button className="btn sec" onClick={() => setConfirmFork(false)}>
                {t('sessions.cancel')}
              </button>
            </>
          ) : (
            <button
              className="btn sec"
              onClick={() => setConfirmFork(true)}
              title={
                current
                  ? t('sessions.forkTitleCurrent')
                  : t('sessions.forkTitleOther', { sentinel: FORK_AT_LAST_USER_MESSAGE })
              }
            >
              {t('sessions.fork')}
            </button>
          )}
          <button className="btn sec" onClick={() => togglePinSession(s.sessionId)}>
            {s.pinned === true ? t('sessions.unpin') : t('sessions.pin')}
          </button>
          <button
            className="btn sec"
            onClick={() => exportSession(s.sessionId)}
            disabled={ui?.busy === 'export'}
            title={t('sessions.exportTitle')}
          >
            {ui?.busy === 'export' ? t('sessions.exporting') : t('sessions.export')}
          </button>
          {confirmDelete ? (
            <>
              <button className="btn danger" onClick={doDelete} title={t('sessions.confirmDeleteTitle')}>
                {t('sessions.confirmDelete')}
              </button>
              <button className="btn sec" onClick={() => setConfirmDelete(false)}>
                {t('sessions.cancel')}
              </button>
            </>
          ) : (
            <button
              className="btn sec danger"
              onClick={() => setConfirmDelete(true)}
              disabled={current || ui?.busy === 'delete'}
              title={current ? t('sessions.deleteDisabledTitle') : t('sessions.deleteTitle')}
            >
              {ui?.busy === 'delete' ? t('sessions.deleting') : t('sessions.delete')}
            </button>
          )}
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
  const t = useT();
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
        <span>{t('sessions.title')}</span>
        <button className="btn" onClick={newSession} title={t('sessions.newTitle')}>
          ＋
        </button>
      </div>
      <input
        className="in grow"
        placeholder={t('sessions.searchPlaceholder')}
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div className="sessionlist">
        {sessions.length === 0 ? (
          <div className="muted empty-note">{t('sessions.empty')}</div>
        ) : filtered.length === 0 ? (
          <div className="muted empty-note">{t('sessions.noMatch')}</div>
        ) : (
          <>
            {pinned.length > 0 && renderGroup('pinned', t('sessions.pinnedGroup'), pinned)}
            {groups.map((g) => renderGroup(g.key, t(g.labelKey), g.items))}
          </>
        )}
      </div>
    </div>
  );
}
