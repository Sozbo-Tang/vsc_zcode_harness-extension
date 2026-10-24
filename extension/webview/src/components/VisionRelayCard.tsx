/**
 * VisionRelayCard 视觉中继卡（PLAN §6.1 / R9）：▸ 🖼 图片已由 … 转述 ▾，
 * 模型名 + 预设档名 + 转述全文可审计（折叠展开）。
 * M5 已接线：vision_relay 帧（main.py _prepare_images → vision_relay.relay_images
 * 每图一帧，入转录可回放）经 store.applyEvent 归约为 relay 条目，由 ChatView
 * 按事件流顺序渲染在消息流内。
 */
import { useT } from '../i18n';
import type { ChatItem } from '../store/store';

/** vision_relay.preset（vision_relay.py PRESET_IDS）→ 档名文案键 */
const PRESET_LABEL_KEYS: Record<string, string> = {
  ocr: 'presets.ocr',
  describe: 'presets.describe',
  frontend: 'presets.frontend',
  custom: 'presets.custom',
};

export function VisionRelayCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'relay' }>;
}): React.JSX.Element {
  const t = useT();
  const presetKey = PRESET_LABEL_KEYS[item.preset];
  const presetLabel = presetKey !== undefined ? t(presetKey) : item.preset;
  return (
    <details className="card relaycard">
      <summary>
        <span className="card-title">
          <span className="card-kicker">RELAY</span>
          <span>{t('relay.title', { preset: presetLabel })}</span>
        </span>
        <span className="card-side muted">
          <span className="relay-model" title={item.model}>{item.model}</span>
          <span className="card-chevron">▸</span>
        </span>
      </summary>
      <pre className="card-body">{item.description}</pre>
    </details>
  );
}
