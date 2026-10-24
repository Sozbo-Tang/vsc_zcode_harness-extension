/**
 * ChatView 聊天视图（PLAN §6.1 完整实现，M1 范围）：
 *  - 流式消息（react-markdown 渲染正文，流式光标 ▌）
 *  - 思考块默认折叠（▸ 思考 12.4s）
 *  - 工具卡（点击发编辑跟随占位 RPC）/ 权限卡（四键 + 写规则）/
 *    diff 审阅卡（+N/−M 统计 + 打开对比页 RPC + 接受/拒绝）/
 *    中继卡 / 子代理卡（M7 run_child）/ 截图 artifact 卡（M6 image_artifact）
 *    —— 全部按转录事件流顺序渲染（见 store.applyEvent 各 reducer 的
 *    「交错接线」注释），与消息流正确交错
 *  - 任务清单条
 *  - 底部控制条 = 车钟 + 模型选择 + 权限模式 + 上下文格 + 性能格
 *    （R19：性能格可点击弹「会话统计 · 按模型」表，见 PerfChip）
 *  - M7：输入框整条 /<前缀> 时在 @ 补全同款下拉位置列出匹配 skill
 *    （数据源 store.skills，点击/Enter 补全 /name）
 *  - R30「回滚到这」：用户消息 hover 署名行出回滚按钮（仅有关联检查点
 *    的消息；点击确认后发 rollback 帧，回执 rollback_result/system 行）
 *
 * 输入不打断保证：权限卡等流内卡片只追加进 chat-log，绝不 disable /
 * 清空输入框、不抢焦点；running 期间 Enter 静默（发送仍按运行态门控，
 * 对权限请求的回应渠道是卡上按钮）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import { useStore, isSubagentTool, currentLevel, formatParam, type ChatItem } from '../store/store';
import type { EditorSelectionResult, PermissionMode } from '../protocol';
import { useHostRpc } from '../host/useHostRpc';
import { Telegraph } from '../components/Telegraph';
import { PerfChip } from '../components/PerfChip';
import { ContextMeter } from '../components/ContextMeter';
import { ToolCallCard } from '../components/ToolCallCard';
import { PermissionCard } from '../components/PermissionCard';
import { DiffReviewCard } from '../components/DiffReviewCard';
import { VisionRelayCard } from '../components/VisionRelayCard';
import { SubagentCard } from '../components/SubagentCard';
import { ImageArtifactCard } from '../components/ImageArtifactCard';
import { CompactionCard } from '../components/CompactionCard';
import { GitCommitCard } from '../components/GitCommitCard';
import { TaskListBar } from '../components/TaskListBar';
import { WelcomePage } from '../components/WelcomePage';
import { Icon } from '../components/Icon';
import { useT } from '../i18n';

function fmtDur(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** 附件路径 → 文件名（chip 只显名，路径放 title） */
const baseName = (p: string): string => p.split(/[\\/]/).pop() || p;

function ThinkingBlock({ item }: { item: Extract<ChatItem, { kind: 'thinking' }> }): React.JSX.Element {
  const t = useT();
  const dur = (item.endedAt ?? Date.now()) - item.startedAt;
  return (
    <details className="think">
      <summary>
        <span className="chev">▸</span>
        <Icon name="sparkles" size={12} />
        <span>{t('chat.thinking')}</span>
        <span className="think-time">{item.endedAt ? fmtDur(dur) : `${fmtDur(dur)}…`}</span>
      </summary>
      <pre className="think-body">{item.text}</pre>
    </details>
  );
}

/**
 * 用户消息条（含 R30「回滚到这」）：该消息触发过 git 检查点（store 已把
 * checkpoint 帧回填到 item.checkpointId）时，hover 在署名行显示回滚按钮；
 * 点击弹确认（rollback 帧即显式确认，核侧收到即执行）→ store.
 * rollbackToCheckpoint。无检查点 / 回合运行中（核侧 busy 会拒）不显示。
 */
