/**
 * SettingsView 设置视图（M1 空壳；分组规划见 PLAN §6.5，R13/R15/R20 等）。
 */
const GROUPS: Array<[string, string]> = [
  ['通用', '界面语言（中/英 R20）、字号 — M4+'],
  ['Agent', '系统提示词、最大轮数 — M2+'],
  ['压缩', '阈值、压缩专用模型 — M8（R14）'],
  ['视觉中继', '三档预设 + 自定义转述提示词 — M5（R9）'],
  ['内联补全', '默认关闭，可选开启 — M9（R13）'],
  ['隐私护栏', '.env/*.pem/id_rsa 模式列表 — M2（R15）'],
  ['数据', '会话目录、清除数据 — M4'],
];

export function SettingsView(): React.JSX.Element {
  return (
    <div className="view">
      <div className="view-head">
        <span>设置</span>
      </div>
      {GROUPS.map(([name, desc]) => (
        <div key={name} className="setting-group">
          <div className="form-title">{name}</div>
          <div className="muted small">{desc}</div>
        </div>
      ))}
    </div>
  );
}
