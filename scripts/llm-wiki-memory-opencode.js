/** Thin OpenCode lifecycle adapter for the shared LLM-Wiki Python pipeline. */

import path from "node:path";

// The installer rewrites the marked line with the vault root it installed. A
// checkout keeps the null, so the public source never claims to be a vault.
// An OpenCode started from a desktop launcher inherits no shell environment,
// and without this fallback its capture was silently disabled.
const _EMBEDDED_ROOT = null; // llm-wiki:embedded-root
const _LLM_WIKI_ROOT = process.env.LLM_WIKI_ROOT || _EMBEDDED_ROOT;
if (!_LLM_WIKI_ROOT) {
  console.warn("[llm-wiki-memory] LLM_WIKI_ROOT is not set; lifecycle capture is disabled.");
}
const SCRIPTS = `${_LLM_WIKI_ROOT || ""}/scripts`;
const MAX_MESSAGES = 12;
const MAX_TRANSCRIPT_CHARS = 8000;
const configuredTimeout = Number(process.env.LLM_WIKI_CAPTURE_TIMEOUT_MS || 5000);
const CAPTURE_TIMEOUT_MS = Math.min(Math.max(configuredTimeout || 5000, 10), 10000);
const CHANGING_TOOLS = new Set(["edit", "write", "multi_edit", "multiedit", "notebook_edit", "notebookedit"]);
// Issue #24, C2: after a grep/glob whose pattern names a code symbol, the
// graph's definitions are appended to the tool output. OpenCode documents that
// this mutation does not reach its UI (anomalyco/opencode#13574); whether it
// reaches the model is not documented, so the hint is best effort.
const HINTED_TOOLS = new Set(["grep", "glob"]);

const isText = (value) => typeof value === "string" && value.length > 0;
const textOr = (value, fallback) => (isText(value) ? value : fallback);
const asList = (value) => (Array.isArray(value) ? value : []);

function asObject(value) {
  if (value && typeof value === "object") return value;
  return null;
}

function sessionId(input) {
  const candidates = [input?.sessionInfo?.id, input?.sessionID, input?.sessionId];
  return candidates.find(isText) || null;
}

function toolName(input) {
  return textOr(input?.tool, "").toLowerCase();
}

function partsText(message) {
  return asList(message?.parts).map((part) => textOr(part?.text, ""));
}

function parsedObject(stdout) {
  try {
    return asObject(stdout ? JSON.parse(stdout) : null);
  } catch {
    return null;
  }
}

function hostProperties(hostEvent) {
  const properties = { ...(asObject(hostEvent?.properties) || {}) };
  if (isText(hostEvent?.id)) properties.source_event_id = hostEvent.id;
  return properties;
}

function userPrompt(output) {
  return asList(output?.parts)
    .filter((part) => part?.type === "text")
    .map((part) => textOr(part?.text, "").trim())
    .filter(Boolean)
    .join("\n");
}

function isUserMessage(output) {
  const role = output?.message?.role;
  return role === undefined || role === "user";
}

