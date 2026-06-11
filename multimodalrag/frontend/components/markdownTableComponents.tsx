import type { Components } from 'react-markdown';

const mergeClasses = (...values: Array<string | undefined>) =>
  values.filter(Boolean).join(' ');

export const markdownTableComponents: Components = {
  table: ({ node, className, children, ...props }) => {
    void node;
    return (
      <div className="my-3 overflow-x-auto rounded-xl border border-[rgba(0,212,255,0.2)] bg-[rgba(10,14,39,0.35)]">
        <table
          {...props}
          className={mergeClasses('min-w-full border-collapse text-sm', className)}
        >
          {children}
        </table>
      </div>
    );
  },
  thead: ({ node, className, ...props }) => {
    void node;
    return (
      <thead
        {...props}
        className={mergeClasses('bg-[rgba(0,212,255,0.12)] text-[#00d4ff]', className)}
      />
    );
  },
  tbody: ({ node, className, ...props }) => {
    void node;
    return <tbody {...props} className={mergeClasses('text-[#e8eaed]', className)} />;
  },
  tr: ({ node, className, ...props }) => {
    void node;
    return <tr {...props} className={mergeClasses('hover:bg-[rgba(0,212,255,0.05)]', className)} />;
  },
  th: ({ node, className, ...props }) => {
    void node;
    return (
      <th
        {...props}
        className={mergeClasses(
          'px-3 py-2 text-left font-semibold text-[#00d4ff] border border-[rgba(0,212,255,0.2)]',
          className
        )}
      />
    );
  },
  td: ({ node, className, ...props }) => {
    void node;
    return (
      <td
        {...props}
        className={mergeClasses(
          'px-3 py-2 align-top text-[#e8eaed] border border-[rgba(0,212,255,0.15)]',
          className
        )}
      />
    );
  },
};
