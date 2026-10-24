/**
 * ApiView API 连接管理（PLAN §6.3 完整实现，M1 范围 / R5）：
 *  - Provider 列表与新增/编辑/删除、默认 Provider 切换
 *  - 协议下拉 / BaseURL / API Key 掩码显示（Key 只进 SecretStorage，
 *    webview 仅拿到 hasKey/尾号，新 Key 只在保存时经 rpc 送宿主，research §3）
 *  - ⚡测试连接（ping）：显示延迟与结果 / 错误分类
 *  - ↘拉取模型列表；模型档案（上下文长度/能力标记）展示
 *  - 视觉中继模型 与 压缩专用模型 选择
 */
import { useState } from 'react';
import { useStore } from '../store/store';
import type { ProviderConfig, ProviderProtocol } from '../protocol';

const PROTOCOLS: Array<{ value: ProviderProtocol; label: string }> = [
  { value: 'anthropic', label: 'anthropic' },
  { value: 'openai', label: 'openai' },
  { value: 'gemini', label: 'gemini' },
  { value: 'ollama', label: 'ollama' },
  { value: 'openrouter', label: 'openrouter' },
  { value: 'custom', label: '自定义(anthropic 兼容)' },
];

interface Draft {
  id: string;
  isNew: boolean;
  name: string;
  protocol: ProviderProtocol;
  baseURL: string;
  selectedModel: string;
  visionRelayModel: string;
  compactionModel: string;
}

function emptyDraft(): Draft {
  return {
    id: `prov-${Date.now().toString(36)}`,
    isNew: true,
    name: '',
    protocol: 'anthropic',
    baseURL: 'https://open.bigmodel.cn/api/anthropic', // PLAN §10 默认端点（GLM anthropic 兼容）
    selectedModel: '',
    visionRelayModel: '',
    compactionModel: '',
  };
}

function draftFrom(p: ProviderConfig): Draft {
  return {
    id: p.id,
    isNew: false,
    name: p.name,
    protocol: p.protocol,
    baseURL: p.baseURL,
    selectedModel: p.selectedModel ?? '',
    visionRelayModel: p.visionRelayModel ?? '',
    compactionModel: p.compactionModel ?? '',
  };
}

function profileLabel(p: ProviderConfig): string {
  const m = p.models.find((x) => x.id === p.selectedModel);
  if (!m) return '档案未知（拉取列表或手填后可知）';
  const ctx = m.contextLength ? `${Math.round(m.contextLength / 1000)}k` : '上下文?';
  const caps = [
    m.capabilities?.thinking ? '🧠✓' : '🧠✗',
    m.capabilities?.vision ? '👁✓' : '👁✗',
    m.capabilities?.tools ? '⚙✓' : '⚙✗',
  ].join(' ');
  return `档案 ${ctx} · ${caps}`;
}

