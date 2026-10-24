/**
 * ChatView 聊天视图（PLAN §6.1 完整实现，M1 范围）：
 *  - 流式气泡（react-markdown 渲染正文，流式光标 ▌）
 *  - 思考块默认折叠（▸ 🧠 思考 12.4s）
 *  - 工具卡（点击发编辑跟随占位 RPC）/ 权限卡（四键 + 写规则）/
 *    diff 审阅卡（+N/−M 统计 + 打开对比页 RPC + 接受/拒绝）/
 *    中继卡 / 子代理卡 —— 全部按转录事件流顺序渲染（见 store.applyEvent
 *    各 reducer 的「交错接线」注释），与消息流正确交错
 *  - 任务清单条
 *  - 底部控制条 = 车钟 + 模型选择 + 权限模式 + 上下文格 + 性能格
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
import { TaskListBar } from '../components/TaskListBar';
import { WelcomePage } from '../components/WelcomePage';

function fmtDur(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** 附件路径 → 文件名（chip 只显名，路径放 title） */
const baseName = (p: string): string => p.split(/[\\/]/).pop() || p;

function ThinkingBlock({ item }: { item: Extract<ChatItem, { kind: 'thinking' }> }): React.JSX.Element {
  const dur = (item.endedAt ?? Date.now()) - item.startedAt;
  return (
    <details className="think">
      <summary>
        <span className="chev">▸</span> 🧠 思考 {item.endedAt ? fmtDur(dur) : `${fmtDur(dur)}…`}
      </summary>
      <pre className="think-body">{item.text}</pre>
    </details>
  );
}

