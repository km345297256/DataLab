import { marked } from 'marked';
import DOMPurify from 'dompurify';

/** Format JSON without round-tripping numeric literals through JavaScript numbers. */
export function formatSkillResource(path: string, content: string): string {
  if (!path.toLowerCase().endsWith('.json')) return content;
  try { JSON.parse(content); } catch { return content; }
  const tokens = content.match(/"(?:\\.|[^"\\])*"|[{}\[\],:]|[^\s{}\[\],:]+/g) || [];
  let depth = 0, result = '';
  const newline = () => '\n' + '  '.repeat(depth);
  for (const [index, token] of tokens.entries()) {
    if (token === '{' || token === '[') {
      result += token;
      depth++;
      if (tokens[index + 1] !== (token === '{' ? '}' : ']')) result += newline();
    } else if (token === '}' || token === ']') {
      depth--;
      if (tokens[index - 1] !== (token === '}' ? '{' : '[')) result += newline();
      result += token;
    } else if (token === ',') result += ',' + newline();
    else if (token === ':') result += ': ';
    else result += token;
    // Deeply nested or very compact files stay fully available as their
    // original text instead of expanding into excessive indentation.
    if (depth > 64 || result.length > 4 * 1024 * 1024) return content;
  }
  return result;
}

function packageLink(path: string, href: string, resources: string[]): string | null {
  if (!href || /^[a-z][a-z\d+.-]*:/i.test(href) || href.startsWith('/') || href.startsWith('#')) return null;
  let decoded: string;
  try { decoded = decodeURIComponent(href.split(/[?#]/, 1)[0]); } catch { return null; }
  const parts = path.split('/').slice(0, -1);
  for (const part of decoded.split('/')) {
    if (part === '..') { if (!parts.length) return null; parts.pop(); }
    else if (part && part !== '.') parts.push(part);
  }
  const target = parts.join('/');
  return resources.includes(target) ? target : null;
}

/** Read-only document HTML: no embedded media, active content or automatic external requests. */
export function renderSkillMarkdown(content: string, path: string, resources: string[]): string {
  const html = DOMPurify.sanitize(marked.parse(content, { async: false, gfm: true, breaks: true }) as string, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['img', 'video', 'audio', 'source', 'iframe', 'object', 'embed', 'form', 'input', 'button', 'style', 'link'],
    FORBID_ATTR: ['style', 'src', 'srcset', 'poster', 'background', 'ping'],
    ALLOW_DATA_ATTR: false,
  });
  const doc = new DOMParser().parseFromString(html, 'text/html');
  doc.querySelectorAll('a').forEach(link => {
    const href = link.getAttribute('href') || '';
    const target = packageLink(path, href, resources);
    if (target) {
      link.setAttribute('href', '#');
      link.dataset.skillResource = target;
    } else if (/^https?:\/\//i.test(href)) {
      link.setAttribute('target', '_blank');
      link.setAttribute('rel', 'noopener noreferrer');
    } else {
      link.removeAttribute('href');
      link.removeAttribute('target');
    }
  });
  return doc.body.innerHTML;
}
