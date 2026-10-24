/**
 * PermissionCard 权限卡（PLAN §6.3 线框：⚠ 需要确认 + 允许/会话内总是/
 * 拒绝/写规则）。
 *  - 展示：bash 类工具渲染 `$ <command>` 命令行，其余按 `工具名 + 参数键值`
 *    （参数完整 JSON 收进「参数」折叠块供审计）
 *  - 四键全部走 §5 permission_response：allow / always / deny；
 *    「⚙写规则」展开内联编辑器（预填建议规则），保存时随 decision='always'
 *    附带 rule 文本 —— M2 权限规则引擎消费并持久化（PLAN §3.2），当前
 *    Python 侧 decode 忽略未知字段，规则仅随会话生效（同 always），见
 *    protocol.ts PermissionResponseMessage.rule 注释
 */
import { useState } from 'react';
import { useStore } from '../store/store';
import type { ChatItem } from '../store/store';

/** 主参数预览：常见键优先，否则第一个 string 值 */
function mainArg(args: Record<string, unknown>): string {
  for (const k of ['command', 'cmd', 'file_path', 'path', 'pattern', 'url', 'query']) {
    const v = args?.[k];
    if (typeof v === 'string' && v) return v;
  }
  const first = Object.values(args ?? {})[0];
  return typeof first === 'string' ? first : '';
}

/** 命令行预览：bash 类 `$ cmd`；其余 `tool 主参数` */
function commandLine(tool: string, args: Record<string, unknown>): string {
  const cmd = args?.command ?? args?.cmd;
  if (typeof cmd === 'string' && cmd) return `$ ${cmd}`;
  const arg = mainArg(args);
  return arg ? `${tool} ${arg}` : tool;
}

/** 建议规则文本（语法 M2 随规则引擎定稿，先自由文本） */
function suggestRule(tool: string, args: Record<string, unknown>): string {
  const cmd = args?.command ?? args?.cmd;
  if (typeof cmd === 'string' && cmd) {
    const head = cmd.trim().split(/\s+/)[0] ?? '';
    return head ? `允许 ${tool}: ${head} …` : `允许 ${tool}: *`;
  }
  const arg = mainArg(args);
  return arg ? `允许 ${tool}: ${arg}` : `允许 ${tool}: *`;
}

export function PermissionCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'permission' }>;
}): React.JSX.Element {
  const respond = useStore((s) => s.respondPermission);
  const [writingRule, setWritingRule] = useState(false);
  const [ruleText, setRuleText] = useState(() => suggestRule(item.tool, item.args));
  const decided = item.decision != null;

  return (
    <div className="card permcard">
      <div className="card-title warn">⚠ 需要确认 — Agent 想执行:</div>
      <pre className="card-body cmdline">{commandLine(item.tool, item.args)}</pre>
      <details className="permargs">
        <summary className="muted small">参数 ▾</summary>
        <pre className="card-body">
          {(() => {
            try {
              return JSON.stringify(item.args, null, 2).slice(0, 1200);
            } catch {
              return String(item.args);
            }
          })()}
        </pre>
      </details>

      {decided ? (
        <div className="muted">
          已处理：
          {item.decision === 'allow' && '✓ 允许（本次）'}
          {item.decision === 'always' && '✓✓ 会话内总是允许'}
          {item.decision === 'deny' && '✗ 已拒绝'}
          {item.rule != null && item.rule !== '' && (
            <span title="M2 权限规则引擎落地后持久化（PLAN §3.2）">（规则：{item.rule}）</span>
          )}
        </div>
      ) : writingRule ? (
        <div className="ruleedit">
          <input
            className="in"
            type="text"
            value={ruleText}
            onChange={(e) => setRuleText(e.target.value)}
            // 权限卡不与聊天输入抢焦点：规则框聚焦由用户点击触发，非自动
            placeholder="规则文本（语法 M2 定稿，先自由文本）"
          />
          <div className="btnrow">
            <button
              className="btn"
              disabled={ruleText.trim() === ''}
              title="保存规则并本次及会话内总是允许"
              onClick={() => respond(item.id, 'always', ruleText.trim())}
            >
              ✓ 保存规则并允许
            </button>
            <button className="btn sec" onClick={() => setWritingRule(false)}>
              取消
            </button>
          </div>
        </div>
      ) : (
        <div className="btnrow">
          <button className="btn" onClick={() => respond(item.id, 'allow')}>
            ✓ 允许
          </button>
          <button className="btn" onClick={() => respond(item.id, 'always')}>
            ✓✓ 会话内总是
          </button>
          <button className="btn sec" onClick={() => respond(item.id, 'deny')}>
            ✗ 拒绝
          </button>
          <button
            className="btn sec"
            title="写入持久权限规则（M2 规则引擎消费 rule 字段）"
            onClick={() => setWritingRule(true)}
          >
            ⚙ 写规则
          </button>
        </div>
      )}
    </div>
  );
}
