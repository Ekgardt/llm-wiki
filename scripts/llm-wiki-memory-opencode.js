/**
 * LLM-Wiki Memory Plugin for OpenCode (PORTABLE version).
 *
 * This version uses $LLM_WIKI_ROOT env var instead of hardcoded paths.
 * Copy this file to ~/.config/opencode/plugins/llm-wiki-memory.js
 *
 * Events handled:
 *   session.created, tool.execute.after, session.idle,
 *   experimental.session.compacting
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

  return {
    "session.created": async (input) => {
      if (isVault()) return;
      const sid = input?.sessionInfo?.id || input?.sessionId || "opencode";
      const slug = await computeSlug(directory);
      await heartbeat(slug, String(directory||""), "opencode-start", String(sid));
      await drainQueue();
      await triggerCompile();
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
        if (output?.context) output.context.push(`Memory: precompact capture attempted. Run compile after session if needed.`);
      } catch {}
    },
  };
};
