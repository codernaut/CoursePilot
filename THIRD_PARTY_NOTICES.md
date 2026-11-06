# Third-party notices

## Vendored files

- **htmx** (`backend/app/web/static/htmx.min.js`), version 2.0.11, downloaded from
  <https://unpkg.com/htmx.org@2/dist/htmx.min.js>. Licence: BSD Zero Clause (0BSD), (c) Big Sky
  Software. AGPL-compatible.

## Bundled data

- **Common-password list** (`backend/app/resources/common_passwords.txt`): written for this
  project (a curated set of widely known weak passwords plus common word and number variants,
  lower-cased, at least 10 characters). It contains no text copied from a third-party list, so it
  carries the project's own licence (AGPL-3.0-only). It is used only to reject weak passwords.

## Python dependencies

Dependencies are installed from PyPI (see `backend/uv.lock`) and are MIT, BSD or Apache-2.0
licensed. Check the licence of every new dependency before adding it (CLAUDE.md section 2).
