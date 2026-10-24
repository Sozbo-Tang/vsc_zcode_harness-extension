/**
 * TaskListBar 任务清单条（PLAN §6.1：任务清单 ████████░░░░ 5/12 [展开▾]）。
 * 数据源：todos 切片（store 内由 todo 工具调用临时归约，M2 后由正式事件驱动）。
 */
import { useStore } from '../store/store';

const STATUS_ICON: Record<string, string> = {
  pending: '☐',
  in_progress: '◐',
  completed: '☑',
};

export function TaskListBar(): React.JSX.Element | null {
  const todos = useStore((s) => s.todos);
  if (todos.length === 0) return null;
  const done = todos.filter((t) => t.status === 'completed').length;
  const pct = Math.round((done / todos.length) * 100);
  return (
    <details className="taskbar">
      <summary>
        <span className="taskbar-title">
          任务清单 <span className="meter"><i style={{ width: `${pct}%` }} /></span>{' '}
          {done}/{todos.length}
        </span>
        <span className="muted">展开 ▾</span>
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
