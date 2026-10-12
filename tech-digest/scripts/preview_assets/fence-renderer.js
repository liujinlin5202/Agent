md.renderer.rules.fence = (tokens, idx) => {
  const token = tokens[idx]
  const lang = token.info ? md.utils.escapeHtml(token.info.trim().split(/\s+/g)[0]) : ''
  const langClass = lang ? ` class="language-${lang}"` : ''
  const code = md.utils.escapeHtml(token.content)
  return (
    `<div class="md-code-block">`
    + `<button type="button" class="md-code-block__copy" aria-label="复制代码" title="复制代码">`
    + `<svg class="md-code-block__copy-icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false" fill="currentColor">`
    + `<path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5A1.75 1.75 0 0 1 9.25 14h-7.5A1.75 1.75 0 0 1 0 12.25v-5.5z"/>`
    + `<path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0 1 14.25 10h-7.5A1.75 1.75 0 0 1 5 8.25v-7.5zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25h-7.5z"/>`
    + `</svg><span class="md-code-block__copy-text">复制</span></button>`
    + `<pre><code${langClass}>${code}</code></pre></div>`
  )
}