/**
 * ImageArtifactCard 图片 artifact 卡（M6/R8：image_artifact{path} 帧投影）。
 *
 * - path 是宿主侧本地绝对路径，webview CSP img-src 只认 cspSource → 必须
 *   经宿主 RPC workspace.imageUri 转 asWebviewUri 再作 <img src>；
 * - 宿主侧白名单 = 首工作区根 + ~/.zcode_py 系，越界应答错误 →
 *   回退显示提示文案；<img> 加载失败（onError）同样回退；
 * - 卡片下方常显路径（可审计/可定位落盘文件）。
 */
import { useEffect, useState } from 'react';
import { useHostRpc } from '../host/useHostRpc';
import type { ChatItem } from '../store/store';
import type { WorkspaceImageUriResult } from '../protocol';

export function ImageArtifactCard({
  item,
}: {
  item: Extract<ChatItem, { kind: 'image' }>;
}): React.JSX.Element {
  const rpc = useHostRpc();
  const [uri, setUri] = useState<string | undefined>(undefined);
  /** RPC 失败（路径越界 / webview 未就绪 / 宿主超时）→ 回退路径文本态 */
  const [failed, setFailed] = useState(false);
  /** <img> 本身加载失败（onError）→ 同样回退 */
  const [broken, setBroken] = useState(false);

  useEffect(() => {
    let alive = true;
    setFailed(false);
    setBroken(false);
    rpc
      .request<WorkspaceImageUriResult>('workspace.imageUri', { path: item.path })
      .then((r) => {
        if (!alive) return;
        if (typeof r?.uri === 'string' && r.uri !== '') setUri(r.uri);
        else setFailed(true);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [item.path, rpc]);

  const showImg = uri !== undefined && !failed && !broken;

  return (
    <figure className="card imgcard">
      {showImg ? (
        <img
          className="imgcard-img"
          src={uri}
          alt={item.path}
          loading="lazy"
          onError={() => setBroken(true)}
        />
      ) : (
        <div className="muted small imgcard-fallback">🖼 图片无法显示（路径不在允许范围或 webview 未就绪）</div>
      )}
      <figcaption className="imgcard-path" title={item.path}>
        🖼 {item.path}
      </figcaption>
    </figure>
  );
}
