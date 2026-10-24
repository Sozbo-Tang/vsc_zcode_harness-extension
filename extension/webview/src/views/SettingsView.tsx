/**
 * SettingsView 设置视图（R15/R20 实装，七组全部落地；PLAN §6.5；R6 收尾
 * 追加「音效」组，共八组）。
 *
 *  1. 通用：界面语言（zh/en）+ 字号缩放（App 根节点 font-size）——webview
 *     本地设置，vscode getState/setState 持久（i18n 键抽取属后续轨道，先
 *     中文字面量）；
 *  2. 音效：车钟换挡咔哒开关（R6 收尾）——control settings_set 落
 *     settings.json 键 telegraphClickSound（默认开；WebAudio 合成无音频
 *     资源，AudioContext 失败静默降级）；
 *  3. Agent：系统提示词追加文本 + 最大轮数——control settings_set 落
 *     ~/.zcode-py/config/settings.json，核侧热应用（后续轮次生效）；
 *  4. 压缩：L3 自动摘要阈值（默认 85%）+ 压缩专用模型覆盖；
 *  5. 视觉中继：转述预设（ocr/通用描述/前端还原/自定义）+ 自定义提示词；
 *  6. 内联补全：读写 VS Code 原生配置 zcode-py.inlineCompletion.enabled
 *     ——宿主 config.get/config.update RPC（不进 settings.json）；
 *  7. 隐私护栏：文件模式列表可编辑（每行一条 glob；镜像核心
 *     privacy.PRIVACY_BASENAME_PATTERNS 的默认清单供「恢复默认」）；
 *  8. 数据：会话目录显示 + 打开（宿主 workspace.revealPath）+ 清除会话
 *     数据（二次确认；control clear_sessions，保留 config）。
 *
 * 交互约定：每组一行卡片；本地项即时保存，核心项显式「保存」按钮（避免
 * 逐键发 settings_set）；保存态/错误经 settingsNotice 统一展示。
 */
import { useEffect, useRef, useState } from 'react';
import { useStore } from '../store/store';
import { hostRequest } from '../host/transport';
import { useT, t as ti18n } from '../i18n';
import type { ConfigGetResult, ConfigUpdateResult, VisionRelayPreset } from '../protocol';
import { DEFAULT_PRIVACY_PATTERNS } from './settingsDefaults';

/** 内联补全开关的 VS Code 原生配置键（package.json contributes） */
const INLINE_CONFIG_KEY = 'zcode-py.inlineCompletion.enabled';

/** 视觉中继预设选项（id ↔ 文案键；标签经 t() 渲染期解析，镜像 vision_relay._PRESET_PROMPTS） */
const PRESETS: Array<{ id: VisionRelayPreset; labelKey: string }> = [
  { id: 'ocr', labelKey: 'presets.ocr' },
  { id: 'describe', labelKey: 'presets.describe' },
  { id: 'frontend', labelKey: 'presets.frontend' },
  { id: 'custom', labelKey: 'presets.custom' },
];

/* ---------- 通用卡片骨架 ---------- */

function GroupCard({
  title,
  desc,
  children,
  extra,
}: {
  title: string;
  desc?: string;
  children: React.ReactNode;
  extra?: React.ReactNode;
}): React.JSX.Element {
  return (
    <div className="setting-group">
      <div className="sethead">
        <div>
          <div className="settitle">{title}</div>
          {desc !== undefined && desc !== '' && (
            <div className="muted small setdesc">{desc}</div>
          )}
        </div>
        {extra}
      </div>
      <div className="setbody">{children}</div>
    </div>
  );
}

/* ---------- 1. 通用（本地 UI 设置，即时保存） ---------- */

