/**
 * TasksView 任务视图（PLAN §6.5，M9 完整落地：后台任务 / 定时任务 / 工作流）。
 *
 *  - 运行中任务：task_started/output/finished 帧实时归约的任务卡（名称/
 *    命令/状态/耗时/尾部输出滚动 ≤4KB/⏹停止 → control task_stop）；
 *    首开发 control tasks_list 拉存量（result.detail JSON 宽容解析）。
 *  - 定时任务：cron_list 列表（schedule/action/enabled）、新建表单
 *    （schedule + prompt 文本 或 command + 类型选择 → cron_add）、
 *    删除/启停 → cron_remove / cron_toggle（乐观翻转 + 成功回拉纠正）。
 *  - 工作流：workflows_list 列表 + ▶运行 → control workflow_run；
 *    运行态沿 workflowRuns 切片（单飞对账锚）显示，报告 result.detail
 *    JSON 宽容渲染（JSON 美化，非 JSON 原文直显）。
 *
 * 数据全经 result.detail JSON 宽容解析（Python 侧并行定稿：数组或
 * {items:[...]} 都收，字段兼容多命名——见 store parseTasksDetail 等）；
 * 核心未识别命令时回 error 帧，页签 busy 立即解锁并显示提示（宽容消化）。
 */
import { useEffect, useState } from 'react';
import { useStore } from '../store/store';
import { useT } from '../i18n';
import type { BackgroundTask, CronEntry } from '../protocol';

/** 页签 id 稳定（与语言无关）；标签经 t() 渲染期解析（R20 i18n） */
const TABS = ['running', 'cron', 'workflow'] as const;
type TasksTabId = (typeof TABS)[number];
const TAB_LABEL_KEYS: Record<TasksTabId, string> = {
  running: 'tasks.tab.running',
  cron: 'tasks.tab.cron',
  workflow: 'tasks.tab.workflow',
};

/* ---------- 运行中任务页签 ---------- */

