/**
 * The note view (#/note/<id>): Text or Markdown, Copy all, Edit, Delete and attachments.
 */

import * as api from "../api.js";
import { el, formatDate, formatSize } from "../dom.js";
import { icon } from "../icons.js";
import { followInNoteLink, renderMarkdown, titleKey } from "../markdown.js";
import { confirmAction, copyText, errorPage, showError, toast } from "../ui.js";
import { tagChips } from "./list.js";

const MODE_KEY = "plainnotes.view_mode";
const WRAP_KEY = "plainnotes.wrap";

/** @returns {"text"|"markdown"} the remembered view mode */
function storedMode() {
  try {
    return localStorage.getItem(MODE_KEY) === "markdown" ? "markdown" : "text";
  } catch {
    return "text";
  }
}

function storeMode(mode) {
  try {
    localStorage.setItem(MODE_KEY, mode);
  } catch {
    // Private browsing: the choice just isn't remembered.
  }
}

/** @returns {boolean} whether long lines wrap in Text view and the editor (default: yes) */
function storedWrap() {
  try {
    return localStorage.getItem(WRAP_KEY) !== "off";
  } catch {
    return true;
  }
}

function storeWrap(wrap) {
  try {
    localStorage.setItem(WRAP_KEY, wrap ? "on" : "off");
  } catch {
    // Private browsing: the choice just isn't remembered.
  }
}

/**
 * The "Wrap lines" checkbox shared by Text view and the editor. The choice is remembered for
 * both; apply(wrap) runs once now and again after every change.
 * @param {(wrap: boolean) => void} apply
 * @returns {HTMLLabelElement}
 */
export function wrapToggle(apply) {
  const box = el("input", { type: "checkbox", checked: storedWrap() });
  box.addEventListener("change", () => {
    storeWrap(box.checked);
    apply(box.checked);
  });
  apply(box.checked);
  return el("label", { class: "check" }, box, "Wrap lines");
}

/**
 * Bring a note back from the trash right after it was deleted. Deleting answers without a body,
 * so the trash entry is looked up: the newest one for this note comes first.
 * @param {string} noteId
 * @param {Object} app
 */
async function undoDelete(noteId, app) {
  try {
    const entry = (await api.listTrash()).find((item) => item.id === noteId);
    if (!entry) {
      toast("This note is no longer in the trash.", "error");
      return;
    }
    await api.restoreTrash(entry.trash_id);
    toast("Note restored");
    app.navigate(`#/note/${encodeURIComponent(noteId)}`);
  } catch (error) {
    showError(error);
  }
}

/**
 * A link that opens or downloads one attachment.
 * @param {string} noteId
 * @param {{name: string}} attachment
 * @param {string} [label]
 * @returns {HTMLAnchorElement}
 */
export function attachmentLink(noteId, attachment, label = attachment.name) {
  const inline = api.opensInBrowser(attachment.name);
  return el(
    "a",
    {
      href: api.attachmentUrl(noteId, attachment.name),
      target: inline ? "_blank" : null,
      rel: inline ? "noopener noreferrer" : null,
    },
    label,
  );
}

function attachmentsSection(note) {
  if (!note.attachments.length) return null;
  return el(
    "section",
    { class: "attachments", "aria-labelledby": "attachments-heading" },
    el("h2", { id: "attachments-heading" }, "Attachments"),
    el(
      "ul",
      {},
      note.attachments.map((attachment) =>
        el(
          "li",
          {},
          attachmentLink(note.id, attachment),
          el("span", { class: "muted" }, formatSize(attachment.size)),
        ),
      ),
    ),
  );
}

/**
 * Render one note.
 * @param {{id: string}} params
 * @param {Object} app
 */
export async function noteView({ id }, app) {
  let note;
  try {
    note = await api.getNote(id);
  } catch (error) {
    return { node: errorPage(error) };
  }
  // Wikilinks need every note's title; fetch them only when the note uses [[…]].
  let titles = new Map();
  if (note.content.includes("[[")) {
    try {
      titles = new Map((await api.listNotes()).map((item) => [titleKey(item.title), item.id]));
    } catch (error) {
      showError(error);
    }
  }
  const readOnly = app.session?.read_only === true;
  const editHash = `#/note/${encodeURIComponent(note.id)}/edit`;
  let mode = storedMode();

  const body = el("div", { class: "note-body" });
  const textButton = el("button", { type: "button", class: "button" }, "Text");
  const markdownButton = el("button", { type: "button", class: "button" }, "Markdown");
  const copyButton = el("button", { type: "button", class: "button" }, icon("copy"), "Copy all");
  let wrap = true;
  const wrapControl = wrapToggle((value) => {
    wrap = value;
    body.querySelector(".note-text")?.classList.toggle("nowrap", !wrap);
  });

  function show() {
    textButton.setAttribute("aria-pressed", String(mode === "text"));
    markdownButton.setAttribute("aria-pressed", String(mode === "markdown"));
    copyButton.hidden = mode !== "text";
    wrapControl.hidden = mode !== "text";
    if (mode === "text") {
      const pre = el("pre", { class: wrap ? "note-text" : "note-text nowrap", translate: "no" });
      pre.textContent = note.content;
      body.replaceChildren(pre);
    } else {
      const container = el("div", { class: "markdown", translate: "no" });
      container.addEventListener("click", (event) => followInNoteLink(event, container));
      renderMarkdown(container, note.content, { noteId: note.id, titles });
      body.replaceChildren(container);
    }
  }

  function setMode(next) {
    mode = next;
    storeMode(next);
    show();
  }

  async function remove() {
    const message = `“${note.title}” will be moved to the trash.`;
    const button = { label: "Move to trash", kind: "danger", icon: "trash-2" };
    if (!(await confirmAction("Move to trash?", message, button))) return;
    try {
      await api.deleteNote(note.id);
    } catch (error) {
      showError(error);
      return;
    }
    toast("Moved to trash", "info", { label: "Undo", icon: "undo-2", run: () => undoDelete(note.id, app) });
    app.navigate("#/");
  }

  textButton.addEventListener("click", () => setMode("text"));
  markdownButton.addEventListener("click", () => setMode("markdown"));
  copyButton.addEventListener("click", () => copyText(note.content, body.querySelector("pre")));

  const node = el(
    "article",
    { class: "note" },
    el(
      "header",
      { class: "note-header" },
      el("h1", { class: "note-title" }, note.title),
      el(
        "p",
        { class: "note-meta" },
        el("span", {}, "Created ", el("time", { datetime: note.created }, formatDate(note.created))),
        el("span", {}, "Modified ", el("time", { datetime: note.modified }, formatDate(note.modified))),
      ),
      tagChips(note.tags),
    ),
    el(
      "div",
      { class: "toolbar" },
      el("div", { class: "segmented", role: "group", "aria-label": "View as" }, textButton, markdownButton),
      copyButton,
      wrapControl,
      el("span", { class: "spacer" }),
      readOnly ? null : el("a", { class: "button", href: editHash }, icon("pencil"), "Edit"),
      readOnly
        ? null
        : el("button", { type: "button", class: "button danger", on: { click: remove } }, icon("trash-2"), "Delete"),
    ),
    body,
    attachmentsSection(note),
  );
  show();
  return {
    node,
    title: note.title,
    singleKey(key) {
      if (key !== "e" || readOnly) return false;
      app.navigate(editHash);
      return true;
    },
  };
}
