/**
 * WelcomePage 空状态欢迎页（PLAN §4 组件表 / R26；M4 完整落地，
 * M1 先提供空会话占位 + 建议提示词卡片）。
 */
const SUGGESTIONS = [
  '解释这个项目的架构',
  '帮我定位登录模块的潜在问题',
  '为 src/ 下的核心模块写单元测试',
];

export function WelcomePage({ onPick }: { onPick: (text: string) => void }): React.JSX.Element {
  return (
    <div className="welcome">
      <div className="welcome-title">Zcode-Py</div>
      <div className="muted">
        Python 手搓 Agent Harness · 侧边栏聊天
        <br />
        配置模型请前往「API」视图；底部控制条可调思考车钟 / 权限模式。
      </div>
      <div className="chips">
        {SUGGESTIONS.map((s) => (
          <button key={s} className="btn sec chip-sug" onClick={() => onPick(s)}>
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}
