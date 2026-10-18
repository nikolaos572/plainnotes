/**
 * The login form (#/login?next=…).
 */

import * as api from "../api.js";
import { el } from "../dom.js";
import { icon } from "../icons.js";

function failureMessage(error) {
  if (error.status === 429) {
    const seconds = Number(error.headers?.get("Retry-After")) || 60;
    const minutes = Math.max(1, Math.ceil(seconds / 60));
    return `Too many attempts. Try again in ${minutes} minute${minutes === 1 ? "" : "s"}.`;
  }
  if (error.code === "login_failed") return "Wrong username or password.";
  return error.message;
}

/**
 * The message for a login the server accepted but whose session cookie the browser threw away.
 * Browsers silently refuse Secure cookies on plain-HTTP pages (except localhost) and never tell
 * the page, so this is only noticed when the next request arrives without the cookie.
 * @returns {string}
 */
function cookieRefusedMessage() {
  if (location.protocol === "http:") {
    return (
      "Your password was accepted, but the browser refused the login cookie because this page " +
      "uses plain HTTP. Open plainnotes over HTTPS, or set PLAINNOTES_COOKIE_SECURE=false for " +
      "testing."
    );
  }
  return (
    "Your password was accepted, but the browser did not keep the login cookie. " +
    "Check that cookies are allowed for this site."
  );
}

/**
 * Render the login form.
 * @param {{query: URLSearchParams}} params
 * @param {Object} app
 */
export async function loginView({ query }, app) {
  const next = query.get("next") ?? "";
  const username = el("input", {
    id: "login-username",
    name: "username",
    autocomplete: "username",
    autocapitalize: "off",
    spellcheck: "false",
    required: true,
  });
  const password = el("input", {
    id: "login-password",
    name: "password",
    type: "password",
    autocomplete: "current-password",
    required: true,
  });
  const error = el("p", { class: "form-error", role: "alert" });
  const submit = el("button", { type: "submit", class: "button primary" }, icon("log-in"), "Log in");

  async function onSubmit(event) {
    event.preventDefault();
    submit.disabled = true;
    error.textContent = "";
    let accepted = false;
    try {
      await api.login(username.value, password.value);
      accepted = true;
      await app.refreshSession();
      app.navigate(next.startsWith("#/") && !next.startsWith("#/login") ? next : "#/");
    } catch (failure) {
      const cookieRefused = accepted && failure.code === "not_authenticated";
      error.textContent = cookieRefused ? cookieRefusedMessage() : failureMessage(failure);
      if (!cookieRefused) password.select();
    } finally {
      submit.disabled = false;
    }
  }

  const form = el(
    "form",
    { class: "login-form", on: { submit: onSubmit } },
    el("h1", {}, el("img", { class: "brand-logo", src: "static/favicon.svg", alt: "" }), "plainnotes"),
    el("label", { for: "login-username" }, "Username"),
    username,
    el("label", { for: "login-password" }, "Password"),
    password,
    error,
    submit,
  );
  return { node: el("section", { class: "login" }, form), mounted: () => username.focus() };
}
