/**
 * PerfChip 性能格（PLAN §6.1 / R19）：
 *  - 运行中：首 token 到达瞬间定格 TTFT（perf.ttftMs 事件首带即不再变），
 *    速度显示最新 tokPerSec —— 周期刷新由宿主 perf 事件驱动
 *    （PLAN §3.1 perf.py 每 0.5s 下发，webview 只显示事件值）
 *  - 任务结束：显示上一轮数值（perf.lastTurn）
 *  - 点击查看会话统计（R19 详情）为后续里程碑，此处仅提示。
 */
import { useStore } from '../store/store';

function fmtMs(ms?: number): string {
  if (ms == null) return '…';
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export function PerfChip(): React.JSX.Element {
  const perf = useStore((s) => s.perf);
  const running = useStore((s) => s.chat.running);
  const live = running || perf.phase === 'streaming';
  const ttft = live ? perf.ttftMs : perf.lastTurn?.ttftMs;
  const tps = live ? perf.tokPerSec : perf.lastTurn?.avgTokPerSec;
  const idle = !live && !perf.lastTurn;

  return (
    <span
      className="perf"
      title={
        idle
          ? '性能：任务运行后显示 首 token 耗时 / 输出速度'
          : `首 token 耗时 ${fmtMs(ttft)} · 输出速度 ${tps != null ? Math.round(tps) : '—'} tok/s${live ? '（实时）' : '（上一轮）'}\n会话统计详情：R19 后续版本`
      }
    >
      {idle ? (
        <>⏱— ⚡—</>
      ) : (
        <>
          ⏱{fmtMs(ttft)} ⚡{tps != null ? Math.round(tps) : '—'}/s
        </>
      )}
    </span>
  );
}
