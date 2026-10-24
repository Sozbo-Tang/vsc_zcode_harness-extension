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
import { useT, type TranslateParams } from '../i18n';
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

/** 建议规则文本（语法 M2 随规则引擎定稿，先自由文本；文案经 t() 生成） */
function suggestRule(
  tool: string,
  args: Record<string, unknown>,
  t: (key: string, params?: TranslateParams) => string,
): string {
  const cmd = args?.command ?? args?.cmd;
  if (typeof cmd === 'string' && cmd) {
    const head = cmd.trim().split(/\s+/)[0] ?? '';
    return head ? t('perm.ruleSuggestCmd', { tool, head }) : t('perm.ruleSuggestAll', { tool });
  }
  const arg = mainArg(args);
  return arg ? t('perm.ruleSuggestArg', { tool, arg }) : t('perm.ruleSuggestAll', { tool });
}

export function PermissionCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'permission' }>;
}): React.JSX.Element {
  const respond = useStore((s) => s.respondPermission);
  const t = useT();
  const [writingRule, setWritingRule] = useState(false);
  const [ruleText, setRuleText] = useState(() => suggestRule(item.tool, item.args, t));
  const decided = item.decision != null;

  return (
    <div className={decided ? 'card permcard decided' : 'card permcard'}>
      <div className="card-head">
        <div className="card-heading">
          <span className="card-kicker">PERMISSION</span>
          <span className="card-title">{t('perm.needConfirm')}</span>
        </div>
        <span className={decided ? 'card-status muted' : 'card-status warn'}>
          {decided ? t('perm.decided') : t('perm.waiting')}
        </span>
      </div>
      <div className="perm-request muted">{t('perm.requestLine')}</div>
      <pre className="card-body cmdline">{commandLine(item.tool, item.args)}</pre>
      <details className="permargs">
        <summary className="muted small">{t('perm.viewArgs')}<span>▸</span></summary>
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
        <div className="card-resolution muted">
          {item.decision === 'allow' && t('perm.allowedOnce')}
          {item.decision === 'always' && t('perm.alwaysAllowed')}
          {item.decision === 'deny' && t('perm.denied')}
          {item.rule != null && item.rule !== '' && (
            <span title={t('perm.ruleTitle')}>{t('perm.ruleSuffix', { rule: item.rule })}</span>
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
            placeholder={t('perm.rulePlaceholder')}
          />
          <div className="btnrow">
            <button
              className="btn"
              disabled={ruleText.trim() === ''}
              title={t('perm.saveRuleTitle')}
              onClick={() => respond(item.id, 'always', ruleText.trim())}
            >
              {t('perm.saveRuleBtn')}
            </button>
            <button className="btn sec" onClick={() => setWritingRule(false)}>
              {t('sessions.cancel')}
            </button>
          </div>
        </div>
      ) : (
        <div className="btnrow permission-actions">
          <button className="btn" onClick={() => respond(item.id, 'allow')}>
            {t('perm.allowOnce')}
          </button>
          <button className="btn sec" onClick={() => respond(item.id, 'always')}>
            {t('perm.alwaysBtn')}
          </button>
          <button className="btn sec danger" onClick={() => respond(item.id, 'deny')}>
            {t('perm.denyBtn')}
          </button>
          <button
            className="btn sec rulebtn"
            title={t('perm.writeRuleTitle')}
            onClick={() => setWritingRule(true)}
          >
            {t('perm.writeRuleBtn')}
          </button>
        </div>
      )}
    </div>
  );
}
