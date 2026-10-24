/**
 * TaskListBar 任务清单条（PLAN §6.1：任务清单 ████████░░░░ 5/12 [展开▾]）。
 * 数据源：todos 切片（store 内由 todo 工具调用临时归约，M2 后由正式事件驱动）。
 */
import { useStore } from '../store/store';
import { useT } from '../i18n';

const STATUS_ICON: Record<string, string> = {
  pending: '☐',
  in_progress: '◐',
  completed: '☑',
};

export function TaskListBar(): React.JSX.Element | null {
  const todos = useStore((s) => s.todos);
  const t = useT();
  if (todos.length === 0) return null;
  const done = todos.filter((td) => td.status === 'completed').length;
  const pct = Math.round((done / todos.length) * 100);
  return (
    <details className="taskbar">
      <summary>
        <span className="taskbar-title">
          {t('taskbar.title')} <span className="meter"><i style={{ width: `${pct}%` }} /></span>{' '}
          {done}/{todos.length}
        </span>
        <span className="taskbar-toggle muted">{t('taskbar.details')}<span className="chev">▸</span></span>
      </summary>
      <ul className="tasklist">
        {todos.map((t, i) => (
          <li key={i} className={t.status === 'completed' ? 'done' : ''}>
            <span className="task-ico">{STATUS_ICON[t.status] ?? '☐'}</span> {t.content}
          </li>
        ))}
      </ul>
    </details>
  );
}
