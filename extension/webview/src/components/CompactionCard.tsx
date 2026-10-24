/**
 * CompactionCard 压缩报告卡（M8，R14 / PLAN §3.4）：
 * compaction{layer, report} 帧的 chat 投影（store 归约仅 L2/L3 产卡，
 * L4 冷存无卡）。字段由 store extractCompaction 宽容提取。
 *  - L2 机械淘汰：淘过 N 条工具输出（旧工具 result 换占位符）+ 省 savedTokens
 *  - L3 自动摘要：beforeTokens → afterTokens 对比 + 七要素摘要折叠查看
 *    （任务目标/关键决策/文件及状态/错误与解法/环境事实/待办/下一步）
 *  - 无结构化字段时兜底展示原始 report JSON（截断 2000 字符）
 */
import type { ChatItem } from '../store/store';

function fmtTok(n: number): string {
  return n >= 10_000 ? `${(n / 1000).toFixed(1)}k` : n.toLocaleString();
}

export function CompactionCard({ item }: { item: Extract<ChatItem, { kind: 'compaction' }> }): React.JSX.Element {
  const stats: string[] = [];
  if (item.layer === 'L2' && item.evicted !== undefined) {
    stats.push(`淘过 ${item.evicted} 条工具输出`);
  }
  if (item.beforeTokens !== undefined && item.afterTokens !== undefined) {
    const pct =
      item.beforeTokens > 0
        ? Math.max(0, Math.round((1 - item.afterTokens / item.beforeTokens) * 100))
        : 0;
    stats.push(
      `${item.beforeTokens.toLocaleString()} → ${item.afterTokens.toLocaleString()} tokens（省 ${pct}%）`,
    );
  }
  if (item.savedTokens !== undefined) {
    stats.push(`省 ${fmtTok(item.savedTokens)} tokens`);
  }
  if (stats.length === 0) {
    stats.push(item.layer === 'L2' ? '旧工具输出已换占位符' : '结构化压缩完成');
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
      <summary title="上下文压缩报告（M8 四层防御：L2 机械淘汰 / L3 自动摘要）">
        <span className="card-title">
          🗜 压缩 {item.layer} · {stats.join(' · ')}
        </span>
        <span className="card-side muted">瘦身</span>
      </summary>
      {item.summary !== undefined && item.summary !== '' ? (
        <details className="compsummary">
          <summary>七要素摘要</summary>
          <pre className="card-body">{item.summary}</pre>
        </details>
      ) : (
        rawText !== undefined && <pre className="card-body">{rawText.slice(0, 2000)}</pre>
      )}
      {item.summary !== undefined && item.summary !== '' && rawText !== undefined && (
        <details className="compraw">
          <summary className="muted">原始报告</summary>
          <pre className="card-body">{rawText.slice(0, 2000)}</pre>
        </details>
      )}
    </details>
  );
}