function UserMessageView({ item }: { item: Extract<ChatItem, { kind: 'user' }> }): React.JSX.Element {
  const t = useT();
  const running = useStore((s) => s.chat.running);
  const showRollback = item.checkpointId != null && !running;
  const onRollback = (): void => {
    if (item.checkpointId == null) return;
    const ok = window.confirm(t('chat.rollbackConfirm'));
    if (ok) useStore.getState().rollbackToCheckpoint(item.checkpointId);
  };
  return (
    <div className="msg-user">
      <div className="who">
        {showRollback && (
          <button
            type="button"
            className="rollback-btn"
            onClick={onRollback}
            title={t('chat.rollbackTitle', { sha: item.checkpointSha?.slice(0, 10) ?? '—' })}
            aria-label={t('chat.rollbackAria')}
          >
            <Icon name="undo" size={11} />{t('chat.rollbackBtn')}
          </button>
        )}
        <span>{t('chat.you')}</span>
      </div>
      <div className="bubble">{item.text}</div>
      {(item.context?.fileRefs?.length ?? 0) > 0 && (
        <div className="attach">
          {item.context?.fileRefs?.map((f) => (
            <span key={f} className="chip" title={f}><Icon name="file" size={12} />{baseName(f)}</span>
          ))}
        </div>
      )}
      {(item.context?.images?.length ?? 0) > 0 && (
        <div className="attach">
          {item.context?.images?.map((f) => (
            <span key={f} className="chip" title={f}><Icon name="image" size={12} />{baseName(f)}</span>
          ))}
        </div>
      )}
      {item.context?.selection && (
        <div className="attach">
          <span className="chip"><Icon name="selection" size={12} />{t('chat.selectionChip', { text: item.context.selection.slice(0, 40) })}</span>
        </div>
      )}
      {item.context?.terminalSel && (
        <div className="attach">
          <span className="chip">{t('chat.terminalChip', { text: item.context.terminalSel.slice(0, 40) })}</span>
        </div>
      )}
      {item.context?.problems && (
        <div className="attach">
          <span className="chip"><Icon name="warning" size={12} />{t('chat.problemsChip')}</span>
        </div>
      )}
    </div>
  );
}

function ItemView({ item }: { item: ChatItem }): React.JSX.Element {
  switch (item.kind) {
    case 'user':
      return <UserMessageView item={item} />;
    case 'assistant':
      return (
        <div className="msg-ai">
          <div className="who"><span className="ai-mark"><Icon name="sparkles" size={11} /></span>Zcode-Py</div>
          <div className="body">
            <Markdown>{item.text}</Markdown>
            {item.streaming && <span className="cursor">▌</span>}
          </div>
        </div>
      );
    case 'thinking':
      return <ThinkingBlock item={item} />;
    case 'tool':
      return isSubagentTool(item.name) ? (
        <SubagentCard item={item} />
      ) : (
        <ToolCallCard item={item} />
      );
    case 'permission':
      return <PermissionCard item={item} />;
    case 'diff':
      return <DiffReviewCard item={item} />;
    case 'relay':
      return <VisionRelayCard item={item} />;
    case 'image':
      // M6（R8）：image_artifact 帧投影 → 截图卡（宿主 RPC 换 webview URI）
      return <ImageArtifactCard item={item} />;
    case 'compaction':
      // M8（R14）：compaction{layer, report} 帧投影 → 压缩报告卡（仅 L2/L3）
      return <CompactionCard item={item} />;
    case 'error':
      return (
        <div className="syserr" title={`kind: ${item.errorKind}`}>
          <Icon name="warning" />{item.message}
        </div>
      );
    case 'system':
      // R30：中性系统提示行（回滚成功等回执，非错误不着红装）
      return (
        <div className="sysline">
          <Icon name="undo" size={12} />{item.text}
        </div>
      );
  }
}

/** 权限模式选项（文案键经 t() 渲染期解析，R20 i18n） */
const PERMISSION_OPTIONS: Array<{ value: PermissionMode; labelKey: string; hintKey: string }> = [
  { value: 'build', labelKey: 'chat.permissionNormal', hintKey: 'chat.permissionNormalHint' },
  { value: 'plan', labelKey: 'chat.permissionPlan', hintKey: 'chat.permissionPlanHint' },
  { value: 'yolo', labelKey: 'chat.permissionYolo', hintKey: 'chat.permissionYoloHint' },
];

