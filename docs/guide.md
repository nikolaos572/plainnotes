# User guide

How plainnotes stores your notes and how to use it. To install and configure it, see the [README](../README.md).

## Your data

```
data/
├── notes/
│   ├── 20261018-143205-k3f9/          one folder per note; the name is the note's ID
│   │   ├── Docker commands.md         the note; the file name is the title
│   │   └── attachments/
│   │       └── compose.yml
│   ├── 20261018-150011-a71c/
│   │   └── Shopping list.md
│   └── .trash/
│       └── 20261019-101500_20261017-090000-zz12/
│           └── Old note.md
└── testing/                           used only by the storage check at startup
```

- **Never put anything in `testing/`:** it is used only by the storage check at startup, which first deletes its contents. plainnotes never writes anywhere else outside `notes/`.
- **Earlier versions** kept the note folders and `.trash` directly in the data folder. plainnotes refuses to start on that layout and says so in its log; move them into `notes/` first.

- **Ownership:** in Docker, plainnotes runs as UID 61001, which matches no account on the host, so everything it writes belongs to 61001. Your own account can read the notes and attachments (but not the private `.history` folders) and cannot change them. Add, edit, import or restore files with `sudo`, then give them to plainnotes: `sudo chown -R 61001:61001 data/notes`.
- **Edit files directly** with any editor or sync tool. plainnotes reads the disk on every request, so changes, renamed files and new attachments appear on the next page load. Changes detected before saving show a conflict dialog. Avoid simultaneous writes with external editors: they do not participate in the app lock. Content saves retain the exact displaced file under the note folder’s hidden .history/ directory, including an external version replaced at the final instant.
- **Import existing notes** (in writable mode) by placing `.md` files directly in the `notes/` folder. On the next start or page load, each one is moved into a new note folder (the file itself and its date stay unchanged). Files whose name is not a valid title, whose title is already used, or that plainnotes cannot read stay where they are, and the server log says why. Try it on a copy of your files first.
- **Back up** by copying the whole data folder as root (`sudo`), so that the private `.history` folders are included; it contains everything.
- **The trash:** deleting a note moves its folder, with its attachments, to `.trash/`. From the Trash page you can restore it or delete it permanently. Nothing is purged automatically.
- **Titles** are unique (ignoring case) and cannot contain `/ \ : * ? " < > |` or start with `.`.
- **Line endings:** changing only the title preserves every content byte. Text edits preserve uniform LF, CRLF, or lone-CR line endings. Mixed line endings normalize to CRLF if the original contains CRLF, otherwise LF. Recovery drafts retain this information.
- **No index:** every search and every list reads the notes from disk, so they always match the files. This stays fast up to a few thousand notes.

## Viewing and editing

- **Text and Markdown views:** Text shows the file exactly as stored, and **Copy all** copies it exactly. Markdown renders it read-only. The choice is remembered.
- **Wrap lines:** in Text view and in the editor, untick **Wrap lines** to keep long lines (such as commands) on one line and scroll sideways instead. The choice is remembered for both.
- **Pinned notes:** add the tag `#pinned` anywhere in a note to list it in a **Pinned** section at the top of the home page. Remove the tag to unpin it.
- **Deleting:** **Delete** moves the note to the trash; the message that follows has an **Undo** button for a few seconds.
- **Drafts:** each editor keeps an independent recovery draft in this browser, including separate new-note tabs. Recovery is offered when you reopen the editor, even if the original note was deleted. A persistent warning appears if storage fails. Drafts are written shortly after typing pauses; the newest keystrokes may not survive an immediate crash, so save valuable work explicitly. Identical drafts are removed after that text is saved, while other versions remain until discarded. Discarding an editor copy does not delete the recovery record it was restored from; use Discard in the recovery dialog to delete that record explicitly. Logout asks before deleting this installation’s drafts and ending editing in its other tabs. Cancelling or a failed logout keeps drafts; editing pauses while the logout request is pending and resumes on failure or timeout.
- **Printing:** printing a note (`Ctrl`/`Cmd` + `P`) leaves out the header and buttons, uses black on white in either theme, and wraps long lines so nothing is cut off.

