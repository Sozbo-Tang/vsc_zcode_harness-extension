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
import type { ViewId } from './store/store';

const NAV: Array<{ id: ViewId; label: string }> = [
  { id: 'chat', label: '聊天' },
  { id: 'sessions', label: '会话' },
  { id: 'api', label: 'API' },
  { id: 'env', label: '环境' },
  { id: 'tools', label: '工具' },
  { id: 'tasks', label: '任务' },
  { id: 'settings', label: '设置' },
];

export function App(): React.JSX.Element {
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const hostConnected = useStore((s) => s.hostConnected);
  const rpc = useHostRpc();

  useEffect(() => {
    const store = useStore.getState();
    const offEvents = rpc.onEvent((e) => {
      useStore.setState({ hostConnected: true });
      useStore.getState().applyEvent(e);
    });
    const offErr = rpc.onHostError((e) => {
      useStore.setState({ hostConnected: true });
      useStore.getState().applyHostError(e);
    });
    const offProc = rpc.onProcessState((e) => {
      useStore.setState({ hostConnected: true });
      useStore.getState().applyProcessState(e);
    });
    void store.boot();
    return () => {
      offEvents();
      offErr();
      offProc();
    };
  }, [rpc]);

  return (
    <div className="root">
      <nav className="topnav">
        {NAV.map((n) => (
          <button
            key={n.id}
            className={view === n.id ? 'navbtn active' : 'navbtn'}
            onClick={() => setView(n.id)}
          >
            {n.label}
          </button>
        ))}
        <span
          className={`conn${hostConnected ? ' on' : ''}`}
          title={hostConnected ? '已连接扩展宿主' : '未连接扩展宿主（M1 宿主接线后自动恢复）'}
        >
          {hostConnected ? '●' : '○'}
        </span>
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
