/**
 * ToolsView 工具·Skills·MCP 视图（M1 空壳；工具开关 M2、Skills M7、MCP M8）。
 */
import { useState } from 'react';
import { useStore } from '../store/store';

const TABS = ['工具', 'Skills', 'MCP'] as const;

export function ToolsView(): React.JSX.Element {
  const [tab, setTab] = useState<(typeof TABS)[number]>('工具');
  return (
    <div className="view">
      <div className="tabrow">
        {TABS.map((t) => (
          <button
            key={t}
            className={tab === t ? 'tab active' : 'tab'}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>
      <div className="muted empty-note">
        {tab === '工具' && '工具开关 + 默认权限配置在 M2 工具系统落地后开放（R3）。'}
        {tab === 'Skills' && 'SKILL.md 解析 + 斜杠命令在 M7 接入（R3/M7）。'}
        {tab === 'MCP' && 'MCP client 连接管理 + 状态灯在 M8 接入（R3/M8）。'}
      </div>
    </div>
  );
}
