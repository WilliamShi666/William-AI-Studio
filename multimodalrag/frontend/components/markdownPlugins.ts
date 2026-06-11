import rehypeKatex from 'rehype-katex';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';

const katexOptions = {
  strict: false,
  throwOnError: false,
};

const sanitizeSchema = {
  ...defaultSchema,
  tagNames: Array.from(
    new Set([...(defaultSchema.tagNames || []), 'table', 'thead', 'tbody', 'tr', 'th', 'td'])
  ),
  attributes: {
    ...defaultSchema.attributes,
    code: [
      ...(defaultSchema.attributes?.code || []),
      ['className', /^language-./, 'math-inline', 'math-display'],
    ],
    th: [
      ...(defaultSchema.attributes?.th || []),
      'colspan',
      'rowspan',
      'align',
    ],
    td: [
      ...(defaultSchema.attributes?.td || []),
      'colspan',
      'rowspan',
      'align',
    ],
  },
};

export const markdownRemarkPlugins = [remarkMath, remarkGfm];
export const markdownRehypePlugins = [
  rehypeRaw,
  [rehypeSanitize, sanitizeSchema],
  [rehypeKatex, katexOptions],
];

export function normalizeMathDelimiters(markdown: string) {
  if (!markdown) return markdown;
  const blockPattern = /\\\[([\s\S]*?)\\\]/g;
  const inlinePattern = /\\\(([\s\S]*?)\\\)/g;

  return markdown
    .replace(blockPattern, (_, expr) => `\n$$\n${expr}\n$$\n`)
    .replace(inlinePattern, (_, expr) => `$${expr}$`);
}
