# Repository security cleanup — September 23, 2026

## Finding

When preparing to push the locally reviewed trading project, a fetch revealed
that remote `main` had been replaced with an unrelated root commit:
`7bc2272dd82159effd8d9c7772e44d375be9c309`. Its original Python project files
matched local root `598be32`, but it also contained unrelated editor and font
files.

Static inspection found a concealed code-execution chain:

- `.vscode/tasks.json` configured an automatic task to run Node on
  `public/fonts/fa-solid-500.woff2` on folder opening, with task output hidden.
- `.vscode/settings.json` requested automatic tasks and suppressed terminal
  visibility.
- `public/fonts/fa-solid-500.woff2` contained 27,410 bytes of obfuscated
  JavaScript, not a font. It contained logic to retrieve and decode additional
  code and execute it with `eval` and a Node subprocess.

The disguised script's SHA-256 is
`fb23a2532b38d50370ff156bcc79e1c60ded946aa33bcea15fd73f7a8dcd1366`.
The ultimate downloaded payload and any prior execution on other machines are
unknown. Commit metadata alone does not establish who introduced these files.

## Cleanup

The cleanup merge starts from local commit `dbbe046` and reconciles the remote
history without force-pushing. No unsafe remote file was checked out or executed
during this review. No embedded network endpoint was contacted.

- Exclude the automatic task and disguised JavaScript file from the merged tree.
- Replace the remote workspace settings with `task.allowAutomaticTasks: false`.
- Exclude the unrelated Node/SST debug launcher in `.vscode/launch.json`.
- Retain the 15 inspected font files, inert extension recommendations, and
  spelling dictionary. Extension recommendations are not an endorsement or an
  instruction to install extensions.
- Replace the unrelated Blockchain Explorer font README with an accurate note.
- Retain the additional named temporary-file ignore rules, but omit the remote
  rule that hides `.gitignore` itself.
- Preserve the reviewed local Python code, tests, data exclusions, and docs.

No broker connection or trading permission is enabled by this cleanup.

## Important historical limitation

This is a history-preserving cleanup, not a purge of Git objects. The unsafe
files remain in the old remote commit and may remain in other clones. Do not
check out or open that historical version in a trusted editor, and do not
restore its startup task or disguised script. Disabling automatic tasks in the
current settings does not sanitize older commits or other machines.

This review does not establish whether another clone previously executed the
payload. If the affected version was opened with automatic tasks allowed,
investigate that machine before trusting it with credentials or trading access.
