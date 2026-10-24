/**
 * ToolsView 工具·Skills·MCP 视图。
 *  - 工具（R3，已实装）：tools_list 回执 → 开关 + 默认权限（每次询问/
 *    始终允许），tool_toggle / tool_permission 即时生效并自动回拉；
 *  - Skills（M7/R3）：control skills_list 回执（store.skills，reduceResult
 *    对账）→ 列表（名称 + 描述 + 来源路径）；首开自动拉取一次，「刷新」
 *    重发；核心未就绪/无技能时显示空态 + SKILL.md 放置指引。
 *  - MCP（M8/R3）：服务列表（状态灯 ●绿=connected ●红=error ○灰=未连 +
 *    每服务工具数 + 展开工具浏览）、添加表单（name + 空格分隔 argv 的
 *    command + env 键值 → control mcp_add）、删除（mcp_remove）、「↻ 刷新」
 *    （mcp_refresh）；数据源 mcp_list/mcp_refresh 回执 result.detail JSON
 *    宽容解析（store.parseMcpDetail）+ 15s busy 兜底（沿 skills 同款模式）。
 *    装配缝缺失态：status=error 且说明含「装配缝」→ 指引文案
 *    （docs/MCP_BACKEND_SPEC.md）。
 */
import { useEffect, useState } from 'react';
import { useStore } from '../store/store';
import type { McpServerInfo } from '../protocol';

const TABS = ['工具', 'Skills', 'MCP'] as const;

/* ---------- R3：工具开关 + 默认权限 ---------- */