function ItemView({ item }: { item: ChatItem }): React.JSX.Element {
  switch (item.kind) {
    case 'user':
      return (
        <div className="msg-user">
          <div className="who">👤 用户</div>
          <div className="bubble">{item.text}</div>
          {(item.context?.fileRefs?.length ?? 0) > 0 && (
            <div className="attach">
              {item.context?.fileRefs?.map((f) => (
                <span key={f} className="chip" title={f}>📎 {baseName(f)}</span>
              ))}
            </div>
          )}
          {(item.context?.images?.length ?? 0) > 0 && (
            <div className="attach">
              {item.context?.images?.map((f) => (
                <span key={f} className="chip" title={f}>🖼 {baseName(f)}</span>
              ))}
            </div>
          )}
          {item.context?.selection && (
            <div className="attach">
              <span className="chip">📎 选中: {item.context.selection.slice(0, 40)}</span>
            </div>
          )}
          {item.context?.terminalSel && (
            <div className="attach">
              <span className="chip">⌨ 终端选中: {item.context.terminalSel.slice(0, 40)}</span>
            </div>
          )}
          {item.context?.problems && (
            <div className="attach">
              <span className="chip">⚠ Problems 诊断</span>
            </div>
          )}
        </div>
      );
    case 'assistant':
      return (
        <div className="msg-ai">
          <div className="who">🤖 Zcode-Py</div>
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
    case 'error':
      return (
        <div className="syserr" title={`kind: ${item.errorKind}`}>
          ⛔ {item.message}
        </div>
      );
  }
}

const PERMISSION_OPTIONS: Array<{ value: PermissionMode; label: string }> = [
  { value: 'build', label: '🛡 普通' },
  { value: 'plan', label: '🛡 只读计划' },
  { value: 'yolo', label: '🛡 YOLO' },
];

export function ChatView(): React.JSX.Element {
  const items = useStore((s) => s.chat.items);
  const running = useStore((s) => s.chat.running);
  const draft = useStore((s) => s.inputDraft);
  const setInputDraft = useStore((s) => s.setInputDraft);
  const sendUserMessage = useStore((s) => s.sendUserMessage);
  const newSession = useStore((s) => s.newSession);
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
  const rpc = useHostRpc();

  const [teleOpen, setTeleOpen] = useState(false);
  // M3 @补全：输入尾部的 @token 触发文件下拉（query 为 @ 后前缀）
  const [atMenu, setAtMenu] = useState<{ query: string; items: string[]; active: number } | null>(null);
  const atFetchRef = useRef<number | undefined>(undefined);
  const logRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  // M4/R27：输入历史回溯指针（null = 不在回溯态；否则指向 history 下标，
  // 键入（非历史选择）即复位 —— 见 onDraftChange）
  const histNavRef = useRef<number | null>(null);

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
      <div className="chat-head">
        <span className="brand">Zcode-Py</span>
        <button className="btn sec" onClick={newSession} title="新会话（并行多开 M4）">
          ＋ 新会话
        </button>
      </div>

      <div className="chat-log" ref={logRef} onScroll={onLogScroll}>
        {items.length === 0 ? (
          <WelcomePage onPick={(t) => setInputDraft(t)} />
        ) : (
          items.map((it) => <ItemView key={it.id} item={it} />)
        )}
      </div>

      <TaskListBar />

      {teleOpen && (
        <div className="telegraph-panel">
          <Telegraph
            levels={telegraph.levels}
            current={telegraph.current}
            paramTranslation={telegraph.paramTranslation}
            onChange={setTelegraphLevel}
          />
        </div>
      )}

      <div className="ctlbar">
        <button
          className="chipbtn"
          onClick={() => setTeleOpen((v) => !v)}
          title={`思考车钟：当前 ${currentLevel(telegraph)?.label ?? telegraph.current}${telegraph.paramTranslation ? ` → ${formatParam(telegraph.paramTranslation)}` : ''}（点击展开盘面）`}
        >
          🚢{currentLevel(telegraph)?.label ?? telegraph.current}
        </button>
        <select
          className="sel ctl-sel"
          value={modelValue}
          onChange={(e) => onModelChange(e.target.value)}
          title="模型（在 API 视图配置 Provider 与模型）"
        >
          {providers.length === 0 && <option value="">未配置模型</option>}
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
                    {p.isDefault ? ' ·默认' : ''}
                  </option>
                ))}
            </optgroup>
          ))}
        </select>
        <select
          className="sel"
          value={permissionMode}
          onChange={(e) => setPermissionMode(e.target.value as PermissionMode)}
          title="权限模式（写/删/命令默认询问；YOLO 需显式切换 — PLAN §9）"
        >
          {PERMISSION_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <ContextMeter />
        <PerfChip />
      </div>

      {(atMenu !== null && atMenu.items.length > 0) ||
      (pendingContext.fileRefs?.length ?? 0) > 0 ||
      (pendingContext.images?.length ?? 0) > 0 ||
      pendingContext.selection !== undefined ||
      pendingContext.terminalSel !== undefined ||
      pendingContext.problems !== undefined ? (
        <div className="composer-attach">
          {atMenu !== null && atMenu.items.length > 0 && (
            <div className="atmenu" role="listbox">
              {atMenu.items.map((f, i) => (
                <div
                  key={f}
                  role="option"
                  aria-selected={i === atMenu.active}
                  className={i === atMenu.active ? 'atitem active' : 'atitem'}
                  onClick={() => pickAtFile(f)}
                  onMouseEnter={() => setAtMenu({ ...atMenu, active: i })}
                >
                  📎 {f}
                </div>
              ))}
            </div>
          )}
          {(pendingContext.fileRefs?.length ?? 0) > 0 &&
            pendingContext.fileRefs?.map((f) => (
              <span key={f} className="chip" title={f}>
                📎 {baseName(f)}
                <button className="chip-x" onClick={() => removeContextChip('fileRef', f)} title="移除引用">
                  ×
                </button>
              </span>
            ))}
          {(pendingContext.images?.length ?? 0) > 0 &&
            pendingContext.images?.map((f) => (
              <span key={f} className="chip" title={f}>
                🖼 {baseName(f)}
                <button className="chip-x" onClick={() => removeContextChip('image', f)} title="移除图片">
                  ×
                </button>
              </span>
            ))}
          {pendingContext.selection !== undefined && (
            <span className="chip">
              ⊞ 选中代码
              <button className="chip-x" onClick={() => removeContextChip('selection')} title="移除选区">
                ×
              </button>
            </span>
          )}
          {pendingContext.terminalSel !== undefined && (
            <span className="chip">
              ⌨ 终端选中
              <button className="chip-x" onClick={() => removeContextChip('terminalSel')} title="移除终端选中">
                ×
              </button>
            </span>
          )}
          {pendingContext.problems !== undefined && (
            <span className="chip">
              ⚠ Problems
              <button className="chip-x" onClick={() => removeContextChip('problems')} title="移除 Problems">
                ×
              </button>
            </span>
          )}
        </div>
      ) : null}

      <div className="inputrow">
        <textarea
          className="in inputbox"
          rows={2}
          placeholder="问点什么… (@引用文件 / Shift+Enter 换行)"
          value={draft}
          onChange={(e) => onDraftChange(e.target.value)}
          onKeyDown={onInputKeyDown}
        />
        <button
          className={running ? 'btn stop' : 'btn send'}
          onClick={onSend}
          title={running ? '停止生成' : '发送（Enter 发送 / Shift+Enter 换行）'}
        >
          {running ? '⏹' : '➤'}
        </button>
      </div>

      <div className="quickrow">
        <button
          className="btn sec"
          onClick={attachFile}
          title="引用工作区文件（也可在输入框输入 @ 触发补全）"
        >
          📎 文件
        </button>
        <button className="btn sec" onClick={attachImage} title="附加图片（主模型无视觉时自动经视觉中继转述，PLAN R9）">
          🖼 图片
        </button>
        <button
          className="btn sec"
          onClick={attachSelection}
          title="把编辑器当前选中代码附为上下文"
        >
          ⊞ 选中代码
        </button>
        <button className="btn sec" disabled title="一键 Commit & Push — R17（后续里程碑）">
          ⚡ Commit
        </button>
      </div>
    </div>
  );
}
