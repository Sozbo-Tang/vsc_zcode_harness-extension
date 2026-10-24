/**
 * Telegraph 车钟组件（PLAN §6.2 / R6）：大和号样式圆形仪表，刻度直接显示
 * 思考程度；盘面由模型档案 telegraphLevels 数据驱动渲染（UI 只消费不计算）。
 *
 * 零自定颜色约束（PLAN §4）下的样式逼近：
 *  - 黄铜外圈 → var(--vscode-charts-yellow)（主题黄，深浅色自动跟随）
 *  - 黑盘面   → var(--vscode-editorWidget-background)
 *  - 白刻度字 → var(--vscode-editorWidget-foreground)
 *  - 置灰挡位 → var(--vscode-disabledForeground) + 降透明度
 *
 * 四种盘面模式由 levels 数据推导：预算型 / 档位型 / 开关型 / 无思考型（整体
 * 置灰不可拖拽）。中央把手拖拽换挡带吸附（就近吸附到可用挡位，禁用挡位跳过）；
 * 主指针即时，回令针延迟 0.5s 追上（PLAN §6.2）。咔哒音效在 R6 后续版本接入
 *（需音频资源 + 设置开关，M1 webview 范围外）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { TelegraphLevel } from '../protocol';
import { formatParam } from '../store/store';

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

const MODE_LABEL: Record<TelegraphDialMode, string> = {
  budget: '预算型',
  levels: '档位型',
  toggle: '开关型',
  none: '无思考型',
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

/** 刻度弧：-150°（左下）顺时针扫到 +150°（右下），底部留给把手（PLAN §6.2 线框） */
const ARC_START = -150;
const ARC_END = 150;

function polar(cx: number, cy: number, r: number, deg: number): { x: number; y: number } {
  const rad = (deg * Math.PI) / 180;
  return { x: cx + r * Math.sin(rad), y: cy - r * Math.cos(rad) };
}

export function Telegraph({
  levels,
  current,
  paramTranslation,
  onChange,
  size = 176,
}: TelegraphProps) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const mode = useMemo(() => deriveDialMode(levels), [levels]);
  const disabled = mode === 'none';

  // 每个挡位的角位
  const angles = useMemo(() => {
    const n = levels.length;
    if (n <= 1) return levels.map(() => 0);
    return levels.map((_, i) => ARC_START + ((ARC_END - ARC_START) * i) / (n - 1));
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

  const cx = size / 2;
  const cy = size / 2;
  const R = size / 2 - 5; // 黄铜外圈中心线半径
  const face = R - 9; // 盘面半径
  const tickOuter = face - 3;
  const tickInner = face - 10;
  const labelR = face - 22;
  const needleLen = face - 27;
  const ackLen = face - 7;
  const knobR = 13;

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
    if (lv && lv.id !== current) onChange(lv.id);
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

  return (
    <div className={`telegraph${disabled ? ' telegraph-off' : ''}`}>
      <svg
        ref={svgRef}
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        className="telegraph-svg"
        role="img"
        aria-label={`思考程度车钟，当前 ${current}`}
      >
        {/* 黄铜外圈（双环逼近金属圈） */}
        <circle cx={cx} cy={cy} r={R} className="tg-brass" strokeWidth={5} fill="none" />
        <circle cx={cx} cy={cy} r={R - 5} className="tg-brass-thin" strokeWidth={1.2} fill="none" />
        {/* 盘面 */}
        <circle cx={cx} cy={cy} r={face} className="tg-face" />

        {/* 刻度 + 挡位标签（刻度直接显示思考程度） */}
        {levels.map((l, i) => {
          const a = angles[i];
          const p1 = polar(cx, cy, tickInner, a);
          const p2 = polar(cx, cy, tickOuter, a);
          const lp = polar(cx, cy, labelR, a);
          const isCur = !disabled && l.ok && l.id === current;
          return (
            <g key={l.id ?? i} className={l.ok ? '' : 'tg-disabled'}>
              <line
                x1={p1.x}
                y1={p1.y}
                x2={p2.x}
                y2={p2.y}
                className={isCur ? 'tg-tick tg-tick-cur' : 'tg-tick'}
                strokeWidth={isCur ? 2.5 : 1.5}
              />
              <text
                x={lp.x}
                y={lp.y + 3.5}
                textAnchor="middle"
                className={isCur ? 'tg-label tg-label-cur' : 'tg-label'}
              >
                {l.label}
              </text>
            </g>
          );
        })}

        {/* 回令针（细，延迟追上） */}
        <g
          className="tg-ack"
          style={{
            transform: `rotate(${ackAngle}deg)`,
            transformOrigin: `${cx}px ${cy}px`,
            transition: dragging ? 'none' : 'transform .4s ease',
          }}
        >
          <line x1={cx} y1={cy} x2={cx} y2={cy - ackLen} className="tg-ack-line" />
        </g>

        {/* 中央把手（主指针 + 拖拽旋钮） */}
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
            cursor: disabled ? 'not-allowed' : 'grab',
          }}
        >
          <line x1={cx} y1={cy} x2={cx} y2={cy - needleLen} className="tg-needle" />
          <circle cx={cx} cy={cy} r={knobR} className="tg-knob" />
          {/* 把手防滑纹 */}
          <line x1={cx - 5} y1={cy - 6} x2={cx - 5} y2={cy + 6} className="tg-grip" />
          <line x1={cx} y1={cy - 8} x2={cx} y2={cy + 8} className="tg-grip" />
          <line x1={cx + 5} y1={cy - 6} x2={cx + 5} y2={cy + 6} className="tg-grip" />
        </g>
      </svg>
      <div className="tg-caption">
        {disabled ? (
          <span>该模型未声明思考能力，车钟置灰</span>
        ) : (
          <span>
            当前: <b>{levels[indexOfCurrent]?.label ?? current}</b> → {curParam}
          </span>
        )}
        <span className="tg-mode">{MODE_LABEL[mode]}</span>
      </div>
    </div>
  );
}
