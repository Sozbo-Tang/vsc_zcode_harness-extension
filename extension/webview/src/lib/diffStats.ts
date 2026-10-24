/**
 * 行级 diff 统计（DiffReviewCard 的 +N/−M 徽标）。
 *
 * 算法：先裁掉公共前缀/后缀行，再对剩余中段跑经典 LCS（滚动两行数组，
 * 空间 O(n)）；added = 中段新行数 − LCS，removed = 中段旧行数 − LCS。
 * 中段过大（合计 > 4000 行）时跳过 O(n·m) 的 LCS，用多重集匹配
 * （行文本 → 出现次数，O(n)）估计匹配行数 —— 是 LCS 的上界（忽略顺序），
 * 但远比总量差贴近真实（如 3000 行改 400 行不会显示 ±3000）。
 * 空串与 '' 按 0 行计（与「无内容」直觉一致，新增文件 = 全部 added）。
 */

export interface LineDiffStats {
  added: number;
  removed: number;
}

/** 中段行数合计超过该值即跳过 LCS，走多重集匹配估计 */
const LCS_MAX_LINES = 4000;

/** 多重集匹配行数（忽略顺序的 LCS 上界估计，O(n)）——巨型 diff 退化路径 */
function matchedLineCount(a: string[], b: string[], start: number, endA: number, endB: number): number {
  const counts = new Map<string, number>();
  for (let i = start; i < endA; i++) {
    counts.set(a[i], (counts.get(a[i]) ?? 0) + 1);
  }
  let matched = 0;
  for (let j = start; j < endB; j++) {
    const c = counts.get(b[j]) ?? 0;
    if (c > 0) {
      counts.set(b[j], c - 1);
      matched++;
    }
  }
  return matched;
}

export function lineDiffStats(oldText: string, newText: string): LineDiffStats {
  const a = oldText === '' ? [] : oldText.split('\n');
  const b = newText === '' ? [] : newText.split('\n');

  // 公共前缀 / 后缀裁剪
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) {
    start++;
  }
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }

  const midA = endA - start;
  const midB = endB - start;
  if (midA === 0) return { added: midB, removed: 0 };
  if (midB === 0) return { added: 0, removed: midA };

  const matched =
    midA + midB > LCS_MAX_LINES
      ? matchedLineCount(a, b, start, endA, endB)
      : lcsLength(a, b, start, midA, midB);
  return { added: midB - matched, removed: midA - matched };
}

/** 中段 LCS 长度（滚动数组：prev = 第 i-1 行，cur = 第 i 行） */
function lcsLength(a: string[], b: string[], start: number, midA: number, midB: number): number {
  const n = midB;
  let prev = new Array<number>(n + 1).fill(0);
  let cur = new Array<number>(n + 1).fill(0);
  for (let i = 1; i <= midA; i++) {
    const lineA = a[start + i - 1];
    for (let j = 1; j <= n; j++) {
      cur[j] = lineA === b[start + j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], cur[j - 1]);
    }
    const swap = prev;
    prev = cur;
    cur = swap;
  }
  return prev[n];
}
