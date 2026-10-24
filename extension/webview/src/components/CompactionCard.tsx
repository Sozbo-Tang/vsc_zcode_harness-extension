/**
 * CompactionCard 压缩报告卡（M8，R14 / PLAN §3.4）：
 * compaction{layer, report} 帧的 chat 投影（store 归约仅 L2/L3 产卡，
 * L4 冷存无卡）。字段由 store extractCompaction 宽容提取。
 *  - L2 机械淘汰：淘过 N 条工具输出（旧工具 result 换占位符）+ 省 savedTokens
 *  - L3 自动摘要：beforeTokens → afterTokens 对比 + 七要素摘要折叠查看
 *    （任务目标/关键决策/文件及状态/错误与解法/环境事实/待办/下一步）
 *  - 无结构化字段时兜底展示原始 report JSON（截断 2000 字符）
 */
import { useT } from '../i18n';
import type { ChatItem } from '../store/store';

function fmtTok(n: number): string {
  return n >= 10_000 ? `${(n / 1000).toFixed(1)}k` : n.toLocaleString();
}

export function CompactionCard({ item }: { item: Extract<ChatItem, { kind: 'compaction' }> }): React.JSX.Element {
  const t = useT();
  const stats: string[] = [];
  if (item.layer === 'L2' && item.evicted !== undefined) {
    stats.push(t('compaction.evicted', { n: item.evicted }));
  }
  if (item.beforeTokens !== undefined && item.afterTokens !== undefined) {
    const pct =
      item.beforeTokens > 0
        ? Math.max(0, Math.round((1 - item.afterTokens / item.beforeTokens) * 100))
        : 0;
    stats.push(
      t('compaction.tokens', {
        before: item.beforeTokens.toLocaleString(),
        after: item.afterTokens.toLocaleString(),
        pct,
      }),
    );
  }
  if (item.savedTokens !== undefined) {
    stats.push(t('compaction.saved', { n: fmtTok(item.savedTokens) }));
  }
  if (stats.length === 0) {
    stats.push(item.layer === 'L2' ? t('compaction.evictedFallback') : t('compaction.doneFallback'));
  }

  const rawText = (() => {
    try {
      return item.raw !== undefined ? JSON.stringify(item.raw, null, 2) : undefined;
    } catch {
      return undefined;
    }
  })();

  return (
    <details className="card compcard">
      <summary title={t('compaction.cardTitle')}>
        <span className="card-title">
          <span className="card-kicker">CONTEXT</span>
          <span className="comp-title">{t('compaction.title', { layer: item.layer, stats: stats.join(' · ') })}</span>
        </span>
        <span className="card-side muted"><span className="card-chevron">▸</span></span>
      </summary>
      {item.summary !== undefined && item.summary !== '' ? (
        <details className="compsummary">
          <summary>{t('compaction.summary')}</summary>
          <pre className="card-body">{item.summary}</pre>
        </details>
      ) : (
        rawText !== undefined && <pre className="card-body">{rawText.slice(0, 2000)}</pre>
      )}
      {item.summary !== undefined && item.summary !== '' && rawText !== undefined && (
        <details className="compraw">
          <summary className="muted">{t('compaction.raw')}</summary>
          <pre className="card-body">{rawText.slice(0, 2000)}</pre>
        </details>
      )}
    </details>
  );
}