function ToolsTab(): React.JSX.Element {
  const tools = useStore((s) => s.toolsList);
  const refreshing = useStore((s) => s.toolsRefreshing);
  const requested = useStore((s) => s.toolsRequested);
  const pending = useStore((s) => s.toolActionPending);
  const refreshTools = useStore((s) => s.refreshTools);
  const toggleTool = useStore((s) => s.toggleTool);
  const setToolPermission = useStore((s) => s.setToolPermission);

  // 首开自动拉取一次（幂等标记）；之后靠「刷新」手动重发
  useEffect(() => {
    if (!requested) refreshTools();
  }, [requested, refreshTools]);

  return (
    <div className="toolspanel">
      <div className="skillhead">
        <span>
          工具 <span className="muted small">（{tools.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={refreshTools}
          disabled={refreshing}
          title="重新拉取工具注册表（control tools_list）"
        >
          {refreshing ? '刷新中…' : '↻ 刷新'}
        </button>
      </div>
      {tools.length === 0 ? (
        <div className="muted empty-note">
          {refreshing ? '正在获取工具列表…' : '暂无工具数据（核心未就绪？点「刷新」重试）。'}
        </div>
      ) : (
        <ul className="toollist">
          {tools.map((t) => (
            <li key={t.name} className="toolrow" title={t.description ?? t.name}>
              <label className="toolname">
                <input
                  type="checkbox"
                  checked={t.enabled}
                  disabled={pending}
                  onChange={(e) => toggleTool(t.name, e.target.checked)}
                  title={t.enabled ? '关闭后模型不可见不可调' : '开启后恢复注册'}
                />
                <span className="toolrow-name">{t.name}</span>
                {t.tier === 'readonly' ? (
                  <span className="tierbadge ro">只读</span>
                ) : (
                  <span className="tierbadge wr">写</span>
                )}
              </label>
              <select
                className="sel"
                value={t.alwaysAllowed ? 'always' : 'ask'}
                disabled={pending || !t.enabled}
                onChange={(e) => setToolPermission(t.name, e.target.value as 'ask' | 'always')}
                title="默认权限：始终允许不会绕过危险命令黑名单（PLAN §9.3）"
              >
                <option value="ask">每次询问</option>
                <option value="always">始终允许</option>
              </select>
            </li>
          ))}
        </ul>
      )}
      <div className="muted small skillhint">
        关闭的工具不进模型 schema（不可见不可调）；「始终允许」写入核心白名单
        （~/.zcode-py/config/tools.json），危险命令黑名单仍强制确认。
      </div>
    </div>
  );
}

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

/* ---------- M8：MCP 页签 ---------- */

const STATUS_GREEN = new Set(['connected', 'ok', 'ready', 'running']);
const STATUS_RED = new Set(['error', 'failed', 'crashed']);

function statusClass(status: string): 'on' | 'bad' | '' {
  const s = status.toLowerCase();
  if (STATUS_GREEN.has(s)) return 'on';
  if (STATUS_RED.has(s)) return 'bad';
  return '';
}

/**
 * 装配缝缺失态判定（M8）：status 为 error 系 且 detail/状态说明含
 * 「装配缝」标记（Python 侧装配未就绪时的约定提示词）。
 */
function isAssemblyGap(sv: McpServerInfo): boolean {
  if (statusClass(sv.status) !== 'bad') return false;
  return `${sv.status}\n${sv.detail ?? ''}`.includes('装配缝');
}

function McpStatusDot({ status }: { status: string }): React.JSX.Element {
  const cls = statusClass(status);
  const label = cls === 'on' ? 'connected' : cls === 'bad' ? 'error' : '未连接';
  return <span className={`mcpdot ${cls}`.trim()} role="img" aria-label={label} title={`状态：${status}（${label}）`} />;
}

function McpTab(): React.JSX.Element {
  const servers = useStore((s) => s.mcpServers);
  const refreshing = useStore((s) => s.mcpRefreshing);
  const requested = useStore((s) => s.mcpRequested);
  const pendingAction = useStore((s) => s.mcpPendingAction);
  const notice = useStore((s) => s.mcpNotice);
  const refreshMcp = useStore((s) => s.refreshMcp);
  const addMcpServer = useStore((s) => s.addMcpServer);
  const removeMcpServer = useStore((s) => s.removeMcpServer);

  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [formOpen, setFormOpen] = useState(false);
  const [name, setName] = useState('');
  const [command, setCommand] = useState('');
  const [envText, setEnvText] = useState('');
  const [formError, setFormError] = useState<string | undefined>();

  // 首开自动拉取一次（幂等：mcpRequested 标记）；「↻ 刷新」走 mcp_refresh 重连重扫
  useEffect(() => {
    if (!requested) refreshMcp('mcp_list');
  }, [requested, refreshMcp]);

  /** env 文本域（每行 KEY=VALUE，# 注释行忽略）→ 键值；格式非法返回 undefined */
  const parseEnv = (): Record<string, string> | undefined => {
    const out: Record<string, string> = {};
    for (const line of envText.split('\n')) {
      const t = line.trim();
      if (t === '' || t.startsWith('#')) continue;
      const i = t.indexOf('=');
      if (i <= 0) return undefined;
      out[t.slice(0, i).trim()] = t.slice(i + 1);
    }
    return out;
  };

  const submitAdd = (): void => {
    const n = name.trim();
    const c = command.trim();
    if (n === '' || c === '') {
      setFormError('服务名与启动命令必填');
      return;
    }
    const env = parseEnv();
    if (env === undefined) {
      setFormError('env 格式：每行一条 KEY=VALUE');
      return;
    }
    setFormError(undefined);
    addMcpServer({ name: n, command: c, env });
    setName('');
    setCommand('');
    setEnvText('');
    setFormOpen(false);
  };

  const toolCountLabel = (sv: McpServerInfo): string => {
    if (sv.tools.length > 0) return `${sv.tools.length} 工具`;
    if (sv.toolsCount !== undefined) return `${sv.toolsCount} 工具`;
    return '无工具';
  };

  return (
    <div className="skillspanel">
      <div className="skillhead">
        <span>
          MCP 服务 <span className="muted small">（{servers.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={() => refreshMcp('mcp_refresh')}
          disabled={refreshing}
          title="重连并重扫 MCP 服务与工具（control mcp_refresh）"
        >
          {refreshing ? '刷新中…' : '↻ 刷新'}
        </button>
      </div>

      {notice !== undefined && (
        <div className={notice.kind === 'error' ? 'mcpnotice bad' : 'mcpnotice ok'}>{notice.text}</div>
      )}

      {servers.length === 0 ? (
        <div className="muted empty-note">
          {refreshing
            ? '正在获取 MCP 服务列表…'
            : '还没有 MCP 服务。用下方「＋ 添加服务」接入 stdio MCP server（核心未识别 mcp_* 命令时会回错误提示，属正常——后端并行落地中）。'}
        </div>
      ) : (
        <ul className="mcplist">
          {servers.map((sv) => {
            const open = expanded[sv.name] ?? false;
            const gap = isAssemblyGap(sv);
            return (
              <li key={sv.name} className="mcpitem">
                <div className="mcprow">
                  <McpStatusDot status={sv.status} />
                  <button
                    className="mcpname"
                    onClick={() => setExpanded((e) => ({ ...e, [sv.name]: !open }))}
                    title={open ? '收起工具清单' : '展开工具清单'}
                  >
                    <span className="chev">{open ? '▾' : '▸'}</span> {sv.name}
                  </button>
                  <span className="muted small mcptoolslabel">{toolCountLabel(sv)}</span>
                  <button
                    className="iconbtn"
                    title="移除服务（control mcp_remove）"
                    disabled={pendingAction !== undefined}
                    onClick={() => removeMcpServer(sv.name)}
                  >
                    🗑
                  </button>
                </div>
                {sv.detail !== undefined && sv.detail !== '' && (
                  <div className="muted small mcpdetail" title={sv.detail}>
                    {sv.detail}
                  </div>
                )}
                {gap && (
                  <div className="mcpgap">
                    装配缝缺失：核心侧 MCP 装配未就绪。参照 <span className="mcpdoc">docs/MCP_BACKEND_SPEC.md</span>{' '}
                    补齐装配后点「↻ 刷新」。
                  </div>
                )}
                {open &&
                  (sv.tools.length > 0 ? (
                    <ul className="mcptools">
                      {sv.tools.map((t) => (
                        <li key={t.name} className="mcptool" title={t.description ?? t.name}>
                          <span className="mcptoolname">{t.name}</span>
                          {t.description !== undefined && t.description !== '' && (
                            <span className="muted small mcptooldesc">{t.description}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="muted small mcpnotools">
                      （无工具清单{sv.toolsCount !== undefined ? '，仅计数' : '；连接成功后点「↻ 刷新」拉取'}）
                    </div>
                  ))}
              </li>
            );
          })}
        </ul>
      )}

      <div className="mcpformrow">
        {formOpen ? (
          <div className="form">
            <div className="form-title">添加 MCP 服务</div>
            <div className="form-row">
              <label htmlFor="mcp-name">名称</label>
              <input
                id="mcp-name"
                className="in grow"
                value={name}
                placeholder="如 filesystem"
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="form-row">
              <label htmlFor="mcp-cmd">命令</label>
              <input
                id="mcp-cmd"
                className="in grow"
                value={command}
                placeholder="npx -y @modelcontextprotocol/server-filesystem /tmp"
                title="空格分隔的 argv：首段为可执行文件，其余为参数（引号暂不支持，含空格路径请用 env/后端配置）"
                onChange={(e) => setCommand(e.target.value)}
              />
            </div>
            <div className="form-row top">
              <label htmlFor="mcp-env" title="每行一条 KEY=VALUE；# 开头行忽略">
                env
              </label>
              <textarea
                id="mcp-env"
                className="in grow"
                rows={2}
                value={envText}
                placeholder={'每行一条 KEY=VALUE\n如 API_BASE=http://localhost:9000'}
                onChange={(e) => setEnvText(e.target.value)}
              />
            </div>
            {formError !== undefined && <div className="bad small">{formError}</div>}
            <div className="form-row end">
              <button className="btn sec" onClick={() => setFormOpen(false)}>
                取消
              </button>
              <button
                className="btn"
                onClick={submitAdd}
                disabled={pendingAction !== undefined}
                title="发 control mcp_add（成功后自动回拉列表）"
              >
                {pendingAction === 'add' ? '添加中…' : '添加'}
              </button>
            </div>
            <div className="muted small">
              命令按空格分隔成 argv 启动 stdio server；env 为子进程环境变量。协议细节见 docs/MCP_BACKEND_SPEC.md（后端并行定稿中）。
            </div>
          </div>
        ) : (
          <button className="btn sec" onClick={() => setFormOpen(true)} title="添加 MCP 服务（control mcp_add）">
            ＋ 添加服务
          </button>
        )}
      </div>

      <div className="muted small skillhint">
        状态灯：● 绿 = connected · ● 红 = error · ○ 灰 = 未连接/装配中。
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
      ) : tab === 'MCP' ? (
        <McpTab />
      ) : (
        <ToolsTab />
      )}
    </div>
  );
}
