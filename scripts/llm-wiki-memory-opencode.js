/**
 * LLM-Wiki Memory Plugin for OpenCode (PORTABLE version).
 *
 * This version uses $LLM_WIKI_ROOT env var instead of hardcoded paths.
 * Copy this file to ~/.config/opencode/plugins/llm-wiki-memory.js
 *
 * Features:
 *   - Event handlers: session.created, tool.execute.after, session.idle,
 *     experimental.session.compacting
 *   - Custom tools: memory.context (session-start knowledge),
 *     memory.recall (real-time search)
 *   - Context file generation at session.created (for fallback / non-tool agents)
 */

const _LLM_WIKI_ROOT = process.env.LLM_WIKI_ROOT;
if (!_LLM_WIKI_ROOT) {
  console.warn("[llm-wiki-memory] LLM_WIKI_ROOT is not set — memory capture will be disabled. Set it to your vault path.");
}
const SCRIPTS = `${_LLM_WIKI_ROOT || ""}/scripts`;
const SIGNIFICANT_TOOLS = new Set(["edit", "write", "multi_edit", "notebook_edit", "bash"]);

export const LlmWikiMemoryPlugin = async ({ client, $, directory }) => {
  const isVault = () => directory?.replace(/\\/g, "/") === (process.env.LLM_WIKI_ROOT || "").replace(/\\/g, "/");

  async function drainQueue() {
    try { await $`uv run python ${SCRIPTS}/memory_queue.py drain`.quiet().nothrow(); } catch {}
  }
  async function triggerCompile() {
    try { await $`uv run python ${SCRIPTS}/maybe_compile.py`.quiet().nothrow(); } catch {}
  }
  async function warmStartVectorSearch() {
    try { await $`uv run python ${SCRIPTS}/search_memory.py "warmup" --semantic --limit 1`.quiet().nothrow(); } catch {}
  }
  async function appendDaily(slug, sessionId, block) {
    try {
      const p = JSON.stringify({ slug, sessionId, block });
      await $`uv run python ${SCRIPTS}/daily_log_append.py`.stdin(p).quiet().nothrow();
    } catch {}
  }
  async function appendTool(slug, sid, tool, target) {
    try {
      const p = JSON.stringify({ slug, sessionId: sid, tool, target: (target||"").slice(0,100) });
      await $`uv run python ${SCRIPTS}/tool_breadcrumb_append.py`.stdin(p).quiet().nothrow();
    } catch {}
  }
  async function heartbeat(slug, dir, reason, sid) {
    try {
      const p = JSON.stringify({ slug, projectRoot: String(dir||""), reason, sessionId: String(sid) });
      await $`uv run python ${SCRIPTS}/heartbeat_record.py`.stdin(p).quiet().nothrow();
    } catch {}
  }
  async function computeSlug(cwd) {
    try {
      const r = await $`uv run python ${SCRIPTS}/codex_memory.py state-path --cwd ${cwd} --json`.quiet().nothrow();
      const m = (r.stdout?.toString()||"").match(/"slug"\s*:\s*"([^"]+)"/);
      return m ? m[1] : (cwd||"").replace(/\\/g,"/").split("/").pop()?.toLowerCase() || "unknown";
    } catch { return "unknown"; }
  }

  /**
   * Generate session-start context and write to cache/session-context.md.
   * This file is the fallback for agents that don't support custom tools
   * (Cursor, Antigravity) and the source for opencode.json instructions.
   */
  async function generateContextFile() {
    try {
      const ctxFile = `${_LLM_WIKI_ROOT}/cache/session-context.md`;
      await $`uv run python ${SCRIPTS}/session_start_context.py --output-file ${ctxFile}`.quiet().nothrow();
    } catch {}
  }

  return {
    // ─── Event Handlers ─────────────────────────────────────────────

    "session.created": async (input) => {
      if (isVault()) return;
      const sid = input?.sessionInfo?.id || input?.sessionId || "opencode";
      const slug = await computeSlug(directory);
      await heartbeat(slug, String(directory||""), "opencode-start", String(sid));
      await drainQueue();
      await triggerCompile();
      await generateContextFile();
      warmStartVectorSearch(); // fire-and-forget: preload model in background
    },

    "tool.execute.after": async (input) => {
      if (isVault()) return;
      const tool = String(input?.tool||"").toLowerCase();
      if (!SIGNIFICANT_TOOLS.has(tool)) return;
      const slug = await computeSlug(directory);
      const sid = String(input?.sessionInfo?.id || input?.sessionId || "opencode").slice(0,8);
      const target = String(input?.input?.filePath||input?.input?.command||"");
      await appendTool(slug, sid, tool, target);
    },

    "session.idle": async (input) => {
      if (isVault()) return;
      const sid = input?.sessionInfo?.id || input?.sessionId || "opencode";
      const slug = await computeSlug(directory);
      let transcript = "";
      try {
        const msgs = await client.session.messages({ path: { id: sid } });
        transcript = (msgs?.data||[]).slice(-12).map(m => (m.parts||[]).map(p=>p.text||"").join("")).join("\n\n").slice(-8000);
      } catch {}
      if (transcript.length < 50) { await heartbeat(slug, String(directory||""), "idle-short", String(sid)); return; }
      const prompt = `Classify this transcript:\n\n${transcript}\n\nRespond with FLUSH_MAJOR, FLUSH_MINOR, or FLUSH_OK as first line.`;
      let tier = "ok", body = "";
      try {
        let sessId2 = null;
        const sess = await client.session.create({ body: { title: "memory-ephemeral" } });
        sessId2 = sess?.data?.id || sess?.id;
        if (sessId2) {
          const result = await client.session.prompt({ path: { id: sessId2 }, body: { parts: [{ type: "text", text: prompt }] } });
          const parts = result?.data?.parts || [];
          const text = parts.map(p=>p.text||"").join("").trim();
          const first = (text.split(/\r?\n/)[0]||"").toUpperCase().replace(/[.`*]/g,"");
          if (first === "FLUSH_MAJOR") tier = "major";
          else if (first === "FLUSH_MINOR") tier = "minor";
          body = text.split(/\r?\n/).slice(1).join("\n").trim();
          try { await client.session.delete({ path: { id: sessId2 } }); } catch {}
        }
      } catch {}
      if (tier === "ok") { await heartbeat(slug, String(directory||""), "flush-ok", String(sid)); return; }
      // Match Python split_session_blocks / Evidence format: ## [HH:MM:SS] ...
      const ts = new Date().toTimeString().slice(0, 8);
      await appendDaily(slug, String(sid), `## [${ts}] opencode-idle | ${sid}\n- Tier: \`${tier}\`\n\n${body||"(no body)"}\n`);
      if ((tier === "major" || tier === "minor") && body) {
        try {
          const fp = JSON.stringify({ text: body, session_id: String(sid), slug, trigger: "opencode-idle" });
          await $`uv run python ${SCRIPTS}/feedback_capture.py`.stdin(fp).quiet().nothrow();
        } catch {}
      }
      if (tier === "major") { await triggerCompile(); }
    },

    "experimental.session.compacting": async (input, output) => {
      if (isVault()) return;
      try {
        const sid = String(input?.sessionID || input?.sessionId || "unknown");
        // Best-effort flush before context loss (parity with Claude PreCompact).
        try {
          await $`uv run python ${SCRIPTS}/precompact_capture.py`.stdin(JSON.stringify({
            session_id: sid,
            transcript_path: "",
            reason: "opencode-compacting",
          })).quiet().nothrow();
        } catch {}
        // Inject knowledge context into the compacted session so it survives.
        if (output?.context) {
          output.context.push(`Memory: precompact capture attempted. Knowledge context is available via the memory.context and memory.recall tools.`);
        }
      } catch {}
    },

    // ─── Custom Tools (agent-native, real-time, zero infrastructure) ─
    //
    // These tools appear in the agent's tool-list. The agent calls them
    // when it needs knowledge context or search results. No file I/O on
    // the agent side — the tools call Python scripts via shell.
    //
    // Security: all calls are local (shell → Python script → local files).
    // No network, no server process, no external dependencies.

    tool: {
      "memory.context": {
        description:
          "Get session-start knowledge context: vault inventory, knowledge " +
          "index, guardrails (learned corrections), advisory (open threads, " +
          "last decision), and the latest daily-log excerpt. Call this at " +
          "the start of every session to understand the project knowledge state.",
        args: {},
        async execute() {
          try {
            const r = await $`uv run python ${SCRIPTS}/session_start_context.py`.quiet().nothrow();
            const stdout = r.stdout?.toString()?.trim();
            if (stdout && stdout.startsWith("{")) {
              // Claude Code hook JSON format — extract additionalContext
              try {
                const parsed = JSON.parse(stdout);
                const ctx = parsed?.hookSpecificOutput?.additionalContext;
                if (ctx) return ctx;
              } catch {}
            }
            return stdout || "(no context available — run compile first)";
          } catch {
            return "(memory.context: error generating context)";
          }
        },
      },

      "memory.recall": {
        description:
          "Search the knowledge base for relevant pages. Returns ranked " +
          "results with titles, paths, and summaries. Use this when you " +
          "need to find decisions, patterns, debugging notes, or Q&A pages " +
          "related to the current task.",
        args: {
          query: {
            type: "string",
            description: "Natural-language search query (e.g. 'auth middleware decision')",
          },
        },
        async execute(args) {
          const query = String(args?.query || "").trim();
          if (!query) return "Usage: memory.recall(query='your search query')";
          try {
            const r = await $`uv run python ${SCRIPTS}/search_memory.py ${query}`.quiet().nothrow();
            return r.stdout?.toString()?.trim() || "(no results found)";
          } catch {
            return "(memory.recall: search error)";
          }
        },
      },
    },
  };
};
