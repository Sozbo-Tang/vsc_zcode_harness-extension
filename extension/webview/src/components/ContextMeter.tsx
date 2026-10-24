/**
 * ContextMeter 上下文格（PLAN §6.1 控制条 ◔42%）：
 * 占用比由 usage 事件 + 当前模型档案 contextLength 在 store 内计算。
 */
import { useStore } from '../store/store';

export function ContextMeter(): React.JSX.Element {
  const pct = useStore((s) => s.contextPct);
  const tokens = useStore((s) => s.usageTokens);
  return (
    <span
      className="ctxmeter"
      title={
        pct == null
          ? '上下文占用：等待 usage 事件 / 模型档案 contextLength'
          : `上下文占用 ${pct}%${tokens != null ? `（${tokens.toLocaleString()} tokens）` : ''}`
      }
    >
      ◔{pct == null ? '—' : `${pct}%`}
    </span>
  );
}