export function ChatView(): React.JSX.Element {
  const items = useStore((s) => s.chat.items);
  const running = useStore((s) => s.chat.running);
  const draft = useStore((s) => s.inputDraft);
  const setInputDraft = useStore((s) => s.setInputDraft);
  const sendUserMessage = useStore((s) => s.sendUserMessage);
  const cancelRunning = useStore((s) => s.cancelRunning);
  const telegraph = useStore((s) => s.telegraph);
  const setTelegraphLevel = useStore((s) => s.setTelegraphLevel);
  const providers = useStore((s) => s.providers);
  const switchModel = useStore((s) => s.switchModel);
  const permissionMode = useStore((s) => s.permissionMode);
  const setPermissionMode = useStore((s) => s.setPermissionMode);
  const pendingContext = useStore((s) => s.pendingContext);
  const attachContext = useStore((s) => s.attachContext);
  const removeContextChip = useStore((s) => s.removeContextChip);
  // M7 Skills：斜杠命令提示数据源（store.skills ← control skills_list 回执）
  const skills = useStore((s) => s.skills);
  const skillsRequested = useStore((s) => s.skillsRequested);
  const refreshSkills = useStore((s) => s.refreshSkills);
  // R17/R22：一键 git 流（⚡Commit 按钮开关 GitCommitCard）
  const gitPanelOpen = useStore((s) => s.gitPanelOpen);
  const openGitPanel = useStore((s) => s.openGitPanel);
  const closeGitPanel = useStore((s) => s.closeGitPanel);
  const rpc = useHostRpc();
  const t = useT();

  const [teleOpen, setTeleOpen] = useState(false);
  // M3 @补全：输入尾部的 @token 触发文件下拉（query 为 @ 后前缀）
  const [atMenu, setAtMenu] = useState<{ query: string; items: string[]; active: number } | null>(null);
  const atFetchRef = useRef<number | undefined>(undefined);
  const logRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  // M4/R27：输入历史回溯指针（null = 不在回溯态；否则指向 history 下标，
  // 键入（非历史选择）即复位 —— 见 onDraftChange）
  const histNavRef = useRef<number | null>(null);
  // M7 斜杠命令提示：草稿整体是 /<前缀> 时列出匹配 skill（active 高亮下标
  // + dismissed 关闭标记；换前缀即复位）。与 @ 补全共用 atmenu 下拉样式与
  // 位置（composer-attach 内 bottom:100%）。
  const [slashActive, setSlashActive] = useState(0);
  const [slashDismissed, setSlashDismissed] = useState(false);

  const slashQuery = (() => {
    const m = /^\/([A-Za-z0-9_-]*)$/.exec(draft);
    return m === null ? null : m[1].toLowerCase();
  })();
  const slashItems =
    slashQuery === null
      ? []
      : skills
          .filter((sk) => sk.name.toLowerCase().startsWith(slashQuery))
          .slice(0, 8);
  const slashMenuOpen = slashQuery !== null && slashItems.length > 0 && !slashDismissed;

  // 前缀变化即重置高亮/关闭态（Escape 只对当前前缀生效）
  useEffect(() => {
    setSlashActive(0);
    setSlashDismissed(false);
  }, [slashQuery]);

  // 本会话输入历史：转录内 user 条目按序取文本（resume 重放同样覆盖）；
  // 「另发过的」消息全部经 store.sendUserMessage 落入 items，无需单独记账
  const history = useMemo(
    () =>
      items
        .filter((it): it is Extract<ChatItem, { kind: 'user' }> => it.kind === 'user' && it.text.trim() !== '')
        .map((it) => it.text),
    [items],
  );

  // 流式自动吸底（用户上滚则不打扰）
  useEffect(() => {
    const el = logRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [items]);

  const onLogScroll = (): void => {
    const el = logRef.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  const defProvider = providers.find((p) => p.isDefault) ?? providers[0];
  const modelValue =
    defProvider && defProvider.selectedModel
      ? `${defProvider.id}::${defProvider.selectedModel}`
      : '';

  const onModelChange = (v: string): void => {
    const idx = v.indexOf('::');
    if (idx < 0) return;
    switchModel(v.slice(0, idx), v.slice(idx + 2));
  };

  const onSend = (): void => {
    if (running) {
      cancelRunning();
      return;
    }
    histNavRef.current = null; // 发送即离开历史回溯态（下次 ↑ 从最新一条起）
    sendUserMessage(draft);
  };

  const onDraftChange = (text: string): void => {
    histNavRef.current = null; // 键入即离开历史回溯态（历史选择走 setInputDraft 不经此）
    setInputDraft(text);
    // M7 斜杠命令提示：以 / 起头时惰性拉一次 skills 列表（幂等：skillsRequested）
    if (text.startsWith('/') && !skillsRequested) refreshSkills();
    const m = /@([A-Za-z0-9_./-]*)$/.exec(text);
    if (m === null) {
      setAtMenu(null);
      return;
    }
    const query = m[1];
    setAtMenu((cur) => ({ query, items: cur?.items ?? [], active: 0 }));
    window.clearTimeout(atFetchRef.current);
    atFetchRef.current = window.setTimeout(() => {
      void rpc
        .request<{ files: string[] }>('editor.listFiles', { query })
        .then((r) =>
          setAtMenu((cur) =>
            cur !== null && cur.query === query
              ? { ...cur, items: r.files, active: Math.min(cur.active, Math.max(0, r.files.length - 1)) }
              : cur,
          ),
        )
        .catch(() => undefined); // 宿主未就绪/超时静默
    }, 150);
  };

  const pickAtFile = (file: string): void => {
    setInputDraft(draft.replace(/@([A-Za-z0-9_./-]*)$/, ''));
    attachContext({ fileRefs: [file] });
    setAtMenu(null);
  };

  /** M7：点击/Enter 选中 skill → 补全完整 /name（尾随空格便于继续输入参数） */
  const pickSlashSkill = (name: string): void => {
    setInputDraft(`/${name} `);
    setSlashDismissed(false);
  };

  const onInputKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    if (atMenu !== null && atMenu.items.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setAtMenu({ ...atMenu, active: (atMenu.active + 1) % atMenu.items.length });
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setAtMenu({ ...atMenu, active: (atMenu.active - 1 + atMenu.items.length) % atMenu.items.length });
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        const f = atMenu.items[atMenu.active];
        if (f !== undefined) pickAtFile(f);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setAtMenu(null);
        return;
      }
    }
    // M7 斜杠命令提示：菜单开着时 ↑↓ 换高亮、Enter/Tab 补全、Esc 关闭
    // （与 @ 补全互斥：slash 匹配要求整条草稿是 /<前缀>，不会与 @token 共存）
    if (slashMenuOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSlashActive((a) => (a + 1) % slashItems.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSlashActive((a) => (a - 1 + slashItems.length) % slashItems.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        const sk = slashItems[slashActive];
        if (sk !== undefined) pickSlashSkill(sk.name);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setSlashDismissed(true);
        return;
      }
    }
    // M4/R27：输入历史回溯 —— 空草稿按 ↑ 进入（逐条向更早），↓ 前进；
    // 到尾回空串退出回溯。回溯态下 ↑ 可连续翻（草稿已被历史值填充）。
    if (e.key === 'ArrowUp' && !e.shiftKey) {
      if (history.length === 0) return; // 无历史：保持默认光标行为
      const cur = histNavRef.current;
      if (cur === null) {
        if (draft !== '') return; // 仅空输入时进入回溯（shift+↑ 仍可挪光标）
        histNavRef.current = history.length - 1;
      } else if (cur > 0) {
        histNavRef.current = cur - 1;
      } else {
        e.preventDefault(); // 已到最早一条：停在原值
        return;
      }
      e.preventDefault();
      setInputDraft(history[histNavRef.current]);
      return;
    }
    if (e.key === 'ArrowDown' && !e.shiftKey) {
      const cur = histNavRef.current;
      if (cur === null) return; // 非回溯态：保持默认光标行为
      e.preventDefault();
      const next = cur + 1;
      if (next >= history.length) {
        histNavRef.current = null;
        setInputDraft('');
      } else {
        histNavRef.current = next;
        setInputDraft(history[next]);
      }
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      histNavRef.current = null;
      if (!running) sendUserMessage(draft);
    }
  };

  const attachFile = (): void => {
    void rpc
      .request<{ file: string } | null>('editor.pickFile')
      .then((r) => {
        if (r !== null) attachContext({ fileRefs: [r.file] });
      })
      .catch(() => undefined);
  };

  const attachSelection = (): void => {
    void rpc
      .request<EditorSelectionResult | null>('editor.getSelection')
      .then((sel) => {
        if (sel === null) return; // 无编辑器/无选中：宿主不提示，这里也静默
        attachContext({
          fileRefs: [sel.file],
          selection: `# ${sel.file} L${sel.startLine}-${sel.endLine}\n${sel.text}`,
        });
      })
      .catch(() => undefined);
  };

  // M5（R9）：图片附件 —— 宿主文件对话框挑图 → chip；发送时进 context.images，
  // 主模型无视觉时由核心经视觉中继转述（中继卡回显，原图退场省 token）
  const attachImage = (): void => {
    void rpc
      .request<{ file: string } | null>('editor.pickImage')
      .then((r) => {
        if (r !== null) attachContext({ images: [r.file] });
      })
      .catch(() => undefined);
  };

  return (
    <div className="chat">
      <div className="chat-log" ref={logRef} onScroll={onLogScroll}>
        {items.length === 0 ? (
          <WelcomePage onPick={(t) => setInputDraft(t)} />
        ) : (
          items.map((it) => <ItemView key={it.id} item={it} />)
        )}
      </div>

      <TaskListBar />

      {teleOpen && (
        <section className="telegraph-panel" id="telegraph-panel" aria-label={t('chat.telegraphPanelAria')}>
          <div className="telegraph-heading">
            <div>
              <span className="telegraph-eyebrow">ENGINE ORDER</span>
              <span className="telegraph-title">{t('chat.telegraphPanelTitle')}</span>
            </div>
            <button className="iconbtn" onClick={() => setTeleOpen(false)} title={t('chat.collapseTelegraph')} aria-label={t('chat.collapseTelegraph')}>
              <Icon name="close" />
            </button>
          </div>
          <Telegraph
            levels={telegraph.levels}
            current={telegraph.current}
            paramTranslation={telegraph.paramTranslation}
            onChange={setTelegraphLevel}
          />
        </section>
      )}

      <div className="chat-dock">
        {gitPanelOpen && <GitCommitCard />}
        <div className="ctlbar">
          <div className="ctl-main">
            <select
              className="sel ctl-sel"
              value={modelValue}
              onChange={(e) => onModelChange(e.target.value)}
              title={t('chat.modelTitle')}
              aria-label={t('chat.modelAria')}
            >
              {providers.length === 0 && <option value="">{t('chat.noModel')}</option>}
              {providers.map((p) => (
                <optgroup key={p.id} label={p.name}>
                  {(p.models.length > 0
                    ? p.models
                    : [{ id: p.selectedModel ?? '', contextLength: undefined }]
                  )
                    .filter((m) => m.id)
                    .map((m) => (
                      <option key={m.id} value={`${p.id}::${m.id}`}>
                        {m.id}
                        {p.isDefault ? t('chat.modelDefaultSuffix') : ''}
                      </option>
                    ))}
                </optgroup>
              ))}
            </select>
            <button
              className={teleOpen ? 'chipbtn active' : 'chipbtn'}
              onClick={() => setTeleOpen((v) => !v)}
              title={t('chat.telegraphChipTitle', {
                current: currentLevel(telegraph)?.label ?? telegraph.current,
                param: telegraph.paramTranslation
                  ? ` → ${formatParam(telegraph.paramTranslation)}`
                  : '',
              })}
              aria-expanded={teleOpen}
              aria-controls="telegraph-panel"
            >
              <Icon name="gauge" />
              <span>{currentLevel(telegraph)?.label ?? telegraph.current}</span>
            </button>
            <label className="ctl-permission">
              <Icon name="shield" size={12} />
              <select
                className="sel"
                value={permissionMode}
                onChange={(e) => setPermissionMode(e.target.value as PermissionMode)}
                title={t('chat.permissionSelectTitle')}
                aria-label={t('chat.permissionAria')}
              >
                {PERMISSION_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value} title={t(o.hintKey)}>
                    {t(o.labelKey)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="ctl-meta">
            <ContextMeter />
            <PerfChip />
          </div>
        </div>

        <div className="composer">
          {(atMenu !== null && atMenu.items.length > 0) ||
          slashMenuOpen ||
          (pendingContext.fileRefs?.length ?? 0) > 0 ||
          (pendingContext.images?.length ?? 0) > 0 ||
          pendingContext.selection !== undefined ||
          pendingContext.terminalSel !== undefined ||
          pendingContext.problems !== undefined ? (
            <div className="composer-attach">
              {atMenu !== null && atMenu.items.length > 0 && (
                <div className="atmenu" role="listbox" aria-label={t('chat.fileMenuAria')}>
                  {atMenu.items.map((f, i) => (
                    <div
                      key={f}
                      role="option"
                      aria-selected={i === atMenu.active}
                      className={i === atMenu.active ? 'atitem active' : 'atitem'}
                      onClick={() => pickAtFile(f)}
                      onMouseEnter={() => setAtMenu({ ...atMenu, active: i })}
                    >
                      <Icon name="file" size={12} />{f}
                    </div>
                  ))}
                </div>
              )}
              {slashMenuOpen && (
                <div className="atmenu" role="listbox" aria-label={t('chat.skillMenuAria')}>
                  {slashItems.map((sk, i) => (
                    <div
                      key={sk.name}
                      role="option"
                      aria-selected={i === slashActive}
                      className={i === slashActive ? 'atitem active' : 'atitem'}
                      onClick={() => pickSlashSkill(sk.name)}
                      onMouseEnter={() => setSlashActive(i)}
                      title={sk.path ?? sk.name}
                    >
                      <span className="slashname">/{sk.name}</span>
                      {sk.description !== undefined && sk.description !== '' && (
                        <span className="slashdesc">
                          {sk.description.slice(0, 60)}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              )}
              {(pendingContext.fileRefs?.length ?? 0) > 0 &&
                pendingContext.fileRefs?.map((f) => (
                  <span key={f} className="chip" title={f}>
                    <Icon name="file" size={12} />{baseName(f)}
                    <button className="chip-x" onClick={() => removeContextChip('fileRef', f)} title={t('chat.removeRef')} aria-label={t('chat.removeChipAria', { name: baseName(f) })}>
                      <Icon name="close" size={11} />
                    </button>
                  </span>
                ))}
              {(pendingContext.images?.length ?? 0) > 0 &&
                pendingContext.images?.map((f) => (
                  <span key={f} className="chip" title={f}>
                    <Icon name="image" size={12} />{baseName(f)}
                    <button className="chip-x" onClick={() => removeContextChip('image', f)} title={t('chat.removeImage')} aria-label={t('chat.removeChipAria', { name: baseName(f) })}>
                      <Icon name="close" size={11} />
                    </button>
                  </span>
                ))}
              {pendingContext.selection !== undefined && (
                <span className="chip">
                  <Icon name="selection" size={12} />{t('chat.selectionChipLabel')}
                  <button className="chip-x" onClick={() => removeContextChip('selection')} title={t('chat.removeSelection')} aria-label={t('chat.removeSelection')}>
                    <Icon name="close" size={11} />
                  </button>
                </span>
              )}
              {pendingContext.terminalSel !== undefined && (
                <span className="chip">
                  {t('chat.terminalChipLabel')}
                  <button className="chip-x" onClick={() => removeContextChip('terminalSel')} title={t('chat.removeTerminal')} aria-label={t('chat.removeTerminal')}>
                    <Icon name="close" size={11} />
                  </button>
                </span>
              )}
              {pendingContext.problems !== undefined && (
                <span className="chip">
                  <Icon name="warning" size={12} />Problems
                  <button className="chip-x" onClick={() => removeContextChip('problems')} title={t('chat.removeProblems')} aria-label={t('chat.removeProblems')}>
                    <Icon name="close" size={11} />
                  </button>
                </span>
              )}
            </div>
          ) : null}

          <div className="inputrow">
            <textarea
              className="in inputbox"
              rows={2}
              placeholder={t('chat.inputPlaceholder')}
              title={t('chat.inputTitle')}
              value={draft}
              onChange={(e) => onDraftChange(e.target.value)}
              onKeyDown={onInputKeyDown}
            />
          </div>

          <div className="quickrow">
            <div className="quick-actions">
              <button
                className="quickbtn"
                onClick={attachFile}
                title={t('chat.attachFileTitle')}
              >
                <Icon name="file" /><span className="quick-label">{t('chat.fileBtn')}</span>
              </button>
              <button className="quickbtn" onClick={attachImage} title={t('chat.attachImageTitle')}>
                <Icon name="image" /><span className="quick-label">{t('chat.imageBtn')}</span>
              </button>
              <button
                className="quickbtn"
                onClick={attachSelection}
                title={t('chat.attachSelectionTitle')}
              >
                <Icon name="selection" /><span className="quick-label">{t('chat.selectionBtn')}</span>
              </button>
              <button
                className={gitPanelOpen ? 'quickbtn active' : 'quickbtn'}
                onClick={() => (gitPanelOpen ? closeGitPanel() : openGitPanel())}
                title={t('chat.commitBtnTitle')}
                aria-expanded={gitPanelOpen}
              >
                <Icon name="commit" /><span className="quick-label">{t('chat.commitBtn')}</span>
              </button>
            </div>
            <span className="key-hint">{t('chat.enterHint')}</span>
            <button
              className={running ? 'btn send stop' : 'btn send'}
              onClick={onSend}
              title={running ? t('chat.stopTitle') : t('chat.sendTitle')}
              aria-label={running ? t('chat.stopAria') : t('chat.sendAria')}
            >
              <Icon name={running ? 'stop' : 'send'} />
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
