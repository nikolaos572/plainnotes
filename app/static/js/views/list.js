/**
 * The notes list and search results (#/?q=…&sort=…&order=…).
 */

import * as api from "../api.js";
import { el, formatDate, segmentNodes } from "../dom.js";
import { icon } from "../icons.js";
import { tagHref } from "../markdown.js";

// Notes with this tag are listed first on the home page. It is an ordinary tag, written in the
// note text, so pinning needs no extra data on the server.
const PINNED_TAG = "pinned";
const SORTS = [
  ["relevance", "Relevance"],
  ["title", "Title"],
  ["modified", "Modified"],
];

/**
 * The hash for a list with these options; defaults are left out.
 * @returns {string}
 */
export function listHash(q, sort = "", order = "") {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (sort) params.set("sort", sort);
  if (order) params.set("order", order);
  const query = params.toString();
  return query ? `#/?${query}` : "#/";
}

/**
 * Tag chips linking to tag searches.
 * @param {string[]} tags
 * @returns {HTMLElement|null}
 */
export function tagChips(tags) {
  if (!tags.length) return null;
  return el(
    "ul",
    { class: "tags", "aria-label": "Tags" },
    tags.map((tag) => el("li", {}, el("a", { class: "tag", href: tagHref(tag) }, `#${tag}`))),
  );
}

function resultRow(result) {
  const titleNodes = result.title_segments ? segmentNodes(result.title_segments) : [result.title];
  return el(
    "li",
    { class: "note-row" },
    el("a", { class: "note-link", href: `#/note/${encodeURIComponent(result.id)}` }, titleNodes),
    el(
      "div",
      { class: "note-meta" },
      el("time", { datetime: result.modified }, formatDate(result.modified)),
      tagChips(result.tags),
    ),
    (result.snippets ?? []).map((snippet) =>
      el("p", { class: "snippet", translate: "no" }, segmentNodes(snippet)),
    ),
  );
}

function noteList(results) {
  return el("ul", { class: "note-list" }, results.map(resultRow));
}

/**
 * All notes, with the ones tagged #pinned in their own section at the top. Both sections keep
 * the chosen sort order. Search results are never split this way.
 * @param {Object[]} results
 * @returns {HTMLElement}
 */
function pinnedFirst(results) {
  const pinned = results.filter((result) => result.tags.includes(PINNED_TAG));
  if (!pinned.length) return noteList(results);
  const others = results.filter((result) => !result.tags.includes(PINNED_TAG));
  const section = (id, title, items) =>
    el("section", { "aria-labelledby": id }, el("h2", { id }, title), noteList(items));
  return el(
    "div",
    { class: "note-sections" },
    section("pinned-heading", "Pinned", pinned),
    others.length ? section("others-heading", "Other notes", others) : null,
  );
}

function controls(q, sort, order) {
  const select = el(
    "select",
    {
      id: "sort",
      on: { change: () => (location.hash = listHash(q, select.value, "")) },
    },
    SORTS.map(([value, label]) => el("option", { value, selected: value === sort }, label)),
  );
  const nextOrder = order === "asc" ? "desc" : "asc";
  return el(
    "div",
    { class: "list-controls" },
    el("label", { for: "sort" }, "Sort by"),
    select,
    el(
      "button",
      {
        type: "button",
        class: "button",
        "aria-label": `Order: ${order === "asc" ? "ascending" : "descending"}. Change order`,
        on: { click: () => (location.hash = listHash(q, sort, nextOrder)) },
      },
      icon(order === "asc" ? "arrow-up-narrow-wide" : "arrow-down-wide-narrow"),
      order === "asc" ? "Ascending" : "Descending",
    ),
  );
}

/**
 * Render the list or search results.
 * @param {{query: URLSearchParams}} params
 * @param {Object} app
 */
export async function listView({ query }, app) {
  const q = query.get("q") ?? "";
  const searching = q.trim() !== "" && q.trim() !== "*";
  const sort = query.get("sort") || (searching ? "relevance" : "modified");
  const order = query.get("order") || (sort === "title" ? "asc" : "desc");
  app.setSearch(q);
  const heading = el("h1", {}, searching ? "Search results" : "All notes");
  let results;
  try {
    results = await api.searchNotes({ q, sort, order });
  } catch (error) {
    if (error.code === "not_authenticated") return { node: el("section") };
    return { node: el("section", { class: "list" }, heading, el("p", { class: "form-error" }, error.message)) };
  }
  let body;
  if (results.length) {
    body = searching ? noteList(results) : pinnedFirst(results);
  } else if (searching) {
    body = el("p", { class: "empty" }, "No notes match your search.");
  } else {
    body = el(
      "div",
      { class: "empty" },
      el("p", {}, "No notes yet."),
      app.session?.read_only ? null : el("a", { class: "button primary", href: "#/new" }, icon("plus"), "New note"),
    );
  }
  return {
    node: el("section", { class: "list" }, el("div", { class: "list-head" }, heading, controls(q, sort, order)), body),
  };
}
