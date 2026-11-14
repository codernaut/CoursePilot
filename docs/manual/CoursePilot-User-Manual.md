---
title: "CoursePilot"
subtitle: "User manual"
author: "Version 0.1.0"
date: "2026-11-14"
---

# What is CoursePilot

CoursePilot is a study assistant for a university programme's teaching material. Faculty upload lecture slides, PDFs, notes and handouts into a fixed structure of **Year › Block › Module** (by default 3 years, each with 3 blocks, each with 3 modules, so 27 modules). Students then chat with an assistant that:

- answers **from the uploaded material first**, and shows exactly which document and page or slide each statement came from (citations such as `[C1]`);
- only when the material is not enough, searches the internet and says **clearly** that the answer, or part of it, comes from the web, with links (citations such as `[W1]`);
- says so plainly when it cannot find anything, instead of inventing sources.

CoursePilot runs as a web site. Everyone signs in with an account created by an administrator; there is no self-registration.

**Roles**

| Role | What they can do |
|---|---|
| **Student** | Chat, search, read cited passages and download documents |
| **Faculty** | Everything a student can, plus upload, delete and re-index material, and edit module titles, descriptions and the web-search setting |
| **Administrator** | Everything a student can, plus manage users, rename years and blocks, and watch jobs and usage. Administrators do not upload material |

> **Note.** Every signed-in user can search and read **all** uploaded material. There are no per-module permissions. Conversations, however, are private to their owner (not even administrators can open them).

> **About the screenshots.** The screenshots in this manual come from a demo installation with sample users (Dr. Amina Admin, Prof. Farah Khan and Sara Ahmed). The AI answers shown in them come from a test model and are **examples only**; real answers come from the AI provider you configure.

Throughout this manual, a line such as **Who: Students, Faculty** tells you which roles a section is for.

# Installation and first-time setup

**Who: whoever installs CoursePilot (usually the administrator or IT).**

## Requirements