function fmtElapsed(from: number, to: number): string {
  const s = Math.max(0, Math.floor((to - from) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

/** 状态元数据（labelKey 经 t() 渲染期解析） */
const TASK_STATUS_META: Record<BackgroundTask['status'], { icon: string; cls: string; labelKey: string }> = {
  running: { icon: '●', cls: 'run', labelKey: 'tasks.statusRunning' },
  done: { icon: '✓', cls: 'ok', labelKey: 'tasks.statusDone' },
  failed: { icon: '✗', cls: 'bad', labelKey: 'tasks.statusFailed' },
  stopped: { icon: '⏹', cls: 'off', labelKey: 'tasks.statusStopped' },
};

function TaskCard({ task }: { task: BackgroundTask }): React.JSX.Element {
  const taskStopping = useStore((s) => s.taskStopping[task.id] ?? false);
  const stopTask = useStore((s) => s.stopTask);
  const t = useT();
  // 运行中任务的耗时需要秒级走表：由页签统一 tick（见 RunningTasksTab），
  // 卡片只按 tick 重渲染时的 Date.now() 计算
  const tick = useStore((s) => s.taskTick);
  const meta = TASK_STATUS_META[task.status];
  const elapsed =
    task.startedAt !== undefined
      ? fmtElapsed(task.startedAt, task.endedAt ?? (tick ?? Date.now()))
      : undefined;

  return (
    <li className="taskcard">
      <div className="taskrow">
        <span className={`taskdot ${meta.cls}`} role="img" aria-label={t(meta.labelKey)} title={t(meta.labelKey)}>
          {meta.icon}
        </span>
        <span className="taskname" title={task.command ?? task.name}>
          {task.name}
        </span>
        <span className="muted small taskmeta">
          {elapsed !== undefined && <span title={t('tasks.elapsed')}>{elapsed}</span>}
          {task.exitCode !== undefined && (
            <span title={t('tasks.exitCode')} className={task.exitCode === 0 ? '' : 'bad'}>
              {' '}exit {task.exitCode}
            </span>
          )}
        </span>
        {task.status === 'running' && (
          <button
            className="iconbtn"
            title={t('tasks.stopTitle')}
            disabled={taskStopping}
            onClick={() => stopTask(task.id)}
          >
            {taskStopping ? '…' : '⏹'}
          </button>
        )}
      </div>
      {task.command !== undefined && task.command !== '' && task.command !== task.name && (
        <div className="taskcmd mono muted" title={task.command}>
          {task.command}
        </div>
      )}
      {task.tail !== undefined && task.tail !== '' && (
        <pre className="tasktail mono" title={t('tasks.tailTitle')}>
          {task.tail}
        </pre>
      )}
      {task.detail !== undefined && task.detail !== '' && (
        <div className="muted small" title={task.detail}>
          {task.detail}
        </div>
      )}
    </li>
  );
}

function RunningTasksTab(): React.JSX.Element {
  const tasks = useStore((s) => s.tasks);
  const refreshing = useStore((s) => s.tasksRefreshing);
  const requested = useStore((s) => s.tasksRequested);
  const refreshTasks = useStore((s) => s.refreshTasks);
  const t = useT();
  const runningCount = tasks.filter((tk) => tk.status === 'running').length;

  // 首开自动拉取一次（幂等：tasksRequested 标记）
  useEffect(() => {
    if (!requested) refreshTasks();
  }, [requested, refreshTasks]);

  // 有运行中任务时秒级走表（驱动耗时显示重算）
  useEffect(() => {
    if (runningCount === 0) return;
    useStore.setState({ taskTick: Date.now() });
    const h = window.setInterval(() => useStore.setState({ taskTick: Date.now() }), 1_000);
    return () => window.clearInterval(h);
  }, [runningCount]);

  return (
    <div className="skillspanel">
      <div className="skillhead">
        <span>
          {t('tasks.tab.running')} <span className="muted small">（{runningCount}/{tasks.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={refreshTasks}
          disabled={refreshing}
          title={t('tasks.refreshTitle')}
        >
          {refreshing ? t('tools.refreshing') : t('tools.refresh')}
        </button>
      </div>
      {tasks.length === 0 ? (
        <div className="muted empty-note">
          {refreshing ? t('tasks.loading') : t('tasks.empty')}
        </div>
      ) : (
        <ul className="bgtasklist">
          {tasks.map((tk) => (
            <TaskCard key={tk.id} task={tk} />
          ))}
        </ul>
      )}
      <div className="muted small skillhint">{t('tasks.hint')}</div>
    </div>
  );
}

/* ---------- 定时任务页签 ---------- */

/** cron.action 动作类型的展示推断（kind 宽容缺省时按内容猜，猜不出 → '—'） */
function cronKindLabel(c: CronEntry): string {
  if (c.kind === 'prompt' || c.kind === 'command') return c.kind;
  const a = c.action ?? '';
  if (a === '') return '—';
  // 命令行启发：首 token 是常见可执行形态（字母数字+.sh/.py/含路径）
  return /^[A-Za-z0-9_.\-/]+\s+\S/.test(a) ? 'command' : 'prompt';
}

function CronTab(): React.JSX.Element {
  const crons = useStore((s) => s.crons);
  const refreshing = useStore((s) => s.cronsRefreshing);
  const requested = useStore((s) => s.cronsRequested);
  const pending = useStore((s) => s.cronPendingAction);
  const notice = useStore((s) => s.cronNotice);
  const refreshCrons = useStore((s) => s.refreshCrons);
  const addCron = useStore((s) => s.addCron);
  const removeCron = useStore((s) => s.removeCron);
  const toggleCron = useStore((s) => s.toggleCron);
  const t = useT();

  const [formOpen, setFormOpen] = useState(false);
  const [schedule, setSchedule] = useState('');
  const [kind, setKind] = useState<'prompt' | 'command'>('prompt');
  const [prompt, setPrompt] = useState('');
  const [command, setCommand] = useState('');
  const [formError, setFormError] = useState<string | undefined>();

  // 首开自动拉取一次（幂等：cronsRequested 标记）
  useEffect(() => {
    if (!requested) refreshCrons();
  }, [requested, refreshCrons]);

  const submitAdd = (): void => {
    const sch = schedule.trim();
    if (sch === '') {
      setFormError(t('tasks.cronScheduleRequired'));
      return;
    }
    if (kind === 'prompt' && prompt.trim() === '') {
      setFormError(t('tasks.cronPromptRequired'));
      return;
    }
    if (kind === 'command' && command.trim() === '') {
      setFormError(t('tasks.cronCommandRequired'));
      return;
    }
    setFormError(undefined);
    addCron({
      schedule: sch,
      prompt: kind === 'prompt' ? prompt.trim() : undefined,
      command: kind === 'command' ? command.trim() : undefined,
    });
    setSchedule('');
    setPrompt('');
    setCommand('');
    setFormOpen(false);
  };

  return (
    <div className="skillspanel">
      <div className="skillhead">
        <span>
          {t('tasks.tab.cron')} <span className="muted small">（{crons.length}）</span>
        </span>
        <span className="headbtns">
          <button
            className="btn sec"
            onClick={refreshCrons}
            disabled={refreshing}
            title={t('tasks.cronRefreshTitle')}
          >
            {refreshing ? t('tools.refreshing') : t('tools.refresh')}
          </button>
          <button
            className="btn sec"
            onClick={() => setFormOpen((v) => !v)}
            title={t('tasks.cronAddTitle')}
          >
            {t('tasks.cronNewBtn')}
          </button>
        </span>
      </div>

      {notice !== undefined && (
        <div className={notice.kind === 'error' ? 'mcpnotice bad' : 'mcpnotice ok'}>{notice.text}</div>
      )}

      {crons.length === 0 ? (
        <div className="muted empty-note">
          {refreshing ? t('tasks.cronLoading') : t('tasks.cronEmpty')}
        </div>
      ) : (
        <ul className="cronlist">
          {crons.map((c) => (
            <li key={c.name} className="cronitem">
              <div className="cronrow">
                <button
                  className={c.enabled ? 'crondot on' : 'crondot'}
                  title={c.enabled ? t('tasks.cronEnabledTitle') : t('tasks.cronDisabledTitle')}
                  disabled={pending !== undefined}
                  onClick={() => toggleCron(c.name, !c.enabled)}
                >
                  {c.enabled ? '●' : '○'}
                </button>
                <span className="cronname" title={c.name}>
                  {c.name}
                </span>
                <span className="cronbadge" title={t('tasks.cronKindTitle')}>
                  {cronKindLabel(c)}
                </span>
                <button
                  className="iconbtn"
                  title={t('tasks.cronDeleteTitle')}
                  disabled={pending !== undefined}
                  onClick={() => removeCron(c.name)}
                >
                  🗑
                </button>
              </div>
              <div className="cronsched mono" title={c.action ?? undefined}>
                {c.schedule}
                {c.action !== undefined && c.action !== '' && (
                  <span className="muted"> · {c.action.length > 80 ? `${c.action.slice(0, 80)}…` : c.action}</span>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="mcpformrow">
        {formOpen ? (
          <div className="form">
            <div className="form-title">{t('tasks.cronFormTitle')}</div>
            <div className="form-row">
              <label htmlFor="cron-schedule">{t('tasks.cronScheduleLabel')}</label>
              <input
                id="cron-schedule"
                className="in grow mono"
                value={schedule}
                placeholder={t('tasks.cronSchedulePlaceholder')}
                title={t('tasks.cronScheduleTitle')}
                onChange={(e) => setSchedule(e.target.value)}
              />
            </div>
            <div className="form-row">
              <label htmlFor="cron-kind">{t('tasks.cronKindLabel')}</label>
              <select
                id="cron-kind"
                className="sel"
                value={kind}
                onChange={(e) => setKind(e.target.value as 'prompt' | 'command')}
                title={t('tasks.cronKindTitle2')}
              >
                <option value="prompt">{t('tasks.cronKindPrompt')}</option>
                <option value="command">{t('tasks.cronKindCommand')}</option>
              </select>
            </div>
            {kind === 'prompt' ? (
              <div className="form-row top">
                <label htmlFor="cron-prompt">{t('tasks.cronActionLabel')}</label>
                <textarea
                  id="cron-prompt"
                  className="in grow"
                  rows={2}
                  value={prompt}
                  placeholder={t('tasks.cronPromptPlaceholder')}
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </div>
            ) : (
              <div className="form-row">
                <label htmlFor="cron-cmd">{t('tasks.cronActionLabel')}</label>
                <input
                  id="cron-cmd"
                  className="in grow mono"
                  value={command}
                  placeholder="npm run report -- --daily"
                  onChange={(e) => setCommand(e.target.value)}
                />
              </div>
            )}
            {formError !== undefined && <div className="bad small">{formError}</div>}
            <div className="form-row end">
              <button className="btn sec" onClick={() => setFormOpen(false)}>
                {t('sessions.cancel')}
              </button>
              <button
                className="btn"
                onClick={submitAdd}
                disabled={pending !== undefined}
                title={t('tasks.cronAddBtnTitle')}
              >
                {pending === 'add' ? t('tasks.cronCreating') : t('tasks.cronCreateBtn')}
              </button>
            </div>
            <div className="muted small">{t('tasks.cronPayloadHint')}</div>
          </div>
        ) : null}
      </div>

      <div className="muted small skillhint">{t('tasks.cronHint')}</div>
    </div>
  );
}

/* ---------- 工作流页签 ---------- */

/** result.detail 宽容渲染：JSON 则美化，非 JSON 原文直显 */
function renderReport(report: string | undefined): string | undefined {
  if (report === undefined || report === '') return undefined;
  try {
    return JSON.stringify(JSON.parse(report), null, 2);
  } catch {
    return report;
  }
}

function WorkflowTab(): React.JSX.Element {
  const workflows = useStore((s) => s.workflows);
  const refreshing = useStore((s) => s.workflowsRefreshing);
  const requested = useStore((s) => s.workflowsRequested);
  const runs = useStore((s) => s.workflowRuns);
  const pendingRun = useStore((s) => s.pendingWorkflowRun);
  const notice = useStore((s) => s.workflowNotice);
  const refreshWorkflows = useStore((s) => s.refreshWorkflows);
  const runWorkflow = useStore((s) => s.runWorkflow);
  const t = useT();

  // 首开自动拉取一次（幂等：workflowsRequested 标记）
  useEffect(() => {
    if (!requested) refreshWorkflows();
  }, [requested, refreshWorkflows]);

  return (
    <div className="skillspanel">
      <div className="skillhead">
        <span>
          {t('tasks.tab.workflow')} <span className="muted small">（{workflows.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={refreshWorkflows}
          disabled={refreshing}
          title={t('tasks.wfRefreshTitle')}
        >
          {refreshing ? t('tools.refreshing') : t('tools.refresh')}
        </button>
      </div>

      {notice !== undefined && (
        <div className={notice.kind === 'error' ? 'mcpnotice bad' : 'mcpnotice ok'}>{notice.text}</div>
      )}

      {workflows.length === 0 ? (
        <div className="muted empty-note">
          {refreshing ? t('tasks.wfLoading') : t('tasks.wfEmpty')}
        </div>
      ) : (
        <ul className="wflist">
          {workflows.map((w) => {
            const run = runs[w.name];
            const isRunning = run?.status === 'running' || pendingRun === w.name;
            const report = renderReport(run?.report);
            return (
              <li key={w.name} className="wfitem">
                <div className="wfrow">
                  <span className="wfname" title={w.path ?? w.name}>
                    {w.name}
                  </span>
                  <button
                    className="btn sec wfrunbtn"
                    title={t('tasks.wfRunTitle')}
                    disabled={isRunning || pendingRun !== undefined}
                    onClick={() => runWorkflow(w.name)}
                  >
                    {isRunning ? t('tasks.wfRunningBtn') : t('tasks.wfRunBtn')}
                  </button>
                </div>
                {w.description !== undefined && w.description !== '' && (
                  <div className="muted small wfdesc">{w.description}</div>
                )}
                {run !== undefined && (
                  <div className={`mcpnotice ${run.status === 'failed' ? 'bad' : 'ok'}`} title={`at ${new Date(run.at).toLocaleTimeString()}`}>
                    {run.status === 'running'
                      ? t('tasks.wfRunning')
                      : run.status === 'done'
                        ? t('tasks.wfDone')
                        : t('tasks.wfFailed')}
                  </div>
                )}
                {report !== undefined && (
                  <details className="wfreport">
                    <summary>{t('tasks.wfReport')}</summary>
                    <pre className="mono wfreport-body">{report}</pre>
                  </details>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <div className="muted small skillhint">{t('tasks.wfHint')}</div>
    </div>
  );
}

/* ---------- 视图 ---------- */

export function TasksView(): React.JSX.Element {
  const t = useT();
  const [tab, setTab] = useState<TasksTabId>('running');
  return (
    <div className="view">
      <div className="tabrow">
        {TABS.map((tabId) => (
          <button
            key={tabId}
            className={tab === tabId ? 'tab active' : 'tab'}
            onClick={() => setTab(tabId)}
          >
            {t(TAB_LABEL_KEYS[tabId])}
          </button>
        ))}
      </div>
      {tab === 'running' ? <RunningTasksTab /> : tab === 'cron' ? <CronTab /> : <WorkflowTab />}
    </div>
  );
}
