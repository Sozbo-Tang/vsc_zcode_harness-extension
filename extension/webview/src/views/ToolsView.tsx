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
import { useT } from '../i18n';
import type { McpServerInfo } from '../protocol';

/** 页签 id 稳定（与语言无关）；标签经 t() 渲染期解析（R20 i18n） */
const TABS = ['tools', 'skills', 'mcp'] as const;
type ToolsTabId = (typeof TABS)[number];
const TAB_LABEL_KEYS: Record<ToolsTabId, string> = {
  tools: 'tools.tab.tools',
  skills: 'tools.tab.skills',
  mcp: 'tools.tab.mcp',
};

/* ---------- R3：工具开关 + 默认权限 ---------- */

function ToolsTab(): React.JSX.Element {
  const tools = useStore((s) => s.toolsList);
  const refreshing = useStore((s) => s.toolsRefreshing);
  const requested = useStore((s) => s.toolsRequested);
  const pending = useStore((s) => s.toolActionPending);
  const refreshTools = useStore((s) => s.refreshTools);
  const toggleTool = useStore((s) => s.toggleTool);
  const setToolPermission = useStore((s) => s.setToolPermission);
  const t = useT();

  // 首开自动拉取一次（幂等标记）；之后靠「刷新」手动重发
  useEffect(() => {
    if (!requested) refreshTools();
  }, [requested, refreshTools]);

  return (
    <div className="toolspanel">
      <div className="skillhead">
        <span>
          {t('tools.toolsHead')} <span className="muted small">（{tools.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={refreshTools}
          disabled={refreshing}
          title={t('tools.refreshTitle')}
        >
          {refreshing ? t('tools.refreshing') : t('tools.refresh')}
        </button>
      </div>
      {tools.length === 0 ? (
        <div className="muted empty-note">
          {refreshing ? t('tools.loading') : t('tools.empty')}
        </div>
      ) : (
        <ul className="toollist">
          {tools.map((tl) => (
            <li key={tl.name} className="toolrow" title={tl.description ?? tl.name}>
              <label className="toolname">
                <input
                  type="checkbox"
                  checked={tl.enabled}
                  disabled={pending}
                  onChange={(e) => toggleTool(tl.name, e.target.checked)}
                  title={tl.enabled ? t('tools.disableHint') : t('tools.enableHint')}
                />
                <span className="toolrow-name">{tl.name}</span>
                {tl.tier === 'readonly' ? (
                  <span className="tierbadge ro">{t('tools.readonlyBadge')}</span>
                ) : (
                  <span className="tierbadge wr">{t('tools.writeBadge')}</span>
                )}
              </label>
              <select
                className="sel"
                value={tl.alwaysAllowed ? 'always' : 'ask'}
                disabled={pending || !tl.enabled}
                onChange={(e) => setToolPermission(tl.name, e.target.value as 'ask' | 'always')}
                title={t('tools.permissionTitle')}
              >
                <option value="ask">{t('tools.permAsk')}</option>
                <option value="always">{t('tools.permAlways')}</option>
              </select>
            </li>
          ))}
        </ul>
      )}
      <div className="muted small skillhint">{t('tools.hint')}</div>
    </div>
  );
}

function SkillsTab(): React.JSX.Element {
  const skills = useStore((s) => s.skills);
  const skillsRefreshing = useStore((s) => s.skillsRefreshing);
  const skillsRequested = useStore((s) => s.skillsRequested);
  const refreshSkills = useStore((s) => s.refreshSkills);
  const t = useT();

  // 首开自动拉取一次（幂等：skillsRequested 标记）；之后靠「刷新」手动重发
  useEffect(() => {
    if (!skillsRequested) refreshSkills();
  }, [skillsRequested, refreshSkills]);

  return (
    <div className="skillspanel">
      <div className="skillhead">
        <span>
          {t('tools.skillsHead')} <span className="muted small">（{skills.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={refreshSkills}
          disabled={skillsRefreshing}
          title={t('tools.skillsRefreshTitle')}
        >
          {skillsRefreshing ? t('tools.refreshing') : t('tools.refresh')}
        </button>
      </div>
      {skills.length === 0 ? (
        <div className="muted empty-note">
          {skillsRefreshing ? t('tools.skillsLoading') : t('tools.skillsEmpty')}
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
      <div className="muted small skillhint">{t('tools.skillsHint')}</div>
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
  const t = useT();
  const cls = statusClass(status);
  const label =
    cls === 'on' ? t('tools.mcpStatusConnected') : cls === 'bad' ? t('tools.mcpStatusError') : t('tools.mcpStatusDisconnected');
  return <span className={`mcpdot ${cls}`.trim()} role="img" aria-label={label} title={t('tools.mcpStatusTitle', { status, label })} />;
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
  const t = useT();

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
      const trimmed = line.trim();
      if (trimmed === '' || trimmed.startsWith('#')) continue;
      const i = trimmed.indexOf('=');
      if (i <= 0) return undefined;
      out[trimmed.slice(0, i).trim()] = trimmed.slice(i + 1);
    }
    return out;
  };

  const submitAdd = (): void => {
    const n = name.trim();
    const c = command.trim();
    if (n === '' || c === '') {
      setFormError(t('tools.formErrorName'));
      return;
    }
    const env = parseEnv();
    if (env === undefined) {
      setFormError(t('tools.formErrorEnv'));
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
    if (sv.tools.length > 0) return t('tools.toolCount', { n: sv.tools.length });
    if (sv.toolsCount !== undefined) return t('tools.toolCount', { n: sv.toolsCount });
    return t('tools.noTools');
  };

  return (
    <div className="skillspanel">
      <div className="skillhead">
        <span>
          {t('tools.mcpHead')} <span className="muted small">（{servers.length}）</span>
        </span>
        <button
          className="btn sec"
          onClick={() => refreshMcp('mcp_refresh')}
          disabled={refreshing}
          title={t('tools.mcpRefreshTitle')}
        >
          {refreshing ? t('tools.refreshing') : t('tools.refresh')}
        </button>
      </div>

      {notice !== undefined && (
        <div className={notice.kind === 'error' ? 'mcpnotice bad' : 'mcpnotice ok'}>{notice.text}</div>
      )}

      {servers.length === 0 ? (
        <div className="muted empty-note">
          {refreshing ? t('tools.mcpLoading') : t('tools.mcpEmpty')}
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
                    title={open ? t('tools.collapseTools') : t('tools.expandTools')}
                  >
                    <span className="chev">{open ? '▾' : '▸'}</span> {sv.name}
                  </button>
                  <span className="muted small mcptoolslabel">{toolCountLabel(sv)}</span>
                  <button
                    className="iconbtn"
                    title={t('tools.removeServerTitle')}
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
                {gap && <div className="mcpgap">{t('tools.assemblyGap')}</div>}
                {open &&
                  (sv.tools.length > 0 ? (
                    <ul className="mcptools">
                      {sv.tools.map((mt) => (
                        <li key={mt.name} className="mcptool" title={mt.description ?? mt.name}>
                          <span className="mcptoolname">{mt.name}</span>
                          {mt.description !== undefined && mt.description !== '' && (
                            <span className="muted small mcptooldesc">{mt.description}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <div className="muted small mcpnotools">
                      {t('tools.noToolList', {
                        suffix:
                          sv.toolsCount !== undefined
                            ? t('tools.noToolsCountSuffix')
                            : t('tools.noToolsRefreshSuffix'),
                      })}
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
            <div className="form-title">{t('tools.addFormTitle')}</div>
            <div className="form-row">
              <label htmlFor="mcp-name">{t('tools.nameLabel')}</label>
              <input
                id="mcp-name"
                className="in grow"
                value={name}
                placeholder={t('tools.namePlaceholder')}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="form-row">
              <label htmlFor="mcp-cmd">{t('tools.cmdLabel')}</label>
              <input
                id="mcp-cmd"
                className="in grow"
                value={command}
                placeholder="npx -y @modelcontextprotocol/server-filesystem /tmp"
                title={t('tools.cmdTitle')}
                onChange={(e) => setCommand(e.target.value)}
              />
            </div>
            <div className="form-row top">
              <label htmlFor="mcp-env" title={t('tools.envTitle')}>
                env
              </label>
              <textarea
                id="mcp-env"
                className="in grow"
                rows={2}
                value={envText}
                placeholder={t('tools.envPlaceholder')}
                onChange={(e) => setEnvText(e.target.value)}
              />
            </div>
            {formError !== undefined && <div className="bad small">{formError}</div>}
            <div className="form-row end">
              <button className="btn sec" onClick={() => setFormOpen(false)}>
                {t('sessions.cancel')}
              </button>
              <button
                className="btn"
                onClick={submitAdd}
                disabled={pendingAction !== undefined}
                title={t('tools.addBtnTitle')}
              >
                {pendingAction === 'add' ? t('tools.adding') : t('tools.addBtn')}
              </button>
            </div>
            <div className="muted small">{t('tools.argvHint')}</div>
          </div>
        ) : (
          <button className="btn sec" onClick={() => setFormOpen(true)} title={t('tools.addServiceTitle')}>
            {t('tools.addServiceBtn')}
          </button>
        )}
      </div>

      <div className="muted small skillhint">{t('tools.mcpHint')}</div>
    </div>
  );
}

export function ToolsView(): React.JSX.Element {
  const t = useT();
  const [tab, setTab] = useState<ToolsTabId>('tools');
  return (
    <div className="view">
      <div className="tabrow">
        {TABS.map((tabId) => (
          <button
            key={tabId}
            className={tab === tabId ? 'tab active' : 'tab'}
            onClick={() => setTab(tabId)}
          >
            {t(TAB_LABEL_KEYS[tabId])}
          </button>
        ))}
      </div>
      {tab === 'skills' ? <SkillsTab /> : tab === 'mcp' ? <McpTab /> : <ToolsTab />}
    </div>
  );
}