function GeneralGroup(): React.JSX.Element {
  const uiLanguage = useStore((s) => s.uiLanguage);
  const fontScale = useStore((s) => s.fontScale);
  const setUiLanguage = useStore((s) => s.setUiLanguage);
  const setFontScale = useStore((s) => s.setFontScale);
  const t = useT();

  return (
    <GroupCard title={t('settings.generalTitle')} desc={t('settings.generalDesc')}>
      <div className="setrow">
        <label htmlFor="set-lang">{t('settings.languageLabel')}</label>
        <select
          id="set-lang"
          className="sel"
          value={uiLanguage}
          onChange={(e) => setUiLanguage(e.target.value === 'en' ? 'en' : 'zh')}
          title={t('settings.languageTitle')}
        >
          <option value="zh">{t('settings.zhOption')}</option>
          <option value="en">{t('settings.enOption')}</option>
        </select>
      </div>
      <div className="setrow">
        <label htmlFor="set-font">{t('settings.fontLabel')}</label>
        <select
          id="set-font"
          className="sel"
          value={String(fontScale)}
          onChange={(e) => setFontScale(Number(e.target.value))}
          title={t('settings.fontTitle')}
        >
          {[85, 90, 95, 100, 105, 110, 120, 130, 150].map((v) => (
            <option key={v} value={v}>
              {v === 100 ? t('settings.fontDefault') : `${v}%`}
            </option>
          ))}
        </select>
      </div>
    </GroupCard>
  );
}

/* ---------- 2. 音效（车钟咔哒，核心 settings 通道，即时保存） ---------- */

function SoundGroup(): React.JSX.Element {
  const settings = useStore((s) => s.settings);
  const saving = useStore((s) => s.settingsSaving);
  const saveSettings = useStore((s) => s.saveSettings);
  const t = useT();
  // 未拉取（null）按默认开展示；与核侧/parseSettingsDetail 同语义：
  // 只有显式 false 才关
  const enabled = settings === null || settings.telegraphClickSound !== false;

  /** 复选框即时保存（与视觉中继预设同口径；合并补丁不碰其他键） */
  const toggle = (next: boolean): void => {
    saveSettings({ telegraphClickSound: next });
  };

  return (
    <GroupCard title={t('settings.soundTitle')} desc={t('settings.soundDesc')}>
      <div className="setrow">
        <label htmlFor="set-sound">{t('settings.soundLabel')}</label>
        <input
          id="set-sound"
          type="checkbox"
          checked={enabled}
          disabled={saving}
          onChange={(e) => toggle(e.target.checked)}
          title={t('settings.soundCheckboxTitle')}
        />
        <span className="muted small">
          {settings === null
            ? t('settings.soundDefaultOn')
            : enabled
              ? t('settings.soundOn')
              : t('settings.soundOff')}
        </span>
      </div>
    </GroupCard>
  );
}

/* ---------- 3. Agent（系统提示词追加 + 最大轮数） ---------- */

function AgentGroup(): React.JSX.Element {
  const settings = useStore((s) => s.settings);
  const saving = useStore((s) => s.settingsSaving);
  const saveSettings = useStore((s) => s.saveSettings);
  const t = useT();
  const [append, setAppend] = useState('');
  const [maxTurns, setMaxTurns] = useState('8');
  const loadedRef = useRef(false);

  // settings 回填一次（settings_get 到达；仅未编辑过时，避免覆盖输入中草稿）
  useEffect(() => {
    if (settings === null || loadedRef.current) return;
    loadedRef.current = true;
    setAppend(settings.systemPromptAppend);
    setMaxTurns(String(settings.maxTurns));
  }, [settings]);

  const submit = (): void => {
    const turns = Number(maxTurns);
    if (!Number.isInteger(turns) || turns < 1 || turns > 200) return; // 输入框已约束
    saveSettings({ systemPromptAppend: append, maxTurns: turns });
  };

  return (
    <GroupCard title="Agent" desc={t('settings.agentDesc')}>
      <div className="setrow top">
        <label htmlFor="set-append" title={t('settings.appendTitle')}>
          {t('settings.appendLabel')}
        </label>
        <textarea
          id="set-append"
          className="in grow"
          rows={3}
          value={append}
          placeholder={t('settings.appendPlaceholder')}
          onChange={(e) => setAppend(e.target.value)}
        />
      </div>
      <div className="setrow end">
        <label htmlFor="set-turns">{t('settings.maxTurnsLabel')}</label>
        <input
          id="set-turns"
          className="in setnum"
          type="number"
          min={1}
          max={200}
          value={maxTurns}
          onChange={(e) => setMaxTurns(e.target.value)}
          title={t('settings.maxTurnsTitle')}
        />
        <button className="btn" onClick={submit} disabled={saving || settings === null}>
          {saving ? t('settings.saving') : t('settings.save')}
        </button>
      </div>
    </GroupCard>
  );
}

