/**
 * App：顶部视图导航 + 七视图切换（PLAN §4 views/）。
 * 启动时 boot()（宿主 bootstrap 快照 + 轻量 UI 态注水），并把核 → 壳
 * 事件接入 store.applyEvent 归约（事件订阅生命周期与 App 一致）。
 */
import { useEffect } from 'react';
import { useStore } from './store/store';
import { useHostRpc } from './host/useHostRpc';
import { ChatView } from './views/ChatView';
import { SessionsView } from './views/SessionsView';
import { ApiView } from './views/ApiView';
import { EnvView } from './views/EnvView';
import { ToolsView } from './views/ToolsView';
import { TasksView } from './views/TasksView';
import { SettingsView } from './views/SettingsView';
import { Icon, type IconName } from './components/Icon';
import { useT } from './i18n';
import type { ViewId } from './store/store';

/** 视图导航配置：label 键在渲染期经 t() 解析（R20 i18n，切换语言即时生效） */
const NAV: Array<{ id: ViewId; labelKey: string; icon: IconName }> = [
  { id: 'chat', labelKey: 'nav.chat', icon: 'chat' },
  { id: 'sessions', labelKey: 'nav.sessions', icon: 'sessions' },
  { id: 'api', labelKey: 'nav.api', icon: 'api' },
  { id: 'env', labelKey: 'nav.env', icon: 'env' },
  { id: 'tools', labelKey: 'nav.tools', icon: 'tools' },
  { id: 'tasks', labelKey: 'nav.tasks', icon: 'tasks' },
  { id: 'settings', labelKey: 'nav.settings', icon: 'settings' },
];

export function App(): React.JSX.Element {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const newSession = useStore((s) => s.newSession);
  const hostConnected = useStore((s) => s.hostConnected);
  const fontScale = useStore((s) => s.fontScale);
  const rpc = useHostRpc();
  const t = useT();

  useEffect(() => {
    const store = useStore.getState();
    const offEvents = rpc.onEvent((e, sessionKey) => {
      useStore.setState({ hostConnected: true });
      useStore.getState().applyEvent(e, sessionKey);
    });
    const offErr = rpc.onHostError((e) => {
      useStore.setState({ hostConnected: true });
      useStore.getState().applyHostError(e);
    });
    const offProc = rpc.onProcessState((e) => {
      useStore.setState({ hostConnected: true });
      useStore.getState().applyProcessState(e);
    });
    const offPrompt = rpc.onHostPrompt((e) => {
      useStore.setState({ hostConnected: true });
      useStore.getState().applyHostPrompt(e.text, e.context);
    });
    void store.boot();
    return () => {
      offEvents();
      offErr();
      offProc();
      offPrompt();
    };
  }, [rpc]);

  // R15/R20 设置页「通用」组：字号缩放应用到 webview 根部 font-size——相对
  // --vscode-font-size 基准按百分比缩放（绝对 px 的控件字号不受影响，正文
  // 文本层级整体跟随）
  const rootStyle: React.CSSProperties =
    fontScale === 100 ? {} : { fontSize: `calc(var(--vscode-font-size, 13px) * ${fontScale / 100})` };

  return (
    <div className="root" style={rootStyle}>
      <nav className="topnav" aria-label={t('app.viewsAria')}>
        <div className="navviews">
          {NAV.map((n) => (
            <button
              key={n.id}
              className={view === n.id ? 'navbtn active' : 'navbtn'}
              onClick={() => setView(n.id)}
              title={t(n.labelKey)}
              aria-label={t(n.labelKey)}
              aria-current={view === n.id ? 'page' : undefined}
            >
              <Icon name={n.icon} />
              <span className="nav-label">{t(n.labelKey)}</span>
            </button>
          ))}
        </div>
        <div className="navactions">
          <span
            className={`conn${hostConnected ? ' on' : ''}`}
            title={hostConnected ? t('app.connected') : t('app.disconnected')}
          >
            {hostConnected ? '●' : '○'}
          </span>
          <button className="navbtn nav-new" onClick={newSession} title={t('app.newSession')} aria-label={t('app.newSession')}>
            <Icon name="plus" />
          </button>
        </div>
      </nav>
      <div className="viewhost">
        {view === 'chat' && <ChatView />}
        {view === 'sessions' && <SessionsView />}
        {view === 'api' && <ApiView />}
        {view === 'env' && <EnvView />}
        {view === 'tools' && <ToolsView />}
        {view === 'tasks' && <TasksView />}
        {view === 'settings' && <SettingsView />}
      </div>
    </div>
  );
}
