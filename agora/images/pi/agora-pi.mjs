#!/usr/bin/env node
// agora's adapter for pi: speaks the part of ACP agora uses on stdio and runs
// each conversation with pi's SDK. Nothing is discovered: the conversation is
// built only from what agora passes (docs/harness-adapters.md).
//
// Environment (set by agora):
//   AGORA_PI_MODEL           provider/model, e.g. anthropic/claude-haiku-5-5
//   AGORA_INSTRUCTIONS_FILE  the module's instructions (optional)
//   AGORA_SKILLS_DIR         the module's skills
//   AGORA_BUILTIN_TOOLS      "1" to give pi's built-in tools, "0" for none
//   AGORA_LLM_URL / AGORA_LLM_KEY  the gateway and the session's key (optional)
// MCP servers (agora's bridge and the module's) come in ACP session/new.

import { randomUUID } from "node:crypto";
import { readFileSync, mkdtempSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { Readable } from "node:stream";
import {
	createAgentSession,
	createExtensionRuntime,
	loadSkillsFromDir,
	ModelRuntime,
	SessionManager,
	SettingsManager,
} from "@earendil-works/pi-coding-agent";
import { createMcpToolName, McpClient, StdioTransport, StreamableHttpTransport, toLlmContent } from "@earendil-works/pi-mcp";

const VERSION = "0.2.0";
// agora tools can wait up to 600 s (receive, spawn): allow that and some.
const TOOL_TIMEOUT_MS = 20 * 60 * 1000;
const MODEL_CONFIG_ID = "model";

// stdout carries ACP only.
console.log = console.error;
console.info = console.error;

const env = process.env;
const [provider, ...rest] = (env.AGORA_PI_MODEL ?? "").split("/");
const modelId = rest.join("/");
const instructionsFile = env.AGORA_INSTRUCTIONS_FILE;
const skillsDir = env.AGORA_SKILLS_DIR;
const builtinTools = env.AGORA_BUILTIN_TOOLS !== "0";
const llmUrl = env.AGORA_LLM_URL;
const llmKey = env.AGORA_LLM_KEY;
// The key stays in this process: pi's tools (bash) and MCP servers never see it.
delete env.AGORA_LLM_KEY;

const stateDir = mkdtempSync(join(tmpdir(), "agora-pi-"));
const modelRuntime = await ModelRuntime.create({ authPath: join(stateDir, "auth.json"), modelsPath: null });
let model = modelRuntime.getModel(provider, modelId);
// Request headers of every LLM call. agora gives each turn a traceparent, so
// the gateway's record of the call nests under that turn.
const llmHeaders = {};
if (model) model = { ...model, ...(llmUrl ? { baseUrl: llmUrl } : {}), headers: Object.assign(llmHeaders, model.headers) };
if (llmKey) await modelRuntime.setRuntimeApiKey(provider, llmKey);

const agentsFiles = instructionsFile ? [{ path: instructionsFile, content: readFileSync(instructionsFile, "utf8") }] : [];
const skills = skillsDir ? loadSkillsFromDir({ dir: skillsDir, source: "agora" }).skills : [];
const resourceLoader = {
	getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
	getSkills: () => ({ skills, diagnostics: [] }),
	getPrompts: () => ({ prompts: [], diagnostics: [] }),
	getThemes: () => ({ themes: [], diagnostics: [] }),
	getAgentsFiles: () => ({ agentsFiles }),
	getSystemPrompt: () => undefined, // pi's own base prompt
	getSystemPromptSource: () => undefined,
	getAppendSystemPrompt: () => [],
	getAppendSystemPromptSources: () => [],
	extendResources: () => {},
	reload: async () => {},
};

// --- ACP over stdio (newline-delimited JSON-RPC) ---------------------------

const send = (msg) => process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...msg })}\n`);
const notify = (method, params) => send({ method, params });
class RpcError extends Error {
	constructor(code, message) {
		super(message);
		this.code = code;
	}
}

// --- MCP servers as pi tools ------------------------------------------------

// fetch over node:http(s). Node's own fetch gives up waiting for response
// headers after 300 s, shorter than the longest agora tool call.
function longFetch(url, init = {}) {
	return new Promise((resolve, reject) => {
		const u = new URL(url);
		const req = (u.protocol === "https:" ? https : http).request(
			u,
			{ method: init.method ?? "GET", headers: Object.fromEntries(new Headers(init.headers ?? {})) },
			(res) => {
				const headers = new Headers();
				for (const [k, v] of Object.entries(res.headers)) {
					if (v !== undefined) headers.set(k, Array.isArray(v) ? v.join(", ") : v);
				}
				const nobody = res.statusCode === 202 || res.statusCode === 204 || init.method === "HEAD";
				resolve(new Response(nobody ? null : Readable.toWeb(res), { status: res.statusCode, headers }));
			},
		);
		req.on("error", reject);
		init.signal?.addEventListener("abort", () => req.destroy(new Error("aborted")), { once: true });
		if (init.body) req.write(typeof init.body === "string" ? init.body : Buffer.from(init.body));
		req.end();
	});
}

const mcpClients = new Map(); // server config (JSON) -> connected client
async function mcpClient(server) {
	const key = JSON.stringify(server);
	if (!mcpClients.has(key)) {
		const client = new McpClient({ name: "agora-pi", version: VERSION });
		const transport = server.url
			? new StreamableHttpTransport({
					url: server.url,
					headers: Object.fromEntries((server.headers ?? []).map((h) => [h.name, h.value])),
					fetch: longFetch,
				})
			: new StdioTransport({
					command: server.command,
					args: server.args ?? [],
					// Only what the server is given, not this process's environment.
					inheritEnv: false,
					env: {
						PATH: env.PATH ?? "/usr/bin:/bin",
						HOME: env.HOME ?? "/tmp",
						...Object.fromEntries((server.env ?? []).map((e) => [e.name, e.value])),
					},
					stderr: "inherit",
				});
		mcpClients.set(key, client.connect(transport).then(() => client));
	}
	return mcpClients.get(key);
}

async function mcpTools(servers) {
	const tools = [];
	const taken = new Set();
	for (const server of servers) {
		const client = await mcpClient(server);
		for (const tool of await client.listTools()) {
			const name = createMcpToolName(server.name, tool.name, (n) => taken.has(n));
			taken.add(name);
			tools.push({
				name,
				label: tool.title ?? tool.name,
				description: tool.description ?? tool.name,
				parameters: { ...tool.inputSchema, type: "object", properties: tool.inputSchema?.properties ?? {} },
				exposure: "direct", // declared to the model from the first request
				execute: async (_id, params, signal) => {
					const result = await client.callTool(tool.name, params ?? {}, { signal, timeoutMs: TOOL_TIMEOUT_MS });
					return { content: toLlmContent(result), details: undefined, isError: result.isError === true };
				},
			});
		}
	}
	return tools;
}

// --- conversations ------------------------------------------------------------

const sessions = new Map(); // ACP session id -> pi AgentSession

function configOptions() {
	const value = `${provider}/${modelId}`;
	return [
		{
			type: "select",
			id: MODEL_CONFIG_ID,
			category: "model",
			name: "Model",
			currentValue: value,
			options: [{ value, name: value }],
		},
	];
}

async function newSession(params) {
	if (!model) throw new RpcError(-32000, `pi does not know the model ${provider}/${modelId}`);
	const cwd = params.cwd ?? process.cwd();
	const { session } = await createAgentSession({
		cwd,
		agentDir: stateDir,
		model,
		modelRuntime,
		resourceLoader,
		customTools: await mcpTools(params.mcpServers ?? []),
		...(builtinTools ? {} : { noTools: "builtin" }),
		sessionManager: SessionManager.inMemory(cwd),
		settingsManager: SettingsManager.inMemory({ cacheWarming: "off" }),
	});
	const sessionId = randomUUID();
	sessions.set(sessionId, session);
	session.subscribe((event) => {
		const update = toAcpUpdate(event);
		if (update) notify("session/update", { sessionId, update });
	});
	return { sessionId, configOptions: configOptions() };
}

function toAcpUpdate(event) {
	switch (event.type) {
		case "message_update":
			if (event.assistantMessageEvent?.type === "text_delta") {
				return { sessionUpdate: "agent_message_chunk", content: { type: "text", text: event.assistantMessageEvent.delta } };
			}
			return null;
		case "tool_execution_start":
			return {
				sessionUpdate: "tool_call",
				toolCallId: event.toolCallId,
				title: event.toolName,
				kind: "other",
				status: "in_progress",
				rawInput: event.args,
			};
		case "tool_execution_end":
			return {
				sessionUpdate: "tool_call_update",
				toolCallId: event.toolCallId,
				status: event.isError ? "failed" : "completed",
				rawOutput: event.result,
			};
		default:
			return null;
	}
}

function getSession(params) {
	const session = sessions.get(params?.sessionId);
	if (!session) throw new RpcError(-32602, `unknown session ${params?.sessionId}`);
	return session;
}

const handlers = {
	initialize: async () => ({
		protocolVersion: 1,
		agentCapabilities: { loadSession: false, sessionCapabilities: { close: {} }, mcpCapabilities: { http: true, sse: false } },
		agentInfo: { name: "agora-pi", version: VERSION },
		authMethods: [],
	}),
	"session/new": newSession,
	"session/set_config_option": async (params) => {
		getSession(params);
		if (params.configId !== MODEL_CONFIG_ID || params.value !== `${provider}/${modelId}`) {
			throw new RpcError(-32602, `only ${provider}/${modelId} is offered`);
		}
		return { configOptions: configOptions() };
	},
	"session/prompt": async (params) => {
		const session = getSession(params);
		const text = (params.prompt ?? [])
			.filter((b) => b.type === "text")
			.map((b) => b.text)
			.join("\n");
		const traceparent = params._meta?.traceparent;
		if (typeof traceparent === "string") llmHeaders.traceparent = traceparent;
		else delete llmHeaders.traceparent;
		let aborted = false;
		const off = session.subscribe((e) => {
			if (e.type === "agent_settled") aborted = e.aborted;
		});
		try {
			await session.prompt(text);
		} finally {
			off?.();
		}
		const last = session.messages.at(-1);
		if (last?.role === "assistant" && last.stopReason === "error") {
			throw new RpcError(-32000, last.errorMessage ?? "the model request failed");
		}
		return { stopReason: aborted ? "cancelled" : "end_turn" };
	},
	"session/close": async (params) => {
		getSession(params).dispose();
		sessions.delete(params.sessionId);
		return {};
	},
};

const notifications = {
	"session/cancel": async (params) => {
		await sessions.get(params?.sessionId)?.abort();
	},
};

createInterface({ input: process.stdin }).on("line", async (line) => {
	if (!line.trim()) return;
	let msg;
	try {
		msg = JSON.parse(line);
	} catch {
		return send({ id: null, error: { code: -32700, message: "parse error" } });
	}
	if (msg.id === undefined) {
		try {
			await notifications[msg.method]?.(msg.params);
		} catch (e) {
			console.error(`notification ${msg.method} failed:`, e);
		}
		return;
	}
	const handler = handlers[msg.method];
	if (!handler) return send({ id: msg.id, error: { code: -32601, message: `method not found: ${msg.method}` } });
	try {
		send({ id: msg.id, result: await handler(msg.params ?? {}) });
	} catch (e) {
		send({ id: msg.id, error: { code: e.code ?? -32000, message: String(e.message ?? e) } });
	}
}).on("close", () => process.exit(0));
