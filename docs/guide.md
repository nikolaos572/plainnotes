# User guide

How plainnotes stores your notes and how to use it. To install and configure it, see the [README](../README.md).

## Your data

```
data/
├── 20261018-143205-k3f9/              one folder per note; the name is the note's ID
│   ├── Docker commands.md             the note; the file name is the title
│   └── attachments/
│       └── compose.yml
├── 20261018-150011-a71c/
│   └── Shopping list.md
└── .trash/
    └── 20261019-101500_20261017-090000-zz12/
        └── Old note.md
```

- **Edit files directly** with any editor or sync tool. plainnotes reads the disk on every request, so changes, renamed files and new attachments appear on the next page load. If a note changes on disk while it is open in the editor, saving shows a conflict dialog instead of overwriting.
- **Import existing notes** by placing `.md` files directly in the data folder. On the next start or page load, each one is moved into a new note folder (the file itself and its date stay unchanged). Files whose name is not a valid title, or whose title is already used, stay where they are. Try it on a copy of your files first.
- **Back up** by copying the whole data folder; it contains everything.
- **The trash:** deleting a note moves its folder, with its attachments, to `.trash/`. From the Trash page you can restore it or delete it permanently. Nothing is purged automatically.
- **Titles** are unique (ignoring case) and cannot contain `/ \ : * ? " < > |` or start with `.`.
- **Line endings:** files with Windows line endings (`\r\n`) keep them when you save. A file with mixed line endings is saved with `\r\n` throughout once you edit it.
- **No index:** every search and every list reads the notes from disk, so they always match the files. This stays fast up to a few thousand notes.

## Viewing and editing

- **Text and Markdown views:** Text shows the file exactly as stored, and **Copy all** copies it exactly. Markdown renders it read-only. The choice is remembered.
- **Wrap lines:** in Text view and in the editor, untick **Wrap lines** to keep long lines (such as commands) on one line and scroll sideways instead. The choice is remembered for both.
- **Pinned notes:** add the tag `#pinned` anywhere in a note to list it in a **Pinned** section at the top of the home page. Remove the tag to unpin it.
- **Deleting:** **Delete** moves the note to the trash; the message that follows has an **Undo** button for a few seconds.
- **Drafts:** unsaved changes are kept in the browser and offered again if the tab was closed. Logging out removes them.
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
