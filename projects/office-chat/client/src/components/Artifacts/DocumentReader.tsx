import React from 'react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export function DocumentReader({ source, markdown, title }: {
  source: string;
  markdown: boolean;
  title?: string;
}) {
  return (
    <div className="sb-document-reader" role="region" aria-label={title} tabIndex={0}>
      <article>
        {markdown ? (
          <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
            table: ({ children }) => <div className="sb-table-scroll"><table>{children}</table></div>,
            a: ({ href, children, title }) => <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a>,
            img: ({ src, alt, title }) => <img src={src} alt={alt ?? ''} title={title} loading="lazy" referrerPolicy="no-referrer" />,
          }}>{source}</Markdown>
        ) : <pre className="sb-plain-text">{source}</pre>}
      </article>
    </div>
  );
}
