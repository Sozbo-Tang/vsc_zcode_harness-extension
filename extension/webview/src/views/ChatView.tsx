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
import { useEffect, useRef, useState } from 'react';
import Markdown from 'react-markdown';
import { useStore, isSubagentTool, currentLevel, formatParam, type ChatItem } from '../store/store';
import type { PermissionMode } from '../protocol';
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
                <span key={f} className="chip">📎 {f}</span>
              ))}
            </div>
          )}
          {item.context?.selection && (
            <div className="attach">
              <span className="chip">📎 选中: {item.context.selection.slice(0, 40)}</span>
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

  const [teleOpen, setTeleOpen] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);

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
    sendUserMessage(draft);
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

      <div className="inputrow">
        <textarea
          className="in inputbox"
          rows={2}
          placeholder="问点什么… (@引用 / 技能)"
          value={draft}
          onChange={(e) => setInputDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              if (!running) sendUserMessage(draft);
            }
          }}
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
        <button className="btn sec" disabled title="文件引用 — M3 编辑器联动">
          📎 文件
        </button>
        <button className="btn sec" disabled title="图片输入 — M5 Vision 中继">
          🖼 图片
        </button>
        <button className="btn sec" disabled title="选中代码作为上下文 — M3 编辑器联动">
          ⊞ 选中代码
        </button>
        <button className="btn sec" disabled title="一键 Commit & Push — R17（后续里程碑）">
          ⚡ Commit
        </button>
      </div>
    </div>
  );
}