/* ---------- 4. 压缩（L3 阈值 + 专用模型） ---------- */

function CompactionGroup(): React.JSX.Element {
  const settings = useStore((s) => s.settings);
  const saving = useStore((s) => s.settingsSaving);
  const saveSettings = useStore((s) => s.saveSettings);
  const t = useT();
  const [pct, setPct] = useState('85');
  const [model, setModel] = useState('');
  const loadedRef = useRef(false);

  useEffect(() => {
    if (settings === null || loadedRef.current) return;
    loadedRef.current = true;
    setPct(String(Math.round(settings.compaction.summarizeThreshold * 100)));
    setModel(settings.compaction.compactionModel);
  }, [settings]);

  const submit = (): void => {
    const n = Number(pct);
    if (!Number.isFinite(n) || n <= 0 || n > 100) return;
    saveSettings({
      compaction: { summarizeThreshold: n / 100, compactionModel: model.trim() },
    });
  };

  return (
    <GroupCard title={t('settings.compTitle')} desc={t('settings.compDesc')}>
      <div className="setrow">
        <label htmlFor="set-threshold" title={t('settings.thresholdTitle')}>
          {t('settings.thresholdLabel')}
        </label>
        <input
          id="set-threshold"
          className="in setnum"
          type="number"
          min={1}
          max={100}
          value={pct}
          onChange={(e) => setPct(e.target.value)}
        />
        <span className="muted small">%</span>
      </div>
      <div className="setrow end">
        <label htmlFor="set-compmodel" title={t('settings.compModelTitle')}>
          {t('settings.compModelLabel')}
        </label>
        <input
          id="set-compmodel"
          className="in grow"
          value={model}
          placeholder={t('settings.compModelPlaceholder')}
          onChange={(e) => setModel(e.target.value)}
        />
        <button className="btn" onClick={submit} disabled={saving || settings === null}>
          {saving ? t('settings.saving') : t('settings.save')}
        </button>
      </div>
    </GroupCard>
  );
}

/* ---------- 5. 视觉中继（预设 + 自定义提示词） ---------- */

function VisionRelayGroup(): React.JSX.Element {
  const settings = useStore((s) => s.settings);
  const saving = useStore((s) => s.settingsSaving);
  const saveSettings = useStore((s) => s.saveSettings);
  const t = useT();
  const [preset, setPreset] = useState<VisionRelayPreset>('describe');
  const [custom, setCustom] = useState('');
  const loadedRef = useRef(false);

  useEffect(() => {
    if (settings === null || loadedRef.current) return;
    loadedRef.current = true;
    setPreset(settings.visionRelayPreset);
    setCustom(settings.visionRelayCustomPrompt);
  }, [settings]);

  /** 预设切换即时保存；自定义提示词（多行文本）走显式保存 */
  const changePreset = (next: VisionRelayPreset): void => {
    setPreset(next);
    saveSettings({
      visionRelayPreset: next,
      visionRelayCustomPrompt: custom,
    });
  };

  return (
    <GroupCard title={t('settings.visionTitle')} desc={t('settings.visionDesc')}>
      <div className="setrow">
        <label htmlFor="set-preset">{t('settings.presetLabel')}</label>
        <select
          id="set-preset"
          className="sel"
          value={preset}
          onChange={(e) => changePreset(e.target.value as VisionRelayPreset)}
        >
          {PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              {t(p.labelKey)}
            </option>
          ))}
        </select>
      </div>
      {preset === 'custom' && (
        <>
          <div className="setrow top">
            <label htmlFor="set-customprompt">{t('settings.customPromptLabel')}</label>
            <textarea
              id="set-customprompt"
              className="in grow"
              rows={3}
              value={custom}
              placeholder={t('settings.customPromptPlaceholder')}
              onChange={(e) => setCustom(e.target.value)}
            />
          </div>
          <div className="setrow end">
            <span className="muted small grow">{t('settings.customPromptHint')}</span>
            <button
              className="btn"
              disabled={saving}
              onClick={() => saveSettings({ visionRelayCustomPrompt: custom })}
            >
              {saving ? t('settings.saving') : t('settings.save')}
            </button>
          </div>
        </>
      )}
    </GroupCard>
  );
}

