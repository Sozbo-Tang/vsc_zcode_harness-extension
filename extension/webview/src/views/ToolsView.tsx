/**
 * ToolsView 工具·Skills·MCP 视图（M1 空壳 → M7 Skills 页签实装）。
 *  - 工具开关 M2、MCP M8（占位文案不变）；
 *  - Skills（M7/R3）：control skills_list 回执（store.skills，reduceResult
 *    对账）→ 列表（名称 + 描述 + 来源路径）；首开自动拉取一次，「刷新」
 *    重发；核心未就绪/无技能时显示空态 + SKILL.md 放置指引。
 */
import { useEffect, useState } from 'react';
import { useStore } from '../store/store';

const TABS = ['工具', 'Skills', 'MCP'] as const;

function SkillsTab(): React.JSX.Element {
  const skills = useStore((s) => s.skills);
  const skillsRefreshing = useStore((s) => s.skillsRefreshing);
  const skillsRequested = useStore((s) => s.skillsRequested);
  const refreshSkills = useStore((s) => s.refreshSkills);

  // 首开自动拉取一次（幂等：skillsRequested 标记）；之后靠「刷新」手动重发
  useEffect(() => {
    if (!skillsRequested) refreshSkills();
  }, [skillsRequested, refreshSkills]);

  return (
    <div className="skillspanel">
      <div className="skillhead">
        <span>
          Skills <span className="muted small">（{skills.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={refreshSkills}
          disabled={skillsRefreshing}
          title="重新扫描 SKILL.md（control skills_list）"
        >
          {skillsRefreshing ? '刷新中…' : '↻ 刷新'}
        </button>
      </div>
      {skills.length === 0 ? (
        <div className="muted empty-note">
          {skillsRefreshing
            ? '正在获取 Skills 列表…'
            : '还没有 Skills。把 SKILL.md 放到 ~/.zcode-py/skills/<名称>/（用户级）或 工作区/.zcode-py/skills/<名称>/（项目级，同名覆盖）后点「刷新」。'}
        </div>
      ) : (
        <ul className="skilllist">
          {skills.map((sk) => (
            <li key={sk.name} className="skillitem" title={sk.path ?? sk.name}>
              <div className="skillname">/{sk.name}</div>
              {sk.description !== undefined && sk.description !== '' && (
                <div className="skilldesc muted">{sk.description}</div>
              )}
              {sk.path !== undefined && sk.path !== '' && (
                <div className="skillpath muted small">{sk.path}</div>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="muted small skillhint">
        输入框键入 / 可按名称补全技能命令。
      </div>
    </div>
  );
}

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
      {tab === 'Skills' ? (
        <SkillsTab />
      ) : (
        <div className="muted empty-note">
          {tab === '工具' && '工具开关 + 默认权限配置在 M2 工具系统落地后开放（R3）。'}
          {tab === 'MCP' && 'MCP client 连接管理 + 状态灯在 M8 接入（R3/M8）。'}
        </div>
      )}
    </div>
  );
}
