/**
 * ContextMeter 上下文格（PLAN §6.1 控制条 ◔42%）：
 * 占用比由 usage 事件 + 当前模型档案 contextLength 在 store 内计算；
 * M8 起 L2/L3 compaction 帧到达时按 report.afterTokens 校正（压缩后
 * 占用回落，lastCompaction 记录校正来源，title 中标注）。
 */
import { useStore } from '../store/store';

export function ContextMeter(): React.JSX.Element {
  const pct = useStore((s) => s.contextPct);
  const tokens = useStore((s) => s.usageTokens);
  const lastCompaction = useStore((s) => s.lastCompaction);
  const compHint =
    lastCompaction !== undefined
      ? `（${lastCompaction.layer} 压缩后校正${
          lastCompaction.afterTokens != null ? `：${lastCompaction.afterTokens.toLocaleString()} tokens` : ''
        }）`
      : '';
  return (
    <span
      className="ctxmeter"
      title={
        pct == null
          ? '上下文占用：等待 usage 事件 / 模型档案 contextLength'
          : `上下文占用 ${pct}%${tokens != null ? `（${tokens.toLocaleString()} tokens）` : ''}${compHint}`
      }
    >
      ◔{pct == null ? '—' : `${pct}%`}
    </span>
  );
}