/* ---------- 6. 内联补全（VS Code 原生配置，宿主 config.* RPC） ---------- */

function InlineGroup(): React.JSX.Element {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | undefined>(undefined);
  const t = useT();

  useEffect(() => {
    // 读原生配置（宿主 config.get；undefined/false 都按关闭展示）。
    // 失败文案走事件时翻译（ti18n，调用时读当前 locale）——effect 只挂载
    // 一次，hook 版 t 闭包会停在挂载语言
    hostRequest<ConfigGetResult>('config.get', { key: INLINE_CONFIG_KEY })
      .then((r) => setEnabled(r?.value === true))
      .catch((err) => setError(err instanceof Error ? err.message : ti18n('settings.readFailed')));
  }, []);

  const toggle = (next: boolean): void => {
    setBusy(true);
    setError(undefined);
    hostRequest<ConfigUpdateResult>('config.update', { key: INLINE_CONFIG_KEY, value: next })
      .then(() => setEnabled(next))
      .catch((err) => setError(err instanceof Error ? err.message : ti18n('settings.writeFailed')))
      .finally(() => setBusy(false));
  };

  return (
    <GroupCard title={t('settings.inlineTitle')} desc={t('settings.inlineDesc')}>
      <div className="setrow">
        <label htmlFor="set-inline">{t('settings.inlineLabel')}</label>
        <input
          id="set-inline"
          type="checkbox"
          checked={enabled === true}
          disabled={busy || enabled === null}
          onChange={(e) => toggle(e.target.checked)}
          title={t('settings.inlineCheckboxTitle')}
        />
        <span className="muted small">
          {enabled === null
            ? t('settings.inlineReading')
            : enabled
              ? t('settings.inlineOn')
              : t('settings.inlineOff')}
        </span>
      </div>
      {error !== undefined && <div className="bad small">{error}</div>}
    </GroupCard>
  );
}

/* ---------- 7. 隐私护栏（文件模式列表） ---------- */

function PrivacyGroup(): React.JSX.Element {
  const settings = useStore((s) => s.settings);
  const saving = useStore((s) => s.settingsSaving);
  const saveSettings = useStore((s) => s.saveSettings);
  const t = useT();
  const [text, setText] = useState('');
  const loadedRef = useRef(false);

  useEffect(() => {
    if (settings === null || loadedRef.current) return;
    loadedRef.current = true;
    setText(settings.privacyFilePatterns.join('\n'));
  }, [settings]);

  /** 文本域（每行一条）→ 模式列表；空行/空白行剔除 */
  const parsePatterns = (): string[] =>
    text
      .split('\n')
      .map((l) => l.trim())
      .filter((l) => l !== '');

  const submit = (patterns?: string[]): void => {
    saveSettings({ privacyFilePatterns: patterns ?? parsePatterns() });
  };

  const resetDefault = (): void => {
    setText(DEFAULT_PRIVACY_PATTERNS.join('\n'));
    submit(DEFAULT_PRIVACY_PATTERNS);
  };

  return (
    <GroupCard title={t('settings.privacyTitle')} desc={t('settings.privacyDesc')}>
      <div className="setrow top">
        <label htmlFor="set-privacy">{t('settings.patternsLabel')}</label>
        <textarea
          id="set-privacy"
          className="in grow mono"
          rows={6}
          value={text}
          spellCheck={false}
          onChange={(e) => setText(e.target.value)}
          placeholder={'.env\n*.pem\nid_rsa*'}
        />
      </div>
      <div className="setrow end">
        <button className="btn sec" onClick={resetDefault} disabled={saving || settings === null}>
          {t('settings.resetDefault')}
        </button>
        <button className="btn" onClick={() => submit()} disabled={saving || settings === null}>
          {saving ? t('settings.saving') : t('settings.save')}
        </button>
      </div>
      <div className="muted small">{t('settings.privacyHint')}</div>
    </GroupCard>
  );
}

