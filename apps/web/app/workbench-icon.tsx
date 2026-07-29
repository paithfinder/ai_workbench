export type IconName =
  | "archive"
  | "branch"
  | "chevron"
  | "clock"
  | "database"
  | "file"
  | "folder"
  | "hash"
  | "plus"
  | "search"
  | "send"
  | "terminal"
  | "x";

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  const common = {
    width: size,
    height: size,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.6,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };

  const paths: Record<IconName, React.ReactNode> = {
    archive: <><path d="M4 7.5h16v12H4z" /><path d="M3 4.5h18v3H3zM9 11h6" /></>,
    branch: <><circle cx="6" cy="5" r="2" /><circle cx="18" cy="7" r="2" /><circle cx="6" cy="19" r="2" /><path d="M6 7v10M8 9c5 0 4-2 8-2" /></>,
    chevron: <path d="m9 18 6-6-6-6" />,
    clock: <><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></>,
    database: <><ellipse cx="12" cy="5.5" rx="7.5" ry="3" /><path d="M4.5 5.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6M4.5 11.5v6c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-6" /></>,
    file: <><path d="M6 3.5h8l4 4v13H6z" /><path d="M14 3.5v4h4M9 12h6M9 16h5" /></>,
    folder: <path d="M3.5 6.5h6l2 2h9v10.5h-17z" />,
    hash: <><path d="M9 3 7 21M17 3l-2 18M4 9h17M3 15h17" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m15.5 15.5 4 4" /></>,
    send: <><path d="m4 4 17 8-17 8 3-8z" /><path d="M7 12h14" /></>,
    terminal: <><path d="m5 7 4 4-4 4M11 16h7" /><rect x="2.5" y="3.5" width="19" height="17" rx="1" /></>,
    x: <><path d="M6 6l12 12M18 6 6 18" /></>,
  };

  return <svg {...common}>{paths[name]}</svg>;
}
