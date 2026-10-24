/**
 * EnvView 环境面板（M5 完整实现，R7/R21；线框 PLAN §6.4）：
 *  - 硬件卡：CPU 型号·核数 / RAM / GPU·显存 / OS·架构
 *    （envprobe/hardware.py 字段 cpuModel/cpuCores/ramTotal/gpuModel/gpuVram/os/arch，
 *    早期镜像字段 cpu/gpu/ram 兜底）
 *  - 工作区语言检测：只显示特征文件命中的语言（决策 PLAN §10）——
 *    ✓ 工具链版本·包管理器·conda/nvm extras；⚠ missing 黄牌 +
 *    installHint「查看指引」展开（R21）
 *  - 🔄刷新：control env_refresh → 核心重新探测并广播 env_info
 * 数据源：env_info 事件 → store.env（init 后台探测 / 手动刷新均走同一帧）。
 */
import { useStore } from '../store/store';
import type { HardwareInfo, WorkspaceLang } from '../protocol';

function HardwareCard({ hw }: { hw: HardwareInfo }): React.JSX.Element {
  const cpu = hw.cpuModel ?? hw.cpu;
  const ram = hw.ramTotal ?? hw.ram;
  const gpu = hw.gpuModel ?? hw.gpu;
  const os = hw.os;
  const rows: Array<[string, string]> = [
    ['CPU', cpu ? (hw.cpuCores != null ? `${cpu} · ${hw.cpuCores} 核` : cpu) : '未知'],
    ['RAM', ram ?? '未知'],
    ['GPU', gpu ? (hw.gpuVram ? `${gpu} · ${hw.gpuVram}` : gpu) : '未探测到'],
    ['OS', os ? (hw.arch ? `${os} · ${hw.arch}` : os) : hw.arch ?? '未知'],
  ];
  return (
    <div className="card">
      {rows.map(([k, v]) => (
        <div key={k} className="card-row">
          <span className="muted">{k}</span>
          <span className="env-val">{v}</span>
        </div>
      ))}
    </div>
  );
}

function kvLine(entries: Array<[string, string]>): string {
  return entries.map(([k, v]) => `${k} ${v}`).join(' · ');
}

function LangCard({ lang }: { lang: WorkspaceLang }): React.JSX.Element {
  // missing（M5 定稿字段）优先；早期镜像的 ok===false 兜底
  const missing = lang.missing === true || (lang.missing === undefined && lang.ok === false);
  const title = lang.label ?? lang.language ?? '未知语言';
  const markers = lang.markers ?? [];
  const managers = Object.entries(lang.packageManagers ?? {});
  const extras = Object.entries(lang.extras ?? {});

  return (
    <details className="card langcard" open={missing}>
      <summary>
        <span className="card-title">
          {missing ? <span className="warn">⚠</span> : <span className="ok">✓</span>} {title}
        </span>
        <span className={`card-side ${missing ? 'warn' : 'muted'}`}>
          {missing ? '缺工具链' : lang.version ?? '就绪'}
        </span>
      </summary>
      <div className="lang-body">
        {markers.length > 0 && (
          <div className="muted small">
            发现: {markers.join(' · ')}
            {(lang.markerPaths?.length ?? 0) > 1 ? `（${lang.markerPaths?.length} 处）` : ''}
          </div>
        )}
        {missing ? (
          <div className="warn small">
            检测到 {markers.join(' / ') || '特征文件'}，但未装工具链
          </div>
        ) : (
          <>
            {lang.version != null && lang.version !== '' && <div>工具链: {lang.version}</div>}
            {managers.length > 0 && <div>包管理: {kvLine(managers)}</div>}
            {extras.length > 0 && <div className="muted small">{kvLine(extras)}</div>}
          </>
        )}
        {/* 早期镜像形状（ok/detail）兜底显示 */}
        {!missing && lang.detail != null && <div className="muted small">{lang.detail}</div>}
        {missing && lang.installHint != null && lang.installHint !== '' && (
          <details className="lang-hint">
            <summary>查看指引 ▾</summary>
            <div className="muted small lang-hint-body">{lang.installHint}</div>
          </details>
        )}
      </div>
    </details>
  );
}

export function EnvView(): React.JSX.Element {
  const env = useStore((s) => s.env);
  const envRefreshing = useStore((s) => s.envRefreshing);
  const refreshEnv = useStore((s) => s.refreshEnv);

  return (
    <div className="view">
      <div className="view-head">
        <span>环境</span>
        <button
          className="btn sec"
          onClick={refreshEnv}
          disabled={envRefreshing}
          title="重新探测硬件与工作区语言（control env_refresh → env_info）"
        >
          {envRefreshing ? '⏳ 探测中…' : '🔄刷新'}
        </button>
      </div>
      {env === null ? (
        <div className="muted empty-note">
          {envRefreshing
            ? '环境探测进行中（硬件 + 工作区语言并行探测）…'
            : '等待环境探测（env_info）—— 发送消息或点击「刷新」触发。'}
        </div>
      ) : (
        <div className="envinfo">
          <div className="form-title">💻 硬件</div>
          <HardwareCard hw={env.hardware} />
          <div className="form-title">📂 工作区语言检测</div>
          {env.workspaceLangs.length === 0 ? (
            <div className="muted small">
              未命中语言特征文件（pyproject.toml / package.json / Cargo.toml / go.mod /
              pom.xml / build.gradle / CMakeLists.txt）
            </div>
          ) : (
            env.workspaceLangs.map((l, i) => <LangCard key={l.language ?? i} lang={l} />)
          )}
        </div>
      )}
    </div>
  );
}
