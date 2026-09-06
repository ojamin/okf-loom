export const styles = `
.loom { --ink:var(--color-text-primary,#192b31); --muted:var(--color-text-secondary,#65767c); --paper:var(--color-surface-raised,#fff); --back:var(--color-surface-base,#f5f7f7); --line:var(--color-border-subtle,#dce4e5); --accent:var(--color-text-link,#176976); color:var(--ink); background:var(--paper); font:14px/1.6 system-ui,sans-serif; min-height:480px; container-type:inline-size; }
.loom * { box-sizing:border-box; }
.loom button,.loom input,.loom select,.loom textarea { font:inherit; color:inherit; }
.loom button,.loom select { padding:8px 12px; border:1px solid var(--line); background:var(--paper); border-radius:7px; cursor:pointer; min-height:40px; }
.loom button:hover,.loom button[aria-pressed=true] { color:var(--accent); border-color:var(--accent); }
.loom button:disabled { cursor:default; opacity:.55; }
.loom :focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
.loom input,.loom textarea { border:1px solid var(--line); background:var(--paper); border-radius:6px; padding:10px 12px; min-width:0; }
.loom header.loom-bar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; border-bottom:1px solid var(--line); padding:16px 24px; }
.loom-brand { font-size:19px; font-weight:700; letter-spacing:-.03em; }
.loom small,.loom-muted { color:var(--muted); }
.loom-actions { display:flex; flex-wrap:wrap; align-items:center; gap:8px; }
.loom-primary { background:var(--accent)!important; color:var(--paper)!important; }
.loom-grid { display:grid; grid-template-columns:230px minmax(0,1fr); }
.loom-catalog { background:var(--back); padding:20px 14px; border-right:1px solid var(--line); min-width:0; }
.loom-catalog form { display:grid; gap:8px; margin-bottom:16px; }
.loom-catalog ol { list-style:none; margin:12px 0; padding:0; max-height:65vh; overflow-y:auto; }
.loom-catalog li button { text-align:left; border:0; width:100%; background:transparent; display:grid; line-height:1.4; gap:4px; margin:2px 0; }
.loom-catalog li button[aria-current=page] { background:var(--paper); color:var(--accent); box-shadow:inset 3px 0 var(--accent); }
.loom-catalog li small { font-size:11px; overflow-wrap:anywhere; }
.loom-content { min-width:0; padding:28px clamp(20px,4cqw,56px) 56px; }
.loom-content h1 { font-size:clamp(28px,4cqw,42px); letter-spacing:-.035em; line-height:1.2; margin:10px 0 16px; }
.loom-content h2 { font-size:22px; margin:28px 0 12px; }
.loom-content h3 { font-size:17px; }
.loom-eyebrow { color:var(--accent); font-size:11px; letter-spacing:.1em; text-transform:uppercase; }
.loom-tabs { display:flex; gap:8px; flex-wrap:wrap; border-bottom:1px solid var(--line); padding-bottom:16px; margin:20px 0; }
.loom-prose { max-width:82ch; font-size:16px; line-height:1.85; overflow-wrap:anywhere; }
.loom-prose a { color:var(--accent); }
.loom pre { background:var(--back); border:1px solid var(--line); padding:16px; border-radius:8px; overflow:auto; font:13px/1.7 ui-monospace,monospace; white-space:pre-wrap; overflow-wrap:anywhere; }
.loom table { display:block; overflow:auto; border-collapse:collapse; }
.loom td,.loom th { padding:8px 12px; border:1px solid var(--line); text-align:left; }
.loom blockquote { border-left:3px solid var(--accent); padding-left:18px; margin-left:0; color:var(--muted); }
.loom details { border:1px solid var(--line); padding:12px 16px; border-radius:7px; margin:16px 0; }
.loom summary { cursor:pointer; font-weight:600; }
.loom textarea { width:100%; resize:vertical; min-height:120px; }
.loom-source { min-height:440px!important; font:13px/1.7 ui-monospace,monospace!important; tab-size:2; }
.loom-split { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.loom-notice { margin:16px 24px; padding:12px 16px; border:1px solid var(--line); border-left:3px solid var(--accent); border-radius:6px; }
.loom-notice[role=alert] { border-left-color:#be5942; }
.loom-card { padding:16px 0; border-bottom:1px solid var(--line); }
.loom-card p { white-space:pre-wrap; overflow-wrap:anywhere; }
.loom-thread { border-left:2px solid var(--line); margin-left:12px; padding-left:16px; }
.loom-form { display:grid; gap:12px; margin:20px 0; }
.loom-form label { display:grid; gap:6px; font-weight:550; }
.loom-check { display:flex!important; gap:8px!important; align-items:center; font-weight:400!important; }
.loom-graph { width:100%; max-height:460px; border:1px solid var(--line); border-radius:8px; background:var(--back); }
.loom-graph line { stroke:var(--line); stroke-width:2; }
.loom-graph circle { fill:var(--accent); }
.loom-graph text { fill:var(--ink); font:12px system-ui; }
.loom-empty { padding:40px 0; max-width:60ch; }
@container (max-width:720px) { .loom-grid { grid-template-columns:1fr; } .loom-catalog { border-right:0; border-bottom:1px solid var(--line); } .loom-catalog ol { max-height:180px; } .loom-split { grid-template-columns:1fr; } .loom-content { padding:24px 18px; } .loom button { min-height:44px; } }
@media (prefers-reduced-motion:reduce) { .loom * { scroll-behavior:auto; } }
`;
