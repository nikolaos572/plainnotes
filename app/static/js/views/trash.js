/**
 * The trash (#/trash): restore notes or delete them permanently.
 */

import * as api from "../api.js";
import { el, formatDate } from "../dom.js";
import { icon } from "../icons.js";
import { confirmAction, errorPage, showDialog, showError, toast } from "../ui.js";

/**
 * Render the trash.
 * @param {Object} _params
 * @param {Object} app
 */
export async function trashView(_params, app) {
  const readOnly = app.session?.read_only === true;
  const section = el("section", { class: "trash-page" });

  async function restore(entry) {
    try {
      const note = await api.restoreTrash(entry.trash_id);
      toast(`Restored “${note.title}”`);
    } catch (error) {
      if (error.status === 409) {
        await showDialog({
          title: "Cannot restore this note",
          message: error.message,
          buttons: [{ label: "OK", value: true, icon: "check" }],
        });
      } else {
        showError(error);
      }
    }
    await render();
  }

  async function remove(entry) {
    const name = entry.title ?? "This entry";
    const message = `“${name}” and its attachments will be deleted permanently.`;
    const button = { label: "Delete permanently", kind: "danger", icon: "trash-2" };
    if (!(await confirmAction("Delete permanently?", message, button))) return;
    try {
      await api.deleteTrash(entry.trash_id);
    } catch (error) {
      showError(error);
    }
    await render();
  }

  async function empty(count) {
    const message = `${count} deleted note${count === 1 ? "" : "s"} will be deleted permanently.`;
    const button = { label: "Empty trash", kind: "danger", icon: "trash-2" };
    if (!(await confirmAction("Empty the trash?", message, button))) return;
    try {
      await api.emptyTrash();
      toast("Trash emptied");
    } catch (error) {
      showError(error);
    }
    await render();
  }

  function row(entry) {
    return el(
      "li",
      {},
      el(
        "span",
        { class: "title" },
        entry.title ?? el("em", { class: "muted" }, "No note file"),
        el("br"),
        el("span", { class: "muted" }, "Deleted ", el("time", { datetime: entry.deleted }, formatDate(entry.deleted))),
      ),
      readOnly || entry.title === null
        ? null
        : el(
            "button",
            { type: "button", class: "button", on: { click: () => restore(entry) } },
            icon("archive-restore"),
            "Restore",
          ),
      readOnly
        ? null
        : el(
            "button",
            { type: "button", class: "button danger", on: { click: () => remove(entry) } },
            icon("trash-2"),
            "Delete permanently",
          ),
    );
  }

  async function render() {
    let entries;
    try {
      entries = await api.listTrash();
    } catch (error) {
      section.replaceChildren(errorPage(error));
      return;
    }
    const head = el(
      "div",
      { class: "list-head" },
      el("h1", {}, "Trash"),
      entries.length && !readOnly
        ? el(
            "button",
            { type: "button", class: "button danger", on: { click: () => empty(entries.length) } },
            icon("trash-2"),
            "Empty trash",
          )
        : null,
    );
    const body = entries.length
      ? el("ul", { class: "trash-list" }, entries.map(row))
      : el("p", { class: "empty" }, "The trash is empty.");
    section.replaceChildren(head, body);
  }

  await render();
  return { node: section, title: "Trash" };
}
