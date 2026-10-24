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
import type { BackgroundTask, CronEntry } from '../protocol';

const TABS = ['运行中任务', '定时任务', '工作流'] as const;

/* ---------- 运行中任务页签 ---------- */

function fmtElapsed(from: number, to: number): string {
  const s = Math.max(0, Math.floor((to - from) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60}s`;
  return `${Math.floor(m / 60)}h${m % 60}m`;
}

const TASK_STATUS_META: Record<BackgroundTask['status'], { icon: string; cls: string; label: string }> = {
  running: { icon: '●', cls: 'run', label: '运行中' },
  done: { icon: '✓', cls: 'ok', label: '完成' },
  failed: { icon: '✗', cls: 'bad', label: '失败' },
  stopped: { icon: '⏹', cls: 'off', label: '已停止' },
};

function TaskCard({ task }: { task: BackgroundTask }): React.JSX.Element {
  const taskStopping = useStore((s) => s.taskStopping[task.id] ?? false);
  const stopTask = useStore((s) => s.stopTask);
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
        <span className={`taskdot ${meta.cls}`} role="img" aria-label={meta.label} title={meta.label}>
          {meta.icon}
        </span>
        <span className="taskname" title={task.command ?? task.name}>
          {task.name}
        </span>
        <span className="muted small taskmeta">
          {elapsed !== undefined && <span title="耗时">{elapsed}</span>}
          {task.exitCode !== undefined && (
            <span title="退出码" className={task.exitCode === 0 ? '' : 'bad'}>
              {' '}exit {task.exitCode}
            </span>
          )}
        </span>
        {task.status === 'running' && (
          <button
            className="iconbtn"
            title="停止任务（control task_stop）"
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
        <pre className="tasktail mono" title="尾部输出（滚动窗口 ≤4KB）">
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
  const runningCount = tasks.filter((t) => t.status === 'running').length;

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
          运行中任务 <span className="muted small">（{runningCount}/{tasks.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={refreshTasks}
          disabled={refreshing}
          title="拉取后台任务快照（control tasks_list）"
        >
          {refreshing ? '刷新中…' : '↻ 刷新'}
        </button>
      </div>
      {tasks.length === 0 ? (
        <div className="muted empty-note">
          {refreshing
            ? '正在获取后台任务…'
            : '没有后台任务。Agent 的 bash 后台命令 / cron 触发 / 工作流步骤会以任务卡出现在这里（帧实时归约；核心未实现 tasks_list 时此提示常驻——后端并行落地中）。'}
        </div>
      ) : (
        <ul className="bgtasklist">
          {tasks.map((t) => (
            <TaskCard key={t.id} task={t} />
          ))}
        </ul>
      )}
      <div className="muted small skillhint">
        任务卡由 task_started / task_output / task_finished 帧实时驱动；尾部输出滚动保留最新 4KB。
      </div>
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
      setFormError('调度表达式必填（如 "0 30 9 * * * *" = 每天 9:30）');
      return;
    }
    if (kind === 'prompt' && prompt.trim() === '') {
      setFormError('prompt 动作文本必填');
      return;
    }
    if (kind === 'command' && command.trim() === '') {
      setFormError('command 命令行必填');
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
          定时任务 <span className="muted small">（{crons.length}）</span>
        </span>
        <span className="headbtns">
          <button
            className="btn sec"
            onClick={refreshCrons}
            disabled={refreshing}
            title="拉取定时任务列表（control cron_list）"
          >
            {refreshing ? '刷新中…' : '↻ 刷新'}
          </button>
          <button
            className="btn sec"
            onClick={() => setFormOpen((v) => !v)}
            title="新建定时任务（control cron_add）"
          >
            ＋新建
          </button>
        </span>
      </div>

      {notice !== undefined && (
        <div className={notice.kind === 'error' ? 'mcpnotice bad' : 'mcpnotice ok'}>{notice.text}</div>
      )}

      {crons.length === 0 ? (
        <div className="muted empty-note">
          {refreshing
            ? '正在获取定时任务…'
            : '还没有定时任务。用「＋新建」添加（schedule + prompt 文本 或 command 命令行；核心未实现 cron_* 时会提示错误——后端并行落地中）。'}
        </div>
      ) : (
        <ul className="cronlist">
          {crons.map((c) => (
            <li key={c.name} className="cronitem">
              <div className="cronrow">
                <button
                  className={c.enabled ? 'crondot on' : 'crondot'}
                  title={c.enabled ? '已启用（点击停用 → cron_toggle）' : '已停用（点击启用 → cron_toggle）'}
                  disabled={pending !== undefined}
                  onClick={() => toggleCron(c.name, !c.enabled)}
                >
                  {c.enabled ? '●' : '○'}
                </button>
                <span className="cronname" title={c.name}>
                  {c.name}
                </span>
                <span className="cronbadge" title="动作类型">
                  {cronKindLabel(c)}
                </span>
                <button
                  className="iconbtn"
                  title="删除（control cron_remove）"
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
            <div className="form-title">新建定时任务</div>
            <div className="form-row">
              <label htmlFor="cron-schedule">调度</label>
              <input
                id="cron-schedule"
                className="in grow mono"
                value={schedule}
                placeholder="0 30 9 * * * *（7 段 cron：秒 分 时 日 月 星期 年）"
                title="7 段 cron 表达式（CloudBase/Quartz 风格）；描述文本由核心宽容解析"
                onChange={(e) => setSchedule(e.target.value)}
              />
            </div>
            <div className="form-row">
              <label htmlFor="cron-kind">类型</label>
              <select
                id="cron-kind"
                className="sel"
                value={kind}
                onChange={(e) => setKind(e.target.value as 'prompt' | 'command')}
                title="prompt = 到点把文本作为用户消息发给 Agent；command = 到点直接执行命令行"
              >
                <option value="prompt">prompt（发给 Agent）</option>
                <option value="command">command（执行命令）</option>
              </select>
            </div>
            {kind === 'prompt' ? (
              <div className="form-row top">
                <label htmlFor="cron-prompt">动作</label>
                <textarea
                  id="cron-prompt"
                  className="in grow"
                  rows={2}
                  value={prompt}
                  placeholder="到点发给 Agent 的提示词，如「拉取主干并汇总今日提交」"
                  onChange={(e) => setPrompt(e.target.value)}
                />
              </div>
            ) : (
              <div className="form-row">
                <label htmlFor="cron-cmd">动作</label>
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
                取消
              </button>
              <button
                className="btn"
                onClick={submitAdd}
                disabled={pending !== undefined}
                title="发 control cron_add（成功后自动回拉列表）"
              >
                {pending === 'add' ? '创建中…' : '创建'}
              </button>
            </div>
            <div className="muted small">
              载荷双通道（schedule/prompt/command 专用字段 + query JSON 镜像），Python 侧定稿前从任一通道取参。
            </div>
          </div>
        ) : null}
      </div>

      <div className="muted small skillhint">
        到点动作：prompt 型在活跃会话发用户消息；command 型由核心执行（危险命令仍走权限黑名单）。
      </div>
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

  // 首开自动拉取一次（幂等：workflowsRequested 标记）
  useEffect(() => {
    if (!requested) refreshWorkflows();
  }, [requested, refreshWorkflows]);

  return (
    <div className="skillspanel">
      <div className="skillhead">
        <span>
          工作流 <span className="muted small">（{workflows.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={refreshWorkflows}
          disabled={refreshing}
          title="拉取工作流列表（control workflows_list）"
        >
          {refreshing ? '刷新中…' : '↻ 刷新'}
        </button>
      </div>

      {notice !== undefined && (
        <div className={notice.kind === 'error' ? 'mcpnotice bad' : 'mcpnotice ok'}>{notice.text}</div>
      )}

      {workflows.length === 0 ? (
        <div className="muted empty-note">
          {refreshing
            ? '正在获取工作流列表…'
            : '还没有工作流。工作流由 Python 核心编排（脚本/声明式定义，核心未实现 workflows_list 时此提示常驻——后端并行落地中）。'}
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
                    title="运行工作流（control workflow_run；运行态沿任务帧/工具卡显示，报告到达后此处展示）"
                    disabled={isRunning || pendingRun !== undefined}
                    onClick={() => runWorkflow(w.name)}
                  >
                    {isRunning ? '⏳ 运行中…' : '▶ 运行'}
                  </button>
                </div>
                {w.description !== undefined && w.description !== '' && (
                  <div className="muted small wfdesc">{w.description}</div>
                )}
                {run !== undefined && (
                  <div className={`mcpnotice ${run.status === 'failed' ? 'bad' : 'ok'}`} title={`at ${new Date(run.at).toLocaleTimeString()}`}>
                    {run.status === 'running' ? '运行中…' : run.status === 'done' ? '已运行' : '运行失败'}
                  </div>
                )}
                {report !== undefined && (
                  <details className="wfreport">
                    <summary>运行报告</summary>
                    <pre className="mono wfreport-body">{report}</pre>
                  </details>
                )}
              </li>
            );
          })}
        </ul>
      )}
      <div className="muted small skillhint">
        运行是异步的：等待期间可切回聊天视图看工具卡与任务帧进度；完成后此处展开报告（JSON 宽容渲染）。
      </div>
    </div>
  );
}

/* ---------- 视图 ---------- */

export function TasksView(): React.JSX.Element {
  const [tab, setTab] = useState<(typeof TABS)[number]>('运行中任务');
  return (
    <div className="view">
      <div className="tabrow">
        {TABS.map((t) => (
          <button
            key={t}
            className={tab === t ? 'tab active' : 'tab'}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === '运行中任务' ? (
        <RunningTasksTab />
      ) : tab === '定时任务' ? (
        <CronTab />
      ) : (
        <WorkflowTab />
      )}
    </div>
  );
}
