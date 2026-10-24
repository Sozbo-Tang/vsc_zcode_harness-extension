/**
 * ToolCallCard 工具卡（PLAN §6.1 线框：▸ 🔨 read src/auth.py ✓ 34ms）。
 *  - 标题行：工具名 + 主参数；右侧状态（⋯ 运行中 / ✓ 完成）+ 耗时
 *  - 结果折叠：details 展开 card-body（超长截断 2000 字符）
 *  - 点击标题行 → host RPC `editor.followToolCall`（编辑跟随，R10）：
 *    占位消息约定（EditorFollowToolCallParams ↔ 宿主 protocol.ts
 *    RPC_EDITOR_FOLLOW_TOOL_CALL）。宿主 M3 注册处理器前会应答
 *    unknown_method，这里静默吞错 —— 注册后同一点击即刻生效。
 */
import { useHostRpc } from '../host/useHostRpc';
import type { ChatItem } from '../store/store';
import { formatParam } from '../store/store';
import type { EditorFollowToolCallParams } from '../protocol';

function mainArg(args: Record<string, unknown>): string {
  for (const k of ['file_path', 'path', 'command', 'pattern', 'url', 'query']) {
    const v = args?.[k];
    if (typeof v === 'string' && v) return v;
  }
  const first = Object.values(args ?? {})[0];
  return typeof first === 'string' ? first : '';
}

