/**
 * Telegraph 车钟组件（PLAN §6.2 / R6）：船舶发动机式机械车钟，扇形指令区
 * 直接显示思考程度；盘面由模型档案 telegraphLevels 数据驱动（UI 只消费不计算）。
 *
 * 零自定颜色约束（PLAN §4）下的样式逼近：
 *  - 金属厚外圈 → editorWidget / button-secondary 变量 + charts-yellow 细描边
 *  - 盘面与扇区文字 → editorWidget-background / editorWidget-foreground
 *  - 外延握把 → var(--vscode-charts-orange)
 *  - 置灰挡位 → var(--vscode-disabledForeground) + 降透明度
 *
 * 四种盘面模式由 levels 数据推导：预算型 / 档位型 / 开关型 / 无思考型（整体
 * 置灰不可拖拽）。机械手柄拖拽换挡带吸附（就近吸附到可用挡位，禁用挡位跳过）；
 * 主指针即时，回令针延迟 0.5s 追上（PLAN §6.2）。换挡吸附瞬间播放 WebAudio
 * 合成的短促机械咔哒（audio/telegraphClick，零音频资源；设置页「音效」组可关，
 * 默认开——core settings 键 telegraphClickSound）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { TelegraphLevel } from '../protocol';
import { useStore, formatParam } from '../store/store';
import { useT } from '../i18n';
import { playTelegraphClick } from '../audio/telegraphClick';

export type TelegraphDialMode = 'budget' | 'levels' | 'toggle' | 'none';

export function deriveDialMode(levels: TelegraphLevel[]): TelegraphDialMode {
  const enabled = levels.filter((l) => l.ok);
  if (enabled.length === 0) return 'none';
  if (enabled.length <= 2) return 'toggle';
  const numeric = levels.some((l) =>
    /budget|tokens|effort|thinking|\d/i.test(formatParam(l.param)),
  );
  return numeric ? 'budget' : 'levels';
}

/** 盘面模式文案键（渲染期经 t() 解析，R20 i18n） */
const MODE_LABEL_KEYS: Record<TelegraphDialMode, string> = {
  budget: 'telegraph.modeBudget',
  levels: 'telegraph.modeLevels',
  toggle: 'telegraph.modeToggle',
  none: 'telegraph.modeNone',
};

interface TelegraphProps {
  levels: TelegraphLevel[];
  /** 当前挡位 id（宿主 telegraph_state.current 语义） */
  current: string;
  paramTranslation?: Record<string, unknown>;
  /** 提交挡位 id */
  onChange?: (id: string) => void;
  size?: number;
}

/** 指令扇区覆盖上部 270°，底部保留制造商铭文区。 */
const ARC_START = -135;
const ARC_END = 135;

function polar(cx: number, cy: number, r: number, deg: number): { x: number; y: number } {
  const rad = (deg * Math.PI) / 180;
  return { x: cx + r * Math.sin(rad), y: cy - r * Math.cos(rad) };
}

function sectorPath(
  cx: number,
  cy: number,
  innerR: number,
  outerR: number,
  start: number,
  end: number,
): string {
  const outerStart = polar(cx, cy, outerR, start);
  const outerEnd = polar(cx, cy, outerR, end);
  const innerEnd = polar(cx, cy, innerR, end);
  const innerStart = polar(cx, cy, innerR, start);
  const large = end - start > 180 ? 1 : 0;
  return [
    `M ${outerStart.x} ${outerStart.y}`,
    `A ${outerR} ${outerR} 0 ${large} 1 ${outerEnd.x} ${outerEnd.y}`,
    `L ${innerEnd.x} ${innerEnd.y}`,
    `A ${innerR} ${innerR} 0 ${large} 0 ${innerStart.x} ${innerStart.y}`,
    'Z',
  ].join(' ');
}

