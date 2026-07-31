/**
 * LLM-Wiki Memory Plugin for OpenCode (PORTABLE version).
 *
 * This version uses $LLM_WIKI_ROOT env var instead of hardcoded paths.
 * Copy this file to ~/.config/opencode/plugins/llm-wiki-memory.js
 *
 * Features:
 *   - Event handlers: session.created, chat.message, tool.execute.after, session.idle,
 *     experimental.session.compacting
 *   - Custom tools: memory_context (session-start knowledge),
 *     memory_recall (real-time search)
 *   - Context file generation at session.created (for fallback / non-tool agents)
 */

import { spawn } from "node:child_process";
import path from "node:path";

const _LLM_WIKI_ROOT = process.env.LLM_WIKI_ROOT;
if (!_LLM_WIKI_ROOT) {
  console.warn("[llm-wiki-memory] LLM_WIKI_ROOT is not set — memory capture will be disabled. Set it to your vault path.");
}
const _LLM_WIKI_STATE_ROOT = process.env.LLM_WIKI_STATE_ROOT || _LLM_WIKI_ROOT || "";
const SCRIPTS = `${_LLM_WIKI_ROOT || ""}/scripts`;
const PYTHON = process.platform === "win32"
  ? `${_LLM_WIKI_ROOT || ""}/.venv/Scripts/python.exe`
  : `${_LLM_WIKI_ROOT || ""}/.venv/bin/python`;
const SIGNIFICANT_TOOLS = new Map([
  ["edit", "Edit"],
  ["write", "Write"],
  ["multi_edit", "MultiEdit"],
  ["notebook_edit", "NotebookEdit"],
  ["apply_patch", "ApplyPatch"],
]);
const MAX_TRANSCRIPT_CHARS = 8000;
const MAX_TRANSCRIPT_MESSAGES = 12;
const MAX_CHILD_STDIN_BYTES = 8 * 1024 * 1024;
const MAX_CHILD_STDOUT_BYTES = 8 * 1024 * 1024;
const MAX_CHILD_STDERR_BYTES = 256 * 1024;
const PYTHON_TIMEOUT_MS = 30_000;
const MAX_QUEUE_TASKS = 5;
const MAX_COMPILE_BATCHES = 10;
const LEASE_RENEWAL_INTERVAL_MS = 60_000;
const SERVICE_MODEL = { providerID: "openai", modelID: "gpt-5.6-luna" };
const OUTCOME_COMPLETE = "complete";
const OUTCOME_FAILED = "failed";
const OUTCOME_CAPPED = "capped";
const DAILY_RECORD_COMPLETION_MARKER = "<!-- llm-wiki-record-complete -->";
const DAILY_APPEND_STATUS = "appended";

