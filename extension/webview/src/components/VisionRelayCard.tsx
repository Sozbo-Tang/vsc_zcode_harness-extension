/**
 * VisionRelayCard 视觉中继卡（PLAN §6.1 / R9）：▸ 🖼 图片已由 … 转述 ▾，
 * 模型名 + 预设档名 + 转述全文可审计（折叠展开）。
 * M5 已接线：vision_relay 帧（main.py _prepare_images → vision_relay.relay_images
 * 每图一帧，入转录可回放）经 store.applyEvent 归约为 relay 条目，由 ChatView
 * 按事件流顺序渲染在消息流内。
 */
import type { ChatItem } from '../store/store';

/** vision_relay.preset（vision_relay.py PRESET_IDS）→ 档名展示 */
const PRESET_LABELS: Record<string, string> = {
  ocr: 'OCR 提取',
  describe: '通用描述',
  frontend: '前端还原',
  custom: '自定义',
};

export function VisionRelayCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'relay' }>;
}): React.JSX.Element {
  const presetLabel = PRESET_LABELS[item.preset] ?? item.preset;
  return (
    <details className="card relaycard">
      <summary>
        <span className="card-title">
          🖼 图片已由 {item.model} 转述（{presetLabel}）
        </span>
        <span className="muted">审计 ▾</span>
      </summary>
      <pre className="card-body">{item.description}</pre>
    </details>
  );
}
