# Portable offline Markdown preview assets

`preview_reader_article.py` embeds every runtime asset into the emitted HTML. It
reads only its adjacent assets and the selected JSON packet. No developer `_preview`
directory, frontend checkout, package manager, service, publisher, credentials,
remote QiuDock code/font or CDN is required at runtime. A system font stack avoids
font downloads. The preview's CSP denies network connections, external scripts,
fonts and images. Ordinary source links remain available for manual inspection.

The Markdown body contains no preview controls, custom style/script or folds.
The width selector and code-copy controls belong only to the local HTML viewer.
Code, text formulas and tables scroll inside their own blocks when necessary.
This offline builder has no KaTeX plugin: use readable symbols with definitions,
or text code blocks; `check.json` flags raw math delimiters for editorial review.

## Provenance

- `preview-template.html`: adapted from the existing local `post-6717` builder.
- `fence-renderer.js` and `sanitizer.js`: copied from the local
  `_fe/src/components/MarkdownContainer.vue`; retains its DOMPurify rules,
  including forbidden inline styles and event attributes.
- `component.css`: that component's scoped CSS with `:deep()` wrappers removed.
- Markdown-it 14.1.0 and github-markdown-light CSS: existing local preview vendor assets.
- DOMPurify 3.2.6, Prism 1.30.0, Bash/Python/JSON grammars and tomorrow theme:
  existing local `post-6717/assets` files. Upstream notices remain in the files.

## Licenses

- Markdown-it: MIT, Vitaly Puzrin and Alex Kocharin.
- github-markdown-css: MIT, Sindre Sorhus.
- Prism and grammars: MIT, Lea Verou and contributors.
- Prism Tomorrow theme: MIT, Rose Pritchard, based on Chris Kempson's theme (notice preserved).
- DOMPurify: Apache 2.0 or MPL 2.0, Cure53 and other contributors; this bundle
  distributes it under Apache 2.0. The unmodified standard Apache license is
  included as `LICENSE-APACHE-2.0.txt`, copied from an existing local Apache-licensed
  dependency. MIT terms and attribution are in `LICENSE-MIT.txt`.
- The project-owned template/component snippets retain the project's licensing.

All copies were performed locally. No dependency was fetched for this task.

## Reproduce the three offline comparisons

The original fixture constructors are in `tests/test_reader_preview.py`.
`sample_packet('empirical')`, `sample_packet('theoretical')` and
`sample_packet('engineering_measurement')` are deliberately synthetic local
sources. Write one to a temporary JSON file and run:

```text
python scripts/preview_reader_article.py --input packet.json --output-dir preview
```

The empirical and engineering numbers demonstrate attribution and comparison
conditions; they are not real research results. The algorithm example includes
its assumptions and a complete induction argument. No resources, courses or
product recommendation are included as positive examples.
