import type { ReactNode } from 'react';

export type IconName =
  | 'chat'
  | 'sessions'
  | 'api'
  | 'env'
  | 'tools'
  | 'tasks'
  | 'settings'
  | 'plus'
  | 'file'
  | 'image'
  | 'selection'
  | 'commit'
  | 'send'
  | 'stop'
  | 'gauge'
  | 'shield'
  | 'arrow'
  | 'check'
  | 'close'
  | 'warning'
  | 'edit'
  | 'tool'
  | 'relay'
  | 'compact'
  | 'sparkles'
  | 'undo';

const paths: Record<IconName, ReactNode> = {
  chat: <path d="M3 3.5h10v7H8l-3 2.3v-2.3H3z" />,
  sessions: (
    <>
      <path d="M5.5 5h8v7h-8z" />
      <path d="M10.5 3h3v7M3 8.5h8v5H3z" />
    </>
  ),
  api: <path d="m6 3-3 5 3 5m4-10 3 5-3 5M9 2 7 14" />,
  env: (
    <>
      <rect x="3" y="3" width="10" height="10" rx="1" />
      <path d="M6 6h4v4H6zM6 1v2m4-2v2M6 13v2m4-2v2M1 6h2m-2 4h2m10-4h2m-2 4h2" />
    </>
  ),
  tools: <path d="M9.5 2.5a3.5 3.5 0 0 0-4.3 4.3L2 10l1.5 1.5 3.2-3.2a3.5 3.5 0 0 0 4.3-4.3l-2 2-1.5-1.5zM10 10l4 4" />,
  tasks: (
    <>
      <path d="m2.5 4 1 1 2-2m-2 5 1 1 2-2m-2 5 1 1 2-2M8 4h6M8 8h6M8 12h6" />
    </>
  ),
  settings: (
    <>
      <circle cx="8" cy="8" r="2" />
      <path d="M6.7 2h2.6l.4 1.8 1.6.9 1.8-.4 1.3 2.3-1.2 1.4v1.8l1.2 1.4-1.3 2.3-1.8-.4-1.6.9-.4 1.8H6.7l-.4-1.8-1.6-.9-1.8.4-1.3-2.3L2.8 10V8.2L1.6 6.8l1.3-2.3 1.8.4 1.6-.9z" />
    </>
  ),
  plus: <path d="M8 3v10M3 8h10" />,
  file: <path d="M9 2H4v12h8V5zm0 0v3h3M6 8h4m-4 3h4" />,
  image: (
    <>
      <rect x="2" y="3" width="12" height="10" rx="1" />
      <circle cx="5.5" cy="6.5" r="1" />
      <path d="m3 12 3.5-3 2.5 2 2-2 3 3" />
    </>
  ),
  selection: <path d="M3 6V3h3m4 0h3v3m0 4v3h-3m-4 0H3v-3M6 5.5h4v5H6z" />,
  commit: (
    <>
      <circle cx="4" cy="8" r="1.5" />
      <circle cx="12" cy="4" r="1.5" />
      <circle cx="12" cy="12" r="1.5" />
      <path d="M5.5 8h3a3 3 0 0 0 3-3v0m-3 3a3 3 0 0 1 3 3" />
    </>
  ),
  send: <path d="m2 2 12 6-12 6 2-6zm2 6h10" />,
  stop: <rect x="4" y="4" width="8" height="8" rx="1" />,
  gauge: (
    <>
      <circle cx="8" cy="8" r="5.5" />
      <path d="M8 8 11 5M8 1.5v1M2 4l.8.8M14 4l-.8.8" />
      <circle cx="8" cy="8" r="1" />
    </>
  ),
  shield: <path d="M8 1.5 13 3v4c0 3.2-2 5.8-5 7.5C5 12.8 3 10.2 3 7V3zm-2 6 1.5 1.5L11 5.5" />,
  arrow: <path d="M3 8h10m-4-4 4 4-4 4" />,
  check: <path d="m3 8 3 3 7-7" />,
  close: <path d="m4 4 8 8m0-8-8 8" />,
  warning: <path d="M8 2 1.8 13h12.4zM8 6v3m0 2v.2" />,
  edit: <path d="m10.5 2.5 3 3L6 13H3v-3zM9 4l3 3M3 2h4" />,
  tool: <path d="M3 3h4v4H3zm6 6h4v4H9zM5 7v4a2 2 0 0 0 2 2h2M11 3v3m-1.5-1.5h3" />,
  relay: (
    <>
      <rect x="2" y="3" width="12" height="9" rx="1" />
      <path d="m5 10 2-2 2 2 2-3 2 3M6 14h4" />
    </>
  ),
  compact: <path d="M3 2v4h4m6 8v-4H9M2 8h12M5 2 2 5m9 6 3 3" />,
  sparkles: <path d="m8 1 1.4 4.6L14 7l-4.6 1.4L8 13 6.6 8.4 2 7l4.6-1.4zM12.5 11l.6 2 1.9.5-1.9.6-.6 1.9-.5-1.9-2-.6 2-.5z" />,
  undo: (
    <>
      <path d="M2.5 3v4h4" />
      <path d="M2.8 7A5.5 5.5 0 1 0 4.2 4" />
    </>
  ),
};

export function Icon({ name, size = 14 }: { name: IconName; size?: number }): React.JSX.Element {
  return (
    <svg
      aria-hidden="true"
      className="icon"
      fill="none"
      height={size}
      viewBox="0 0 16 16"
      width={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.35"
    >
      {paths[name]}
    </svg>
  );
}
