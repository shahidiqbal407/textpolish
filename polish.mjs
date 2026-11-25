#!/usr/bin/env node
// Writing Polisher: turns rough text into clear, natural, human-sounding writing using Groq (free tier).
//
//   polish serve            open the web app at http://127.0.0.1:8765
//   polish clip [tone]      open the desktop app (polisher_app.py) on the selected text
//   polish "some text"      polish text from the command line (or pipe it in via stdin)

import Groq from "groq-sdk";
import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const CONFIG_DIR = path.join(os.homedir(), ".config", "textpolish");
const KEY_FILE = path.join(CONFIG_DIR, "api_key");
// Older installs saved the key here; still read it so they keep working.
const OLD_KEY_FILE = path.join(os.homedir(), ".config", "writing-polisher", "api_key");
const PORT = Number(process.env.POLISH_PORT || 8765);
// Model and default tone live in settings.json (shared with the desktop app); env vars override.
// Lines starting with // are comments, so alternative models can be kept there commented out.
const SETTINGS = JSON.parse(
  fs.readFileSync(path.join(here, "settings.json"), "utf8").replace(/^\s*\/\/.*$/gm, ""),
);
const MODEL = process.env.POLISH_MODEL || SETTINGS.model || "openai/gpt-oss-120b";
const DEFAULT_TONE = process.env.POLISH_TONE || SETTINGS.tone || "natural";

// Writing instructions and tones are shared with the desktop app (polisher_app.py).
const { system: SYSTEM_PROMPT, tones: TONES } = JSON.parse(fs.readFileSync(path.join(here, "prompts.json"), "utf8"));

function loadApiKey() {
  if (process.env.GROQ_API_KEY) return process.env.GROQ_API_KEY;
  for (const file of [KEY_FILE, OLD_KEY_FILE]) {
    try {
      return fs.readFileSync(file, "utf8").trim();
    } catch {}
  }
  return undefined;
}

let client;
function getClient() {
  if (!client) {
    const apiKey = loadApiKey();
    if (!apiKey) {
      throw new Error(`No API key found. Run: polish set-key   (or set GROQ_API_KEY)`);
    }
    client = new Groq({ apiKey });
  }
  return client;
}

function reasoningOptions(model) {
  if (model.startsWith("openai/gpt-oss")) return { reasoning_effort: "low", include_reasoning: false };
  if (model.startsWith("qwen/")) return { reasoning_effort: "low", reasoning_format: "hidden" };
  return {};
}

// Yields the rewrite piece by piece as the model writes it.
export async function* polishStream(text, tone = DEFAULT_TONE) {
  const toneLine = TONES[tone] ?? TONES.natural;
  const stream = await getClient().chat.completions.create({
    model: MODEL,
    max_completion_tokens: 4000,
    stream: true,
    // Low reasoning keeps it fast and saves the free daily token allowance; hidden keeps it out of the reply.
    ...reasoningOptions(MODEL),
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      { role: "user", content: `${toneLine}\n\n<draft>\n${text}\n</draft>` },
    ],
  });
  for await (const chunk of stream) {
    const delta = chunk.choices[0]?.delta?.content;
    if (delta) yield delta;
  }
}

export async function polish(text, tone = DEFAULT_TONE) {
  let raw = "";
  for await (const delta of polishStream(text, tone)) raw += delta;
  const out = cleanOutput(raw);
  if (!out) throw new Error("The model returned an empty response. Try again.");
  return out;
}