export const LlmWikiMemoryPlugin = async ({ client, directory }) => {
  const sessionContexts = new Map();
  const dirtySessions = new Set();
  const workingDirectory = () => (typeof directory === "string" ? directory : null);
  const comparablePath = (value) => {
    if (!isText(value)) return null;
    const resolved = path.resolve(value);
    return process.platform === "win32" ? resolved.toLowerCase() : resolved;
  };

  const isVault = () => {
    const dir = comparablePath(directory);
    const root = comparablePath(_LLM_WIKI_ROOT);
    if (!dir || !root) return false;
    return dir === root || dir.startsWith(`${root}${path.sep}`);
  };

  async function settleWithin(promise, fallback = null) {
    let timer;
    try {
      return await Promise.race([
        Promise.resolve(promise).catch(() => fallback),
        new Promise((resolve) => {
          timer = setTimeout(() => resolve(fallback), CAPTURE_TIMEOUT_MS);
        }),
      ]);
    } finally {
      clearTimeout(timer);
    }
  }

  async function collectTranscript(input) {
    const id = sessionId(input);
    if (!id || !client?.session?.messages) return "";
    const response = await settleWithin(
      client.session.messages({ path: { id }, query: { limit: MAX_MESSAGES } }),
      { data: [] },
    );
    return asList(response?.data)
      .slice(-MAX_MESSAGES)
      .flatMap(partsText)
      .filter(Boolean)
      .join("\n\n")
      .slice(-MAX_TRANSCRIPT_CHARS);
  }

  function stdoutText(proc) {
    if (!proc.stdout) return Promise.resolve("");
    return new Response(proc.stdout).text().catch(() => "");
  }

  async function awaitExit(proc) {
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try { proc.kill(); } catch {}
    }, CAPTURE_TIMEOUT_MS);
    try {
      await proc.exited;
      return !timedOut;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  async function runCapture(args, payload) {
    if (!globalThis.Bun?.spawn) return null;
    const proc = globalThis.Bun.spawn(args, {
      stdin: "pipe",
      stdout: "pipe",
      stderr: "ignore",
    });
    const stdout = stdoutText(proc);
    proc.stdin.write(payload);
    proc.stdin.end();
    const exited = await awaitExit(proc);
    return exited ? await stdout : null;
  }

  function lifecyclePayload(input) {
    try {
      return JSON.stringify({
        ...(input || {}),
        directory: typeof directory === "string" ? directory : null,
      });
    } catch {
      return null;
    }
  }

  async function forwardLifecycle(event, input) {
    const payload = lifecyclePayload(input);
    if (!_LLM_WIKI_ROOT || isVault() || payload === null) return null;
    const stdout = await runCapture(
      ["uv", "run", "--locked", "--no-sync", "--directory", _LLM_WIKI_ROOT, "python",
        `${SCRIPTS}/integration_adapter.py`, "--source", "opencode", "--event", event],
      payload,
    ).catch(() => null);
    return parsedObject(stdout);
  }

  function hintPayload(tool, input) {
    return JSON.stringify({ tool, args: asObject(input?.args) || {}, directory: workingDirectory() });
  }

  async function graphHint(input) {
    const tool = toolName(input);
    if (!_LLM_WIKI_ROOT || !HINTED_TOOLS.has(tool)) return null;
    const payload = hintPayload(tool, input);
    const stdout = await runCapture(
      ["uv", "run", "--locked", "--no-sync", "--directory", _LLM_WIKI_ROOT, "python",
        `${SCRIPTS}/graph_hint.py`, "--source", "opencode"],
      payload,
    ).catch(() => null);
    return textOr(parsedObject(stdout)?.context, null);
  }

  async function appendGraphHint(input, output) {
    const hint = await graphHint(input);
    if (!hint || typeof output?.output !== "string") return;
    output.output = `${output.output}\n\n${hint}`;
  }

  function rememberContext(id, context) {
    if (!id || !isText(context)) return;
    sessionContexts.set(id, context);
    if (sessionContexts.size > 32) sessionContexts.delete(sessionContexts.keys().next().value);
  }

  async function handleSessionCreated(input) {
    const result = await forwardLifecycle("session_start", input);
    rememberContext(sessionId(input), result?.context);
  }

  async function handleSessionIdle(input) {
    const id = sessionId(input);
    const transcriptText = await collectTranscript(input);
    await forwardLifecycle("session_end", {
      ...(input || {}),
      checkpoint_type: "session_idle",
      dirty: Boolean(id && dirtySessions.has(id)),
      transcript_text: transcriptText,
    });
    if (id) dirtySessions.delete(id);
  }

  const hostEventHandlers = {
    "session.created": handleSessionCreated,
    "session.idle": handleSessionIdle,
  };

  async function recordToolUse(input) {
    const id = sessionId(input);
    const changed = CHANGING_TOOLS.has(toolName(input));
    if (id && changed) dirtySessions.add(id);
    const signals = changed ? { changed: true, dirty: true, significant: true } : {};
    await forwardLifecycle("post_tool_use", { ...(input || {}), ...signals });
  }

  return {
    event: async (input) => {
      const hostEvent = input?.event;
      const handler = hostEventHandlers[hostEvent?.type];
      if (handler) await handler(hostProperties(hostEvent));
    },

    "chat.message": async (input, output) => {
      if (!isUserMessage(output)) return;
      const prompt = userPrompt(output);
      if (!prompt) return;
      await forwardLifecycle("user_prompt", {
        ...(input || {}),
        event_id: output?.message?.id,
        prompt,
      });
    },

    "experimental.chat.system.transform": async (input, output) => {
      const context = sessionContexts.get(sessionId(input));
      if (context && Array.isArray(output?.system)) output.system.push(context);
    },

    "tool.execute.after": async (input, output) => {
      await recordToolUse(input);
      await appendGraphHint(input, output);
    },

    "experimental.session.compacting": async (input) => {
      const transcriptText = await collectTranscript(input);
      await forwardLifecycle("pre_compact", { ...(input || {}), transcript_text: transcriptText });
    },
  };
};