- A computer running **Windows, macOS or Linux**.
- **Python 3.12 or newer** ([python.org](https://www.python.org/downloads/)). On Windows, tick "Add Python to PATH" in the installer.
- An **internet connection**, because the AI providers (chat model, embeddings and optionally web search) are cloud services.
- At least one **API key**: one for the chat model (Anthropic Claude or Google Gemini) and one for embeddings (Gemini, Voyage or OpenAI). A Gemini key can serve both. A Tavily key for web search is optional.
- No Docker, no Node.js, no database server. Setup installs everything with Python tools.

## Getting the code

Either:

1. **Clone with git:** `git clone <repository address> CoursePilot`, or
2. **Download a release zip** (`coursepilot-vX.Y.Z.zip`) and unzip it into a folder, for example `C:\CoursePilot` or `~/coursepilot`.

## Running the setup

Open a terminal in the CoursePilot folder and run:

```
setup.bat          (Windows)
./setup.sh         (macOS / Linux)
```

The script checks that Python is available, installs the package manager `uv` if it is missing (with `pip install --user uv`; on Linux systems where pip is "externally managed" it offers the official installer and asks first), installs the dependencies, and then starts the interactive questions below. Press **Enter** to accept the value shown in `[brackets]`.

If Python is older than 3.12, or SQLite on the computer lacks a needed feature, setup stops and explains what is missing.

## The setup questions, step by step

Setup asks the questions in this order.

1. **Port for the web app.** The port the site listens on (default `8000`). Enter a number from 1 to 65535.
2. **Network setting.** Setup first prints whether the current setting is "this computer only" or "other computers may connect without HTTPS". It then asks: *"Will other computers connect to this machine over the network without HTTPS?"*
    - **n** (default): only this computer can open the site, at `http://localhost:<port>`.
    - **y**: other computers on your network can connect. Setup prints a warning: without HTTPS, passwords and session cookies cross the network in clear text, so use this **only on a closed network you trust**. It then asks *"Address other computers will type in their browser"*. Enter the machine's network address as `http://HOST:PORT`, for example `http://192.168.1.20:8000`. `localhost` is not accepted here. See "Choosing the network address" below.
3. **Session secret.** If there is no strong `SECRET_KEY` yet, setup generates one automatically ("Generated a new SECRET_KEY."). An existing strong key is never replaced.
4. **Chat AI provider** (`anthropic` or `gemini`). Default `anthropic`.
5. **Chat model** and **Fast model.** Prefilled with the provider's defaults (Anthropic: `claude-sonnet-5-5` and `claude-haiku-5-5`; Gemini: `gemini-2.5-flash` for both). The fast model is a cheaper one used for rewriting follow-up questions and quick checks. Press Enter to accept.
6. **API key for the chat provider.** The input is hidden. Enter the key, press Enter to keep the current one, type `-` to clear it, or leave it empty to skip. You can skip and add it later, but the chat will say "AI provider not configured" until a key is set.
7. **Embedding provider** (`gemini`, `voyage` or `openai`). Default `gemini`. Embeddings turn text into numbers so the system can find passages by meaning. Anthropic has no embeddings service, so Claude users pick one of these separately. If you chose Gemini for chat, setup tells you the Gemini key is shared and asks for it only once.
8. **Embedding model** and **Embedding dimension.** Defaults: Gemini `gemini-embedding-001` / 768, Voyage `voyage-3.5` / 1024, OpenAI `text-embedding-3-small` / 1536.
9. **API key for the embedding provider** (hidden, same rules as step 6), unless it is the shared Gemini key.
10. **Tavily web search API key** (optional, hidden). Only needed if the assistant may search the internet when the material cannot answer. Only the search query is sent to Tavily. Without a key, the assistant answers from the material only and says that web search is not configured.
11. Setup writes the `.env` file, brings the database up to date, creates the programme structure (Years, Blocks and Modules), and, if no administrator exists yet, asks you to **create the first administrator**:
    - **Admin email**
    - **Display name** (default "Administrator")
    - **Password (min 10 characters, hidden)**, then **Repeat password**. You have 3 attempts.

**Password rules.** At least 10 characters; not only spaces; not the same as your email or the part before the `@`; and not on the built-in list of very common passwords.

### Example setup session

The text below is an **example** with placeholder values. Your output will differ slightly, and the keys you type are hidden on screen.

```text
C:\CoursePilot> setup.bat
...installing dependencies...
CoursePilot setup. Press Enter to accept the value in [brackets].
Port for the web app [8000]:
Current network setting: this computer only.
Will other computers connect to this machine over the network without HTTPS? [y/N]:
Generated a new SECRET_KEY.
Chat AI provider (anthropic/gemini) [anthropic]: gemini
Chat model [gemini-2.5-flash]:
Fast model [gemini-2.5-flash]:
Gemini API key (optional) [(not set)] (Enter keeps it, '-' clears it, empty skips):
Gemini chat and Gemini embeddings share GOOGLE_API_KEY, so it is asked only once.
Embeddings are independent of the chat model (Anthropic has no embeddings API).
Embedding provider (gemini/voyage/openai) [gemini]:
Embedding model [gemini-embedding-001]:
Embedding dimension [768]:
Note: changing the embedding provider, model or dimension later (after material was
processed) requires 'python -m app.cli reindex-all'.
Web search (optional): only when the uploaded material cannot answer a question, ...
Tavily web search API key (optional) [(not set)] (Enter keeps it, '-' clears it, empty skips):
Wrote C:\CoursePilot\.env.
Database is up to date.
Programme structure ready (36 new rows).
Create the first administrator account.
Admin email: admin@example.edu
Display name [Administrator]: Dr. Admin
Password (min 10 characters, hidden):
Repeat password:
Created administrator admin@example.edu.
Setup complete. Start CoursePilot with start.bat (Windows) or ./start.sh.
```

(The "36 new rows" in this example is 3 years + 9 blocks + 27 modules, the default structure.)

## Choosing the network address (only if others will connect)

If you answered **yes** to the network question:

1. Find this computer's IPv4 address. On Windows open a terminal and run `ipconfig`, then look for **IPv4 Address** under your active network adapter (for example `192.168.1.20`). On macOS/Linux use `ip addr` or `ifconfig`.
2. Type the address with the port, for example `http://192.168.1.20:8000`.
3. Give this computer a **fixed (static or reserved) address** in your router or network settings. If the address changes, the saved address no longer works; run setup again to update it.
4. **Everyone must type exactly that address** in their browser. CoursePilot checks the address on every change you make in the site, so reaching it by a different name or address (for example by computer name instead of the IP) can produce a "forbidden" error. See the Troubleshooting chapter.
5. If a firewall is on, allow the chosen port for your local network only. Never expose this setup to the internet.

When this mode is on, administrators see a banner on every page: "Traffic to this server is not encrypted (ALLOW_INSECURE_HTTP=true)." For use beyond a trusted closed network, put CoursePilot behind HTTPS; see `docs/deploy.md`.

## Running setup again

You can run setup again at any time (for example to change a key or the network setting). Existing values are shown as defaults, so pressing **Enter** keeps them. API keys are never printed in full: they are shown masked, for example `****a1b2`, or `(not set)`. Type `-` to clear a key. Comments and unknown lines in `.env` are preserved, and an existing strong secret key is kept. If you change the embedding settings, setup reminds you to re-index existing material (see Troubleshooting).

## What setup writes

- **`.env`** at the top of the CoursePilot folder: all settings and keys. It is created readable by its owner only. **Never share or commit it**, and keep a copy in a password manager.
- **`data/`**: the database files and uploaded documents (created when the app first runs).
- **`backend/.venv/`**: the installed Python packages.

# Starting and stopping

**Who: whoever runs the server.**

## Starting

```
start.bat          (Windows)
./start.sh         (macOS / Linux)
```

This brings the database up to date (migrations), starts the **web app** and the background **worker** (which processes uploaded files), waits until the site answers, and **opens your browser** at the configured address, normally `http://localhost:8000`. Keep the terminal window open while people use CoursePilot.

Example console output (**example**; logging lines vary):

```text
CoursePilot is starting at http://localhost:8000 (Ctrl+C to stop)
(worker and web app log lines appear here)
```

Both processes are needed: without the worker, uploaded documents stay "Queued" forever.

## Stopping

Press **Ctrl+C** in the terminal. The web app stops and the worker is shut down gracefully; a job that was in progress is picked up again next time.

## Changing keys or settings later

1. Stop CoursePilot (Ctrl+C).
2. Run `setup.bat` / `./setup.sh` again, press Enter to keep what is fine and change what you need. (You can also edit `.env` by hand.)
3. Start again with `start.bat` / `./start.sh`.

## Where your data lives, and backups

Everything is in the `data/` folder next to `.env`: `coursepilot.db` (users, structure, documents' text index, conversations), `checkpoints.db` (chat memory) and `files/` (the uploaded originals). Keep it on a local disk, not a network share.

To make a backup (safe while the app runs), from the `backend` folder:

```
python -m uv run python -m app.cli backup
```

Options: `--out DIR` to choose the destination and `--keep N` to keep only the last N backups. A dated folder with both databases, the uploaded files and a checksum manifest is created. The backup does **not** include `.env`; copy that separately. To restore, stop the app and worker, then run `python -m uv run python -m app.cli restore <backup folder>` (it verifies checksums and asks for confirmation). Production deployments (HTTPS, services, schedules) are covered in `docs/deploy.md`.

# Signing in and first steps

**Who: Everyone.**

## What it is for

The login page is the only page you can see without an account. Everything else requires you to sign in.

![The sign-in page. Enter the email and password your administrator gave you.](img/01-login.jpg){width=100%}

## Signing in

1. Open the address your administrator gave you (for example `http://localhost:8000`).
2. Enter your **email** and **password**, then press **Log in**.
3. A wrong email, wrong password or deactivated account all give the same message on purpose. Repeated failures are rate limited; wait a minute and try again.

## First login: choose your own password

If an administrator created your account, or reset your password, you must change it at your first sign-in. Until you do, you can only reach the change-password page, sign-out and static files.

![Change password. Enter your current (temporary) password and a new one.](img/17-change-password.jpg){width=100%}

1. Enter the **current password** (the temporary one you were given).
2. Enter a **new password** (at least 10 characters, not your email, not a common password) and confirm it.
3. Press the change button. Your other sessions are signed out.

You can change your password again any time from the **Change password** link in the footer.

## Themes, accent colours and phones

The top bar has a **sun/moon button** for the dark or light theme and a row of **five coloured dots** to choose an accent colour (crimson, coral, plum, teal, ocean). Your choice is remembered in your browser. If you never choose, CoursePilot follows your device's light or dark setting.

![Dark theme with the teal accent colour on the home page.](img/18-dark-teal-home.jpg){width=100%}

CoursePilot works on phones. On narrow screens the navigation moves behind a **menu button**.

![The home page on a phone.](img/19-mobile-home.jpg){width=45%}

![The menu on a phone: tap the menu button in the top bar to open the navigation.](img/20-mobile-menu.jpg){width=45%}

## The top bar

Across the top you see: **Home**, **Chat**, **Search**, **Materials** and **Programme** for everyone, plus **Users**, **Jobs** and **Usage** for administrators. At the right are the theme button and your name. The page footer shows the version and a link to change your password.

# For students

**Who: Students (also useful for Faculty and Administrators, who can chat too).**

## Home: browsing the programme

**What it is for:** to see which modules exist and which documents each contains.

![Home page: the programme as Year, Block and Module, with documents in each module.](img/02-home-student.jpg){width=100%}

**What you see:** a "Welcome" heading, a **Start a chat** button, and the programme as expandable years and blocks. Each module has a **Materials** link (its documents, which you can open or download) and an **Ask in chat** link that opens a chat about that module.

## Chat

**What it is for:** asking questions and getting an answer built from your course material, with sources.

![A new chat: pick a module (or All material) and type your question.](img/03-chat-start.jpg){width=100%}

**What you see:**

- **Pick a module** (left): choose one module, or **All material** to search everything. The line under it shows the module's Year › Block › Module path.
- **Your conversations** (left): your earlier chats, each with a **Delete** link, and a **+ New chat** button.
- The **message box** at the bottom. Press **Enter** to send, **Shift+Enter** for a new line.

### Asking a question

1. Open **Chat**.
2. Choose a module in **Pick a module**, or **All material**.
3. Type your question and press **Enter** or **Send**.
4. Watch the **status lines** while the answer streams in, such as "Searching Module 2...", "Widening search to the whole block..." or "Searching the web...". They show where the assistant is looking.
5. Ask follow-up questions in the same conversation. The assistant understands "and what changes in heart failure?" from the earlier messages.

![An answer with citation chips ([C1], [C2]), a "Sources from the material" list, and Helpful / Not helpful buttons. The answer text is an example from a test model.](img/04-chat-answer.jpg){width=100%}

### Reading the citations

Every factual statement should carry a marker.

- **`C1`, `C2`... chips** point to your course material. Below the answer, **Sources from the material** lists each cited document with its page or slide and its Year › Block › Module path.
- **`W1`, `W2`... chips** point to web pages and appear in a separate, clearly labelled **From the web** block (see below).

### The citation side panel

Click a `C` chip to open a side panel with the exact passage the answer relied on.

![The passage panel: document title, path, page or slide, the passage text and a link to open the document at that page.](img/05-passage-panel.jpg){width=100%}

1. Click the chip (for example `C1`).
2. Read the **document title**, the **Year › Block › Module path**, the **page or slide**, and the **passage**.
3. Choose **Open document at page N** to open the original file at that page (PDFs open at the page; other formats download).
4. Close the panel with the close button or the **Esc** key.

Always check the passage when something matters, for example before an exam or assignment.

### The "From the web" block

If the material, searched step by step, cannot answer your question, and web search is allowed for the module, the assistant may search the internet. In that case the answer contains a separate **From the web** section with `W` chips. Click one to see the site, title and address; it opens in a new tab. Things to know:

- Web content is **not** from your course. If the web contradicts the material, the assistant says so and prefers the material for anything graded.
- If the assistant could not find anything relevant anywhere, it says so plainly rather than guessing.
- Web search can be switched off per module by faculty, or may not be set up at all on your installation.

### Feedback

Under each answer, press **Helpful** or **Not helpful**. This helps administrators and faculty improve the material. Administrators see the rated answer, your question and your comment, but **not who you are**, and not the rest of your conversation.

### Managing conversations

- Click a conversation in the left list to reopen it.
- Click **Delete** next to a conversation to remove it permanently, including its memory. Your conversations are private: no other user, administrators included, can open them.

### Limits

To keep costs under control each user may send a limited number of messages per day (the exact limit is set by your administrator; the default is 100) and a limited number per minute. If you reach the daily limit you will see that you have used your daily message limit; it resets at 00:00 UTC. If you send messages too quickly, wait the number of seconds shown and try again. Only one answer per user is generated at a time.

## Search

**What it is for:** finding passages directly, without an AI-written answer. Good for locating "where was this covered?"

![The search page: type keywords or a question, choose where to search, and see matching passages.](img/06-search.jpg){width=100%}

**What you see:** a **Question or keywords** box, a **Module** list, **Search in** (module, block or all), an optional **Week** filter, and a **Widen automatically (module, then block, then all)** checkbox.

1. Open **Search**.
2. Type your question or keywords.
3. Choose a module and what to search in. Tick **Widen automatically** if you want the search to widen from the module to its block and then to everything when nothing good is found.
4. Press **Search**.
5. Press **View passage** on a result to open the full passage and a link to the document.

# For faculty

**Who: Faculty.**

Faculty can do everything students can, plus manage the material. Whatever you upload can be read by **every user of the site**.

## Materials index

**What it is for:** choosing the module you want to upload to or manage.

![The Materials page lists every module with a link to its material.](img/07-materials-index.jpg){width=100%}

Open **Materials** in the top bar and choose a module.

## Uploading material

![The module's materials page: the notice, the drop zone and the documents list.](img/08-materials-upload.jpg){width=100%}

**What you see:**

- A yellow notice: **"All users of this site can see uploaded material. Only upload files you have the right to share."** Read it before choosing files.
- A dashed **drop zone** with a **Choose files** button.
- The list of **Documents in this module**.

**Allowed files:** PDF, PPTX, DOCX, MD, TXT and HTML, up to **50 MB per file**. The file's real content is checked, not only its extension. A file already in the same module (same content) is rejected as a duplicate.

To upload:

1. Open **Materials** and choose the module.
2. Drag files onto the drop zone, or press **Choose files** and select them (you can pick several).
3. Each file shows its own progress. When the upload finishes, the document appears in the list.
4. The list refreshes by itself every few seconds while documents are being processed.

## Understanding statuses

![Document cards: status badge, number of passages, and Download, Re-index and Delete.](img/09-materials-documents.jpg){width=100%}

Each card shows the title, original filename, size and upload time, a **status badge**, and how many **passages** (searchable pieces) it was split into, plus pages or slides.

| Status | Meaning |
|---|---|
| **Queued** | Uploaded and waiting for the background worker. |
| **Processing** | The worker is reading the file, splitting it into passages and indexing it. |
| **Ready** | Searchable. Students can now get answers and citations from it. |
| **Failed** | Processing did not work. The card shows the reason. |
| **Re-indexing** | A **Ready** document is being re-processed. It stays searchable with its old passages until the new ones are ready. |

If a document stays **Queued** for a long time, the worker is probably not running (see Troubleshooting).

## When a document fails

![A failed document (a scanned PDF) next to a ready one. The card shows why it failed.](img/10-materials-failed.jpg){width=100%}

Read the message on the card. Typical reasons:

- **"This PDF looks scanned"**: the PDF is only page images with no selectable text, and OCR (text recognition) is not enabled. Re-export the PDF from the original source with text, or run OCR on it first, then upload it again. Your administrator can also enable OCR in the settings.
- **A missing or invalid AI key**: processing needs the embedding key. Ask the administrator to set it, then press **Re-index**.
- **A file that is too large, corrupt or has too many pages.**

After fixing the cause, upload a corrected file, or press **Re-index** on the card if the file itself is fine.

## Re-index and delete

- **Download** (or **Open** for PDFs) gets the original file.
- **Re-index** processes the file again (useful after an AI key was fixed). The document stays searchable meanwhile. If re-indexing fails, the old passages stay and the error is shown.
- **Delete** removes the document, its passages and the stored file. It first disappears from lists and search, then is cleaned up by the worker. This cannot be undone.

## Programme page: modules

**What it is for:** giving each module a clear title and description, and deciding whether web search is allowed.

![Faculty view of the programme: edit a module's title, description and web-search setting.](img/11-programme-faculty.jpg){width=100%}

1. Open **Programme**.
2. Find the module. Edit **Module N title** and **Description**.
3. Tick or untick **Allow web search fallback for this module**. When unticked, the assistant never searches the internet for questions in this module.
4. Press **Save module**.

Faculty can rename modules; renaming years and blocks is for administrators.

# For administrators

**Who: Administrators.**

## Users

**What it is for:** creating accounts and managing who can sign in.

![The Users page: create a user, then manage the list.](img/12-admin-users.jpg){width=100%}

**Create a user**

1. Open **Users**.
2. Fill in **Email**, **Display name** and **Role** (student, faculty or admin).
3. Optionally enter an **Initial password** (at least 10 characters). If you leave it blank, CoursePilot generates a temporary password and shows it **once**; copy it and give it to the person securely.
4. Press **Create user**. The user must choose their own password at first sign-in.

**Manage users**

- **Role:** pick a role in the list and press **Set**.
- **Deactivate / Reactivate:** a deactivated user cannot sign in and is signed out everywhere. Accounts are never deleted, so history is kept.
- **Reset password:** generates a new temporary password (shown once, never stored in readable form). The user is signed out and must choose a new password.
- **Safety rules:** you cannot deactivate or demote yourself, and the **last active administrator** can never be removed.

Forgot-password requests go to an administrator, who uses **Reset password**.

## Programme structure

![Administrators can rename years, blocks and modules.](img/16-programme-admin.jpg){width=100%}

Open **Programme** and edit the title of any **year** or **block** and press **Rename**; modules have the same fields as in the faculty view (title, description, web-search setting). The number of years, blocks and modules is fixed by the installation settings (default 3 x 3 x 3), not changed on this page.

## Jobs

**What it is for:** following the background work (processing, re-indexing and deletion of documents).

![The Jobs page: one failed job with a Retry button.](img/13-admin-jobs.jpg){width=100%}

Jobs are **queued**, **running**, **done** or **failed**. The list refreshes every few seconds. Failed jobs show their error with secrets removed. Interrupted jobs are retried automatically with increasing delays before they are marked failed.

To retry a failed job:

1. Fix the cause first (for example set the missing API key in `.env` and restart).
2. Press **Retry** on the job. Only failed jobs can be retried. This resets its attempts and queues it again.

## Usage

**What it is for:** seeing how much the system is used and what it costs.

![Usage overview: tiles and the messages-per-day chart.](img/14-admin-usage.jpg){width=100%}

At the top are summary **tiles** and a **messages per day** chart (UTC days, last 30 days by default).

![Details: tokens per day and model, top users and thumbs-down feedback.](img/15-admin-usage-details.jpg){width=100%}

Further down:

- **Tokens per day and model**: how much text was sent to and produced by the AI, including **cached** tokens (cheaper repeated parts of prompts).
- **Documents and jobs by status**, **storage used**, and how many answers used **web search**.
- **Top users**: message counts by email (counts only, no content) with today's use against the daily limit.
- **Feedback**: answers rated **Not helpful** (or helpful), with the question and any comment.

> **Privacy.** Conversations stay private to their owners. The feedback list shows a rated answer, its question and comment, but **not who rated it** and not the rest of the conversation.

# How answers are produced

In plain terms, for every question the assistant follows the same fixed path:

1. **Understand the question.** A follow-up such as "and what about heart failure?" is rewritten into a complete question using the recent conversation.
2. **Search the selected module.** It looks for the best matching passages (by meaning and by keywords).
3. **Widen if needed.** If the module does not contain enough, it widens to the whole block, then to all material.
4. **Only then, the web.** If nothing in the material is good enough, and web search is available for the module, it may search the internet.
5. **Write the answer** using only the numbered sources it was given, with `[C#]` markers for material and `[W#]` for web pages.
6. **Check the citations.** Markers that do not match a real source are removed, and only the sources actually cited are shown.

Text inside uploaded documents and web pages is treated as information, never as instructions, so a document cannot "tell" the assistant what to do. The assistant is also designed to encourage learning rather than simply hand over answers to assignments.

# Troubleshooting and FAQ

**The chat says "AI provider not configured."**
No API key is set for the chat provider. An administrator should run setup again (or edit `.env`), enter the key, and restart CoursePilot.

**A document shows Failed with "This PDF looks scanned."**
The PDF has no selectable text. Re-export it with text or OCR it first, then upload again (or ask your administrator to enable OCR).

**A document stays Queued.**
The worker is not running. If you use `start.bat` / `./start.sh` both run together; check the terminal for errors. A missing embedding key also makes documents fail; set it, then press **Re-index** or **Retry** on the job.

**After changing the embedding provider, search or chat reports an index mismatch.**
Vectors built with one embedding model cannot be searched with another. Stop CoursePilot and run, from the `backend` folder, `python -m uv run python -m app.cli reindex-all`. It rebuilds the index and re-processes all documents; they show as Queued/Processing until finished.

**I get "forbidden" when saving something, or login fails, on a LAN.**
You are probably using a different address from the one configured (for example the computer name instead of the IP address). Everyone must use exactly the address saved in setup (`BASE_URL` in `.env`). Run setup again if the address changed.

**"Rate limited" or "daily limit" messages.**
You sent messages too fast (wait the number of seconds shown) or used your daily message allowance (resets at 00:00 UTC). An administrator can raise the limits in `.env` (`CHAT_RATE_LIMIT_PER_MIN`, `CHAT_DAILY_LIMIT_PER_USER`).

**I forgot my password.**
Ask an administrator to use **Reset password** on the Users page. You will get a temporary password and must choose a new one.

**Where are the logs?**
When you use `start.bat` / `./start.sh`, logs appear in the terminal as structured lines (passwords, keys and prompts are not logged). Production services write to your service manager's logs; see `docs/deploy.md`.

**Can I see other people's conversations?**
No. Conversations are private to their owner, even from administrators.

**Why can everyone see uploaded files?**
By design, every signed-in user can read all uploaded material. Only upload what you have the right to share.

# Glossary

**Block**: a group of modules inside a year.

**Chunk / passage**: a searchable piece of a document (roughly a few paragraphs or one slide).

**Citation**: a marker such as `[C1]` (course material) or `[W1]` (web) linking a statement to its source.

**Embedding**: a numeric representation of text meaning, used to find passages similar to a question.

**Index / re-index**: preparing a document for search; doing it again for the same document.

**Job**: a unit of background work such as processing a document.

**Module**: a course unit inside a block; the place documents are uploaded.

**Programme**: the whole structure of Years, Blocks and Modules.

**Worker**: the background process that parses, splits and indexes uploaded files.

**Web fallback**: the labelled internet search used only when the material cannot answer.