export function Telegraph({
  levels,
  current,
  paramTranslation,
  onChange,
  size = 176,
}: TelegraphProps) {
  const t = useT();
  const svgRef = useRef<SVGSVGElement | null>(null);
  const mode = useMemo(() => deriveDialMode(levels), [levels]);
  const disabled = mode === 'none';

  // 每个挡位位于独立扇区中央，拖拽时仍按最近可用角度吸附。
  const angles = useMemo(() => {
    const n = levels.length;
    if (n <= 1) return levels.map(() => 0);
    const span = (ARC_END - ARC_START) / n;
    return levels.map((_, i) => ARC_START + span * (i + 0.5));
  }, [levels]);

  const indexOfCurrent = Math.max(
    0,
    levels.findIndex((l) => l.id === current && l.ok),
  );
  const currentAngle = angles[indexOfCurrent] ?? 0;

  const [dragAngle, setDragAngle] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const [ackAngle, setAckAngle] = useState(currentAngle);

  // 回令针：主针到位后延迟 0.5s 追上
  useEffect(() => {
    const t = setTimeout(() => setAckAngle(currentAngle), 500);
    return () => clearTimeout(t);
  }, [currentAngle]);

  const width = size + 72;
  const height = size + 64;
  const cx = width / 2;
  const cy = height / 2;
  const R = size / 2 - 10;
  const face = R - 9;
  const bandOuter = face - 3;
  const bandInner = face * 0.46;
  const labelR = (bandOuter + bandInner) / 2;
  const ackLen = face - 1;
  const knobR = 10;
  const sectorSpan = levels.length > 0 ? (ARC_END - ARC_START) / levels.length : 0;

  function angleFromPointer(clientX: number, clientY: number): number | null {
    const el = svgRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    const dx = clientX - (rect.left + rect.width / 2);
    const dy = clientY - (rect.top + rect.height / 2);
    let deg = (Math.atan2(dx, -dy) * 180) / Math.PI; // 0=正上，顺时针为正
    if (deg > 180) deg -= 360;
    return Math.min(ARC_END, Math.max(ARC_START, deg));
  }

  function nearestEnabled(deg: number): number {
    let best = -1;
    let bestDist = Infinity;
    levels.forEach((l, i) => {
      if (!l.ok) return;
      const d = Math.abs(deg - angles[i]);
      if (d < bestDist) {
        bestDist = d;
        best = i;
      }
    });
    return best >= 0 ? best : indexOfCurrent;
  }

  function commitTo(deg: number): void {
    if (disabled || !onChange) return;
    const idx = nearestEnabled(deg);
    const lv = levels[idx];
    if (lv && lv.id !== current) {
      // R6：吸附到新挡位的瞬间咔哒。开关现读 store（点击时而非渲染时，
      // 设置页切换即刻生效）；AudioContext 在本次 pointer-up 手势内惰性
      // 创建（telegraphClick 内部，失败静默降级）
      if (useStore.getState().settings?.telegraphClickSound !== false) {
        playTelegraphClick();
      }
      onChange(lv.id);
    }
  }

  const onPointerDown = (e: React.PointerEvent<SVGGElement>): void => {
    if (disabled) return;
    e.preventDefault();
    (e.target as Element).setPointerCapture(e.pointerId);
    setDragging(true);
    const deg = angleFromPointer(e.clientX, e.clientY);
    if (deg != null) setDragAngle(deg);
  };
  const onPointerMove = (e: React.PointerEvent<SVGGElement>): void => {
    if (!dragging) return;
    const deg = angleFromPointer(e.clientX, e.clientY);
    if (deg != null) setDragAngle(deg);
  };
  const onPointerUp = (e: React.PointerEvent<SVGGElement>): void => {
    if (!dragging) return;
    setDragging(false);
    const deg = angleFromPointer(e.clientX, e.clientY) ?? currentAngle;
    setDragAngle(null);
    commitTo(deg); // 松手吸附到就近可用挡位
  };

  const knobAngle = dragAngle ?? currentAngle;
  const curParam = paramTranslation
    ? formatParam(paramTranslation)
    : formatParam(levels[indexOfCurrent]?.param);
  const currentLabel = levels[indexOfCurrent]?.label ?? current;
  const labelWindowWidth = Math.max(26, Math.min(46, currentLabel.length * 11 + 10));
  const labelWindowTop = cy - labelR - 10;
  const labelWindowBottom = cy - labelR + 10;

  return (
    <div className={`telegraph${disabled ? ' telegraph-off' : ''}`}>
      <svg
        ref={svgRef}
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        className="telegraph-svg"
        role="img"
        aria-label={t('telegraph.aria', { current })}
      >
        <title>{t('telegraph.title', { label: levels[indexOfCurrent]?.label ?? current })}</title>
        <defs>
          <linearGradient id="tg-metal-gradient" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" className="tg-metal-stop-hi" />
            <stop offset="24%" className="tg-metal-stop-warm" />
            <stop offset="50%" className="tg-metal-stop-mid" />
            <stop offset="76%" className="tg-metal-stop-low" />
            <stop offset="100%" className="tg-metal-stop-shadow" />
          </linearGradient>
          <radialGradient id="tg-face-gradient" cx="38%" cy="28%" r="78%">
            <stop offset="0%" className="tg-face-stop-hi" />
            <stop offset="72%" className="tg-face-stop-mid" />
            <stop offset="100%" className="tg-face-stop-low" />
          </radialGradient>
          <linearGradient id="tg-lever-gradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" className="tg-metal-stop-shadow" />
            <stop offset="42%" className="tg-metal-stop-hi" />
            <stop offset="66%" className="tg-metal-stop-mid" />
            <stop offset="100%" className="tg-metal-stop-low" />
          </linearGradient>
          <linearGradient id="tg-grip-gradient" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" className="tg-grip-stop-low" />
            <stop offset="42%" className="tg-grip-stop-hi" />
            <stop offset="100%" className="tg-grip-stop-mid" />
          </linearGradient>
        </defs>

        <circle cx={cx} cy={cy + 2} r={R + 4} className="tg-shadow" />
        <circle cx={cx} cy={cy} r={R + 3} fill="url(#tg-metal-gradient)" className="tg-outer-rim" />
        <circle cx={cx} cy={cy} r={R - 2} className="tg-rim-groove" fill="none" />
        <circle cx={cx} cy={cy} r={R - 5} className="tg-brass-thin" strokeWidth={1.2} fill="none" />
        <circle cx={cx} cy={cy} r={face} fill="url(#tg-face-gradient)" className="tg-face" />

        {[45, 135, 225, 315].map((a) => {
          const p = polar(cx, cy, R, a);
          return (
            <g key={a} transform={`translate(${p.x} ${p.y}) rotate(${a})`}>
              <circle r={1.9} className="tg-rivet" />
              <line x1={-1.1} y1={0} x2={1.1} y2={0} className="tg-rivet-slot" />
            </g>
          );
        })}

        {levels.map((l, i) => {
          const start = ARC_START + sectorSpan * i;
          const isCur = !disabled && l.ok && l.id === current;
          return (
            <path
              key={l.id ?? i}
              d={sectorPath(cx, cy, bandInner, bandOuter, start, start + sectorSpan)}
              className={`tg-sector${i % 2 === 0 ? ' tg-sector-alt' : ''}${isCur ? ' tg-sector-cur' : ''}${l.ok ? '' : ' tg-sector-disabled'}`}
            />
          );
        })}

        {levels.length > 0 && Array.from({ length: levels.length + 1 }, (_, i) => {
          const a = ARC_START + sectorSpan * i;
          const p1 = polar(cx, cy, bandInner, a);
          const p2 = polar(cx, cy, bandOuter, a);
          return (
            <line
              key={a}
              x1={p1.x}
              y1={p1.y}
              x2={p2.x}
              y2={p2.y}
              className="tg-sector-divider"
            />
          );
        })}
        <circle cx={cx} cy={cy} r={bandOuter} className="tg-band-ring" fill="none" />
        <circle cx={cx} cy={cy} r={bandInner} className="tg-inner-face" />

        <g
          className="tg-ack"
          style={{
            transform: `rotate(${ackAngle}deg)`,
            transformOrigin: `${cx}px ${cy}px`,
            transition: dragging ? 'none' : 'transform .4s ease',
          }}
        >
          <line x1={cx} y1={cy} x2={cx} y2={cy - ackLen} className="tg-ack-line" />
          <path
            d={`M ${cx} ${cy - ackLen - 3} L ${cx - 2} ${cy - ackLen + 3} L ${cx + 2} ${cy - ackLen + 3} Z`}
            className="tg-ack-tip"
          />
        </g>

        {levels.map((l, i) => {
          const a = angles[i];
          const isCur = !disabled && l.ok && l.id === current;
          const lp = polar(cx, cy, labelR, a);
          const rotation = a > 90 ? a - 180 : a < -90 ? a + 180 : a;
          return (
            <g key={l.id ?? i} className={l.ok ? '' : 'tg-disabled'}>
              <text
                transform={`translate(${lp.x} ${lp.y}) rotate(${rotation})`}
                textAnchor="middle"
                dominantBaseline="central"
                className={isCur ? 'tg-label tg-label-cur' : 'tg-label'}
              >
                {l.label}
              </text>
            </g>
          );
        })}

        <text x={cx} y={cy + face * 0.58} textAnchor="middle" className="tg-dial-brand">
          ZCODE-PY
        </text>
        <text x={cx} y={cy + face * 0.76} textAnchor="middle" className="tg-dial-label">
          ENGINE ORDER
        </text>
        <ellipse cx={cx} cy={cy - face * 0.36} rx={face * 0.72} ry={face * 0.3} className="tg-glass" />

        <g
          className="tg-handle"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          style={{
            transform: `rotate(${knobAngle}deg)`,
            transformOrigin: `${cx}px ${cy}px`,
            transition: dragging ? 'none' : 'transform .18s ease',
            cursor: disabled ? 'not-allowed' : dragging ? 'grabbing' : 'grab',
          }}
        >
          <rect
            x={cx - 8}
            y={cy - (R + 40)}
            width={16}
            height={R + 54}
            fill="transparent"
          />
          <path
            d={`M ${cx - 1.8} ${labelWindowTop + 1} L ${cx - 1.8} ${cy - (R + 15)} L ${cx + 1.8} ${cy - (R + 15)} L ${cx + 1.8} ${labelWindowTop + 1} Z`}
            fill="url(#tg-lever-gradient)"
            className="tg-lever"
          />
          <path
            d={`M ${cx - 2.8} ${cy + 10} L ${cx - 1.8} ${labelWindowBottom} L ${cx + 1.8} ${labelWindowBottom} L ${cx + 2.8} ${cy + 10} Z`}
            fill="url(#tg-lever-gradient)"
            className="tg-lever"
          />
          <path
            d={`M ${cx} ${labelWindowBottom} L ${cx - 5} ${labelWindowBottom + 11} L ${cx - 2.5} ${cy - 4} L ${cx} ${cy + 7} L ${cx + 2.5} ${cy - 4} L ${cx + 5} ${labelWindowBottom + 11} Z`}
            fill="url(#tg-lever-gradient)"
            className="tg-pointer-arrow"
          />
          <path
            d={`M ${cx - labelWindowWidth / 2} ${labelWindowBottom} L ${cx - 5} ${cy + 5} M ${cx + labelWindowWidth / 2} ${labelWindowBottom} L ${cx + 5} ${cy + 5}`}
            fill="none"
            stroke="url(#tg-lever-gradient)"
            className="tg-pointer-frame"
          />
          <rect
            x={cx - labelWindowWidth / 2}
            y={labelWindowTop}
            width={labelWindowWidth}
            height={labelWindowBottom - labelWindowTop}
            rx={1.5}
            fill="transparent"
            stroke="url(#tg-lever-gradient)"
            className="tg-label-window"
          />
          <rect
            x={cx - 5}
            y={cy - (R + 39)}
            width={10}
            height={28}
            rx={4}
            fill="url(#tg-grip-gradient)"
            className="tg-grip-body"
          />
          <rect
            x={cx - 2.8}
            y={cy - (R + 36)}
            width={1.4}
            height={21}
            rx={0.7}
            className="tg-grip-highlight"
          />
          <rect
            x={cx - 6}
            y={cy - (R + 14)}
            width={12}
            height={5}
            rx={1}
            fill="url(#tg-metal-gradient)"
            className="tg-grip-collar"
          />
          <circle cx={cx} cy={cy} r={knobR + 4} fill="url(#tg-metal-gradient)" className="tg-knob-outer" />
          <circle cx={cx} cy={cy} r={knobR} fill="url(#tg-lever-gradient)" className="tg-knob" />
          <circle cx={cx} cy={cy} r={3.5} className="tg-knob-cap" />
          <line x1={cx - 1.8} y1={cy} x2={cx + 1.8} y2={cy} className="tg-knob-screw" />
        </g>
      </svg>
      <div className="tg-caption">
        {disabled ? (
          <span>{t('telegraph.captionOff')}</span>
        ) : (
          <span className="tg-readout">
            {t('telegraph.captionCurrent')} <b>{levels[indexOfCurrent]?.label ?? current}</b>
            <code>{curParam}</code>
          </span>
        )}
        <span className="tg-mode">{t(MODE_LABEL_KEYS[mode])}</span>
      </div>
      {!disabled && <div className="tg-hint">{t('telegraph.hint')}</div>}
    </div>
  );
}