## Markdown

- **Supported:** CommonMark plus tables, strikethrough, task lists (shown as checkboxes that cannot be toggled), and links for `http(s)://` and `mailto:` text. Single line breaks are kept, and quotes and dashes are never "smartened".
- **Code:** put commands in fenced code blocks (```` ``` ````), and each block gets a **Copy** button that copies its exact text.
- **Wikilinks:** `[[Note title]]` links to the note with that title (ignoring case). A link to a missing note is shown in red and opens a new note with that title.
- **Raw HTML is not rendered.** It is shown as text, because HTML inside a note could otherwise run scripts in your logged-in browser.

## Tags and search

- **Tags:** write `#tag` at the start of a line or after a space, using letters, digits, `_` and `-`, with at least one letter: `#docker`, `#linux-tips` and `#συνταγές` are tags. `# Heading`, `issue #42`, `https://x.org/#top` and anything inside `` `code` `` or a fenced code block are not. Indented code blocks are not recognised, so use fenced blocks for code that contains `#`.
- **Search:** all words must match, in the title or the text, ignoring case and accents. Use `"quotes"` for a phrase and `#tag` for an exact tag.

## Attachments

In the editor, upload files with the button, by dragging them onto the editor or by pasting (screenshots are named `pasted-YYYYMMDD-HHMMSS.png`). **Insert link** adds `![name](attachments/…)` for images and `[name](attachments/…)` for other files; these relative links also work in other Markdown tools. Images, audio, video and plain text open in the browser; everything else is downloaded.

## Keyboard shortcuts

| Key | Where | Action |
|---|---|---|
| `/` | anywhere outside text fields | Focus the search field |
| `n` | anywhere outside text fields | New note |
| `Esc` | search field | Clear it and leave the field |
| `e` | note view | Open the editor |
| `Ctrl`/`Cmd` + `S` or `Enter` | editor | Save |
| `Esc` | editor | Close (asks about unsaved changes) |
| `Tab` / `Shift` + `Tab` | editor text | Insert a tab / move focus out |

## Recovering previous content

Every content change retains the displaced file in `data/notes/<note-id>/.history/<random-id>/<title>.md`.
These are ordinary files, excluded from search and the attachment API. Failed or interrupted
saves can also leave the proposed new content there. Compare file contents before recovery;
folder IDs do not indicate chronological order. Copy the chosen file to a safe location,
then restore its content through the editor or while the app and external editors are stopped.

Recovery directories are private to the user plainnotes runs as (UID 61001 in Docker). Run backups as root (`sudo`) so that they include them.

History is not automatically pruned. Include it in backups and remove unwanted versions
manually only after checking them. Moving a note to trash keeps its history; permanently
deleting that trash entry deletes its history too. This protects the file displaced by an
app save, not arbitrary deletions or later overwrites performed by external programs.

Content updates require Linux renameat2(RENAME_EXCHANGE), and attachment uploads require
RENAME_NOREPLACE support on the data filesystem (as in the documented Docker deployment).
Attachments are published from completed temporary files without creating empty placeholders;
a filename collision selects the next numbered name. plainnotes never falls back to a
replacement that could overwrite existing data. Instead, it tries both operations at every
start on its own new files in `testing/`. If either is unsupported (as on NFS or SMB shares),
it runs read-only, logs the reason and shows it at the top of every page.

Saves and uploads write a hidden `.plainnotes-tmp-…` file or folder next to their target and
rename it into place when it is complete. If the server stops in that moment, the hidden copy
stays where it is: plainnotes never shows it and never deletes it. It holds what was being
saved, so check it before you delete it yourself.
