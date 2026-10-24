/**
 * SubagentCard 子代理卡（PLAN §6.1：▸ 🤖 子代理·代码搜索 运行中；M7 实装）。
 *
 * - 识别：store.isSubagentTool（run_child/task/agent；M7 Python 侧工具名
 *   run_child）。标题行 = 任务摘要（args.description 优先，回退 args.prompt
 *   首行）+ 运行状态/耗时（风格沿 ToolCallCard）；
 * - args.model/modelId 指定子代理模型时显示 @model 徽标（R25 便宜模型调研）；
 * - 运行中默认展开显示完整任务 prompt；完成后收起——展开可见「任务」+
 *   「最终结果」两块（结果载荷形状未定稿：string 直读，对象宽容取
 *   result/summary/output/text 字段，兜底 JSON 全文）。
 */
import type { ChatItem } from '../store/store';

function fmtElapsed(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** run_child 入参宽容取值（Python 侧 subagents.py 并行实现中，字段以宽容为先） */
function argString(args: Record<string, unknown>, keys: string[]): string {
  for (const k of keys) {
    const v = args?.[k];
    if (typeof v === 'string' && v !== '') return v;
  }
  return '';
}

function firstLine(s: string): string {
  const line = s.split('\n', 1)[0] ?? '';
  return line.trim();
}

/** 子代理最终结果 → 展示文本（形状未定稿，多形状宽容） */
function resultText(result: unknown): string {
  if (result == null) return '';
  if (typeof result === 'string') return result;
  if (typeof result === 'object') {
    const o = result as Record<string, unknown>;
    for (const k of ['result', 'summary', 'output', 'text', 'final', 'finalResult']) {
      const v = o[k];
      if (typeof v === 'string' && v !== '') return v;
    }
    try {
      return JSON.stringify(result, null, 2);
    } catch {
      return String(result);
    }
  }
  return String(result);
}

export function SubagentCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'tool' }>;
}): React.JSX.Element {
  const description = argString(item.args, ['description', 'subagent_type', 'agent_type']);
  const prompt = argString(item.args, ['prompt', 'task', 'instruction', 'input']);
  const model = argString(item.args, ['model', 'modelId', 'model_id']);

  const label =
    description !== ''
      ? description
      : firstLine(prompt) !== ''
        ? firstLine(prompt)
        : item.name;
  const summaryLabel = label.length > 24 ? `${label.slice(0, 24)}…` : label;

  const running = item.state !== 'finished';
  const result = resultText(item.result);

  return (
    // 运行中默认展开（盯进度）；finished 后 open 变 false 收起——结果折叠展示
    <details className="card subcard" open={running || undefined}>
      <summary title={label}>
        <span className="card-title">
          🤖 子代理·{summaryLabel}
        </span>
        <span className="card-side">
          {model !== '' && <span className="muted" title="子代理模型（R25）"> @{model}</span>}
          {running ? (
            <span className="muted">{item.state === 'update' ? '⟳' : '运行中…'}</span>
          ) : (
            <span className="ok">✓</span>
          )}
          {item.elapsedMs != null && <span className="muted"> {fmtElapsed(item.elapsedMs)}</span>}
        </span>
      </summary>
      {prompt !== '' && (
        <>
          <div className="subcard-label muted small">任务</div>
          <pre className="card-body">{prompt.slice(0, 2000)}</pre>
        </>
      )}
      {!running && result !== '' && (
        <>
          <div className="subcard-label muted small">最终结果</div>
          <pre className="card-body">{result.slice(0, 4000)}</pre>
        </>
      )}
    </details>
  );
}
