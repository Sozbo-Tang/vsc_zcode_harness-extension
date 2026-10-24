/**
 * VisionRelayCard 视觉中继卡（PLAN §6.1 / R9：▸ 🖼 图片已由 … 转述 ▾，
 * 模型名+转述全文可审计）。M1 为占位实现：占位展示，中继链路 M5 接入。
 */
import type { ChatItem } from '../store/store';

export function VisionRelayCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'relay' }>;
}): React.JSX.Element {
  return (
    <details className="card relaycard">
      <summary>
        <span className="card-title">
          🖼 图片已由 {item.model} 转述（{item.preset}）
        </span>
        <span className="muted">审计 ▾</span>
      </summary>
      <pre className="card-body">{item.description}</pre>
    </details>
  );
}