/* ---------- 8. 数据（会话目录 + 清除） ---------- */

function DataGroup(): React.JSX.Element {
  const sessionsDir = useStore((s) => s.sessionsDirPath);
  const settings = useStore((s) => s.settings);
  const saving = useStore((s) => s.settingsSaving);
  const clearSessionsData = useStore((s) => s.clearSessionsData);
  const t = useT();
  const [revealError, setRevealError] = useState<string | undefined>(undefined);
  /** 二次确认：第一次点击进入待确认态，再点执行（点其他处取消） */
  const [armClear, setArmClear] = useState(false);

  const openDir = (): void => {
    if (!sessionsDir) return;
    setRevealError(undefined);
    hostRequest('workspace.revealPath', { path: sessionsDir }).catch((err) => {
      setRevealError(err instanceof Error ? err.message : ti18n('settings.openFailed'));
    });
  };

  const doClear = (): void => {
    setArmClear(false);
    clearSessionsData();
  };

  return (
    <GroupCard title={t('settings.dataTitle')} desc={t('settings.dataDesc')}>
      <div className="setrow">
        <label>{t('settings.sessionsDirLabel')}</label>
        <span className="setpath" title={sessionsDir ?? ''}>
          {sessionsDir || t('settings.sessionsDirFallback')}
        </span>
        <button
          className="btn sec"
          onClick={openDir}
          disabled={!sessionsDir}
          title={t('settings.openDirTitle')}
        >
          {t('settings.openDir')}
        </button>
      </div>
      <div className="setrow end">
        <label>{t('settings.clearLabel')}</label>
        <span className="muted small grow">
          {armClear ? t('settings.clearConfirm') : t('settings.clearHint')}
        </span>
        {armClear ? (
          <>
            <button className="btn sec" onClick={() => setArmClear(false)} disabled={saving}>
              {t('sessions.cancel')}
            </button>
            <button className="btn danger" onClick={doClear} disabled={saving}>
              {saving ? t('settings.clearing') : t('settings.confirmClear')}
            </button>
          </>
        ) : (
          <button
            className="btn danger"
            onClick={() => setArmClear(true)}
            disabled={saving || settings === null}
            title={t('settings.clearBtnTitle')}
          >
            {t('settings.clearBtn')}
          </button>
        )}
      </div>
      {revealError !== undefined && <div className="bad small">{revealError}</div>}
    </GroupCard>
  );
}

/* ---------- 视图 ---------- */

export function SettingsView(): React.JSX.Element {
  const settings = useStore((s) => s.settings);
  const refreshing = useStore((s) => s.settingsRefreshing);
  const notice = useStore((s) => s.settingsNotice);
  const refreshSettings = useStore((s) => s.refreshSettings);
  const t = useT();
  const requestedRef = useRef(false);

  // 首开自动拉取一次（幂等）；之后靠「↻ 刷新」手动重发
  useEffect(() => {
    if (!requestedRef.current) {
      requestedRef.current = true;
      refreshSettings();
    }
  }, [refreshSettings]);

  return (
    <div className="view">
      <div className="view-head">
        <span>{t('settings.title')}</span>
        <button
          className="btn sec"
          onClick={refreshSettings}
          disabled={refreshing}
          title={t('settings.refreshTitle')}
        >
          {refreshing ? t('settings.refreshing') : t('settings.refresh')}
        </button>
      </div>
      {notice !== undefined && (
        <div className={notice.kind === 'error' ? 'mcpnotice bad' : 'mcpnotice ok'}>{notice.text}</div>
      )}
      <GeneralGroup />
      <SoundGroup />
      <AgentGroup />
      <CompactionGroup />
      <VisionRelayGroup />
      <InlineGroup />
      <PrivacyGroup />
      <DataGroup />
    </div>
  );
}
