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

export const LlmWikiMemoryPlugin = async ({ client, directory }) => {
  const sessionContexts = new Map();
  const dirtySessions = new Set();
  const comparablePath = (value) => {
    if (typeof value !== "string" || !value) return null;
    const resolved = path.resolve(value);
    return process.platform === "win32" ? resolved.toLowerCase() : resolved;
  };

  const isVault = () => {
    const dir = comparablePath(directory);
    const root = comparablePath(_LLM_WIKI_ROOT);
    return Boolean(dir && root) && (dir === root || dir.startsWith(`${root}${path.sep}`));
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
    const sessionId = input?.sessionInfo?.id || input?.sessionId || input?.sessionID;
    if (typeof sessionId !== "string" || !client?.session?.messages) return "";
    const response = await settleWithin(
      client.session.messages({ path: { id: sessionId }, query: { limit: MAX_MESSAGES } }),
      { data: [] },
    );
    const messages = Array.isArray(response?.data) ? response.data.slice(-MAX_MESSAGES) : [];
    return messages
      .flatMap((message) => Array.isArray(message?.parts) ? message.parts : [])
      .map((part) => typeof part?.text === "string" ? part.text : "")
      .filter(Boolean)
      .join("\n\n")
      .slice(-MAX_TRANSCRIPT_CHARS);
  }

  async function runCapture(args, payload) {
    if (!globalThis.Bun?.spawn) return null;
    const proc = globalThis.Bun.spawn(args, {
      stdin: "pipe",
      stdout: "pipe",
      stderr: "ignore",
    });
    const stdout = proc.stdout
      ? new Response(proc.stdout).text().catch(() => "")
      : Promise.resolve("");
    proc.stdin.write(payload);
    proc.stdin.end();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      try { proc.kill(); } catch {}
    }, CAPTURE_TIMEOUT_MS);
    try {
      await proc.exited;
      return timedOut ? null : await stdout;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  async function forwardLifecycle(event, input) {
    if (!_LLM_WIKI_ROOT || isVault()) return null;
    try {
      const payload = JSON.stringify({
        ...(input || {}),
        directory: typeof directory === "string" ? directory : null,
      });
      const stdout = await runCapture(
        [
          "uv",
          "run",
          "--locked",
          "--no-sync",
          "--directory",
          _LLM_WIKI_ROOT,
          "python",
          `${SCRIPTS}/integration_adapter.py`,
          "--source",
          "opencode",
          "--event",
          event,
        ],
        payload,
      );
      if (!stdout) return null;
      const parsed = JSON.parse(stdout);
      return parsed && typeof parsed === "object" ? parsed : null;
    } catch {
      return null;
    }
  }

  function sessionId(input) {
    const value = input?.sessionInfo?.id || input?.sessionID || input?.sessionId;
    return typeof value === "string" && value ? value : null;
  }

  async function handleSessionCreated(input) {
    const result = await forwardLifecycle("session_start", input);
    const id = sessionId(input);
    if (id && typeof result?.context === "string" && result.context) {
      sessionContexts.set(id, result.context);
      if (sessionContexts.size > 32) sessionContexts.delete(sessionContexts.keys().next().value);
    }
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

  return {
    event: async (input) => {
      const hostEvent = input?.event;
      const properties = hostEvent?.properties && typeof hostEvent.properties === "object"
        ? { ...hostEvent.properties }
        : {};
      if (typeof hostEvent?.id === "string" && hostEvent.id) {
        properties.source_event_id = hostEvent.id;
      }
      if (hostEvent?.type === "session.created") {
        await handleSessionCreated(properties);
      } else if (hostEvent?.type === "session.idle") {
        await handleSessionIdle(properties);
      }
    },

    "chat.message": async (input, output) => {
      const role = output?.message?.role;
      if (role !== undefined && role !== "user") return;
      const prompt = (Array.isArray(output?.parts) ? output.parts : [])
        .filter((part) => part?.type === "text")
        .map((part) => typeof part?.text === "string" ? part.text.trim() : "")
        .filter(Boolean)
        .join("\n");
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

    "tool.execute.after": async (input) => {
      const id = sessionId(input);
      const tool = typeof input?.tool === "string" ? input.tool.toLowerCase() : "";
      const changed = ["edit", "write", "multi_edit", "multiedit", "notebook_edit", "notebookedit"].includes(tool);
      if (id && changed) dirtySessions.add(id);
      await forwardLifecycle("post_tool_use", {
        ...(input || {}),
        ...(changed ? { changed: true, dirty: true, significant: true } : {}),
      });
    },

    "experimental.session.compacting": async (input) => {
      const transcriptText = await collectTranscript(input);
      await forwardLifecycle("pre_compact", { ...(input || {}), transcript_text: transcriptText });
    },
  };
};
