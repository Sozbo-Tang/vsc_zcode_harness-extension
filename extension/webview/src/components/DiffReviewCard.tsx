/**
 * DiffReviewCard diff 审阅卡（PLAN §6.1：▸ ✏️ edit src/auth.py +38 −12 +
 * [打开对比页][✓接受][✗拒绝]）。
 *  - +N/−M：lib/diffStats 行级统计（前缀/后缀裁剪 + LCS，巨型 diff 多重集估计）
 *  - 打开对比页：host RPC `editor.openDiff`（契约 webview/src/protocol.ts
 *    EditorOpenDiffParams ↔ 宿主 protocol.ts RPC_EDITOR_OPEN_DIFF，宿主
 *    extension.ts 已注册 → editorBridge.openDiffReview 用 vscode.diff 打开
 *    审阅页：左=oldText 虚拟文档，右=磁盘真实文件优先）
 *  - 接受/拒绝：§5 diff_review{id, accepted}
 */
import { useState } from 'react';
import { useStore } from '../store/store';
import type { ChatItem } from '../store/store';
import { useHostRpc } from '../host/useHostRpc';
import { lineDiffStats } from '../lib/diffStats';
import type { EditorOpenDiffParams } from '../protocol';

export function DiffReviewCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'diff' }>;
}): React.JSX.Element {
  const review = useStore((s) => s.reviewDiff);
  const rpc = useHostRpc();
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<string | undefined>(undefined);

  // 行级统计在渲染期计算：裁剪 + LCS 均为毫秒级（见 lib/diffStats 基准注释）
  const { added, removed } = lineDiffStats(item.oldText, item.newText);

  const onOpenDiff = (): void => {
    setOpening(true);
    setOpenError(undefined);
    const params: EditorOpenDiffParams = {
      id: item.id,
      file: item.file,
      oldText: item.oldText,
      newText: item.newText,
    };
    rpc.request('editor.openDiff', params)
      .then(() => setOpenError(undefined))
      .catch((err: unknown) => {
        setOpenError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setOpening(false));
  };

  return (
    <div className="card diffcard">
      <div className="card-row">
        <span className="card-title">
          ✏️ {item.file} <span className="add">+{added}</span>{' '}
          <span className="del">−{removed}</span>
        </span>
      </div>
      <div className="btnrow">
        <button
          className="btn sec"
          disabled={opening}
          title="在 VSCode 对比页审阅（左：改动前 / 右：磁盘文件）"
          onClick={onOpenDiff}
        >
          {opening ? '打开中…' : '打开对比页'}
        </button>
        {item.accepted == null ? (
          <>
            <button className="btn" onClick={() => review(item.id, true)}>
              ✓ 接受
            </button>
            <button className="btn sec" onClick={() => review(item.id, false)}>
              ✗ 拒绝
            </button>
          </>
        ) : (
          <span className="muted">{item.accepted ? '已接受' : '已拒绝'}</span>
        )}
      </div>
      {openError != null && (
        <div className="muted small bad" title={openError}>
          对比页打开失败：{openError.slice(0, 120)}
        </div>
      )}
    </div>
  );
}
