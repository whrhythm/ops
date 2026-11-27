/** Theme the viewer chrome and extracted content while preserving authored page colours. */
export function withOfficeReadingTheme(html: string, dark: boolean): string {
  if (!html) return html;
  const palette = dark
    ? '--bg:#171717;--fg:#e8e8e8;--muted:#b3b3b3;--border:#545454;--row-alt:#202020;--row-hover:#303030;--header-bg:#292929;--tab-active-bg:#333;--link:#91c5ff'
    : '--bg:#fff;--fg:#242424;--muted:#525252;--border:#b9b9b9;--row-alt:#f7f7f7;--row-hover:#eef4fa;--header-bg:#f0f2f4;--tab-active-bg:#e4e8eb;--link:#155998';
  const style = `<style>:root{color-scheme:${dark ? 'dark' : 'light'};${palette};--tab-bg:var(--bg)}
    #lc-render section.docx,.lc-slide-wrap{color-scheme:light;color:#202020;--fg:#202020;--bg:#fff}
    .lc-table-scroll{max-width:100%;overflow-x:auto}
    .lc-word-controls button{color:var(--fg);background:var(--header-bg);border-color:var(--border)}
    </style>`;
  const end = html.toLowerCase().indexOf('</head>');
  return end < 0 ? style + html : html.slice(0, end) + style + html.slice(end);
}
