/**
 * TasksView 后台任务视图（M1 空壳；M9 工作流编排 + 后台任务面板 + cron，R3/M9）。
 */
import { useStore } from '../store/store';

export function TasksView(): React.JSX.Element {
  const tasks = useStore((s) => s.tasks);
  return (
    <div className="view">
      <div className="view-head">
        <span>任务</span>
        <button className="btn sec" disabled title="新建定时任务 M9 接入">
          ＋新建
        </button>
      </div>
      {tasks.length === 0 ? (
        <div className="muted empty-note">
          后台任务 / 定时任务面板在 M9 接入（工作流编排 + cron）。
        </div>
      ) : (
        tasks.map((t) => (
          <div key={t.id} className="session">
            <span className="muted">⠿</span>
            <span className="session-title">{t.name}</span>
            <span className={t.status === 'failed' ? 'bad' : 'muted'}>{t.status}</span>
          </div>
        ))
      )}
    </div>
  );
}
