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

export function ToolCallCard({ item }: { item: Extract<ChatItem, { kind: 'tool' }> }): React.JSX.Element {
  const rpc = useHostRpc();
  const arg = mainArg(item.args);

  // 编辑跟随（占位）：点击即上报宿主；M3 前宿主未注册该方法，吞掉应答错误
  const onFollow = (): void => {
    const params: EditorFollowToolCallParams = {
      callId: item.callId,
      name: item.name,
      args: item.args,
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
      {item.result != null && item.result !== '' && (
        <pre className="card-body">{formatParam(item.result).slice(0, 2000)}</pre>
      )}
    </details>
  );
}
