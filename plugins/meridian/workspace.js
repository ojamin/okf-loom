import { createElement as h, useEffect, useRef, useState } from "react";
import { createPluginClient } from "@meridian/plugin-sdk";
import { configuration } from "./configuration.js";
import {
  connectLoom,
  placement,
  safeMarkup,
  summarizeError,
} from "./bridge.js";
import { styles } from "./styles.js";

const button = (text, onClick, props = {}) =>
  h("button", { type: "button", onClick, ...props }, text);
const notice = (message, error = false) =>
  message &&
  h(
    "div",
    { className: "loom-notice", role: error ? "alert" : "status" },
    message,
  );
const json = (value) => JSON.stringify(value, null, 2);

/** The sandbox mounts this named React export for all three placements. */
export function LoomWorkspace() {
  const [host, setHost] = useState(null),
    [error, setError] = useState(""),
    [identity, setIdentity] = useState("");
  useEffect(() => {
    let active = true,
      client,
      timer;
    createPluginClient()
      .then((value) => {
        client = value;
        if (!active) {
          client.dispose();
          return;
        }
        setHost(client);
        const sync = () =>
          setIdentity(json([client.context.installationId, placement(client)]));
        sync();
        timer = setInterval(sync, 500);
      })
      .catch((e) => {
        if (active) setError(summarizeError(e));
      });
    return () => {
      active = false;
      clearInterval(timer);
      client?.dispose();
    };
  }, []);
  return h(
    "div",
    { className: "loom" },
    h("style", null, styles),
    error
      ? notice(error, true)
      : host
        ? h(Workspace, { host, key: identity })
        : notice("Connecting to Meridian…"),
  );
}