function neutralizeDailyRecordHeaders(body) {
  return String(body || "").split(/\r?\n/).map((line) => {
    const normalized = line.replace(/<\/?(?:analysis|summary)>/gi, "");
    let marker = "";
    if (/^##\s+\[/.test(normalized)) marker = "#";
    else if (/^\s*-\s*`\[/.test(normalized)) marker = "-";
    else if (normalized === DAILY_RECORD_COMPLETION_MARKER) marker = "<";
    const position = marker ? line.indexOf(marker) : -1;
    return position >= 0
      ? `${line.slice(0, position)}\\${line.slice(position)}`
      : line;
  }).join("\n");
}

export const LlmWikiMemoryPlugin = async ({ client, directory, worktree, runtime }) => {
  const projectDirectory = String(worktree || directory || "");
  let sdkCompileRunning = false;
  let maintenanceRunning = false;
  let maintenanceRequested = false;
  let maintenanceContinuationScheduled = false;
  const internalSessionIds = new Set();
  const schedule = runtime?.schedule || ((callback, delay) => setTimeout(callback, delay));
  const scheduleTimeout = runtime?.setTimeout || ((callback, delay) => setTimeout(callback, delay));
  const cancelTimeout = runtime?.clearTimeout || ((handle) => clearTimeout(handle));
  const scheduleInterval = runtime?.setInterval || ((callback, delay) => setInterval(callback, delay));
  const cancelInterval = runtime?.clearInterval || ((handle) => clearInterval(handle));
  const configuredRenewalInterval = Number(runtime?.leaseRenewalIntervalMs);
  const leaseRenewalInterval = Number.isFinite(configuredRenewalInterval) && configuredRenewalInterval > 0
    ? configuredRenewalInterval
    : LEASE_RENEWAL_INTERVAL_MS;
  const configuredPythonTimeout = Number(runtime?.pythonTimeoutMs);
  const pythonTimeoutMs = Number.isFinite(configuredPythonTimeout) && configuredPythonTimeout > 0
    ? configuredPythonTimeout
    : PYTHON_TIMEOUT_MS;
  const isInsideVault = (candidate) => {
    if (!candidate || !process.env.LLM_WIKI_ROOT) return false;
    const normalizeCase = (value) => process.platform === "win32" ? value.toLowerCase() : value;
    const resolved = normalizeCase(path.resolve(String(candidate)));
    const root = normalizeCase(path.resolve(process.env.LLM_WIKI_ROOT));
    const rootPrefix = root.endsWith(path.sep) ? root : `${root}${path.sep}`;
    return resolved === root || resolved.startsWith(rootPrefix);
  };
  const isVault = () => isInsideVault(projectDirectory);

  function defaultRunPython(script, args = [], stdin = "") {
    return new Promise((resolve, reject) => {
      const input = Buffer.isBuffer(stdin) ? stdin : Buffer.from(String(stdin ?? ""));
      if (input.length > MAX_CHILD_STDIN_BYTES) {
        reject(new Error(`${script} stdin exceeded ${MAX_CHILD_STDIN_BYTES} byte limit`));
        return;
      }
      const stdout = [];
      const stderr = [];
      let stdoutBytes = 0;
      let stderrBytes = 0;
      let settled = false;
      let timeoutHandle = null;
      const settle = (handler, value) => {
        if (settled) return;
        settled = true;
        if (timeoutHandle !== null) {
          const handle = timeoutHandle;
          timeoutHandle = null;
          try { cancelTimeout(handle); } catch {}
        }
        handler(value);
      };
      const fail = (error) => settle(reject, error);
      let child;
      try {
        child = spawn(PYTHON, [`${SCRIPTS}/${script}`, ...args], {
          cwd: _LLM_WIKI_ROOT,
          env: process.env,
          stdio: ["pipe", "pipe", "pipe"],
          windowsHide: true,
        });
      } catch (error) {
        fail(error);
        return;
      }
      timeoutHandle = scheduleTimeout(() => {
        if (settled) return;
        try { child.kill(); } catch {}
        for (const handle of child.stdio) {
          try { handle?.destroy(); } catch {}
        }
        fail(new Error(`${script} timed out after ${pythonTimeoutMs} ms`));
      }, pythonTimeoutMs);
      const rejectOverflow = (stream, limit) => {
        try { child.kill(); } catch {}
        for (const handle of child.stdio) {
          try { handle?.destroy(); } catch {}
        }
        fail(new Error(`${script} ${stream} exceeded ${limit} byte limit`));
      };
      child.stdout.on("data", (chunk) => {
        if (settled) return;
        if (stdoutBytes + chunk.length > MAX_CHILD_STDOUT_BYTES) {
          rejectOverflow("stdout", MAX_CHILD_STDOUT_BYTES);
          return;
        }
        stdoutBytes += chunk.length;
        stdout.push(chunk);
      });
      child.stderr.on("data", (chunk) => {
        if (settled) return;
        if (stderrBytes + chunk.length > MAX_CHILD_STDERR_BYTES) {
          rejectOverflow("stderr", MAX_CHILD_STDERR_BYTES);
          return;
        }
        stderrBytes += chunk.length;
        stderr.push(chunk);
      });
      child.on("error", fail);
      child.on("close", (code) => {
        if (settled) return;
        if (code === 0) {
          settle(resolve, Buffer.concat(stdout, stdoutBytes).toString());
          return;
        }
        const error = Buffer.concat(stderr, stderrBytes).toString();
        fail(new Error(error || `${script} exited ${code}`));
      });
      child.stdin.on("error", (error) => {
        if (error?.code !== "EPIPE") fail(error);
      });
      child.stdin.end(input);
    });
  }
  const runPython = runtime?.runPython || defaultRunPython;
  const contextArgs = () => projectDirectory ? ["--directory", projectDirectory] : [];

  function extractContext(stdout) {
    const text = String(stdout || "").trim();
    if (!text.startsWith("{")) return text;
    try {
      return JSON.parse(text)?.hookSpecificOutput?.additionalContext || text;
    } catch {
      return text;
    }
  }

  async function logError(message, error) {
    try {
      await client?.app?.log?.({
        body: {
          service: "llm-wiki-memory",
          level: "error",
          message,
          extra: { error: String(error?.message || error || "unknown") },
        },
      });
    } catch {}
  }

  function hasMemoryTitle(title) {
    return String(title || "").startsWith("memory-");
  }

  async function isInternalSession(sessionId, knownTitle) {
    if (hasMemoryTitle(knownTitle) || internalSessionIds.has(sessionId)) return true;
    if (knownTitle !== undefined || !sessionId) return false;
    try {
      const session = await client.session.get({ path: { id: sessionId } });
      return hasMemoryTitle(session?.data?.title || session?.title);
    } catch {
      return false;
    }
  }

  async function cleanupInternalSession(sessionId, operation) {
    if (!sessionId) return;
    try {
      const response = await client.session.abort({ path: { id: sessionId } });
      if (response?.error) throw new Error(`OpenCode abort error: ${JSON.stringify(response.error)}`);
    } catch (error) {
      await logError(`Failed to abort ${operation} session`, error);
    }
    try {
      const response = await client.session.delete({ path: { id: sessionId } });
      if (response?.error) throw new Error(`OpenCode delete error: ${JSON.stringify(response.error)}`);
    } catch (error) {
      await logError(`Failed to delete ${operation} session`, error);
    } finally {
      internalSessionIds.delete(sessionId);
    }
  }

  function providerError(response) {
    return response?.error || response?.data?.info?.error || response?.info?.error;
  }

  async function renewQueueLease(task) {
    const output = await runPython(
      "memory_queue.py",
      ["--renew-sdk-task"],
      JSON.stringify({
        task_id: task.task_id,
        lease_id: task.lease_id,
        digest: task.digest,
      }),
    );
    let renewed;
    try {
      renewed = JSON.parse(output);
    } catch {
      throw new Error("SDK queue lease renewal returned invalid JSON");
    }
    if (renewed?.ok !== true || renewed?.status !== "renewed") {
      throw new Error(`SDK queue lease renewal rejected: ${renewed?.status || "unknown error"}`);
    }
  }

  async function withQueueLeaseRenewal(task, operation, onResolved) {
    let timer = null;
    let renewalActive = true;
    let operationError = null;
    let transientRenewalError = null;
    let finalRenewalError = null;
    let timerCleanupError = null;
    let value;
    let renewalChain = Promise.resolve();
    const renewPeriodically = () => {
      if (!renewalActive) return;
      renewalChain = renewalChain
        .then(() => renewQueueLease(task))
        .catch((error) => { transientRenewalError ||= error; });
    };

    await renewQueueLease(task);
    try {
      timer = scheduleInterval(renewPeriodically, leaseRenewalInterval);
      value = await operation();
      if (onResolved) onResolved(value);
    } catch (error) {
      operationError = error;
    } finally {
      renewalActive = false;
      if (timer !== null) {
        try {
          cancelInterval(timer);
        } catch (error) {
          timerCleanupError ||= error;
        }
      }
      await renewalChain;
      try {
        await renewQueueLease(task);
      } catch (error) {
        finalRenewalError = error;
      }
    }

    if (transientRenewalError) {
      await logError("Transient SDK queue lease renewal failed", transientRenewalError);
    }
    const renewalError = finalRenewalError || timerCleanupError;
    if (operationError) {
      if (renewalError) {
        await logError("SDK queue lease renewal also failed", renewalError);
      }
      throw operationError;
    }
    if (renewalError) throw renewalError;
    return value;
  }

  async function triggerCompile() {
    try { await runPython("maybe_compile.py"); } catch {}
  }
  async function warmStartVectorSearch() {
    try { await runPython("search_memory.py", ["warmup", "--semantic", "--limit", "1"]); } catch {}
  }
  async function appendDaily(slug, projectRoot, sessionId, block) {
    const p = JSON.stringify({ slug, projectRoot, sessionId, block });
    const output = await runPython("daily_log_append.py", [], p);
    let acknowledgement;
    try {
      acknowledgement = JSON.parse(String(output || ""));
    } catch {
      throw new Error("daily_log_append.py returned an invalid acknowledgement");
    }
    if (acknowledgement?.ok !== true || acknowledgement?.status !== DAILY_APPEND_STATUS) {
      throw new Error("daily_log_append.py did not acknowledge an append");
    }
  }
  async function heartbeat(slug, dir, reason, sid) {
    try {
      const p = JSON.stringify({ slug, projectRoot: String(dir||""), reason, sessionId: String(sid) });
      await runPython("heartbeat_record.py", [], p);
    } catch {}
  }
  async function computeSlug(cwd) {
    try {
      const output = await runPython("codex_memory.py", ["state-path", "--cwd", String(cwd || ""), "--json"]);
      const m = output.match(/"slug"\s*:\s*"([^"]+)"/);
      return m ? m[1] : null;
    } catch { return null; }
  }

  /**
   * Generate session-start context and write to cache/session-context.md.
   * This file is the fallback for agents that don't support custom tools
   * (Cursor, Antigravity) and the source for opencode.json instructions.
   */
  async function generateContextFile() {
    try {
      const ctxFile = `${_LLM_WIKI_STATE_ROOT}/cache/session-context.md`;
      await runPython("session_start_context.py", ["--output-file", ctxFile], "{}");
    } catch {}
  }

  async function collectTranscript(sessionId) {
    try {
      const response = await client.session.messages({ path: { id: sessionId } });
      const messages = Array.isArray(response?.data) ? response.data : [];
      return messages
        .slice(-MAX_TRANSCRIPT_MESSAGES)
        .map((message) => ({ message, role: message?.info?.role || message?.role }))
        .filter(({ role }) => role === "user" || role === "assistant")
        .map(({ message, role }) => {
          const text = (message?.parts || [])
            .filter((part) => part?.type === "text")
            .map((part) => String(part?.text || "").trim())
            .filter(Boolean)
            .join("\n");
          return text ? `${role}: ${text}` : "";
        })
        .filter(Boolean)
        .join("\n\n")
        .slice(-MAX_TRANSCRIPT_CHARS);
    } catch {
      return "";
    }
  }

  async function processPendingCompile(task) {
    if (sdkCompileRunning) return OUTCOME_FAILED;
    sdkCompileRunning = true;
    try {
      for (let index = 0; index < MAX_COMPILE_BATCHES; index += 1) {
        let request;
        try {
          request = JSON.parse(await runPython("compile_memory.py", ["--prepare-sdk-request"]));
        } catch (error) {
          await logError("Failed to prepare SDK compile", error);
          return OUTCOME_FAILED;
        }
        if (!request?.pending) return OUTCOME_COMPLETE;

        let ephemeralId = null;
        let providerComplete = false;
        try {
          const session = await withQueueLeaseRenewal(
            task,
            () => client.session.create({ body: { title: "memory-compile-ephemeral" } }),
            (created) => {
              const createdId = created?.data?.id || created?.id;
              if (createdId) {
                ephemeralId = createdId;
                internalSessionIds.add(createdId);
              }
            },
          );
          if (session?.error) throw new Error(`OpenCode create error: ${JSON.stringify(session.error)}`);
          if (!ephemeralId) throw new Error("OpenCode did not return an ephemeral session id");
          if (request.system_prompt) {
            const systemResult = await withQueueLeaseRenewal(
              task,
              () => client.session.prompt({
                path: { id: ephemeralId },
                body: {
                  noReply: true,
                  model: SERVICE_MODEL,
                  parts: [{ type: "text", text: request.system_prompt }],
                },
              }),
            );
            const systemError = providerError(systemResult);
            if (systemError) throw new Error(`OpenCode provider error: ${JSON.stringify(systemError)}`);
          }
          const result = await withQueueLeaseRenewal(
            task,
            () => client.session.prompt({
              path: { id: ephemeralId },
              body: { model: SERVICE_MODEL, parts: [{ type: "text", text: request.prompt }] },
            }),
          );
          const responseError = providerError(result);
          if (responseError) throw new Error(`OpenCode provider error: ${JSON.stringify(responseError)}`);
          const parts = result?.data?.parts || result?.parts || [];
          const response = parts.map((part) => part?.text || "").join("").trim();
          if (!response) throw new Error("OpenCode returned an empty compile response");
          providerComplete = true;
          const applyOutput = await runPython(
            "compile_memory.py",
            ["--apply-sdk-response", "--trigger", "auto"],
            JSON.stringify({ request, response }),
          );
          let applied;
          try { applied = JSON.parse(applyOutput); } catch {}
          if (!["applied", "already_applied"].includes(applied?.status)) {
            await logError(
              "OpenCode SDK compile apply returned an invalid result",
              new Error(String(applied?.status || "invalid JSON")),
            );
            return OUTCOME_FAILED;
          }
        } catch (error) {
          if (!providerComplete) {
            try {
              await runPython(
                "compile_memory.py",
                ["--record-sdk-failure"],
                JSON.stringify({
                  stage: "provider",
                  error: String(error?.message || error || "unknown provider error"),
                  batch_id: String(request?.batch_id || ""),
                }),
              );
            } catch (recordError) {
              await logError("Failed to persist SDK compile provider failure", recordError);
            }
          }
          await logError("OpenCode SDK compile failed", error);
          return OUTCOME_FAILED;
        } finally {
          await cleanupInternalSession(ephemeralId, "compile");
        }
      }
      return OUTCOME_CAPPED;
    } finally {
      sdkCompileRunning = false;
    }
  }

  async function applyQueueResult(task, result) {
    const output = await runPython(
      "memory_queue.py",
      ["--apply-sdk-result"],
      JSON.stringify({
        task_id: task.task_id,
        lease_id: task.lease_id,
        digest: task.digest,
        ...result,
      }),
    );
    let applied;
    try {
      applied = JSON.parse(output);
    } catch {
      throw new Error("SDK queue apply returned invalid JSON");
    }
    const expectedStatuses = result.defer
      ? ["deferred"]
      : result.success && task.kind === "compile"
        ? ["acknowledged", "compile_pending"]
        : [result.success ? "acknowledged" : "failure recorded"];
    if (applied?.ok !== true || !expectedStatuses.includes(applied?.status)) {
      throw new Error(`SDK queue apply rejected: ${applied?.status || "unknown error"}`);
    }
    return applied;
  }

  async function deferCompileControl(task) {
    try {
      await applyQueueResult(task, { defer: true });
      return OUTCOME_CAPPED;
    } catch (error) {
      await logError("Failed to defer capped compile control", error);
      try {
        await applyQueueResult(task, {
          success: false,
          error: `queued compile deferral rejected: ${String(error?.message || error)}`,
        });
      } catch (persistError) {
        await logError("Failed to persist queued compile attempt", persistError);
      }
      return OUTCOME_FAILED;
    }
  }

  async function processQueueTask(task) {
    if (task.kind === "compile") {
      const outcome = await processPendingCompile(task);
      if (outcome === OUTCOME_CAPPED) return deferCompileControl(task);
      try {
        const applied = await applyQueueResult(task, outcome === OUTCOME_COMPLETE
          ? { success: true, response: "COMPILE_COMPLETED" }
          : { success: false, error: "queued compile did not complete" });
        if (applied.status === "compile_pending") return OUTCOME_CAPPED;
        return outcome;
      } catch (error) {
        await logError("Failed to apply queued compile result", error);
        try {
          await applyQueueResult(task, {
            success: false,
            error: `queued compile result apply rejected: ${String(error?.message || error)}`,
          });
        } catch (persistError) {
          await logError("Failed to persist queued compile attempt", persistError);
        }
        return OUTCOME_FAILED;
      }
    }
    let ephemeralId = null;
    let providerResultReady = false;
    try {
      const session = await withQueueLeaseRenewal(
        task,
        () => client.session.create({ body: { title: "memory-queue-ephemeral" } }),
        (created) => {
          const createdId = created?.data?.id || created?.id;
          if (createdId) {
            ephemeralId = createdId;
            internalSessionIds.add(createdId);
          }
        },
      );
      if (session?.error) throw new Error(`OpenCode create error: ${JSON.stringify(session.error)}`);
      if (!ephemeralId) throw new Error("OpenCode did not return an ephemeral session id");
      if (task.system_prompt) {
        const systemResult = await withQueueLeaseRenewal(
          task,
          () => client.session.prompt({
            path: { id: ephemeralId },
            body: {
              noReply: true,
              model: SERVICE_MODEL,
              parts: [{ type: "text", text: task.system_prompt }],
            },
          }),
        );
        const systemError = providerError(systemResult);
        if (systemError) throw new Error(`OpenCode provider error: ${JSON.stringify(systemError)}`);
      }
      const result = await withQueueLeaseRenewal(
        task,
        () => client.session.prompt({
          path: { id: ephemeralId },
          body: { model: SERVICE_MODEL, parts: [{ type: "text", text: task.prompt }] },
        }),
      );
      const responseError = providerError(result);
      if (responseError) throw new Error(`OpenCode provider error: ${JSON.stringify(responseError)}`);
      const parts = result?.data?.parts || result?.parts || [];
      const response = parts.map((part) => part?.text || "").join("").trim();
      if (!response) throw new Error("OpenCode returned an empty queue response");
      providerResultReady = true;
      await applyQueueResult(task, { success: true, response });
      return OUTCOME_COMPLETE;
    } catch (error) {
      await logError("OpenCode SDK queue task failed", error);
      try {
        await applyQueueResult(task, {
          success: false,
          error: providerResultReady
            ? `successful result apply rejected: ${String(error?.message || error)}`
            : String(error?.message || error || "unknown provider error"),
        });
      } catch (persistError) {
        await logError("Failed to persist SDK queue attempt", persistError);
        return OUTCOME_FAILED;
      }
      return OUTCOME_FAILED;
    } finally {
      await cleanupInternalSession(ephemeralId, "queue");
    }
  }

  async function ensureCompileControl() {
    const ensured = JSON.parse(
      await runPython("memory_queue.py", ["--ensure-compile-task"]),
    );
    if (!ensured || typeof ensured !== "object") {
      throw new Error("compile control ensure returned invalid JSON");
    }
    return ensured;
  }

  async function processMaintenance() {
    let compileControlProcessed = false;
    try {
      await ensureCompileControl();
    } catch (error) {
      await logError("Failed to ensure SDK compile control", error);
      return OUTCOME_FAILED;
    }
    for (let index = 0; index < MAX_QUEUE_TASKS; index += 1) {
      let task;
      try {
        task = JSON.parse(await runPython("memory_queue.py", ["--prepare-sdk-task"]));
      } catch (error) {
        await logError("Failed to prepare SDK queue task", error);
        return OUTCOME_FAILED;
      }
      if (!task?.pending) {
        let ensured;
        try {
          ensured = await ensureCompileControl();
        } catch (error) {
          await logError("Failed to recheck SDK compile control", error);
          return OUTCOME_FAILED;
        }
        if (ensured.pending === true && ensured.state === "pending_eligible") {
          return OUTCOME_CAPPED;
        }
        if (ensured.pending === true && ensured.state === "backoff") {
          const retrySeconds = Number(ensured.retry_delay_seconds);
          if (Number.isFinite(retrySeconds) && retrySeconds > 0) {
            const retryDelay = Math.min(60_000, Math.max(1_000, Math.ceil(retrySeconds * 1000)));
            scheduleMaintenanceContinuation(retryDelay);
          }
        }
        return OUTCOME_COMPLETE;
      }
      if (task.kind === "compile" && compileControlProcessed) {
        return deferCompileControl(task);
      }
      if (task.kind === "compile") compileControlProcessed = true;
      const outcome = await processQueueTask(task);
      if (outcome !== OUTCOME_COMPLETE) return outcome;
    }
    return OUTCOME_CAPPED;
  }

  function scheduleMaintenanceContinuation(delay = 1000) {
    if (maintenanceContinuationScheduled) return;
    maintenanceContinuationScheduled = true;
    schedule(() => {
      maintenanceContinuationScheduled = false;
      requestMaintenance();
    }, delay);
  }

  async function runRequestedMaintenance() {
    if (maintenanceRunning || !maintenanceRequested) return;
    maintenanceRunning = true;
    maintenanceRequested = false;
    let outcome = OUTCOME_FAILED;
    try {
      outcome = await processMaintenance();
    } finally {
      maintenanceRunning = false;
      if (outcome === OUTCOME_CAPPED || maintenanceRequested) {
        scheduleMaintenanceContinuation();
      }
    }
  }

  function requestMaintenance() {
    maintenanceRequested = true;
    if (maintenanceRunning || maintenanceContinuationScheduled) return;
    void runRequestedMaintenance();
  }

  async function persistIdleFallback(slug, sid, redactedTranscript, occurredAt) {
    try {
      await runPython(
        "flush_memory.py",
        [
          "--event", "session-end",
          "--session-id", String(sid),
          "--transcript-stdin",
          "--trigger", "opencode-idle",
          "--project-slug", slug,
          "--project-root", projectDirectory,
          "--occurred-at", occurredAt.toISOString(),
        ],
        redactedTranscript,
      );
      await heartbeat(slug, projectDirectory, "flush-deferred", String(sid));
      return true;
    } catch (error) {
      await logError("OpenCode durable classification fallback failed", error);
      await heartbeat(slug, projectDirectory, "flush-failed", String(sid));
      return false;
    }
  }

  async function handleSessionCreated(event) {
    if (isVault()) return;
    const sid = event?.properties?.info?.id || event?.properties?.id || "opencode";
    if (await isInternalSession(sid, event?.properties?.info?.title)) return;
    requestMaintenance();
    const slug = await computeSlug(projectDirectory);
    if (slug) await heartbeat(slug, projectDirectory, "opencode-start", String(sid));
    await generateContextFile();
    warmStartVectorSearch();
  }

  async function handleSessionIdle(event) {
    if (isVault()) return;
    const occurredAt = new Date();
    const sid = event?.properties?.info?.id || event?.properties?.sessionID || event?.properties?.sessionId || "opencode";
    if (await isInternalSession(sid)) return;
    const slug = await computeSlug(projectDirectory);
    if (!slug) return;
    const transcript = await collectTranscript(sid);
    if (transcript.length < 50) { await heartbeat(slug, projectDirectory, "idle-short", String(sid)); return; }
    let redactedTranscript;
    try {
      redactedTranscript = String(await runPython("secret_redact.py", ["--stdin"], transcript));
      if (!redactedTranscript.trim()) throw new Error("secret redactor returned empty output");
      redactedTranscript = redactedTranscript.slice(-MAX_TRANSCRIPT_CHARS);
    } catch (error) {
      await logError("OpenCode transcript redaction failed", error);
      await heartbeat(slug, projectDirectory, "redaction-failed", String(sid));
      return;
    }
    const prompt = `Classify and distill this transcript into durable project memory.\n\n` +
      `FLUSH_MAJOR requires a concrete decision with rationale, a reusable lesson/pattern, ` +
      `or a non-obvious command/snippet worth remembering across sessions.\n` +
      `FLUSH_MINOR is limited to a debug gotcha (symptom, cause, fix), a durable open question, ` +
      `or one useful non-obvious observation.\n` +
      `FLUSH_OK covers status/progress updates, audit/review verdicts or findings, ` +
      `file/path/code summaries, facts derivable from code/config, navigation, and other material ` +
      `that a future session can recover without memory. When in doubt, choose FLUSH_OK.\n\n` +
      `FLUSH_MAJOR and FLUSH_MINOR require a non-empty distilled body using ONLY the applicable ` +
      `recognized Markdown sections below; omit empty sections:\n` +
      `- **Decisions made** - concrete choices with reasons (MAJOR only).\n` +
      `- **Lessons / patterns** - reusable insights (MAJOR only).\n` +
      `- **Commands / snippets** - non-obvious invocations.\n` +
      `- **Gotchas / debugging** - symptom, cause, and fix.\n` +
      `- **Open questions** - unresolved and worth returning to.\n` +
      `Be terse. Keep each bullet to one line and do not narrate work completed.\n\n` +
      `Respond with FLUSH_MAJOR, FLUSH_MINOR, or FLUSH_OK as the first line. ` +
      `FLUSH_OK must be the token only, with no body.\n\n` +
      `--- BEGIN TRANSCRIPT ---\n${redactedTranscript}\n--- END TRANSCRIPT ---`;
    let classification = null;
    let sessId2 = null;
    try {
      const sess = await client.session.create({ body: { title: "memory-ephemeral" } });
      if (sess?.error) throw new Error(`OpenCode create error: ${JSON.stringify(sess.error)}`);
      sessId2 = sess?.data?.id || sess?.id;
      if (!sessId2) throw new Error("OpenCode did not return an ephemeral session id");
      internalSessionIds.add(sessId2);
      const result = await client.session.prompt({ path: { id: sessId2 }, body: { model: SERVICE_MODEL, parts: [{ type: "text", text: prompt }] } });
      const responseError = providerError(result);
      if (responseError) throw new Error(`OpenCode provider error: ${JSON.stringify(responseError)}`);
      const parts = result?.data?.parts || result?.parts || [];
      const text = parts.map(p=>p.text||"").join("").trim();
      if (!text) throw new Error("OpenCode returned an empty classification response");
      if (text === "FLUSH_OK") {
        classification = { tier: "ok", body: "" };
      } else {
        const lines = text.split(/\r?\n/);
        const first = lines.shift() || "";
        const body = lines.join("\n").trim();
        if (!["FLUSH_MAJOR", "FLUSH_MINOR"].includes(first) || !body) {
          throw new Error("OpenCode returned an invalid classification response");
        }
        classification = {
          tier: first === "FLUSH_MAJOR" ? "major" : "minor",
          body,
        };
      }
    } catch (error) {
      await logError("OpenCode SDK classification failed", error);
    } finally {
      await cleanupInternalSession(sessId2, "classification");
    }
    if (!classification) {
      await persistIdleFallback(slug, sid, redactedTranscript, occurredAt);
      return;
    }
    const { tier, body } = classification;
    if (tier === "ok") { await heartbeat(slug, projectDirectory, "flush-ok", String(sid)); return; }
    const ts = occurredAt.toTimeString().slice(0, 8);
    try {
      await appendDaily(
        slug,
        projectDirectory,
        String(sid),
        `## [${ts}] opencode-idle | ${sid}\n- Trigger: \`opencode-idle\`\n` +
          `- Project slug: \`${slug}\`\n- Project root JSON: ${JSON.stringify(String(projectDirectory || ""))}\n` +
          `- Tier: \`${tier}\`\n- Source session: \`${sid}\`\n\n` +
          `${neutralizeDailyRecordHeaders(body)}\n${DAILY_RECORD_COMPLETION_MARKER}\n`,
      );
    } catch (error) {
      await logError("OpenCode direct daily append failed", error);
      await persistIdleFallback(slug, sid, redactedTranscript, occurredAt);
      return;
    }
    if (tier === "major") await triggerCompile();
  }

  function normalizeToolInput(input) {
    const legacy = input?.input && typeof input.input === "object" ? input.input : {};
    const current = input?.args && typeof input.args === "object" ? input.args : {};
    return { ...legacy, ...current };
  }

  function oneLine(value) {
    return String(value || "").trim().split(/\s+/).filter(Boolean).join(" ");
  }

  function mutationTargets(tool, toolInput) {
    if (tool === "ApplyPatch") {
      return Array.from(
        String(toolInput.patchText || "").matchAll(
          /^\*\*\* (?:Add File|Update File|Delete File|Move to):\s*(.+?)\s*$/gm,
        ),
        (match) => oneLine(match[1]),
      ).filter(Boolean);
    }
    const target = oneLine(toolInput.filePath || toolInput.file_path || toolInput.notebook_path);
    return target ? [target] : [];
  }

  function mutationDirectory(toolInput) {
    const explicitDirectory = oneLine(toolInput.workdir || toolInput.cwd);
    if (!explicitDirectory) return projectDirectory;
    if (path.isAbsolute(explicitDirectory)) return explicitDirectory;
    return path.resolve(projectDirectory || ".", explicitDirectory);
  }

  return {
    // ─── Event Handlers ─────────────────────────────────────────────

    event: async ({ event }) => {
      if (event?.type === "session.created") await handleSessionCreated(event);
      else if (event?.type === "session.idle") await handleSessionIdle(event);
    },

    "chat.message": async (input, output) => {
      const role = output?.message?.role;
      if (isVault() || (role !== undefined && role !== "user")) return;
      const sid = String(input?.sessionID || input?.sessionId || "opencode");
      if (await isInternalSession(sid)) return;
      const prompt = (output?.parts || [])
        .filter((part) => part?.type === "text")
        .map((part) => String(part?.text || "").trim())
        .filter(Boolean)
        .join("\n");
      if (!prompt) return;
      try {
        await runPython("user_prompt_capture.py", [], JSON.stringify({
          session_id: sid,
          prompt,
          cwd: projectDirectory,
          project_root: projectDirectory,
        }));
      } catch {}
      try {
        const slug = await computeSlug(projectDirectory);
        if (!slug) return;
        await runPython("feedback_capture.py", [], JSON.stringify({
          text: prompt,
          session_id: sid,
          slug,
          trigger: "opencode-user-message",
        }));
      } catch {}
    },

    "experimental.chat.system.transform": async (input, output) => {
      if (isVault() || !Array.isArray(output?.system)) return;
      if (await isInternalSession(input?.sessionID || input?.sessionId)) return;
      // App restarts can resume an existing session without emitting
      // session.created. Request bounded maintenance from this guaranteed
      // chat path as well, without delaying the user's prompt.
      requestMaintenance();
      const marker = "# Project memory context";
      if (output.system.some((entry) => String(entry).includes(marker))) return;
      try {
        const context = extractContext(await runPython("session_start_context.py", contextArgs()));
        if (context) output.system.push(context);
      } catch (error) {
        await logError("Failed to inject session context", error);
      }
    },

    "tool.execute.after": async (input) => {
      if (isVault()) return;
      const sid = String(input?.sessionID || input?.sessionInfo?.id || input?.sessionId || "opencode");
      if (await isInternalSession(sid)) return;
      const tool = SIGNIFICANT_TOOLS.get(String(input?.tool || "").toLowerCase());
      if (!tool) return;
      const toolInput = normalizeToolInput(input);
      const targets = mutationTargets(tool, toolInput);
      if (!targets.length) return;
      const eventDirectory = mutationDirectory(toolInput);
      if (targets.some((target) => isInsideVault(path.resolve(eventDirectory || ".", target)))) return;
      try {
        await runPython("post_tool_capture.py", [], JSON.stringify({
          session_id: sid,
          tool_name: tool,
          tool_input: toolInput,
          cwd: eventDirectory,
          project_root: projectDirectory,
        }));
      } catch {}
    },

    "experimental.session.compacting": async (input, output) => {
      if (isVault()) return;
      try {
        const occurredAt = new Date().toISOString();
        const sid = String(input?.sessionID || input?.sessionId || "unknown");
        if (await isInternalSession(sid)) return;
        const transcript = await collectTranscript(sid);
        const slug = await computeSlug(projectDirectory);
        if (!slug) return;
        // Best-effort flush before context loss (parity with Claude PreCompact).
        try {
          await runPython("precompact_capture.py", [], JSON.stringify({
            session_id: sid,
            transcript,
            trigger: "opencode-compacting",
            project_slug: slug,
            project_root: projectDirectory,
            occurred_at: occurredAt,
          }));
        } catch {}
        // Inject knowledge context into the compacted session so it survives.
        if (output?.context) {
          output.context.push(`Memory: precompact capture attempted. Knowledge context is available via the memory_context and memory_recall tools.`);
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
      memory_context: {
        description:
          "Get session-start knowledge context: vault inventory, knowledge " +
          "index, guardrails (learned corrections), advisory (open threads, " +
          "last decision), and the latest daily-log excerpt. Call this at " +
          "the start of every session to understand the project knowledge state.",
        args: {},
        async execute() {
          try {
            const stdout = (await runPython("session_start_context.py", contextArgs())).trim();
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
            return "(memory_context: error generating context)";
          }
        },
      },

      memory_recall: {
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
          if (!query) return "Usage: memory_recall(query='your search query')";
          try {
            const stdout = (await runPython("search_memory.py", ["--stdin"], query)).trim();
            return stdout || "(no results found)";
          } catch {
            return "(memory_recall: search error)";
          }
        },
      },
    },
  };
};
