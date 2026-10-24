/**
 * EnvView 环境面板（M1 空壳；M5 硬件可视化 + 工作区语言检测完整落地，R7/R21）。
 * env_info 事件到达即先显示已有数据（硬件行 + 语言命中列表）。
 */
import { useStore } from '../store/store';

export function EnvView(): React.JSX.Element {
  const env = useStore((s) => s.env);
  return (
    <div className="view">
      <div className="view-head">
        <span>环境</span>
        <button className="btn sec" disabled title="envprobe 刷新 M5 接入">
          🔄刷新
        </button>
      </div>
      {env ? (
        <div className="envinfo">
          <div className="form-title">💻 硬件</div>
          <div className="muted">
            {[
              env.hardware.cpu && `CPU ${env.hardware.cpu}`,
              env.hardware.gpu && `GPU ${env.hardware.gpu}`,
              env.hardware.ram && `RAM ${env.hardware.ram}`,
              env.hardware.os && `OS ${env.hardware.os}`,
              env.hardware.arch,
            ]
              .filter(Boolean)
              .join(' · ')}
          </div>
          <div className="form-title">📂 工作区语言检测</div>
          {env.workspaceLangs.length === 0 && <div className="muted">未命中语言特征文件</div>}
          {env.workspaceLangs.map((l, i) => (
            <div key={l.language ?? i}>
              {l.ok !== false ? '✓' : '⚠'} {l.language ?? '未知语言'} {l.detail ? `· ${l.detail}` : ''}
            </div>
          ))}
        </div>
      ) : (
        <div className="muted empty-note">
          环境探测（硬件可视化 + 工作区语言检测）在 M5 接入（R7/R21）。
        </div>
      )}
    </div>
  );
}
