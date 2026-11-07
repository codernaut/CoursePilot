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

### Direct dependencies added in M3 (document processing)

| Package | Licence | Notes |
|---|---|---|
| `pymupdf` (MuPDF) | AGPL-3.0 (dual-licensed with a commercial licence from Artifex) | Used under the AGPL-3.0, which matches this project's licence. Pinned `<2`. |
| `python-pptx` | MIT | Pulls in `lxml` (BSD-3-Clause), `pillow` (MIT-CMU) and `XlsxWriter` (BSD-2-Clause). |
| `python-docx` | MIT | Uses `lxml`. |
| `markdown-it-py` | MIT | Uses `mdurl` (MIT). |
| `beautifulsoup4` | MIT | Uses `soupsieve` (MIT). |
| `httpx` | BSD-3-Clause | Embedding provider calls (also the test client). |

All of these are AGPL-compatible. `uv.lock` holds the exact versions.

### Direct dependencies added in M5 (chat and the agent)

| Package | Licence | Notes |
|---|---|---|
| `langgraph` | MIT | The agent `StateGraph`. |
| `langgraph-checkpoint-sqlite` | MIT | `AsyncSqliteSaver`, chat memory in `CHECKPOINT_DB_PATH`. |
| `langchain`, `langchain-core` | MIT | `init_chat_model` and the chat-model interface. Pull in `langsmith` (MIT; tracing stays off). |
| `langchain-anthropic` | MIT | Uses the `anthropic` SDK (Apache-2.0). |
| `langchain-google-genai` | MIT | Uses `google-genai` (Apache-2.0). |

All of these are AGPL-compatible. `uv.lock` holds the exact versions.
