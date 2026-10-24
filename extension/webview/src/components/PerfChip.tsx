/**
 * PerfChip 性能格（PLAN §6.1 / R19）：
 *  - 运行中：首 token 到达瞬间定格 TTFT（perf.ttftMs 事件首带即不再变），
 *    速度显示最新 tokPerSec —— 周期刷新由宿主 perf 事件驱动
 *    （PLAN §3.1 perf.py 每 0.5s 下发，webview 只显示事件值）
 *  - 任务结束：显示上一轮数值（perf.lastTurn）
 *  - R19 会话统计：点击弹出按模型分组表（平均/最差/峰值 + 累计 tokens），
 *    数据源 store.perfStats（perf 帧带 model 增量累计，含 resume 重放的
 *    历史帧）与 store.usageByModel（usage 帧 byModel 全量快照）。
 *    弹层为无障碍 button + role=dialog；点击遮罩/再点 chip 关闭。
 */
import { useEffect, useState } from 'react';
import { useStore, emptyPerfStats, type PerfModelStats } from '../store/store';
import { useT } from '../i18n';
import type { UsageModelBucket } from '../protocol';
import { Icon } from './Icon';

function fmtMs(ms?: number): string {
  if (ms == null) return '…';
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function fmtTps(tps?: number): string {
  if (tps == null || !Number.isFinite(tps)) return '—';
  return Math.round(tps).toString();
}

function fmtTokens(n: number): string {
  return n.toLocaleString('en-US');
}

/** 单模型一行：轮次 / 首 token 平均·最差 / 速度平均·最差·峰值 / tokens */
function StatRow({
  model,
  stats,
  bucket,
}: {
  model: string;
  stats: PerfModelStats;
  bucket?: UsageModelBucket;
}): React.JSX.Element {
  const t = useT();
  const avgTtft = stats.turns > 0 ? stats.ttftSum / stats.turns : undefined;
  const avgTps = stats.turns > 0 ? stats.tpsSum / stats.turns : undefined;
  return (
    <tr>
      {/* 归约器把无 model 的 perf 帧归入「未知」桶（store applyPerfStats）——
          展示名走字典，数据键保持稳定 */}
      <th scope="row" title={model}>{model === '未知' ? t('perf.modelUnknown') : model}</th>
      <td>{stats.turns}</td>
      <td>{fmtMs(avgTtft)}</td>
      <td>{fmtMs(stats.ttftWorst)}</td>
      <td>{fmtTps(avgTps)}</td>
      <td>{fmtTps(stats.tpsWorst)}</td>
      <td>{fmtTps(stats.tpsPeak)}</td>
      <td>
        {bucket !== undefined
          ? t('perf.tokensCell', {
              total: fmtTokens(bucket.input + bucket.output),
              in: fmtTokens(bucket.input),
              out: fmtTokens(bucket.output),
            })
          : '—'}
      </td>
    </tr>
  );
}

export function PerfChip(): React.JSX.Element {
  const perf = useStore((s) => s.perf);
  const running = useStore((s) => s.chat.running);
  const perfStats = useStore((s) => s.perfStats);
  const usageByModel = useStore((s) => s.usageByModel);
  const t = useT();
  const live = running || perf.phase === 'streaming';
  const ttft = live ? perf.ttftMs : perf.lastTurn?.ttftMs;
  const tps = live ? perf.tokPerSec : perf.lastTurn?.avgTokPerSec;
  const idle = !live && !perf.lastTurn;

  const [open, setOpen] = useState(false);

  // Escape 关弹层（无障碍：dialog 语义下键盘可达）
  useEffect(() => {
    if (!open) return;
    const onKey = (ev: KeyboardEvent): void => {
      if (ev.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  // 行序：有统计的模型在前，仅有 token 计数的模型（中继/压缩等无 perf 帧
  // 的模型调用）随后，保证 usage 里出现过的模型不遗漏
  const models = Array.from(new Set([...Object.keys(perfStats), ...Object.keys(usageByModel)]));
  const hasData = models.length > 0;

  return (
    <span className="perf-wrap">
      <button
        type="button"
        className={open ? 'perf active' : 'perf'}
        onClick={() => setOpen((v) => !v)}
        title={
          idle
            ? t('perf.idleTitle')
            : t('perf.liveTitle', {
                ttft: fmtMs(ttft ?? undefined),
                tps: tps != null ? Math.round(tps) : '—',
                phase: live ? t('perf.phaseLive') : t('perf.phaseLast'),
              })
        }
        aria-expanded={open}
        aria-haspopup="dialog"
      >
        {idle ? (
          <>— · —/s</>
        ) : (
          <>
            {fmtMs(ttft ?? undefined)} · {tps != null ? Math.round(tps) : '—'}/s
          </>
        )}
      </button>
      {open && (
        <>
          {/* 透明遮罩：点外关闭（不遮 perf chip 自身的点击切换） */}
          <div className="perf-backdrop" onClick={() => setOpen(false)} aria-hidden="true" />
          <div className="perf-popover" role="dialog" aria-label={t('perf.popoverAria')}>
            <div className="perf-popover-head">
              <span>{t('perf.popoverTitle')}</span>
              <button
                type="button"
                className="iconbtn"
                onClick={() => setOpen(false)}
                title={t('perf.close')}
                aria-label={t('perf.closeAria')}
              >
                <Icon name="close" size={12} />
              </button>
            </div>
            {hasData ? (
              <table className="perf-table">
                <thead>
                  <tr>
                    <th>{t('perf.colModel')}</th>
                    <th>{t('perf.colTurns')}</th>
                    <th title={t('perf.colTtftAvgTitle')}>{t('perf.colTtftAvg')}</th>
                    <th title={t('perf.colTtftWorstTitle')}>{t('perf.colWorst')}</th>
                    <th title={t('perf.colTpsAvgTitle')}>{t('perf.colTpsAvg')}</th>
                    <th title={t('perf.colTpsWorstTitle')}>{t('perf.colWorst')}</th>
                    <th title={t('perf.colTpsPeakTitle')}>{t('perf.colTpsPeak')}</th>
                    <th>{t('perf.colTokens')}</th>
                  </tr>
                </thead>
                <tbody>
                  {models.map((m) => (
                    <StatRow key={m} model={m} stats={perfStats[m] ?? emptyPerfStats()} bucket={usageByModel[m]} />
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="perf-empty">{t('perf.empty')}</div>
            )}
            <div className="perf-note">{t('perf.note')}</div>
          </div>
        </>
      )}
    </span>
  );
}
