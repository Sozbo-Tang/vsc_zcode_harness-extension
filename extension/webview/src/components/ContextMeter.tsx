/**
 * ContextMeter 上下文格（PLAN §6.1 控制条 ◔42%）：
 * 占用比由 usage 事件 + 当前模型档案 contextLength 在 store 内计算；
 * M8 起 L2/L3 compaction 帧到达时按 report.afterTokens 校正（压缩后
 * 占用回落，lastCompaction 记录校正来源，title 中标注）。
 */
import { useStore } from '../store/store';
import { useT } from '../i18n';

export function ContextMeter(): React.JSX.Element {
  const t = useT();
  const pct = useStore((s) => s.contextPct);
  const tokens = useStore((s) => s.usageTokens);
  const lastCompaction = useStore((s) => s.lastCompaction);
  const compHint =
    lastCompaction !== undefined
      ? t('ctx.compHint', {
          layer: lastCompaction.layer,
          detail:
            lastCompaction.afterTokens != null
              ? t('ctx.compHintDetail', { n: lastCompaction.afterTokens.toLocaleString() })
              : '',
        })
      : '';
  return (
    <span
      className="ctxmeter"
      title={
        pct == null
          ? t('ctx.titleEmpty')
          : t('ctx.title', {
              pct,
              tokens: tokens != null ? t('ctx.titleTokens', { n: tokens.toLocaleString() }) : '',
            }) + compHint
      }
    >
      ◔{pct == null ? '—' : `${pct}%`}
    </span>
  );
}
