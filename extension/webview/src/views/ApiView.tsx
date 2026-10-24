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
import { useT, type TranslateParams } from '../i18n';
import type { ProviderConfig, ProviderProtocol } from '../protocol';

const PROTOCOLS: Array<{ value: ProviderProtocol; labelKey: string }> = [
  { value: 'anthropic', labelKey: '' }, // 协议名原样显示（zh/en 同文）
  { value: 'openai', labelKey: '' },
  { value: 'gemini', labelKey: '' },
  { value: 'ollama', labelKey: '' },
  { value: 'openrouter', labelKey: '' },
  { value: 'custom', labelKey: 'api.protocolCustom' },
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

/** 模型档案一行摘要（文案经 t() 渲染期解析，R20 i18n） */
function profileLabel(
  p: ProviderConfig,
  t: (key: string, params?: TranslateParams) => string,
): string {
  const m = p.models.find((x) => x.id === p.selectedModel);
  if (!m) return t('api.profileUnknown');
  const ctx = m.contextLength ? `${Math.round(m.contextLength / 1000)}k` : t('api.profileCtxUnknown');
  const caps = [
    m.capabilities?.thinking ? '🧠✓' : '🧠✗',
    m.capabilities?.vision ? '👁✓' : '👁✗',
    m.capabilities?.tools ? '⚙✓' : '⚙✗',
  ].join(' ');
  return t('api.profile', { ctx, caps });
}

export function ApiView(): React.JSX.Element {
  const providers = useStore((s) => s.providers);
  const providerUi = useStore((s) => s.providerUi);
  const saveProvider = useStore((s) => s.saveProvider);
  const deleteProvider = useStore((s) => s.deleteProvider);
  const setDefaultProvider = useStore((s) => s.setDefaultProvider);
  const pingProvider = useStore((s) => s.pingProvider);
  const fetchModels = useStore((s) => s.fetchModels);
  const t = useT();

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
        <span>{t('api.title')}</span>
        <button className="btn" onClick={onNew}>
          {t('api.add')}
        </button>
      </div>

      <div className="provlist">
        {providers.length === 0 && <div className="muted">{t('api.empty')}</div>}
        {providers.map((p) => (
          <div
            key={p.id}
            className={`prov${draft?.id === p.id ? ' sel' : ''}`}
            onClick={() => onSelect(p.id)}
          >
            <button
              className={`dot${p.isDefault ? ' on' : ''}`}
              title={p.isDefault ? t('api.defaultDotTitle') : t('api.setDefaultDotTitle')}
              disabled={p.isDefault}
              onClick={(e) => {
                e.stopPropagation();
                setDefaultProvider(p.id);
              }}
            />
            <span className="prov-name">{p.name}</span>
            {p.isDefault && <span className="chip">{t('api.defaultChip')}</span>}
            <span className="muted prov-proto">{p.protocol}</span>
          </div>
        ))}
      </div>

      {draft && (
        <div className="form">
          <div className="form-title">
            {draft.isNew ? t('api.newProvider') : t('api.editProvider', { name: draft.name || draft.id })}
          </div>

          <div className="form-row">
            <label>{t('api.nameLabel')}</label>
            <input className="in" value={draft.name} onChange={(e) => set('name', e.target.value)} placeholder={t('api.namePlaceholder')} />
          </div>

          <div className="form-row">
            <label>{t('api.protocolLabel')}</label>
            <select className="sel" value={draft.protocol} onChange={(e) => set('protocol', e.target.value as ProviderProtocol)}>
              {PROTOCOLS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.labelKey === '' ? o.value : t(o.labelKey)}
                </option>
              ))}
            </select>
            <label>{t('api.baseURLLabel')}</label>
            <input className="in grow" value={draft.baseURL} onChange={(e) => set('baseURL', e.target.value)} placeholder="https://…" />
          </div>

          <div className="form-row">
            <label>{t('api.apiKeyLabel')}</label>
            <input
              className="in grow"
              type={showKey ? 'text' : 'password'}
              value={newApiKey}
              onChange={(e) => setNewApiKey(e.target.value)}
              placeholder={
                editing?.hasKey
                  ? t('api.keyPlaceholderSaved', { last4: editing.apiKeyLast4 ?? '' })
                  : t('api.keyPlaceholderUnset')
              }
              autoComplete="off"
            />
            <button className="btn sec" onClick={() => setShowKey((v) => !v)} title={t('api.showKeyTitle')}>
              {showKey ? '🙈' : '👁'}
            </button>
          </div>

          <div className="form-row">
            <button
              className="btn"
              disabled={!editing || ui?.pingBusy}
              onClick={() => editing && void pingProvider(editing.id)}
              title={t('api.pingTitle')}
            >
              {t('api.ping')}
            </button>
            {ui?.pingBusy && <span className="muted">⋯</span>}
            {ui?.ping &&
              (ui.ping.ok ? (
                <span className="ok">{t('api.pingOk', { ms: ui.ping.latencyMs ?? '?' })}</span>
              ) : (
                <span className="bad" title={ui.ping.message ?? ''}>
                  ✗ {ui.ping.errorKind ?? t('api.pingErrorKind')}
                </span>
              ))}
            {!editing && <span className="muted">{t('api.saveFirst')}</span>}
          </div>

          <div className="form-row">
            <label>{t('api.modelLabel')}</label>
            <input
              className="in grow"
              list={`models-${draft.id}`}
              value={draft.selectedModel}
              onChange={(e) => set('selectedModel', e.target.value)}
              placeholder={t('api.modelPlaceholder')}
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
              ↘{ui?.modelsBusy ? t('api.fetching') : t('api.fetchList')}
            </button>
          </div>
          {ui?.modelsError && <div className="bad small">{t('api.fetchFailed', { error: ui.modelsError })}</div>}
          {editing && <div className="muted small">{profileLabel(editing, t)}</div>}

          <div className="form-row">
            <label>{t('api.visionRelayLabel')}</label>
            <select
              className="sel"
              value={draft.visionRelayModel}
              onChange={(e) => set('visionRelayModel', e.target.value)}
              title={t('api.visionRelayTitle')}
            >
              <option value="">{t('api.unset')}</option>
              {modelOptions.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
          </div>

          <div className="form-row">
            <label>{t('api.compactionLabel')}</label>
            <select
              className="sel"
              value={draft.compactionModel}
              onChange={(e) => set('compactionModel', e.target.value)}
              title={t('api.compactionTitle')}
            >
              <option value="">{t('api.unset')}</option>
              {modelOptions.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
          </div>

          <div className="form-row end">
            <button className="btn" onClick={onSave} disabled={!draft.name.trim()}>
              {t('api.save')}
            </button>
            {!draft.isNew && (
              <button className="btn sec danger" onClick={onDelete}>
                {deleteArmed ? t('api.deleteConfirm') : t('api.delete')}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