function Workspace({ host }) {
  const [api, setApi] = useState(null),
    [capabilities, setCapabilities] = useState(null);
  const [connected, setConnected] = useState(false);
  const [token, setToken] = useState(""),
    [connectionOpen, setConnectionOpen] = useState(false);
  const [catalog, setCatalog] = useState([]),
    [title, setTitle] = useState("Knowledge workspace");
  const [selected, setSelected] = useState(""),
    [doc, setDoc] = useState(null),
    [tab, setTab] = useState("Read");
  const [query, setQuery] = useState(""),
    [mode, setMode] = useState("lexical"),
    [hits, setHits] = useState(null);
  const [comments, setComments] = useState([]),
    [events, setEvents] = useState([]),
    [presence, setPresence] = useState(null);
  const [message, setMessage] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const [drafts, setDrafts] = useState({}),
    [newId, setNewId] = useState(""),
    [creating, setCreating] = useState(false);
  const [preview, setPreview] = useState(null),
    [forward, setForward] = useState(false),
    [showArchived, setShowArchived] = useState(false);
  const [bound, setBound] = useState(null),
    [historyLimit, setHistoryLimit] = useState(100);
  const latest = useRef({}),
    generation = useRef(0),
    alive = useRef(true),
    action = useRef(false);
  const binding = placement(host);
  const actor = `meridian:${host.context.userId}`;
  const draft = drafts[selected];
  const editing = Boolean(api && connected && capabilities?.editing);
  latest.current = { selected, draft };
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const fail = (e) => {
    if (alive.current && e.name !== "AbortError") setError(summarizeError(e));
  };

  async function run(work) {
    if (action.current) return;
    action.current = true;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await work();
      return result ?? true;
    } catch (e) {
      fail(e);
    } finally {
      action.current = false;
      if (alive.current) setBusy(false);
    }
  }

  async function refresh(client = api) {
    const [index, notes, feed, status] = await Promise.all([
      client.request("/__data/content.json"),
      client.comments(),
      client.request("/__data/events", {
        query: { order: "desc", limit: historyLimit },
      }),
      client.health(),
    ]);
    if (!alive.current) return;
    setCatalog(index.concepts);
    setTitle(index.name);
    setComments(notes.comments);
    setEvents(feed.events);
    setPresence(status.presence || null);
  }

  useEffect(() => {
    let active = true;
    (async () => {
      const client = connectLoom(host, configuration.origin);
      const info = await client.discover();
      if (info.api_version !== "1" || !info.routes.save)
        throw new Error(
          "This Loom server needs the source-save API. Update it to the plugin-compatible version.",
        );
      const [settings, stored] = await Promise.all([
        host.settings.get(),
        host.can("storage.get")
          ? host.storage.get(binding.key, binding.scope)
          : null,
      ]);
      if (!active) return;
      setCapabilities(info.capabilities);
      setApi(client);
      setBound(stored);
      setSelected(
        typeof stored?.value?.concept === "string"
          ? stored.value.concept
          : settings.startConcept || "",
      );
      await refresh(client);
    })().catch(fail);
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!api) return;
    let stopped = false,
      timer;
    const tick = async () => {
      if (document.visibilityState !== "hidden" && !action.current) {
        try {
          await refresh(api);
          const id = latest.current.selected;
          if (id && !latest.current.draft) {
            const fresh = await api.document(id);
            if (
              !stopped &&
              id === latest.current.selected &&
              !latest.current.draft
            )
              setDoc(fresh);
          }
        } catch (e) {
          if (!stopped) fail(e);
        }
      }
      if (!stopped) timer = setTimeout(tick, 5000);
    };
    timer = setTimeout(tick, 5000);
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [api, historyLimit]);

  useEffect(() => {
    const seq = ++generation.current;
    setDoc(null);
    setPreview(null);
    setError("");
    if (!api || !selected || creating) return;
    const controller = new AbortController();
    api
      .document(selected, { signal: controller.signal })
      .then((value) => {
        if (seq === generation.current && alive.current) setDoc(value);
      })
      .catch(fail);
    return () => controller.abort();
  }, [api, selected, creating]);

  function navigate(id) {
    setSelected(id);
    setCreating(false);
    setTab("Read");
    setPreview(null);
  }
  function edit() {
    if (!draft && doc)
      setDrafts((old) => ({
        ...old,
        [selected]: { source: doc.source, rev: doc.rev },
      }));
    setTab("Edit");
  }
  function changeDraft(source) {
    setDrafts((old) => ({ ...old, [selected]: { ...old[selected], source } }));
    setPreview(null);
  }
  function discardDraft() {
    setDrafts((old) => {
      const next = { ...old };
      delete next[selected];
      return next;
    });
    setTab("Read");
  }
  async function save() {
    const result = await api.save({
      id: selected,
      source: draft.source,
      expected_rev: draft.rev,
      actor,
      allow_forward_reference: forward,
    });
    if (!result.ok) throw new Error(result.error || "Save failed");
    discardDraft();
    setCreating(false);
    setDoc(await api.document(selected));
    await refresh();
    setMessage("Saved. The previous source is available in Changes → Undo.");
  }
  async function pin() {
    const entry = await host.storage.set(
      binding.key,
      { concept: selected },
      { scope: binding.scope, ...(bound ? { ifVersion: bound.version } : {}) },
    );
    setBound(entry);
    setMessage("This concept is now the starting page for this placement.");
  }
  function follow(event) {
    const a = event.target.closest("a");
    if (!a) return;
    const href = a.getAttribute("href") || "";
    if (!href || /^(?:https?:|mailto:|#)/i.test(href)) return;
    event.preventDefault();
    const url = new URL(href, `${configuration.origin}/${selected}`);
    const id = decodeURIComponent(url.pathname)
      .replace(/^\//, "")
      .replace(/\.md$/, "");
    if (catalog.some((c) => c.id === id)) navigate(id);
    else
      setError(
        `No concept named ${id} is available. The original link is preserved in Source.`,
      );
  }
  function rendered(html) {
    return h("div", {
      className: "loom-prose",
      onClick: follow,
      dangerouslySetInnerHTML: { __html: safeMarkup(html, document) },
    });
  }
  const selectedComments = comments.filter(
    (c) => c.concept === selected && (showArchived || !c.archived),
  );
  const searchItems =
    hits === null
      ? catalog
      : hits.map((result) => ({
          ...catalog.find((c) => c.id === result.id),
          ...result,
        }));
  const roots = selectedComments.filter((c) => !c.parent_id);

  return h(
    "div",
    null,
    h(
      "header",
      { className: "loom-bar" },
      h(
        "div",
        null,
        h("div", { className: "loom-brand" }, title),
        h(
          "small",
          null,
          `OKF Loom · ${host.context.extensionType.replaceAll("_", " ")}`,
        ),
      ),
      h(
        "div",
        { className: "loom-actions" },
        button("Refresh", () => run(() => refresh()), {
          disabled: busy || !api,
        }),
        button(editing ? "Editing connected" : "Connect editing", () =>
          setConnectionOpen(!connectionOpen),
        ),
        h(
          "a",
          {
            href: configuration.origin,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          "Open full studio ↗",
        ),
      ),
    ),
    notice(error, true),
    notice(message),
    connectionOpen &&
      h(
        "form",
        {
          className: "loom-notice loom-form",
          onSubmit: (e) => {
            e.preventDefault();
            run(async () => {
              const connected = connectLoom(host, configuration.origin, token);
              const probe = await connected.request("/__preview", {
                body: { markdown: "" },
              });
              if (!probe.ok)
                throw new Error("Could not verify editing connection");
              setApi(connected);
              setConnected(true);
              setConnectionOpen(false);
              setMessage("Editing connected for this open frame.");
            });
          },
        },
        h(
          "label",
          null,
          "Loom session token",
          h("input", {
            type: "password",
            value: token,
            autoComplete: "off",
            onChange: (e) => setToken(e.target.value),
            required: true,
          }),
        ),
        h(
          "small",
          null,
          "Use the token from your running Loom session. It stays in this frame’s memory and is cleared when the frame closes. Meridian relays requests to the approved Loom host.",
        ),
        h(
          "div",
          { className: "loom-actions" },
          h("button", { type: "submit", disabled: busy }, "Connect"),
          button("Disconnect", () => {
            setToken("");
            setConnected(false);
            setApi(connectLoom(host, configuration.origin));
            setConnectionOpen(false);
          }),
        ),
      ),
    h(
      "div",
      { className: "loom-grid" },
      h(
        "nav",
        { className: "loom-catalog", "aria-label": "Concepts" },
        h(
          "form",
          {
            onSubmit: (e) => {
              e.preventDefault();
              run(async () =>
                setHits(query.trim() ? await api.search(query, mode) : null),
              );
            },
          },
          h(
            "label",
            null,
            "Find a concept",
            h("input", {
              type: "search",
              value: query,
              onChange: (e) => {
                setQuery(e.target.value);
                if (!e.target.value) setHits(null);
              },
              "aria-label": "Search concepts",
            }),
          ),
          h(
            "select",
            {
              value: mode,
              onChange: (e) => setMode(e.target.value),
              "aria-label": "Search method",
            },
            (capabilities?.search_modes || ["lexical"]).map((m) =>
              h("option", { key: m, value: m }, m),
            ),
          ),
          h("button", { type: "submit", disabled: busy || !api }, "Search"),
        ),
        h("small", { role: "status" }, `${searchItems.length} concepts`),
        h(
          "ol",
          null,
          searchItems.map((c) =>
            h(
              "li",
              { key: c.id },
              button(
                h(
                  "span",
                  null,
                  c.title || c.id,
                  h(
                    "small",
                    { style: { display: "block" } },
                    `${c.type || "Concept"} · ${c.id}${drafts[c.id] ? " · draft" : ""}`,
                  ),
                ),
                () => navigate(c.id),
                { "aria-current": selected === c.id ? "page" : undefined },
              ),
            ),
          ),
        ),
        !searchItems.length &&
          h(
            "p",
            { className: "loom-muted" },
            query
              ? "No matches. Try another search method."
              : "No concepts yet.",
          ),
        button(
          "New concept",
          () => {
            setNewId("");
            setCreating(true);
            setSelected("");
          },
          { disabled: !editing || busy },
        ),
      ),
      h(
        "main",
        { className: "loom-content", "aria-label": "Knowledge document" },
        creating && !selected
          ? h(
              "form",
              {
                className: "loom-form",
                onSubmit: (e) => {
                  e.preventDefault();
                  if (
                    !/^[\w-]+(?:\/[\w-]+)*$/.test(newId) ||
                    /(?:^|\/)(?:index|log)$/i.test(newId)
                  ) {
                    setError(
                      "Use a concept ID such as research/new-idea, excluding index and log.",
                    );
                    return;
                  }
                  setSelected(newId);
                  setDrafts((old) => ({
                    ...old,
                    [newId]: {
                      source: `---\ntype: Note\ntitle: ${newId.split("/").pop()}\n---\n\n`,
                      rev: null,
                    },
                  }));
                  setTab("Edit");
                },
              },
              h("h1", null, "New concept"),
              h(
                "label",
                null,
                "Concept ID",
                h("input", {
                  value: newId,
                  onChange: (e) => setNewId(e.target.value),
                  placeholder: "research/new-idea",
                  required: true,
                }),
              ),
              h("button", { type: "submit" }, "Start writing"),
            )
          : !selected
            ? h(
                "div",
                { className: "loom-empty" },
                h("p", { className: "loom-eyebrow" }, "Connected knowledge"),
                h("h1", null, "Ideas, with their context."),
                h(
                  "p",
                  null,
                  "Choose a concept to read its evidence, follow relationships, or direct an agent through comments. Your Markdown bundle remains the source of truth.",
                ),
                h(
                  "p",
                  { className: "loom-muted" },
                  `${catalog.length} concepts · ${comments.filter((c) => c.state === "open").length} open comments`,
                ),
              )
            : !doc && !creating
              ? notice("Loading document…")
              : h(
                  "article",
                  null,
                  h(
                    "p",
                    { className: "loom-eyebrow" },
                    `${doc?.type || "New concept"} · ${selected}`,
                  ),
                  h("h1", null, doc?.title || newId),
                  doc?.frontmatter?.description &&
                    h(
                      "p",
                      { className: "loom-muted" },
                      String(doc.frontmatter.description),
                    ),
                  h(
                    "div",
                    { className: "loom-actions" },
                    !creating &&
                      button("Pin to this placement", () => run(pin), {
                        disabled: busy || !host.can("storage.set"),
                      }),
                    !creating &&
                      button(draft ? "Resume draft" : "Edit source", edit, {
                        disabled: !editing || busy,
                      }),
                    h(
                      "small",
                      null,
                      editing
                        ? "Changes are saved to Loom"
                        : "Read-only · connect a session token to edit",
                    ),
                  ),
                  h(
                    "nav",
                    { className: "loom-tabs", "aria-label": "Document views" },
                    [
                      "Read",
                      "Source",
                      "Relationships",
                      "Comments",
                      "Changes",
                      "Agent",
                      ...(draft ? ["Edit"] : []),
                    ].map((t) =>
                      button(t, () => setTab(t), {
                        key: t,
                        "aria-pressed": tab === t,
                      }),
                    ),
                  ),
                  tab === "Read" &&
                    doc &&
                    h(
                      "div",
                      null,
                      h(
                        "details",
                        null,
                        h("summary", null, "Page context & all fields"),
                        h("pre", null, json(doc.frontmatter)),
                      ),
                      rendered(doc.html),
                    ),
                  tab === "Source" &&
                    doc &&
                    h("pre", { tabIndex: 0 }, doc.source),
                  tab === "Relationships" &&
                    doc &&
                    h(Relationships, { doc, catalog, navigate }),
                  tab === "Edit" &&
                    draft &&
                    h(
                      "div",
                      null,
                      h(
                        "p",
                        { className: "loom-muted" },
                        "Edit the complete Markdown source. Unknown fields and formatting are preserved exactly. Drafts remain available while this placement stays open.",
                      ),
                      h(
                        "div",
                        { className: preview === null ? "" : "loom-split" },
                        h(
                          "label",
                          null,
                          "Markdown source",
                          h("textarea", {
                            className: "loom-source",
                            value: draft.source,
                            onChange: (e) => changeDraft(e.target.value),
                            spellCheck: false,
                          }),
                        ),
                        preview !== null && rendered(preview),
                      ),
                      h(
                        "label",
                        { className: "loom-check" },
                        h("input", {
                          type: "checkbox",
                          checked: forward,
                          onChange: (e) => setForward(e.target.checked),
                        }),
                        "Allow forward references to concepts not yet created",
                      ),
                      h(
                        "div",
                        { className: "loom-actions" },
                        button("Save source", () => run(save), {
                          className: "loom-primary",
                          disabled: busy || !editing,
                        }),
                        button(
                          "Preview",
                          () =>
                            run(async () =>
                              setPreview(
                                (
                                  await api.request("/__preview", {
                                    body: {
                                      markdown: draft.source.replace(
                                        /^---\r?\n[\s\S]*?\r?\n---\r?\n/,
                                        "",
                                      ),
                                    },
                                  })
                                ).html,
                              ),
                            ),
                          { disabled: busy || !editing },
                        ),
                        button("Discard draft", discardDraft, {
                          disabled: busy,
                        }),
                        button(
                          "Compare current source",
                          () =>
                            run(async () => {
                              const current = await api.document(selected);
                              setDoc(current);
                              setPreview(null);
                              setMessage(
                                "Current server source loaded below. Your draft still targets its original revision.",
                              );
                            }),
                          { disabled: busy || creating },
                        ),
                      ),
                      doc &&
                        h(
                          "details",
                          null,
                          h("summary", null, "Current server source"),
                          h("pre", null, doc.source),
                          doc.rev !== draft.rev &&
                            button("Use this revision as the save base", () => {
                              setDrafts((old) => ({
                                ...old,
                                [selected]: { ...old[selected], rev: doc.rev },
                              }));
                              setMessage(
                                "Save base updated. Review the draft before saving your merged version.",
                              );
                            }),
                        ),
                    ),
                  tab === "Comments" &&
                    h(
                      "section",
                      null,
                      h("h2", null, "Direct the work"),
                      h(
                        "p",
                        { className: "loom-muted" },
                        "Comments are instructions in Loom’s shared queue. A watching agent can claim, implement, and resolve them.",
                      ),
                      h(CommentComposer, {
                        key: selected,
                        editing,
                        busy,
                        submit: (body, parent, anchor, key) =>
                          run(async () => {
                            await api.comment(
                              {
                                concept: selected,
                                body,
                                actor,
                                parent_id: parent,
                                anchor: anchor ? { quote: anchor } : {},
                              },
                              { idempotencyKey: key },
                            );
                            await refresh();
                            return true;
                          }),
                      }),
                      h(
                        "label",
                        { className: "loom-check" },
                        h("input", {
                          type: "checkbox",
                          checked: showArchived,
                          onChange: (e) => setShowArchived(e.target.checked),
                        }),
                        "Include archived threads",
                      ),
                      !roots.length &&
                        h("p", null, "No comments on this concept yet."),
                      roots.map((c) =>
                        h(CommentThread, {
                          key: c.id,
                          comment: c,
                          replies: selectedComments.filter(
                            (r) => r.parent_id === c.id,
                          ),
                          editing,
                          busy,
                          actor,
                          mutate: (route, body) =>
                            run(async () => {
                              await api.request(route, { body });
                              await refresh();
                            }),
                          reply: (body, parent, _anchor, key) =>
                            run(async () => {
                              await api.comment(
                                {
                                  concept: selected,
                                  body,
                                  actor,
                                  parent_id: parent,
                                },
                                { idempotencyKey: key },
                              );
                              await refresh();
                            }),
                        }),
                      ),
                    ),
                  tab === "Changes" &&
                    h(
                      "section",
                      null,
                      h("h2", null, "Changes & undo"),
                      h(
                        "p",
                        { className: "loom-muted" },
                        "Each write is attributed. Undo restores a snapshot through the same write path and can itself be undone.",
                      ),
                      events
                        .filter(
                          (e) =>
                            e.type === "activity" && e.ids?.includes(selected),
                        )
                        .map((e) =>
                          h(
                            "div",
                            { key: e.event_id || e.id, className: "loom-card" },
                            h("strong", null, e.summary || e.action),
                            h("p", null, `${e.actor} · ${e.ts}`),
                            h(
                              "div",
                              { className: "loom-actions" },
                              e.undoable &&
                                e.detail?.before &&
                                button(
                                  "Undo",
                                  () =>
                                    run(async () => {
                                      await api.undo({
                                        concept: selected,
                                        rev: e.detail.before,
                                        actor,
                                      });
                                      await refresh();
                                      setDoc(await api.document(selected));
                                    }),
                                  { disabled: !editing || busy },
                                ),
                              e.group_id &&
                                button(
                                  "Undo group",
                                  () =>
                                    run(async () => {
                                      await api.undo({
                                        group_id: e.group_id,
                                        actor,
                                      });
                                      await refresh();
                                      setDoc(await api.document(selected));
                                    }),
                                  { disabled: !editing || busy },
                                ),
                            ),
                            h(
                              "details",
                              null,
                              h("summary", null, "Event details"),
                              h("pre", null, json(e)),
                            ),
                          ),
                        ),
                      !events.some(
                        (e) =>
                          e.type === "activity" && e.ids?.includes(selected),
                      ) &&
                        h(
                          "p",
                          null,
                          "No writes for this concept in the loaded history.",
                        ),
                      events.length >= historyLimit && historyLimit < 5000
                        ? button(
                            "Load more history",
                            () => {
                              setHistoryLimit((n) => Math.min(n * 2, 5000));
                              run(async () => {
                                setEvents(
                                  (
                                    await api.request("/__data/events", {
                                      query: {
                                        order: "desc",
                                        limit: Math.min(historyLimit * 2, 5000),
                                      },
                                    })
                                  ).events,
                                );
                              });
                            },
                            { disabled: busy },
                          )
                        : h(
                            "p",
                            { className: "loom-muted" },
                            historyLimit >= 5000
                              ? "Showing up to 5,000 recent events. Full history remains in Loom’s event log."
                              : "End of loaded history.",
                          ),
                    ),
                  tab === "Agent" &&
                    h(
                      "section",
                      null,
                      h("h2", null, "Agent activity"),
                      h(
                        "p",
                        null,
                        presence
                          ? `${presence.actor || "Agent"}: ${presence.state}${presence.message ? " · " + presence.message : ""}`
                          : "Agent presence is available in the full studio. Shared activity is shown below.",
                      ),
                      button(
                        "Enable watching",
                        () =>
                          run(async () => {
                            await api.request("/__presence", {
                              body: { actor: "agent", state: "watching" },
                            });
                            setMessage(
                              "Watching requested. Run a Loom agent against this bundle to process the queue.",
                            );
                          }),
                        { disabled: busy || !editing },
                      ),
                      button(
                        "Pause watching",
                        () =>
                          run(async () => {
                            await api.request("/__presence", {
                              body: { actor: "agent", state: "idle" },
                            });
                            setMessage("Watching paused.");
                          }),
                        { disabled: busy || !editing },
                      ),
                      events
                        .filter((e) => e.actor && e.actor !== actor)
                        .slice(0, 30)
                        .map((e) =>
                          h(
                            "div",
                            { key: e.event_id || e.id, className: "loom-card" },
                            h("strong", null, e.summary || e.type),
                            h("p", null, `${e.actor} · ${e.ts}`),
                          ),
                        ),
                    ),
                ),
      ),
    ),
  );
}

function CommentComposer({ submit, editing, busy, parent }) {
  const [body, setBody] = useState(""),
    [anchor, setAnchor] = useState("");
  const pending = useRef(null);
  return h(
    "form",
    {
      className: "loom-form",
      onSubmit: async (e) => {
        e.preventDefault();
        if (!body.trim() || busy) return;
        if (
          !pending.current ||
          pending.current.body !== body ||
          pending.current.anchor !== anchor
        )
          pending.current = { body, anchor, key: crypto.randomUUID() };
        // Keep the draft and idempotency key until the caller confirms success.
        const result = await submit(body, parent, anchor, pending.current.key);
        if (result === true) {
          setBody("");
          setAnchor("");
          pending.current = null;
        }
      },
    },
    h(
      "label",
      null,
      parent ? "Reply" : "Instruction or comment",
      h("textarea", {
        value: body,
        onChange: (e) => setBody(e.target.value),
        disabled: !editing,
        required: true,
      }),
    ),
    !parent &&
      h(
        "label",
        null,
        "Quoted passage (optional)",
        h("input", {
          value: anchor,
          onChange: (e) => setAnchor(e.target.value),
          disabled: !editing,
        }),
      ),
    h(
      "button",
      {
        type: "submit",
        className: "loom-primary",
        disabled: busy || !editing || !body.trim(),
      },
      parent ? "Post reply" : "Post comment",
    ),
  );
}

function CommentThread({
  comment: c,
  replies,
  editing,
  busy,
  actor,
  mutate,
  reply,
}) {
  const [responding, setResponding] = useState(false),
    [resolution, setResolution] = useState("");
  return h(
    "div",
    { className: "loom-card" },
    h(
      "small",
      null,
      `${c.actor} · ${c.state}${c.claimed_by ? " · " + c.claimed_by : ""}${c.archived ? " · archived" : ""}`,
    ),
    c.anchor?.quote && h("blockquote", null, c.anchor.quote),
    h("p", null, c.body),
    c.reply && h("blockquote", null, c.reply),
    h(
      "div",
      { className: "loom-actions" },
      button("Reply", () => setResponding(!responding), { disabled: !editing }),
      c.state === "open" &&
        button("Claim", () => mutate("/__claim", { id: c.id, actor }), {
          disabled: !editing || busy,
        }),
      ["resolved", "dismissed"].includes(c.state) &&
        button(
          "Reopen",
          () => mutate("/__comment-update", { id: c.id, state: "open" }),
          { disabled: !editing || busy },
        ),
      c.state === "open" &&
        button(
          "Dismiss",
          () => mutate("/__comment-update", { id: c.id, state: "dismissed" }),
          { disabled: !editing || busy },
        ),
      c.state === "resolved" &&
        button(
          c.archived ? "Unarchive" : "Archive",
          () =>
            mutate("/__comment-update", { id: c.id, archived: !c.archived }),
          { disabled: !editing || busy },
        ),
    ),
    !["resolved", "dismissed"].includes(c.state) &&
      h(
        "form",
        {
          className: "loom-form",
          onSubmit: (e) => {
            e.preventDefault();
            mutate("/__resolve", {
              id: c.id,
              reply: resolution,
              summary: resolution,
            });
          },
        },
        h(
          "label",
          null,
          "Resolution summary",
          h("input", {
            value: resolution,
            onChange: (e) => setResolution(e.target.value),
            disabled: !editing,
            required: true,
          }),
        ),
        h("button", { type: "submit", disabled: !editing || busy }, "Resolve"),
      ),
    replies.map((r) =>
      h(
        "div",
        { className: "loom-thread", key: r.id },
        h("small", null, `${r.actor} · ${r.state}`),
        h("p", null, r.body),
        r.reply && h("blockquote", null, r.reply),
        r.state !== "resolved" &&
          button("Resolve reply", () => mutate("/__resolve", { id: r.id }), {
            disabled: !editing || busy,
          }),
      ),
    ),
    responding &&
      h(CommentComposer, { submit: reply, parent: c.id, editing, busy }),
  );
}

function Relationships({ doc, catalog, navigate }) {
  const related = [...new Set([...doc.outgoing, ...doc.backlinks])];
  const nodes = [doc.id, ...related.slice(0, 24)];
  const point = (i) =>
    i === 0
      ? { x: 300, y: 230 }
      : {
          x: 300 + 210 * Math.cos(((i - 1) * 2 * Math.PI) / (nodes.length - 1)),
          y: 230 + 175 * Math.sin(((i - 1) * 2 * Math.PI) / (nodes.length - 1)),
        };
  const list = (name, ids) =>
    h(
      "section",
      null,
      h("h3", null, name),
      ids.length
        ? h(
            "ul",
            null,
            ids.map((id) =>
              h(
                "li",
                { key: id },
                button(catalog.find((c) => c.id === id)?.title || id, () =>
                  navigate(id),
                ),
              ),
            ),
          )
        : h("p", { className: "loom-muted" }, "None yet."),
    );
  return h(
    "div",
    null,
    h("h2", null, "Connected concepts"),
    h(
      "svg",
      {
        className: "loom-graph",
        viewBox: "0 0 600 460",
        role: "img",
        "aria-label": `${related.length} related concepts. Use the lists below to navigate.`,
      },
      nodes.slice(1).map((id, i) =>
        h("line", {
          key: id,
          x1: 300,
          y1: 230,
          x2: point(i + 1).x,
          y2: point(i + 1).y,
        }),
      ),
      nodes.map((id, i) =>
        h(
          "g",
          { key: id },
          h("circle", { cx: point(i).x, cy: point(i).y, r: i === 0 ? 10 : 6 }),
          h(
            "text",
            { x: point(i).x, y: point(i).y + 22, textAnchor: "middle" },
            (catalog.find((c) => c.id === id)?.title || id).slice(0, 24),
          ),
        ),
      ),
    ),
    related.length > 24 &&
      h(
        "p",
        { className: "loom-muted" },
        "Graph previews 24 neighbors; the lists include every relationship.",
      ),
    list("Links to", doc.outgoing),
    list("Cited by", doc.backlinks),
    h(
      "details",
      null,
      h("summary", null, "Typed relations, entities, citations & sources"),
      h(
        "pre",
        null,
        json({
          relations: doc.frontmatter.relations || [],
          entities: doc.frontmatter.entities || [],
          citations: doc.frontmatter.citations || [],
          sources: doc.frontmatter.sources || [],
        }),
      ),
    ),
  );
}
