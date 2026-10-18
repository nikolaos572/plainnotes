/**
 * All tags with their note counts (#/tags).
 */

import * as api from "../api.js";
import { el } from "../dom.js";
import { tagHref } from "../markdown.js";
import { errorPage } from "../ui.js";

/** Render the tags page. */
export async function tagsView() {
  let tags;
  try {
    tags = await api.listTags();
  } catch (error) {
    return { node: errorPage(error) };
  }
  const list = tags.length
    ? el(
        "ul",
        { class: "tag-list" },
        tags.map(({ tag, count }) =>
          el(
            "li",
            {},
            el("a", { class: "tag", href: tagHref(tag) }, `#${tag}`),
            el("span", { class: "count", "aria-label": `${count} notes` }, String(count)),
          ),
        ),
      )
    : el("p", { class: "empty" }, "No tags yet. Write #tag anywhere in a note.");
  return { node: el("section", { class: "tags-page" }, el("h1", {}, "Tags"), list), title: "Tags" };
}