export function ApiView(): React.JSX.Element {
  const providers = useStore((s) => s.providers);
  const providerUi = useStore((s) => s.providerUi);
  const saveProvider = useStore((s) => s.saveProvider);
  const deleteProvider = useStore((s) => s.deleteProvider);
  const setDefaultProvider = useStore((s) => s.setDefaultProvider);
  const pingProvider = useStore((s) => s.pingProvider);
  const fetchModels = useStore((s) => s.fetchModels);

  const [draft, setDraft] = useState<Draft | null>(null);
  const [newApiKey, setNewApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [deleteArmed, setDeleteArmed] = useState(false);

  const editing = draft && !draft.isNew ? providers.find((p) => p.id === draft.id) : undefined;
  const ui = editing ? providerUi[editing.id] : undefined;

  const set = <K extends keyof Draft>(k: K, v: Draft[K]): void =>
    setDraft((d) => (d ? { ...d, [k]: v } : d));

  const onNew = (): void => {
    setDraft(emptyDraft());
    setNewApiKey('');
    setDeleteArmed(false);
  };

  const onSelect = (id: string): void => {
    const p = providers.find((x) => x.id === id);
    if (p) {
      setDraft(draftFrom(p));
      setNewApiKey('');
      setDeleteArmed(false);
    }
  };

  const onSave = (): void => {
    if (!draft || !draft.name.trim()) return;
    const prev = providers.find((p) => p.id === draft.id);
    const config: ProviderConfig = {
      id: draft.id,
      name: draft.name.trim(),
      protocol: draft.protocol,
      baseURL: draft.baseURL.trim(),
      isDefault: prev ? prev.isDefault : providers.length === 0,
      models: prev?.models ?? [],
      selectedModel: draft.selectedModel.trim() || undefined,
      visionRelayModel: draft.visionRelayModel || undefined,
      compactionModel: draft.compactionModel || undefined,
      hasKey: prev?.hasKey ?? false,
      apiKeyLast4: prev?.apiKeyLast4,
    };
    void saveProvider(config, newApiKey.trim() || undefined);
    setNewApiKey('');
    setDeleteArmed(false);
    setDraft({ ...draft, isNew: false });
  };

  const onDelete = (): void => {
    if (!draft) return;
    if (!deleteArmed) {
      setDeleteArmed(true);
      return;
    }
    deleteProvider(draft.id);
    setDraft(null);
    setDeleteArmed(false);
  };

  const modelOptions = editing?.models ?? [];

  return (
    <div className="view apiview">
      <div className="view-head">
        <span>API 连接</span>
        <button className="btn" onClick={onNew}>
          ＋新增
        </button>
      </div>

      <div className="provlist">
        {providers.length === 0 && <div className="muted">尚无 Provider，点「＋新增」创建。</div>}
        {providers.map((p) => (
          <div
            key={p.id}
            className={`prov${draft?.id === p.id ? ' sel' : ''}`}
            onClick={() => onSelect(p.id)}
          >
            <button
              className={`dot${p.isDefault ? ' on' : ''}`}
              title={p.isDefault ? '默认 Provider' : '点击设为默认'}
              disabled={p.isDefault}
              onClick={(e) => {
                e.stopPropagation();
                setDefaultProvider(p.id);
              }}
            />
            <span className="prov-name">{p.name}</span>
            {p.isDefault && <span className="chip">默认</span>}
            <span className="muted prov-proto">{p.protocol}</span>
          </div>
        ))}
      </div>

      {draft && (
        <div className="form">
          <div className="form-title">{draft.isNew ? '新增 Provider' : `编辑：${draft.name || draft.id}`}</div>

          <div className="form-row">
            <label>名称</label>
            <input className="in" value={draft.name} onChange={(e) => set('name', e.target.value)} placeholder="如 GLM 编程套餐" />
          </div>

          <div className="form-row">
            <label>协议</label>
            <select className="sel" value={draft.protocol} onChange={(e) => set('protocol', e.target.value as ProviderProtocol)}>
              {PROTOCOLS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <label>BaseURL</label>
            <input className="in grow" value={draft.baseURL} onChange={(e) => set('baseURL', e.target.value)} placeholder="https://…" />
          </div>

          <div className="form-row">
            <label>API Key</label>
            <input
              className="in grow"
              type={showKey ? 'text' : 'password'}
              value={newApiKey}
              onChange={(e) => setNewApiKey(e.target.value)}
              placeholder={
                editing?.hasKey
                  ? `已保存 ••••${editing.apiKeyLast4 ?? ''}（输入则替换）`
                  : '未配置（仅存 SecretStorage，不落盘）'
              }
              autoComplete="off"
            />
            <button className="btn sec" onClick={() => setShowKey((v) => !v)} title="显示/隐藏输入的 Key">
              {showKey ? '🙈' : '👁'}
            </button>
          </div>

          <div className="form-row">
            <button
              className="btn"
              disabled={!editing || ui?.pingBusy}
              onClick={() => editing && void pingProvider(editing.id)}
              title="保存后可测试连接（宿主代理发一条最小请求，显示延迟/错误分类）"
            >
              ⚡测试连接
            </button>
            {ui?.pingBusy && <span className="muted">⋯</span>}
            {ui?.ping &&
              (ui.ping.ok ? (
                <span className="ok">✓ {ui.ping.latencyMs ?? '?'}ms 模型正常</span>
              ) : (
                <span className="bad" title={ui.ping.message ?? ''}>
                  ✗ {ui.ping.errorKind ?? '错误'}
                </span>
              ))}
            {!editing && <span className="muted">先保存后可测试</span>}
          </div>

          <div className="form-row">
            <label>模型</label>
            <input
              className="in grow"
              list={`models-${draft.id}`}
              value={draft.selectedModel}
              onChange={(e) => set('selectedModel', e.target.value)}
              placeholder="拉取列表或手填模型 ID"
            />
            <datalist id={`models-${draft.id}`}>
              {modelOptions.map((m) => (
                <option key={m.id} value={m.id} />
              ))}
            </datalist>
            <button
              className="btn sec"
              disabled={!editing || ui?.modelsBusy}
              onClick={() => editing && void fetchModels(editing.id)}
            >
              ↘{ui?.modelsBusy ? '拉取中⋯' : '拉取列表'}
            </button>
          </div>
          {ui?.modelsError && <div className="bad small">拉取失败：{ui.modelsError}</div>}
          {editing && <div className="muted small">{profileLabel(editing)}</div>}

          <div className="form-row">
            <label>🖼 视觉中继</label>
            <select
              className="sel"
              value={draft.visionRelayModel}
              onChange={(e) => set('visionRelayModel', e.target.value)}
              title="主模型不支持视觉时，图片路由给该多模态模型转述（R9/M5）"
            >
              <option value="">（未设置）</option>
              {modelOptions.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
          </div>

          <div className="form-row">
            <label>🗜 压缩专用</label>
            <select
              className="sel"
              value={draft.compactionModel}
              onChange={(e) => set('compactionModel', e.target.value)}
              title="上下文 L3 摘要压缩使用的便宜模型（R14/M8）"
            >
              <option value="">（未设置）</option>
              {modelOptions.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
          </div>

          <div className="form-row end">
            <button className="btn" onClick={onSave} disabled={!draft.name.trim()}>
              💾 保存
            </button>
            {!draft.isNew && (
              <button className="btn sec danger" onClick={onDelete}>
                {deleteArmed ? '🗑 确认删除？' : '🗑 删除'}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
