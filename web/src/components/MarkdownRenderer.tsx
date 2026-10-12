import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/** Migrated from bai-web's renderer with an explicit prop type. */
export function MarkdownRenderer({ content }: { content: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    a: ({ href, children, ...props }) => <a href={href} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>,
    img: ({ src, alt, ...props }) => <img src={src} alt={alt} loading="lazy" style={{ maxWidth: '100%', height: 'auto' }} {...props} />,
  }}>{content}</ReactMarkdown>;
}
