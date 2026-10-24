/**
 * WelcomePage 欢迎页（PLAN §6.1）：空会话时的首屏——品牌 / 引导文案 /
 * 三条建议提示词（R20 i18n：建议文案走字典，点击仍填入输入框不代发）。
 */
import { Icon } from './Icon';
import { useT } from '../i18n';

export function WelcomePage({ onPick }: { onPick: (text: string) => void }): React.JSX.Element {
  const t = useT();
  const suggestions = [
    t('welcome.suggestion1'),
    t('welcome.suggestion2'),
    t('welcome.suggestion3'),
  ];
  return (
    <div className="welcome">
      <div className="welcome-brand">
        <span className="welcome-mark"><Icon name="sparkles" size={17} /></span>
        <span>Zcode-Py</span>
      </div>
      <div className="welcome-copy">
        <h1 className="welcome-title">{t('welcome.title')}</h1>
        <p className="welcome-subtitle">{t('welcome.subtitle')}</p>
      </div>
      <div className="welcome-suggestions" aria-label={t('welcome.suggestionsAria')}>
        {suggestions.map((suggestion, index) => (
          <button
            key={suggestion}
            className="suggestion"
            onClick={() => onPick(suggestion)}
            title={t('welcome.fill', { text: suggestion })}
          >
            <span className="suggestion-index">0{index + 1}</span>
            <span className="suggestion-label">{suggestion}</span>
            <Icon name="arrow" size={13} />
          </button>
        ))}
      </div>
      <div className="welcome-hints">
        <span><kbd>@</kbd> {t('welcome.hintFile')}</span>
        <span><kbd>/</kbd> {t('welcome.hintSkill')}</span>
      </div>
      <div className="welcome-config">{t('welcome.config')}</div>
    </div>
  );
}
