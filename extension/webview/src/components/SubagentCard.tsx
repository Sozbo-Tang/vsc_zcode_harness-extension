/**
 * SubagentCard 子代理卡（PLAN §6.1：▸ 🤖 子代理·代码搜索 运行中）。
 * M1 为占位实现：由工具名（run_child/task/agent，M7 落地）识别渲染。
 */
import type { ChatItem } from '../store/store';
import { formatParam } from '../store/store';

export function SubagentCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'tool' }>;
}): React.JSX.Element {
  const desc =
    typeof item.args.description === 'string'
      ? item.args.description
      : typeof item.args.prompt === 'string'
        ? item.args.prompt
        : '';
  const running = item.state !== 'finished';
  return (
    <details className="card subcard">
      <summary>
        <span className="card-title">
          🤖 子代理·{desc.length > 24 ? `${desc.slice(0, 24)}…` : desc || item.name}
        </span>
        <span className="card-side">
          {running ? <span className="muted">运行中</span> : <span className="ok">✓</span>}
          {item.elapsedMs != null && <span className="muted"> {item.elapsedMs}ms</span>}
        </span>
      </summary>
      {item.result != null && item.result !== '' && (
        <pre className="card-body">{formatParam(item.result).slice(0, 2000)}</pre>
      )}
    </details>
  );
}
