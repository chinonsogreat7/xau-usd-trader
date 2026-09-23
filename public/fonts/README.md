# Retained font assets

These existing font assets were retained from the remote repository during the
September 23, 2026 security cleanup. The offline Python trading tools do not use
them, and no web application or font-loading code is added by this directory.

The retained families are `fa-brands-400`, `fa-regular-400`, and `fa-solid-900`,
each in EOT, SVG, TTF, WOFF, and WOFF2 formats. Static checks verified their format
signatures and found no scripts, event handlers, or `href`/`src` resource
attributes in the SVG files. W3C and Font Awesome metadata URLs remain. This is
not a blanket security or licensing certification.

The unrelated `fa-solid-500.woff2` file was JavaScript rather than font data and
was intentionally excluded. Do not restore it from older commits. See
[the security cleanup record](../../docs/REPOSITORY_SECURITY.md).
