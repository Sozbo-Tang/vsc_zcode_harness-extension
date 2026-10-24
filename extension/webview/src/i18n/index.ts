/**
 * i18n 轻量上下文与翻译入口（R20 多语言界面）。
 *
 * 设计决策：
 *  - locale 上下文选「独立小 zustand store」而非 React context：与仓库现有
 *    store 风格一致（zustand 订阅驱动重渲染），且依赖方向单一——store/store.ts
 *    与各组件 import 本模块，本模块不反向 import 主 store，避免循环依赖。
 *    主 store 的 uiLanguage 切片仍是「设置项真相源」（vscode getState 持久），
 *    setUiLanguage / boot 在语言落定时同步调用 setLocale 驱动界面即时切换；
 *  - t()（非 React 场景：store 归约器生成的提示文案）在调用时读
 *    getState().locale —— 语言切换后新产生的提示用新语言，已产生的历史
 *    提示保持原文（瞬态提示，不回溯重译）；
 *  - useT()（React 组件）订阅 locale，切换语言即时重渲染；
 *  - 查找链：当前语言字典 → zh（源语言，空缺键回退中文）→ 键名本身
 *    （缺键开发期可见，不静默吞）；
 *  - 默认语言：设置页持久选择 > 宿主语言（vscode.env.language，经 getState
 *    RPC 下发）> navigator.language 兜底 > zh（源语言）。解析规则：zh* →
 *    zh，其余（含未知）→ en；完全无语言信号 → zh。
 */
import { create } from 'zustand';
import { zh } from './zh';
import { en } from './en';

export type Locale = 'zh' | 'en';

export type TranslateParams = Record<string, string | number>;

/** 插值：{name} 占位符替换（未提供的占位符原样保留） */
export function translate(locale: Locale, key: string, params?: TranslateParams): string {
  const dict: Record<string, string> = locale === 'en' ? en : zh;
  const raw = dict[key] ?? zh[key as keyof typeof zh] ?? key;
  if (params === undefined) return raw;
  return raw.replace(/\{(\w+)\}/g, (m, name: string) =>
    params[name] !== undefined ? String(params[name]) : m,
  );
}

interface I18nState {
  locale: Locale;
  setLocale(locale: Locale): void;
}

/** 模块加载即同步定初值（navigator 兜底；boot/宿主语言到达后覆盖） */
export const useI18nStore = create<I18nState>()((set) => ({
  locale: resolveLocale(typeof navigator !== 'undefined' ? navigator.language : undefined),
  setLocale: (locale) => set({ locale }),
}));

/** 非 React 场景翻译（store 归约器等）：调用时读当前 locale */
export function t(key: string, params?: TranslateParams): string {
  return translate(useI18nStore.getState().locale, key, params);
}

/** React 组件翻译 hook：订阅 locale，切换语言即时生效 */
export function useT(): (key: string, params?: TranslateParams) => string {
  const locale = useI18nStore((s) => s.locale);
  return (key, params) => translate(locale, key, params);
}

/** 语言落定入口（主 store setUiLanguage / boot 调用；同步驱动界面切换） */
export function setLocale(locale: Locale): void {
  useI18nStore.getState().setLocale(locale);
}

/** 语言标签（vscode.env.language / navigator.language）→ Locale */
export function resolveLocale(lang?: string): Locale {
  if (typeof lang !== 'string' || lang === '') return 'zh'; // 无信号回源语言
  return lang.toLowerCase().startsWith('zh') ? 'zh' : 'en';
}