// Open models sometimes wrap the answer ("Here's the polished version:", quotes, <draft> tags).
function cleanOutput(text) {
  let out = text.trim();
  out = out.replace(/^(here(?:'s| is) (?:the |a |your )?(?:polished|rewritten|revised|corrected|improved)[^\n]*?:)\s*/i, "");
  out = out.replace(/^<draft>\s*|\s*<\/draft>$/g, "");
  if (/^".*"$/s.test(out) && !out.slice(1, -1).includes('"')) out = out.slice(1, -1);
  return out.trim();
}

function errorMessage(err) {
  if (err instanceof Groq.AuthenticationError) return "Invalid Groq API key. Run: polish set-key";
  if (err instanceof Groq.RateLimitError) return "Free-tier limit reached. Wait a minute (or until tomorrow if the daily cap is used up).";
  if (err instanceof Groq.APIConnectionError) return "Could not reach Groq. Check your internet connection.";
  if (err instanceof Groq.APIError) return `Groq API error (${err.status}): ${err.message}`;
  return err?.message ?? String(err);
}

// ---------- desktop app (for the global keyboard shortcut) ----------

// The hotkey opens the native desktop app, which shows the rewrite as it's written.
function clipMode(tone) {
  const env = tone ? { ...process.env, POLISH_TONE: tone } : process.env;
  spawn("/usr/bin/python3", [path.join(here, "polisher_app.py")], { stdio: "ignore", detached: true, env }).unref();
}

// ---------- web app ----------

async function readJson(req) {
  let body = "";
  for await (const chunk of req) body += chunk;
  return JSON.parse(body || "{}");
}

// Streams newline-delimited JSON: {"delta": "..."} while writing, then {"done": true, "result": "..."} or {"error": "..."}.
async function streamPolish(res, text, tone) {
  res.writeHead(200, { "content-type": "application/x-ndjson", "cache-control": "no-store" });
  const send = (obj) => res.write(JSON.stringify(obj) + "\n");
  if (!text?.trim()) {
    send({ error: "Write something first." });
    return res.end();
  }
  try {
    let raw = "";
    for await (const delta of polishStream(text, tone)) {
      raw += delta;
      send({ delta });
    }
    const result = cleanOutput(raw);
    if (!result) throw new Error("The model returned an empty response. Try again.");
    send({ done: true, result });
  } catch (err) {
    send({ error: errorMessage(err) });
  }
  res.end();
}

function serve() {
  const pages = {
    "/": path.join(here, "index.html"),
  };
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://localhost");
    const json = (status, obj) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(obj));
    };
    try {
      if (req.method === "GET" && pages[url.pathname]) {
        res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
        return res.end(fs.readFileSync(pages[url.pathname]));
      }
      if (req.method === "POST" && url.pathname === "/api/polish") {
        const { text, tone } = await readJson(req);
        return await streamPolish(res, text, tone);
      }
      json(404, { error: "Not found" });
    } catch (err) {
      if (!res.headersSent) json(500, { error: errorMessage(err) });
      else res.end();
    }
  });
  // Bound to localhost only: the page can spend your API quota, so it must not be reachable from the network.
  server.listen(PORT, "127.0.0.1", () => {
    const url = `http://127.0.0.1:${PORT}`;
    console.log(`Writing Polisher running at ${url}  (Ctrl+C to stop)`);
    if (!process.argv.includes("--no-open")) spawn("xdg-open", [url], { stdio: "ignore", detached: true }).unref();
  });
  server.on("error", (err) => {
    if (err.code === "EADDRINUSE") {
      // Already running: just open it.
      if (!process.argv.includes("--no-open")) spawn("xdg-open", [`http://127.0.0.1:${PORT}`], { stdio: "ignore", detached: true }).unref();
    } else {
      console.error(err.message);
      process.exitCode = 1;
    }
  });
}

// ---------- CLI ----------

async function setKey() {
  const readline = await import("node:readline/promises");
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  const key = (await rl.question("Paste your Groq API key (from console.groq.com/keys): ")).trim();
  rl.close();
  if (!key) return console.error("No key entered.");
  fs.mkdirSync(CONFIG_DIR, { recursive: true, mode: 0o700 });
  fs.writeFileSync(KEY_FILE, key + "\n", { mode: 0o600 });
  console.log(`Saved to ${KEY_FILE}`);
}

function printHelp() {
  console.log(`Usage:
  polish serve               open the web app
  polish clip [tone]         open the desktop app on the selected/copied text
  polish [--tone T] "text"   polish text (or pipe it via stdin)
  polish set-key             save your Groq API key

Tones: ${Object.keys(TONES).join(", ")}`);
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  if (cmd === "serve") return serve();
  if (cmd === "clip") return clipMode(rest[0]);
  if (cmd === "set-key") return setKey();
  if (cmd === "-h" || cmd === "--help") return printHelp();

  let args = process.argv.slice(2);
  let tone = DEFAULT_TONE;
  const t = args.indexOf("--tone");
  if (t !== -1) {
    tone = args[t + 1];
    args.splice(t, 2);
  }
  let text = args.join(" ");
  if (!text && !process.stdin.isTTY) text = fs.readFileSync(0, "utf8");
  if (!text.trim()) return printHelp();
  try {
    console.log(await polish(text, tone));
  } catch (err) {
    console.error(errorMessage(err));
    process.exitCode = 1;
  }
}

main();