function fmtElapsed(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/**
 * M6（R8）browser_* 操作时间线：finished 帧的 result.timeline → 有序步骤
 * 列表。tools/browser.py（并行实现中）载荷形状未定稿——宽容解析：
 * result 可为对象或 JSON 字符串；timeline 为字符串数组或对象数组
 * （对象取 description/action/step/summary 字段，兜底拼接/JSON 全文）。
 * 无 timeline（失败/旧核心）→ 返回 null，按普通结果渲染（照旧）。
 */
function timelineStepText(step: unknown): string {
  if (typeof step === 'string') return step;
  if (step !== null && typeof step === 'object') {
    const o = step as Record<string, unknown>;
    for (const k of ['description', 'action', 'step', 'summary', 'detail']) {
      const v = o[k];
      if (typeof v === 'string' && v !== '') return v;
    }
    const parts: string[] = [];
    for (const k of ['action', 'url', 'target', 'selector', 'text', 'status']) {
      const v = o[k];
      if (typeof v === 'string' && v !== '') parts.push(v);
    }
    if (parts.length > 0) return parts.join(' ');
    try {
      return JSON.stringify(step);
    } catch {
      return String(step);
    }
  }
  return '';
}

function parseTimeline(result: unknown): string[] | null {
  let r: unknown = result;
  if (typeof r === 'string') {
    try {
      r = JSON.parse(r);
    } catch {
      return null;
    }
  }
  if (r === null || typeof r !== 'object') return null;
  const tl = (r as Record<string, unknown>).timeline;
  if (!Array.isArray(tl)) return null;
  const steps = tl.map(timelineStepText).filter((s) => s !== '');
  return steps.length > 0 ? steps : null;
}

/**
 * M8（R31）notebook 工具（notebook_run 等）结果 outputs → 输出摘要列表，
 * 沿 timeline 的多形状渲染模式。宽容：result 可为对象或 JSON 字符串；
 * outputs 条目可为字符串或 {text, ename[, evalue]} 对象（ename 采到即
 * 标红）。无 outputs（失败/旧核心）→ null，按普通结果渲染（照旧）。
 */
interface NotebookOutput {
  text: string;
  error: boolean;
}

function parseNotebookOutputs(result: unknown): NotebookOutput[] | null {
  let r: unknown = result;
  if (typeof r === 'string') {
    try {
      r = JSON.parse(r);
    } catch {
      return null;
    }
  }
  if (r === null || typeof r !== 'object') return null;
  const outputs = (r as Record<string, unknown>).outputs;
  if (!Array.isArray(outputs)) return null;
  const out: NotebookOutput[] = [];
  for (const o of outputs) {
    if (typeof o === 'string') {
      if (o !== '') out.push({ text: o, error: false });
      continue;
    }
    if (o === null || typeof o !== 'object') continue;
    const oo = o as Record<string, unknown>;
    const ename = typeof oo.ename === 'string' && oo.ename !== '' ? oo.ename : undefined;
    const parts: string[] = [];
    if (ename !== undefined) parts.push(ename);
    if (typeof oo.evalue === 'string' && oo.evalue !== '') parts.push(oo.evalue);
    for (const k of ['text', 'output', 'data', 'value']) {
      const v = oo[k];
      if (typeof v === 'string' && v !== '') {
        parts.push(v);
      } else if (v !== null && typeof v === 'object') {
        try {
          const s = JSON.stringify(v);
          if (s !== undefined && s !== '{}' && s !== '[]') parts.push(s);
        } catch {
          /* 忽略不可序列化字段 */
        }
      }
    }
    if (parts.length > 0) out.push({ text: parts.join(': '), error: ename !== undefined });
  }
  return out.length > 0 ? out : null;
}

export function ToolCallCard({ item }: { item: Extract<ChatItem, { kind: 'tool' }> }): React.JSX.Element {
  const rpc = useHostRpc();
  const arg = mainArg(item.args);
  // M6：browser_* 完成帧带操作时间线 → 有序步骤列表；失败/无时间线照旧
  const timeline =
    item.state === 'finished' && item.name.toLowerCase().startsWith('browser_')
      ? parseTimeline(item.result)
      : null;
  // M8（R31）：notebook 工具完成帧带 outputs → 输出摘要（ename 标红）；
  // 无 outputs 照旧。工具名宽容匹配 notebook 前缀/包含（notebook_run 等）
  const notebookOutputs =
    item.state === 'finished' && item.name.toLowerCase().includes('notebook')
      ? parseNotebookOutputs(item.result)
      : null;

  // 编辑跟随：点击即上报宿主（M3 已注册：按工具类型跳转文件行；
  // grep 的行号在 result 里，一并直通）
  const onFollow = (): void => {
    const params: EditorFollowToolCallParams = {
      callId: item.callId,
      name: item.name,
      args: item.args,
      result: item.result,
    };
    void rpc.request('editor.followToolCall', params).catch(() => undefined);
  };

  return (
    <details className="card toolcard">
      <summary
        title="点击跳转文件行（编辑跟随，M3 编辑器桥接入后生效）"
        onClick={onFollow}
      >
        <span className="card-title">
          🔨 {item.name} {arg.length > 52 ? `${arg.slice(0, 52)}…` : arg}
        </span>
        <span className="card-side">
          {item.state === 'finished' ? (
            <span className="ok">✓</span>
          ) : (
            <span className="muted">{item.state === 'update' ? '⟳' : '⋯'}</span>
          )}
          {item.elapsedMs != null && <span className="muted"> {fmtElapsed(item.elapsedMs)}</span>}
          <span className="muted" title="编辑跟随（M3）"> ↗</span>
        </span>
      </summary>
      {item.warning && (
        <div className="privacy-warning">⚠ {item.warning}</div>
      )}
      {timeline !== null ? (
        <ol className="timeline">
          {timeline.map((step, i) => (
            <li key={`${i}-${step.slice(0, 24)}`}>{step}</li>
          ))}
        </ol>
      ) : notebookOutputs !== null ? (
        <ol className="timeline">
          {notebookOutputs.map((o, i) => (
            <li key={`${i}-${o.text.slice(0, 24)}`} className={o.error ? 'bad' : undefined}>
              {o.error ? '⛔ ' : ''}
              {o.text}
            </li>
          ))}
        </ol>
      ) : (
        item.result != null &&
        item.result !== '' && (
          <pre className="card-body">{formatParam(item.result).slice(0, 2000)}</pre>
        )
      )}
    </details>
  );
}
