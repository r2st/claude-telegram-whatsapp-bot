#!/usr/bin/env node

const { execSync, spawnSync, spawn } = require("child_process");
const { existsSync, writeFileSync } = require("fs");
const path = require("path");
const readline = require("readline");
const https = require("https");

const fs = require("fs");

const PYPI_PACKAGE = "telechatai";
const NPM_VERSION = require("../package.json").version;

// ─── Data home & working directory ──────────────────────────────────────────
//
// DATA HOME (~/.telechat/) — fixed location for all runtime/meta files:
//   .env, bot.log, bot.err, .telechat.pid, bot.db, config.json
// Always used, never prompted. Lets you run `telechat` from anywhere.
//
// WORKING DIRECTORY (CLAUDE_CLI_WORK_DIR in .env) — the directory Claude CLI
// can read/write when answering messages. Separate concept; chosen at init.

const DATA_HOME = path.join(require("os").homedir(), ".telechat");
const CONFIG_FILE = path.join(DATA_HOME, "config.json");
const ENV_FILE = path.join(DATA_HOME, ".env");

function ensureDataHome() {
  if (!existsSync(DATA_HOME)) fs.mkdirSync(DATA_HOME, { recursive: true });
  return DATA_HOME;
}

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_FILE, "utf8"));
  } catch {
    return {};
  }
}

function saveConfig(config) {
  ensureDataHome();
  writeFileSync(CONFIG_FILE, JSON.stringify(config, null, 2) + "\n");
}

// The Claude working directory (what Claude CLI can access)
function getClaudeWorkdir() {
  const config = loadConfig();
  return config.claudeWorkdir || null;
}

function setClaudeWorkdir(dir) {
  const config = loadConfig();
  config.claudeWorkdir = dir;
  saveConfig(config);
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

// Quote a path for the shell (interpreter paths can contain spaces).
function shq(p) {
  return `'${String(p).replace(/'/g, `'\\''`)}'`;
}

function isPython3(cmd) {
  try {
    const v = execSync(`${shq(cmd)} --version 2>&1`, { encoding: "utf8" });
    return v.includes("Python 3");
  } catch {
    return false;
  }
}

function pythonCandidates() {
  return [...new Set([
    "python3",
    "python",
    "/usr/bin/python3",              // macOS system/CLT Python — common home of user-site installs
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
  ])];
}

// Resolve a bare command name ("python3") to an absolute path so the choice
// survives PATH changes between runs. Already-absolute paths pass through.
function absInterpreter(cmd) {
  if (path.isAbsolute(cmd)) return cmd;
  try {
    const real = execSync(
      `${shq(cmd)} -c "import sys; print(sys.executable)"`,
      { encoding: "utf8" }
    ).trim();
    if (real && path.isAbsolute(real)) return real;
  } catch {}
  return cmd;
}

function findPython() {
  // An explicitly pinned / previously validated interpreter wins outright —
  // keeps the service on the same Python (and the same installed backend)
  // across restarts and PATH changes. A stale pin falls through to detection.
  const config = loadConfig();
  if (config.pythonPath && isPython3(config.pythonPath)) return config.pythonPath;
  const cands = pythonCandidates().filter(isPython3);
  if (cands.length === 0) return null;
  // Prefer an interpreter that can already import the backend: the first
  // python3 on PATH frequently is NOT where telechatai was installed (e.g.
  // Homebrew Python vs. a user-site or editable install under another
  // interpreter), and picking it would trigger a needless — often
  // PEP 668-blocked — reinstall.
  const withPkg = cands.find(isPyPkgInstalled);
  const chosen = absInterpreter(withPkg || cands[0]);
  if (withPkg) {
    config.pythonPath = chosen;
    saveConfig(config);
  }
  return chosen;
}

function isPyPkgInstalled(python) {
  try {
    execSync(`${shq(python)} -c "import telechat_pkg"`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

// True if the backend is an editable/source install (pip install -e) — its
// __file__ lives outside site-packages, or is None under a PEP 660 finder.
function isEditableInstall(python) {
  try {
    const out = execSync(
      `${shq(python)} -c "import telechat_pkg; print(telechat_pkg.__file__)"`,
      { encoding: "utf8" }
    ).trim();
    return out === "None" || !out.includes("site-packages");
  } catch {
    return false;
  }
}

function claudeCliInstalled() {
  try {
    execSync("claude --version 2>&1", { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function openUrl(url) {
  try {
    const cmd = process.platform === "darwin" ? "open"
      : process.platform === "win32" ? "start"
      : "xdg-open";
    execSync(`${cmd} "${url}"`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function httpGet(url) {
  return new Promise((resolve) => {
    https.get(url, (res) => {
      let data = "";
      res.on("data", (chunk) => (data += chunk));
      res.on("end", () => {
        try { resolve({ status: res.statusCode, data: JSON.parse(data) }); }
        catch { resolve({ status: res.statusCode, data }); }
      });
    }).on("error", () => resolve(null));
  });
}

// ─── QR code for web chat ─────────────────────────────────────────────────

function getLocalIP() {
  const os = require("os");
  const nets = os.networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of nets[name]) {
      if (net.family === "IPv4" && !net.internal) return net.address;
    }
  }
  return "localhost";
}

function printWebQR(port) {
  // Reject anything that isn't a bare numeric port. The value reaches us from
  // .env / user input, so even though it's "expected to be numeric" we must
  // not splice it into a `python -c` script unvalidated.
  const portStr = String(port);
  if (!/^\d+$/.test(portStr) || Number(portStr) < 1 || Number(portStr) > 65535) {
    console.log(`\n  (skipping QR: invalid WEB_CHAT_PORT="${portStr}")`);
    return;
  }
  const ip = getLocalIP();
  const url = `http://${ip}:${portStr}`;
  try {
    const python = findPython();
    if (!python) { console.log(`\n  Open on phone: ${url}`); return; }
    // Pass the port as argv rather than interpolating into source, so even
    // a future regression in the regex above can't lead to code injection.
    const script =
      "import sys\n" +
      "from telechat_pkg.qr_util import print_web_qr\n" +
      "print_web_qr(sys.argv[1])\n";
    const result = spawnSync(python, ["-c", script, portStr], {
      encoding: "utf8",
      timeout: 15000,
    });
    if (result.status === 0 && result.stdout && result.stdout.trim()) {
      console.log(result.stdout.trimEnd());
    } else {
      console.log(`\n  Open on phone: ${url}`);
    }
  } catch {
    console.log(`\n  Open on phone: ${url}`);
  }
}

function ask(rl, question, fallback) {
  return new Promise((resolve) => {
    rl.question(question, (answer) => {
      resolve(answer.trim() || fallback || "");
    });
  });
}

function pick(rl, question, options) {
  return new Promise((resolve) => {
    console.log(`\n${question}`);
    options.forEach((o, i) => console.log(`  ${i + 1}) ${o.label}${o.hint ? `  — ${o.hint}` : ""}`));
    rl.question(`Choose [1-${options.length}]: `, (answer) => {
      const idx = parseInt(answer, 10) - 1;
      resolve(options[idx >= 0 && idx < options.length ? idx : 0].value);
    });
  });
}

function spin(msg) {
  const frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  let i = 0;
  const id = setInterval(() => {
    process.stdout.write(`\r  ${frames[i++ % frames.length]} ${msg}`);
  }, 80);
  return {
    ok(text) { clearInterval(id); process.stdout.write(`\r  ✓ ${text}\n`); },
    fail(text) { clearInterval(id); process.stdout.write(`\r  ✗ ${text}\n`); },
  };
}

async function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

function setEnvVar(envFile, key, value) {
  if (!existsSync(envFile)) {
    writeFileSync(envFile, `${key}=${value}\n`);
    return;
  }
  const lines = fs.readFileSync(envFile, "utf8").split("\n");
  let found = false;
  for (let i = 0; i < lines.length; i++) {
    const trimmed = lines[i].trim();
    if (trimmed.startsWith("#") || !trimmed.includes("=")) continue;
    const k = trimmed.split("=")[0].trim();
    if (k === key) {
      lines[i] = `${key}=${value}`;
      found = true;
      break;
    }
  }
  if (!found) lines.push(`${key}=${value}`);
  writeFileSync(envFile, lines.join("\n"));
}

// Choose the Claude working directory — the folder Claude CLI can read/write
// when answering messages. Defaults to the current directory.
async function chooseClaudeWorkdir() {
  const current = getClaudeWorkdir();
  const cwd = process.cwd();
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

  console.log(`\n  ── Claude Working Directory ──`);
  console.log(`  The folder Claude can read/write when answering your messages.`);
  console.log(`  (Config, logs, and .env are stored separately in ${DATA_HOME})\n`);

  const dflt = current || cwd;
  const choice = await ask(rl, `  Working directory (Enter = ${dflt}, or type a path): `);
  rl.close();

  const dir = choice ? choice.replace(/^~/, require("os").homedir()) : dflt;
  if (!existsSync(dir)) {
    try { fs.mkdirSync(dir, { recursive: true }); } catch {}
  }
  setClaudeWorkdir(dir);
  console.log(`  ✓ Claude working directory: ${dir}`);
  return dir;
}

// ─── Token validators ────────────────────────────────────────────────────────

async function validateTelegramToken(token) {
  const res = await httpGet(`https://api.telegram.org/bot${token}/getMe`);
  if (!res || res.status !== 200 || !res.data?.ok) return null;
  return res.data.result;
}

async function validateGreenApi(instanceId, token) {
  const url = `https://api.green-api.com/waInstance${instanceId}/getStateInstance/${token}`;
  const res = await httpGet(url);
  if (!res || res.status !== 200) return null;
  return res.data;
}

async function getGreenApiSettings(instanceId, token) {
  const url = `https://api.green-api.com/waInstance${instanceId}/getSettings/${token}`;
  const res = await httpGet(url);
  if (!res || res.status !== 200) return null;
  return res.data;
}

async function waitForTelegramMessage(token, timeoutSec) {
  const deadline = Date.now() + timeoutSec * 1000;
  let offset = 0;
  while (Date.now() < deadline) {
    const res = await httpGet(
      `https://api.telegram.org/bot${token}/getUpdates?offset=${offset}&timeout=3&allowed_updates=["message"]`
    );
    if (res?.data?.ok && res.data.result?.length) {
      for (const u of res.data.result) {
        offset = u.update_id + 1;
        if (u.message?.text === "/start" || u.message?.text) {
          return u.message.from;
        }
      }
    }
    await sleep(1000);
  }
  return null;
}

// ─── Setup wizard ─────────────────────────────────────────────────────────────

async function setup() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

  console.log(`
┌──────────────────────────────────────────────┐
│          telechat ${NPM_VERSION} — setup wizard           │
│   Claude AI bot for Telegram / WhatsApp / Slack   │
└──────────────────────────────────────────────┘
`);

  // ── Check Python ──
  const python = findPython();
  if (!python) {
    console.error("✗ Python 3.10+ not found. Install from https://python.org");
    rl.close();
    process.exit(1);
  }
  const pyVer = execSync(`${python} --version`, { encoding: "utf8" }).trim();
  console.log(`✓ ${pyVer}`);

  // ── Check Claude CLI ──
  const hasClaude = claudeCliInstalled();
  if (hasClaude) {
    console.log("✓ Claude CLI installed");
  } else {
    console.log("⚠ Claude CLI not found (needed for CLI mode)");
    console.log("  Install: npm install -g @anthropic-ai/claude-code");
    console.log("  Then run: claude auth login\n");
  }

  // ── Platform ──
  const platform = await pick(rl, "Which platform(s)?", [
    { label: "Telegram",               value: "telegram",  hint: "easiest to set up" },
    { label: "WhatsApp",               value: "whatsapp",  hint: "via Green API" },
    { label: "Slack",                   value: "slack",     hint: "Socket Mode" },
    { label: "Telegram + WhatsApp",     value: "telegram,whatsapp" },
    { label: "All three",              value: "all" },
  ]);

  const env = { BOT_MODE: platform };
  const platforms = platform === "all" ? ["telegram", "whatsapp", "slack"]
    : platform.split(",").map((s) => s.trim());

  // ── Telegram setup ──
  if (platforms.includes("telegram")) {
    console.log("\n── Telegram setup ──\n");
    console.log("  I'll open BotFather in Telegram where you can create a bot.");
    console.log("  Send /newbot → pick a name → pick a username → copy the token.\n");

    const opened = openUrl("https://t.me/BotFather");
    if (opened) {
      console.log("  ✓ Opened BotFather in your browser\n");
    } else {
      console.log("  Open this link: https://t.me/BotFather\n");
    }

    await ask(rl, "  Press Enter when you have the token...");

    let botInfo = null;
    while (!botInfo) {
      const token = await ask(rl, "  Paste your bot token: ");
      if (!token) {
        console.log("  ⚠ Skipped — set TELEGRAM_BOT_TOKEN in .env later");
        break;
      }

      const s = spin("Validating token...");
      botInfo = await validateTelegramToken(token);
      if (botInfo) {
        s.ok(`Bot verified: @${botInfo.username} (${botInfo.first_name})`);
        env.TELEGRAM_BOT_TOKEN = token;
      } else {
        s.fail("Invalid token. Check and try again (or press Enter to skip).");
      }
    }

    // Auto-detect user ID
    if (env.TELEGRAM_BOT_TOKEN) {
      console.log("\n  -- Access control --");
      console.log("  To restrict the bot to only you, I need your Telegram user ID.");
      const idMethod = await pick(rl, "  How to set access control?", [
        { label: "Auto-detect", hint: "send any message to your new bot" },
        { label: "Enter manually", hint: "if you know your numeric ID" },
        { label: "Skip", hint: "allow anyone to use the bot" },
      ]);

      if (idMethod === "Auto-detect") {
        console.log(`\n  Open your bot: https://t.me/${botInfo.username}`);
        openUrl(`https://t.me/${botInfo.username}`);
        console.log("  Send any message to the bot (e.g. \"hi\").");

        // flush old updates first
        await httpGet(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getUpdates?offset=-1`);

        const s = spin("Waiting for your message (60s)...");
        const sender = await waitForTelegramMessage(env.TELEGRAM_BOT_TOKEN, 60);
        if (sender) {
          s.ok(`Got it! User ID: ${sender.id} (${sender.first_name})`);
          env.TELEGRAM_ALLOWED_USER_IDS = String(sender.id);
        } else {
          s.fail("Timed out. You can set TELEGRAM_ALLOWED_USER_IDS in .env later.");
        }
      } else if (idMethod === "Enter manually") {
        const uid = await ask(rl, "  Your Telegram user ID: ");
        if (uid) env.TELEGRAM_ALLOWED_USER_IDS = uid;
      }
    }
  }

  // ── WhatsApp setup ──
  if (platforms.includes("whatsapp")) {
    console.log("\n── WhatsApp setup ──\n");
    console.log("  WhatsApp uses Green API (free Developer plan).\n");
    console.log("  I'll open the Green API console. Here's what to do:");
    console.log("    1. Sign up with Google/GitHub/email (takes 30 seconds)");
    console.log("    2. You'll see a free instance already created");
    console.log("    3. Scan the QR code with your WhatsApp phone");
    console.log("       (WhatsApp → Settings → Linked Devices → Link a Device)");
    console.log("    4. Copy the idInstance and apiTokenInstance from the dashboard\n");

    const opened = openUrl("https://console.green-api.com");
    if (opened) {
      console.log("  ✓ Opened Green API console in your browser\n");
    } else {
      console.log("  → Open: https://console.green-api.com\n");
    }

    await ask(rl, "  Press Enter when you have the Instance ID and API Token...");

    let validated = false;
    while (!validated) {
      const instanceId = await ask(rl, "  Instance ID (idInstance): ");
      const apiToken = await ask(rl, "  API Token (apiTokenInstance): ");

      if (!instanceId || !apiToken) {
        console.log("  ⚠ Skipped — set GREEN_API_INSTANCE_ID and GREEN_API_TOKEN in .env later");
        break;
      }

      const s = spin("Connecting to WhatsApp...");
      const state = await validateGreenApi(instanceId, apiToken);

      if (!state) {
        s.fail("Invalid credentials. Check and try again (or press Enter to skip).");
        continue;
      }

      env.GREEN_API_INSTANCE_ID = instanceId;
      env.GREEN_API_TOKEN = apiToken;
      validated = true;

      const stateStr = state.stateInstance || "unknown";

      if (stateStr === "authorized") {
        // Fetch phone number automatically
        const settings = await getGreenApiSettings(instanceId, apiToken);
        const phone = settings?.wid ? settings.wid.split("@")[0] : null;

        if (phone) {
          s.ok(`Connected! WhatsApp number: +${phone}`);
          env.WHATSAPP_ALLOWED_NUMBERS = phone;
          console.log(`  ✓ Access control set to your number (+${phone})`);
        } else {
          s.ok("Connected! WhatsApp instance is authorized.");
        }
      } else if (stateStr === "notAuthorized") {
        s.ok("Credentials valid, but WhatsApp not linked yet.");
        console.log("\n  ⚠ You still need to scan the QR code:");
        console.log("    1. Go to the Green API console");
        console.log("    2. Click your instance → scan QR");
        console.log("    3. On your phone: WhatsApp → Settings → Linked Devices → Link a Device");
        console.log("    4. Restart telechat after scanning\n");
      } else {
        s.ok(`Credentials valid (status: ${stateStr}).`);
      }
    }

    // If we didn't auto-detect the phone, ask manually
    if (env.GREEN_API_INSTANCE_ID && !env.WHATSAPP_ALLOWED_NUMBERS) {
      console.log("\n  Who should be allowed to message the bot?");
      console.log("  Tip: send !id to the bot after starting to discover your number.\n");
      const waNum = await ask(rl, "  WhatsApp number(s) to allow (without +, comma-sep, blank=allow all): ");
      if (waNum) env.WHATSAPP_ALLOWED_NUMBERS = waNum.replace(/[\s+\-()]/g, "");
    }
  }

  // ── Slack setup ──
  if (platforms.includes("slack")) {
    console.log("\n── Slack setup (Socket Mode) ──\n");
    console.log("  I'll open the Slack app creation page.\n");
    console.log("  Steps:");
    console.log("    1. Create New App → From scratch");
    console.log("    2. Enable Socket Mode → create App-Level Token (connections:write)");
    console.log("    3. OAuth & Permissions → add bot scopes:");
    console.log("       chat:write, channels:history, im:history, im:write,");
    console.log("       app_mentions:read, reactions:write");
    console.log("    4. Event Subscriptions → subscribe to:");
    console.log("       message.im, message.channels, app_mention");
    console.log("    5. Install to workspace\n");

    const opened = openUrl("https://api.slack.com/apps");
    if (opened) {
      console.log("  ✓ Opened Slack API console in your browser\n");
    } else {
      console.log("  Open: https://api.slack.com/apps\n");
    }

    await ask(rl, "  Press Enter when you have your tokens...");
    env.SLACK_BOT_TOKEN = await ask(rl, "  Slack Bot Token (xoxb-...): ");
    env.SLACK_APP_TOKEN = await ask(rl, "  Slack App Token (xapp-...): ");
    const slackUser = await ask(rl, "  Your Slack member ID (blank=allow all): ");
    if (slackUser) env.SLACK_ALLOWED_USER_IDS = slackUser;
  }

  // ── Claude mode ──
  const claudeMode = await pick(rl, "How should telechat connect to Claude?", hasClaude
    ? [
        { label: "CLI mode", value: "cli", hint: "free with Claude subscription" },
        { label: "API mode", value: "api", hint: "requires ANTHROPIC_API_KEY" },
      ]
    : [
        { label: "API mode", value: "api", hint: "requires ANTHROPIC_API_KEY" },
        { label: "CLI mode", value: "cli", hint: "install Claude CLI first" },
      ]
  );
  env.CLAUDE_MODE = claudeMode;

  if (claudeMode === "api") {
    env.ANTHROPIC_API_KEY = await ask(rl, "Anthropic API key (sk-ant-...): ");
    if (!env.ANTHROPIC_API_KEY) {
      console.log("  ⚠ Skipped — set ANTHROPIC_API_KEY in .env later");
    }
  } else if (!hasClaude) {
    console.log("\n  ⚠ Claude CLI is required for CLI mode.");
    console.log("    npm install -g @anthropic-ai/claude-code && claude auth login\n");
  }

  // ── Claude Desktop bridge (optional) ──
  console.log("\n── Claude Desktop bridge (optional) ──");
  console.log("  Get Telegram notifications and remote control for your running");
  console.log("  Claude Desktop sessions — reply, decide, and approve from your phone.");
  console.log("  Enabling also installs a persistent background service (auto-start + restart).");
  const enableBridge = (await ask(rl, "  Enable Claude Desktop bridge? (y/N): "))
    .toLowerCase() === "y";
  let wantApproval = false;
  if (enableBridge) {
    wantApproval = (await ask(rl,
      "  Require Telegram approval for Bash/Write/Edit tool calls? (y/N): "
    )).toLowerCase() === "y";
    console.log("\n  For headless `claude --resume` to work, the bridge needs a");
    console.log("  long-lived OAuth token. Run this in another Terminal:");
    console.log("    claude setup-token");
    console.log("  Then paste the sk-ant-oat01-... token here (or leave blank to skip):");
    const oauthToken = await ask(rl, "  CLAUDE_CODE_OAUTH_TOKEN: ");
    if (oauthToken) env.CLAUDE_CODE_OAUTH_TOKEN = oauthToken;
    else console.log("  ⚠ Skipped — set CLAUDE_CODE_OAUTH_TOKEN in .env later or replies will fail");
    env._BRIDGE_ENABLE = "1";
    env._BRIDGE_APPROVAL = wantApproval ? "1" : "0";
  }

  // ── Write .env ──
  if (existsSync(ENV_FILE)) {
    const overwrite = await ask(rl, "\n.env already exists. Overwrite? (y/N): ");
    if (overwrite.toLowerCase() !== "y") {
      console.log("  Kept existing .env");
      rl.close();
      return;
    }
  }

  // Strip internal flags before writing .env.
  const bridgeEnable = env._BRIDGE_ENABLE === "1";
  const bridgeApproval = env._BRIDGE_APPROVAL === "1";
  delete env._BRIDGE_ENABLE;
  delete env._BRIDGE_APPROVAL;
  const finalContent = Object.entries(env)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n") + "\n";
  writeFileSync(ENV_FILE, finalContent);
  console.log(`\n✓ Wrote ${ENV_FILE}`);

  if (bridgeEnable) {
    console.log("\n── Installing Claude Desktop bridge ──");
    if (!isPyPkgInstalled(python)) installPyPkg(python);
    const bridgeArgs = ["-m", "telechat_pkg.main", "bridge", "install"];
    if (bridgeApproval) bridgeArgs.push("--approval");
    const r = spawnSync(python, bridgeArgs, { stdio: "inherit", env: process.env });
    if (r.status === 0) {
      console.log("  ✓ Bridge + persistent service installed. The bot is now running;");
      console.log("    open Telegram and send /desktop to see your sessions.");
    } else {
      console.log("  ⚠ Bridge install failed — run `telechat bridge install` manually");
    }
  }

  rl.close();
}

// ─── Install Python package ──────────────────────────────────────────────────

function installPyPkg(python) {
  console.log(`Installing ${PYPI_PACKAGE} from PyPI...`);
  try {
    // Capture stderr so PEP 668 rejections can be detected; stdout still
    // streams pip's progress to the user.
    execSync(`${shq(python)} -m pip install --upgrade ${PYPI_PACKAGE}`, {
      stdio: ["ignore", "inherit", "pipe"],
      encoding: "utf8",
    });
    return true;
  } catch (e) {
    const errOut = `${e.stderr || ""}${e.stdout || ""}`;
    if (errOut.includes("externally-managed-environment")) {
      console.error(
        `\n  ✗ ${python} refuses pip installs (PEP 668: externally-managed-environment).\n\n` +
        `  Options:\n` +
        `    • If ${PYPI_PACKAGE} is already installed under another interpreter,\n` +
        `      point telechat at it:\n` +
        `        telechat python /path/to/that/python3\n` +
        `    • Install with pipx (isolated, recommended):\n` +
        `        brew install pipx && pipx install ${PYPI_PACKAGE}\n` +
        `        telechat python ~/.local/pipx/venvs/${PYPI_PACKAGE}/bin/python\n` +
        `    • Or use a virtual environment:\n` +
        `        python3 -m venv ~/.telechat/venv\n` +
        `        ~/.telechat/venv/bin/pip install ${PYPI_PACKAGE}\n` +
        `        telechat python ~/.telechat/venv/bin/python`
      );
    } else {
      if (e.stderr) process.stderr.write(e.stderr);
      console.error(
        `\nFailed to install from PyPI. You can install manually:\n` +
        `  ${python} -m pip install ${PYPI_PACKAGE}\n\n` +
        `Or clone and run from source:\n` +
        `  git clone https://github.com/telechatai/telechat.git\n` +
        `  cd telechat && ./scripts/install.sh && ./scripts/start.sh`
      );
    }
    return false;
  }
}

// ─── Health checks ───────────────────────────────────────────────────────────

function checkAndFixIssues() {
  console.log(`
┌─────────────────────────────────────────────────┐
│       telechat ${NPM_VERSION} — checking environment…       │
└─────────────────────────────────────────────────┘
`);

  let issues = 0;

  // 1. Python check
  const python = findPython();
  if (!python) {
    console.log("  ✗ Python 3.10+ not found");
    console.log("    Fix: install from https://python.org/downloads\n");
    process.exit(1);
  }
  const pyVer = execSync(`${python} --version`, { encoding: "utf8" }).trim();
  console.log(`  ✓ ${pyVer}`);

  // 2. Install/update Python package
  if (!isPyPkgInstalled(python)) {
    const s = spin("Installing Python backend...");
    if (installPyPkg(python)) {
      s.ok("Python backend installed");
    } else {
      s.fail("Could not install Python backend");
      console.log(`    Fix: ${python} -m pip install ${PYPI_PACKAGE}\n`);
      issues++;
    }
  } else if (isEditableInstall(python)) {
    // Never auto-upgrade an editable/source install — pip would replace the
    // dev tree with the (possibly older) PyPI release.
    console.log("  ✓ Python backend installed (editable — skipping auto-upgrade)");
  } else {
    // Silently upgrade
    try {
      execSync(`${shq(python)} -m pip install --upgrade ${PYPI_PACKAGE}`, { stdio: "ignore" });
    } catch {}
    console.log("  ✓ Python backend up to date");
  }

  // 3. Check if telechat is globally accessible
  try {
    execSync("telechat --version", { stdio: "ignore" });
    console.log("  ✓ telechat command available globally");
  } catch {
    // Not in PATH — fix it
    console.log("  ⚠ telechat not in PATH — installing globally...");
    try {
      execSync("npm install -g telechat", { stdio: "ignore" });
      console.log("  ✓ Fixed: telechat installed globally");
    } catch {
      // Try to fix npm prefix to match current node
      const nodeBin = path.dirname(process.execPath);
      try {
        execSync(`npm config set prefix "${path.dirname(nodeBin)}"`, { stdio: "ignore" });
        execSync("npm install -g telechat", { stdio: "ignore" });
        console.log("  ✓ Fixed: npm prefix corrected, telechat installed");
      } catch {
        console.log("  ⚠ Could not auto-fix. After setup, run:");
        console.log(`    export PATH="${nodeBin}:$PATH"`);
        issues++;
      }
    }
  }

  // 4. Claude CLI check
  if (claudeCliInstalled()) {
    console.log("  ✓ Claude CLI available");
  } else {
    console.log("  ℹ Claude CLI not found (optional — needed for free CLI mode)");
  }

  console.log("");
  return { python, issues };
}

// ─── Service management ──────────────────────────────────────────────────────

const PID_FILE = path.join(DATA_HOME, ".telechat.pid");
const LOG_FILE = path.join(DATA_HOME, "bot.log");
const ERR_FILE = path.join(DATA_HOME, "bot.err");

function getRunningPid() {
  try {
    const pid = parseInt(require("fs").readFileSync(PID_FILE, "utf8").trim(), 10);
    // Check if process is actually running
    process.kill(pid, 0);
    return pid;
  } catch {
    // Clean up stale pid file
    try { require("fs").unlinkSync(PID_FILE); } catch {}
    return null;
  }
}

function stopService() {
  const pid = getRunningPid();
  if (!pid) {
    console.log("  telechat is not running.");
    return false;
  }
  try {
    process.kill(pid, "SIGTERM");
    // Wait briefly for it to die
    for (let i = 0; i < 10; i++) {
      try { process.kill(pid, 0); } catch { break; }
      execSync("sleep 0.3", { stdio: "ignore" });
    }
    try { require("fs").unlinkSync(PID_FILE); } catch {}
    console.log(`  ✓ telechat stopped (PID ${pid})`);
    return true;
  } catch {
    console.log(`  ✗ Failed to stop PID ${pid}`);
    return false;
  }
}

function startService(python, debug) {
  ensureDataHome();
  const out = require("fs").openSync(LOG_FILE, "a");
  const err = require("fs").openSync(ERR_FILE, "a");
  const env = { ...process.env };
  if (debug) env.TELECHAT_DEBUG = "1";
  // Tell the Python backend where the data home is so it loads the right .env
  env.TELECHAT_HOME = DATA_HOME;

  // Run from DATA_HOME so bot.log/bot.db/.env all resolve there
  const child = spawn(python, ["-m", "telechat_pkg.main", "start"], {
    detached: true,
    stdio: ["ignore", out, err],
    cwd: DATA_HOME,
    env,
  });

  child.unref();
  writeFileSync(PID_FILE, String(child.pid));
  return child.pid;
}

// ─── Tips ────────────────────────────────────────────────────────────────────

function printTips() {
  const hasClaude = claudeCliInstalled();
  console.log(`
  Commands:

    telechat [start]      Start bot as background service
    telechat stop         Stop the bot
    telechat restart      Restart the bot
    telechat status       Show status, uptime, health
    telechat logs         Tail the bot log
    telechat env          Show config (tokens masked)
    telechat clean        Remove .env (clear credentials)
    telechat init         Claude-assisted setup (recommended)
    telechat setup        Manual setup wizard
    telechat workdir      Show/set Claude working directory
    telechat python       Show/pin the backend Python interpreter
    telechat update       Update to latest version

  Tips:

    • Bot runs in background — closing the terminal won't stop it
    • Config & data live in ~/.telechat/ (.env, logs, bot.db)
    • Run telechat from any directory — it always finds ~/.telechat
    • Edit config: telechat env, then telechat restart${!hasClaude ? `

  Free Claude access (no API key needed):
    npm i -g @anthropic-ai/claude-code && claude auth login
    Then: telechat init → choose CLI mode` : ""}

  Docs: https://github.com/telechatai/telechat
`);
}

// ─── Post-init validation ───────────────────────────────────────────────────

async function postInitValidation(envFile) {
  if (!existsSync(envFile)) return;

  const content = fs.readFileSync(envFile, "utf8");
  const vars = {};
  for (const line of content.split("\n")) {
    if (!line.trim() || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq > 0) vars[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
  }

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  let changed = false;

  // WhatsApp: ensure WHATSAPP_ALLOWED_NUMBERS is set
  if (vars.GREEN_API_INSTANCE_ID && vars.GREEN_API_TOKEN && !vars.WHATSAPP_ALLOWED_NUMBERS) {
    console.log("\n── WhatsApp access control ──\n");

    // Try auto-detect phone from Green API
    let autoNumber = null;
    try {
      const settings = await getGreenApiSettings(vars.GREEN_API_INSTANCE_ID, vars.GREEN_API_TOKEN);
      if (settings?.wid) autoNumber = settings.wid.split("@")[0];
    } catch {}

    let nums = "";
    if (autoNumber) {
      nums = await ask(rl, `  Your WhatsApp number: +${autoNumber}\n  Restrict bot to this number? (Y/n): `, "y");
      if (nums.toLowerCase() === "y" || nums === "") nums = autoNumber;
      else if (nums.toLowerCase() === "n") nums = "";
    } else {
      nums = await ask(rl, "  WhatsApp number(s) to allow (without +, comma-sep, Enter=allow all): ");
    }

    if (nums) {
      const clean = nums.replace(/[\s+\-()]/g, "");
      setEnvVar(envFile, "WHATSAPP_ALLOWED_NUMBERS", clean);
      console.log(`  ✓ WHATSAPP_ALLOWED_NUMBERS=${clean}`);
      changed = true;
    } else {
      console.log("  → Allowing all numbers.");
    }
  }

  // Telegram: ensure TELEGRAM_ALLOWED_USER_IDS is set
  if (vars.TELEGRAM_BOT_TOKEN && !vars.TELEGRAM_ALLOWED_USER_IDS) {
    console.log("\n── Telegram access control ──\n");
    console.log("  ⚠ No user restriction set — anyone can message your bot.");
    const uid = await ask(rl, "  Your Telegram user ID (Enter=allow all, or send /id to the bot to find it): ");
    if (uid) {
      setEnvVar(envFile, "TELEGRAM_ALLOWED_USER_IDS", uid.trim());
      console.log(`  ✓ TELEGRAM_ALLOWED_USER_IDS=${uid.trim()}`);
      changed = true;
    }
  }

  // Slack: ensure SLACK_ALLOWED_USER_IDS is set
  if (vars.SLACK_BOT_TOKEN && !vars.SLACK_ALLOWED_USER_IDS) {
    console.log("\n── Slack access control ──\n");
    console.log("  ⚠ No user restriction set — anyone in the workspace can use the bot.");
    const sid = await ask(rl, "  Your Slack member ID (Enter=allow all): ");
    if (sid) {
      setEnvVar(envFile, "SLACK_ALLOWED_USER_IDS", sid.trim());
      console.log(`  ✓ SLACK_ALLOWED_USER_IDS=${sid.trim()}`);
      changed = true;
    }
  }

  rl.close();
  if (changed) console.log("");
}


// ─── Main ────────────────────────────────────────────────────────────────────

async function main() {
  const args = process.argv.slice(2);
  const debug = args.includes("--debug") || args.includes("-d");
  const cmd = args.find((a) => !a.startsWith("-"));

  if (args.includes("--help") || args.includes("-h")) {
    console.log(`telechat ${NPM_VERSION} — Claude AI messenger bot (Telegram, WhatsApp, Slack)

Usage:
  telechat [start]       Start bot (program-assisted, no Claude needed)
  telechat stop          Stop the bot
  telechat restart       Restart the bot
  telechat status        Show status, uptime, health, paths
  telechat logs          Tail the bot log
  telechat env           Show config (tokens masked)
  telechat clean         Remove .env (clear credentials)
  telechat init          Claude-assisted setup (opens browser, validates)
  telechat setup         Manual setup wizard (no Claude needed)
  telechat workdir       Show/set Claude working directory
  telechat python        Show/pin the backend Python interpreter (python <path>|clear)
  telechat bridge        Manage Claude Desktop bridge (install/uninstall/status)
  telechat update        Update to latest version
  telechat --debug       Start with verbose logging
  telechat --version     Show version

  Data home: ~/.telechat/  (.env, logs, bot.db, config)
  Run telechat from any directory.

Docs: https://github.com/telechatai/telechat`);
    process.exit(0);
  }

  if (args.includes("--version") || args.includes("-v")) {
    console.log(`telechat ${NPM_VERSION}`);
    process.exit(0);
  }

  // ── Workdir (Claude working directory) ──
  if (cmd === "workdir" || cmd === "dir") {
    const subcmd = args.find((a) => !a.startsWith("-") && a !== cmd);
    if (subcmd) {
      const dir = subcmd.replace(/^~/, require("os").homedir());
      if (!existsSync(dir)) {
        try { fs.mkdirSync(dir, { recursive: true }); } catch {}
      }
      setClaudeWorkdir(dir);
      // Also update CLAUDE_CLI_WORK_DIR in .env if it exists
      if (existsSync(ENV_FILE)) setEnvVar(ENV_FILE, "CLAUDE_CLI_WORK_DIR", dir);
      console.log(`  ✓ Claude working directory set to: ${dir}`);
      if (getRunningPid()) console.log(`  Run 'telechat restart' to apply.`);
    } else {
      const wd = getClaudeWorkdir();
      console.log(`  Data home  : ${DATA_HOME}  (.env, logs, db, config)`);
      if (wd) {
        console.log(`  Claude dir : ${wd}  (what Claude can access)`);
        console.log(`  Change     : telechat workdir /new/path`);
      } else {
        console.log(`  Claude dir : not set (run 'telechat init')`);
      }
    }
    process.exit(0);
  }

  // ── Env / Clean ──
  if (cmd === "clean" || cmd === "clear" || cmd === "reset") {
    // Top-level alias: telechat clean → telechat env clean
    if (!existsSync(ENV_FILE)) {
      console.log("  No .env file found — nothing to clean.");
      process.exit(0);
    }
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    const confirm = await ask(rl, "  Delete .env file? This removes all credentials. (y/N): ");
    rl.close();
    if (confirm.toLowerCase() === "y") {
      require("fs").unlinkSync(ENV_FILE);
      console.log("  ✓ .env deleted. Run 'telechat setup' to reconfigure.");
      if (getRunningPid()) {
        console.log("  ⚠ Bot is still running with old config. Run 'telechat stop' to stop it.");
      }
    } else {
      console.log("  Cancelled.");
    }
    process.exit(0);
  }

  if (cmd === "env") {
    const subcmd = args.find((a) => !a.startsWith("-") && a !== "env");
    if (subcmd === "clean" || subcmd === "clear" || subcmd === "reset") {
      if (!existsSync(ENV_FILE)) {
        console.log("  No .env file found — nothing to clean.");
        process.exit(0);
      }
      const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
      const confirm = await ask(rl, "  Delete .env file? This removes all credentials. (y/N): ");
      rl.close();
      if (confirm.toLowerCase() === "y") {
        require("fs").unlinkSync(ENV_FILE);
        console.log("  ✓ .env deleted. Run 'telechat setup' to reconfigure.");
        if (getRunningPid()) {
          console.log("  ⚠ Bot is still running with old config. Run 'telechat stop' to stop it.");
        }
      } else {
        console.log("  Cancelled.");
      }
      process.exit(0);
    }
    // Default: show env
    if (!existsSync(ENV_FILE)) {
      console.log("  No .env file found. Run 'telechat setup' to create one.");
      process.exit(0);
    }
    const content = require("fs").readFileSync(ENV_FILE, "utf8");
    console.log("\n  .env contents:\n");
    for (const line of content.split("\n")) {
      if (!line.trim() || line.startsWith("#")) {
        if (line.trim()) console.log(`  ${line}`);
        continue;
      }
      const eq = line.indexOf("=");
      if (eq === -1) { console.log(`  ${line}`); continue; }
      const key = line.slice(0, eq);
      const val = line.slice(eq + 1);
      // Mask sensitive values
      const sensitive = /TOKEN|KEY|SECRET|PASSWORD/i.test(key);
      const display = sensitive && val.length > 8
        ? val.slice(0, 4) + "…" + val.slice(-4)
        : val;
      console.log(`  ${key}=${display}`);
    }
    console.log("");
    process.exit(0);
  }

  // ── Init (Claude-guided setup) ──
  if (cmd === "init") {
    if (!claudeCliInstalled()) {
      console.error("  ✗ Claude CLI required. Install: npm i -g @anthropic-ai/claude-code && claude auth login");
      process.exit(1);
    }

    // Pre-flight: Python check
    const python = findPython();
    if (!python) {
      console.error("  ✗ Python 3.10+ required. Install from https://python.org/downloads");
      process.exit(1);
    }
    const pyVer = execSync(`${python} --version`, { encoding: "utf8" }).trim();
    const pyMinor = parseInt(pyVer.match(/3\.(\d+)/)?.[1] || "0", 10);
    if (pyMinor < 10) {
      console.error(`  ✗ ${pyVer} found but Python 3.10+ is required.`);
      process.exit(1);
    }
    console.log(`  ✓ ${pyVer}`);

    // Pre-flight: install/upgrade Python backend
    if (!isPyPkgInstalled(python)) {
      const s = spin("Installing Python backend...");
      if (installPyPkg(python)) {
        s.ok("Python backend installed");
      } else {
        s.fail("Could not install Python backend");
        console.error(`    Fix: ${python} -m pip install ${PYPI_PACKAGE}`);
        process.exit(1);
      }
    } else {
      try { execSync(`${python} -m pip install --upgrade ${PYPI_PACKAGE}`, { stdio: "ignore" }); } catch {}
      console.log("  ✓ Python backend up to date");
    }

    // Pre-flight: stop running bot if any
    const runningPid = getRunningPid();
    if (runningPid) {
      console.log(`  ⚠ telechat is running (PID ${runningPid}) — stopping for reconfiguration...`);
      stopService();
      await sleep(500);
    }

    // Data home holds .env/logs/db; ask only for the Claude working directory
    ensureDataHome();
    const claudeWorkdir = await chooseClaudeWorkdir();

    const envFile = ENV_FILE;
    const existingEnv = existsSync(envFile) ? fs.readFileSync(envFile, "utf8") : null;

    const systemPrompt = `You are telechat's setup agent. Configure ${envFile} silently and autonomously.
Data home: ${DATA_HOME} (this is where .env, logs, and the database live)
Claude working directory (CLAUDE_CLI_WORK_DIR): ${claudeWorkdir}

${existingEnv ? `Current .env:\n${existingEnv}\nPreserve existing values.` : "No .env exists. Create from scratch."}

BEHAVIOR:
- Output ONLY when you need user input or to confirm success.
- Go through ALL platforms: Telegram → WhatsApp → Slack. User says "skip" to skip one.
- Validate every token/credential via curl before saving.
- Use Bash tool to run "open <url>" to open URLs in the user's default browser.
- Use Bash tool with curl to validate tokens/credentials.
- Read and write files directly with your tools.
- IMPORTANT: If a platform is ALREADY configured in .env (token exists), validate it silently with curl first. If valid, print "✓ [Platform] already configured: @username / +number / team-name. Keep it? (yes/reconfigure)" and WAIT for user response. If user says yes/Enter, skip to the next platform. If user says reconfigure, proceed with full setup. Do NOT open any URLs or run setup steps for already-configured platforms unless the user says reconfigure.
- NEVER open URLs or run bash commands without telling the user what you're doing first.
- Between platforms, print a short separator like "── WhatsApp ──" to keep things organized.

FLOW:

1. TELEGRAM
   First check: if TELEGRAM_BOT_TOKEN exists in .env, validate it with curl. If valid, print:
   "── Telegram ──
    ✓ Already configured: @bot_username (Bot Name)
    Keep current config? (Enter = keep, 'reconfigure' = set up fresh)"
   Wait for response. If Enter/yes/keep, skip to WhatsApp. If reconfigure, continue below.

   If NOT configured or user wants to reconfigure:
   Print: "── Telegram ──"

   Step A — Login:
   - Telegram Web is already open (Node opened it before you started).
   - Print these exact instructions:
     "Telegram Web is open in your browser. To log in:
      1. Open Telegram on your phone
      2. Go to Settings → Devices → Link Desktop Device
      3. Point your phone camera at the QR code on screen
      Once logged in, type 'done' here."
   - Wait for user to type done/ok/yes.

   Step B — Create bot:
   - Run bash: open "https://web.telegram.org/k/#@BotFather"
   - Print exactly:
     "BotFather opened in Telegram Web.
      1. Type /newbot and send it
      2. Enter a display name for your bot (e.g. 'My Claude Bot')
      3. Enter a username ending in 'bot' (e.g. 'my_claude_bot')
      4. BotFather will reply with a token like: 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw
      5. Copy the FULL token and paste it here:"
   - Wait for user to paste token.
   - Validate: curl -s "https://api.telegram.org/bot<TOKEN>/getMe"
   - If invalid, say "✗ Invalid token. Make sure you copied the full token from BotFather (including the colon). Try again:" and re-ask.
   - If valid, print: "✓ Bot verified: @username" and save TELEGRAM_BOT_TOKEN.

   Step C — Get user ID:
   - Run bash: open "https://web.telegram.org/k/#@userinfobot"
   - Print exactly:
     "userinfobot opened in Telegram Web.
      1. Send any message to it (e.g. 'hi')
      2. It will reply with your info including 'Id: 123456789'
      3. Copy ONLY the numeric ID and paste it here
      (or press Enter to allow all users to use the bot):"
   - Save TELEGRAM_ALLOWED_USER_IDS.

2. WHATSAPP
   First check: if GREEN_API_INSTANCE_ID and GREEN_API_TOKEN exist in .env, validate with curl. If valid, print:
   "── WhatsApp ──
    ✓ Already configured: instance <id>, status: <stateInstance>
    Keep current config? (Enter = keep, 'reconfigure' = set up fresh)"
   Wait for response. If Enter/yes/keep, skip to Slack. If reconfigure, continue below.

   If NOT configured or user wants to reconfigure:
   Print: "── WhatsApp ──"
   - Run bash: open "https://console.green-api.com"
   - Print exactly:
     "Green API console opened.
      1. Sign up with Google/GitHub/email (free Developer plan)
      2. After login you'll see your instance dashboard
      3. Find idInstance (a number like 7107621928) — shown at the top of your instance
      4. Find apiTokenInstance (a long hex string) — shown below the idInstance
      5. To link your WhatsApp phone:
         • Click your instance → look for the QR code section
         • On your phone: open WhatsApp → Settings → Linked Devices → Link a Device
         • Scan the QR code with your phone camera
      Paste your idInstance (or type 'skip'):"
   - Then ask: "Paste your apiTokenInstance:"
   - Validate: curl -s "https://api.green-api.com/waInstance{id}/getStateInstance/{token}"
   - If invalid, say "✗ Invalid credentials. Double-check both values from the Green API dashboard. Try again." and re-ask both.
   - Save GREEN_API_INSTANCE_ID, GREEN_API_TOKEN.
   - Try to get phone: curl -s "https://api.green-api.com/waInstance{id}/getSettings/{token}" and extract wid field (format: "14155953988@c.us" → extract number before @).
   - If got phone, save WHATSAPP_ALLOWED_NUMBERS and print "✓ WhatsApp linked to +<number>". Otherwise ask: "Enter your WhatsApp number without + (e.g. 919876543210), or press Enter to allow all:"

3. SLACK
   First check: if SLACK_BOT_TOKEN exists in .env, validate with curl (auth.test). If valid, print:
   "── Slack ──
    ✓ Already configured: team <team>, bot user <user>
    Keep current config? (Enter = keep, 'reconfigure' = set up fresh)"
   Wait for response. If Enter/yes/keep, skip to Finalize. If reconfigure, continue below.

   If NOT configured or user wants to reconfigure:
   Print: "── Slack ──"
   - Run bash: open "https://api.slack.com/apps"
   - Print exactly:
     "Slack API console opened. Follow these steps:

      Step 1 — Create App:
      • Click 'Create New App' → 'From scratch'
      • Enter a name (e.g. 'TeleChat') and select your workspace

      Step 2 — Enable Socket Mode:
      • Left sidebar → 'Socket Mode' → toggle ON
      • Create an App-Level Token: name it 'telechat', add scope 'connections:write'
      • Copy the token (starts with xapp-...) — you'll need this

      Step 3 — Bot Permissions:
      • Left sidebar → 'OAuth & Permissions'
      • Scroll to 'Scopes' → 'Bot Token Scopes' → add these:
        chat:write, channels:history, im:history, im:write, app_mentions:read, reactions:write

      Step 4 — Event Subscriptions:
      • Left sidebar → 'Event Subscriptions' → toggle ON
      • Under 'Subscribe to bot events' add: message.im, message.channels, app_mention

      Step 5 — Install:
      • Left sidebar → 'Install App' → 'Install to Workspace' → Allow

      Step 6 — Copy Bot Token:
      • After install, go to 'OAuth & Permissions'
      • Copy 'Bot User OAuth Token' (starts with xoxb-...)
      • ⚠ NOT the 'User OAuth Token' — that one starts with xoxp- and won't work

      Paste your Bot Token (xoxb-...) or type 'skip':"
   - If user pastes a token NOT starting with xoxb-, say: "✗ That's not a bot token. Go to OAuth & Permissions → copy 'Bot User OAuth Token' (starts with xoxb-...). The 'User OAuth Token' (xoxp-/xoxe-) won't work. Try again:"
   - Validate: curl -s -H "Authorization: Bearer xoxb-..." "https://slack.com/api/auth.test"
   - If valid, print "✓ Slack bot verified: team <team>, user <bot_user>"
   - Then ask: "Paste your App-Level Token (xapp-...):"
   - Save SLACK_BOT_TOKEN, SLACK_APP_TOKEN.
   - Ask: "Paste your Slack member ID to restrict access (find it: click your profile pic → 'Profile' → ⋮ → 'Copy member ID'), or press Enter to allow all:"
   - Save SLACK_ALLOWED_USER_IDS.

4. OPTIONAL FEATURES
   Print: "── Optional Features ──"
   Print: "telechat supports several optional features. Each needs an API key."
   Print: "You can skip all of these and add them later by editing ~/.telechat/.env"
   Print: ""

   Ask: "Enable any optional features? (Enter = skip all, or pick numbers)"
   Print:
     "  1) Voice messages — transcribe voice/audio via OpenAI Whisper
      2) Text-to-speech — /tts command to generate spoken audio (OpenAI)
      3) Image generation — /image command via DALL-E 3 (OpenAI)
      4) Web search — /search command via Brave or Tavily
      5) Web fetch — /fetch extracts readable content from URLs (Jina)
      6) Music generation — /music command via Replicate (MusicGen)
      7) Video generation — /video command via Replicate (Luma/Minimax)
      Enter numbers (e.g. '1,2,3') or press Enter to skip:"

   Wait for user response. If they press Enter or say skip/none, skip to Finalize.

   If they chose any feature that needs OpenAI (1, 2, or 3):
   - Check if OPENAI_API_KEY is already set in .env. If so, validate it silently.
   - If not set, ask: "Paste your OpenAI API key (sk-...):"
   - Save OPENAI_API_KEY.
   - For feature 1: set TRANSCRIPTION_ENABLED=true
   - For feature 2: set TTS_ENABLED=true
   - For feature 3: set IMAGE_GEN_ENABLED=true

   If they chose web search (4):
   - Ask: "Which search provider?
       1) Brave Search (free: 2000 queries/month) — https://api.search.brave.com
       2) Tavily (free: 1000 queries/month) — https://tavily.com
       Choose (1/2):"
   - Ask for the chosen API key.
   - Set WEB_SEARCH_ENABLED=true and save the key (BRAVE_SEARCH_API_KEY or TAVILY_API_KEY).

   If they chose web fetch (5):
   - Set WEB_FETCH_ENABLED=true.
   - Ask: "Jina Reader API key (optional, free 1M tokens/month — https://jina.ai/reader):"
   - If provided, save JINA_API_KEY.

   If they chose any Replicate feature (6 or 7):
   - Check if REPLICATE_API_TOKEN is already set. If not:
   - Ask: "Paste your Replicate API token — https://replicate.com :"
   - Save REPLICATE_API_TOKEN.
   - For feature 6: set MUSIC_GEN_ENABLED=true
   - For feature 7: set VIDEO_GEN_ENABLED=true

5. FINALIZE
   Print: "── Finalize ──"
   - If CLAUDE_MODE is already set in .env, keep it. Otherwise ask:
     "How should telechat connect to Claude?
      1. CLI mode (free — uses your Claude CLI subscription)
      2. API mode (pay-per-token — needs ANTHROPIC_API_KEY)
      Choose (1/2, default=1):"
   - If API mode, ask for ANTHROPIC_API_KEY.
   - Set BOT_MODE based on which platforms have tokens configured. Examples:
     - Only Telegram → BOT_MODE=telegram
     - Telegram + WhatsApp → BOT_MODE=telegram,whatsapp
     - All three → BOT_MODE=all
   - Add these defaults if not already present:
     CLAUDE_CLI_WORK_DIR=${claudeWorkdir}
     CLAUDE_CLI_ADD_DIRS=${claudeWorkdir}
     CLAUDE_CLI_PERMISSION_MODE=bypassPermissions
     CLAUDE_TIMEOUT=300
   - Write .env file to ${envFile} (the data home, NOT the working directory).

   IMPORTANT: Before printing the summary, read back the .env file you just wrote
   and verify that at least one platform has valid credentials (TELEGRAM_BOT_TOKEN,
   GREEN_API_INSTANCE_ID+GREEN_API_TOKEN, or SLACK_BOT_TOKEN+SLACK_APP_TOKEN).
   If no platform is configured, warn the user and do NOT run telechat start.

   - Print a clean summary:
     "
     ── Setup Complete ──
     Telegram : ✓ @bot_username (or ── skipped)
     WhatsApp : ✓ +14155953988 (or ── skipped)
     Slack    : ✓ team-name (or ── skipped)
     Claude   : CLI mode (or API mode)
     BOT_MODE : telegram,whatsapp (whatever was configured)
     Features : voice, tts, image gen (or ── none)

     Starting bot..."
   - Then run bash: telechat start
   - This will start the bot and show the startup summary with security warnings.`;

    // Only open Telegram Web if not already configured
    if (!existingEnv || !existingEnv.includes("TELEGRAM_BOT_TOKEN=")) {
      openUrl("https://web.telegram.org/k/");
    }

    const child = spawn("claude", [
      "--system-prompt", systemPrompt,
      "--verbose", "0",
      existingEnv
        ? "Check existing config. For each platform with valid credentials, ask the user if they want to keep or reconfigure. For unconfigured platforms, run the full setup. At the end, ask about Claude mode if not set."
        : "Start Telegram setup. Telegram Web is already open in the browser for QR code login. Print the login instructions and wait for the user to confirm.",
    ], {
      stdio: "inherit",
      cwd: process.cwd(),
    });

    child.on("exit", async (code) => {
      if (code !== 0) {
        console.log(`\n  ⚠ Claude CLI exited with code ${code || "unknown"}.`);
        if (!existsSync(envFile)) {
          console.log("  No .env was created. You can try again:");
          console.log("    telechat init     (AI-guided — requires Claude CLI)");
          console.log("    telechat setup    (manual wizard — no Claude needed)\n");
        } else {
          console.log("  Your .env file may be partially configured.");
          console.log("  Options:");
          console.log("    telechat init     Re-run setup (keeps existing values)");
          console.log("    telechat setup    Manual wizard");
          console.log("    telechat env      Review current config\n");
        }
        return process.exit(code || 1);
      }

      // Post-init validation: catch anything Claude CLI missed
      await postInitValidation(envFile);

      // Final validation: ensure at least one platform is configured
      if (existsSync(envFile)) {
        const finalEnv = fs.readFileSync(envFile, "utf8");
        const hasTg = /TELEGRAM_BOT_TOKEN=.+/.test(finalEnv)
          && !/TELEGRAM_BOT_TOKEN=your_/.test(finalEnv);
        const hasWa = /GREEN_API_INSTANCE_ID=.+/.test(finalEnv)
          && /GREEN_API_TOKEN=.+/.test(finalEnv);
        const hasSl = /SLACK_BOT_TOKEN=xoxb-.+/.test(finalEnv)
          && /SLACK_APP_TOKEN=xapp-.+/.test(finalEnv);

        if (!hasTg && !hasWa && !hasSl) {
          console.log("\n  ⚠ No platform credentials found in .env.");
          console.log("  The bot needs at least one of: Telegram, WhatsApp, or Slack.");
          console.log("  Run 'telechat init' again or edit ~/.telechat/.env manually.\n");
          process.exit(1);
        }

        // Warn about missing access control
        const warnings = [];
        if (hasTg && !/TELEGRAM_ALLOWED_USER_IDS=\d/.test(finalEnv))
          warnings.push("Telegram: no user restriction set (anyone can use your bot)");
        if (hasWa && !/WHATSAPP_ALLOWED_NUMBERS=\d/.test(finalEnv))
          warnings.push("WhatsApp: no number restriction set (anyone can message your bot)");
        if (hasSl && !/SLACK_ALLOWED_USER_IDS=\w/.test(finalEnv))
          warnings.push("Slack: no user restriction set (anyone in workspace can use the bot)");

        if (warnings.length) {
          console.log("\n  ⚠ Security warnings:");
          for (const w of warnings) console.log(`    • ${w}`);
          console.log("    Fix: telechat init → reconfigure, or edit ~/.telechat/.env\n");
        }
      }

      process.exit(0);
    });
    return;
  }

  // ── Stop ──
  if (cmd === "stop") {
    stopService();
    process.exit(0);
  }

  // ── Status ──
  if (cmd === "status") {
    const pid = getRunningPid();
    const claudeWd = getClaudeWorkdir();

    console.log("");
    console.log(`  Data home   : ${DATA_HOME}`);
    console.log(`  Status      : ${pid ? `✓ running (PID ${pid})` : "✗ not running"}`);

    if (existsSync(ENV_FILE)) {
      const envContent = fs.readFileSync(ENV_FILE, "utf8");
      const platforms = [];
      if (envContent.includes("TELEGRAM_BOT_TOKEN=")) platforms.push("Telegram");
      if (envContent.includes("GREEN_API_INSTANCE_ID=")) platforms.push("WhatsApp");
      if (envContent.includes("SLACK_BOT_TOKEN=")) platforms.push("Slack");
      if (platforms.length) console.log(`  Platforms   : ${platforms.join(", ")}`);

      const modeMatch = envContent.match(/CLAUDE_MODE=(\w+)/);
      if (modeMatch) console.log(`  Claude      : ${modeMatch[1]} mode`);
      const wdMatch = envContent.match(/CLAUDE_CLI_WORK_DIR=(.+)/);
      if (wdMatch) console.log(`  Claude dir  : ${wdMatch[1].trim()}`);
    } else if (claudeWd) {
      console.log(`  Claude dir  : ${claudeWd}`);
    }

    if (pid) {
      // Try health check
      try {
        const health = execSync("curl -s http://localhost:8484/health", { encoding: "utf8", timeout: 3000 });
        const h = JSON.parse(health);
        if (h.uptime_seconds) {
          const hrs = Math.floor(h.uptime_seconds / 3600);
          const mins = Math.floor((h.uptime_seconds % 3600) / 60);
          console.log(`  Uptime      : ${hrs}h ${mins}m`);
        }
        if (h.components) {
          const statuses = [];
          for (const [name, info] of Object.entries(h.components)) {
            if (info.healthy !== undefined) {
              statuses.push(`${name}: ${info.healthy ? "✓" : "✗"}`);
            }
          }
          if (statuses.length) console.log(`  Health      : ${statuses.join(", ")}`);
        }
      } catch {}
    } else {
      console.log("");
      console.log(`  Commands:`);
      console.log(`    telechat start      Start the bot`);
      console.log(`    telechat init       Set up / reconfigure`);
    }
    console.log("");
    process.exit(0);
  }

  // ── Logs ──
  if (cmd === "logs") {
    if (!existsSync(LOG_FILE)) {
      console.log("  No log file found. Start the bot first.");
      process.exit(1);
    }
    const tail = spawn("tail", ["-f", LOG_FILE], { stdio: "inherit" });
    tail.on("exit", (code) => process.exit(code || 0));
    return;
  }

  // ── Pin / show the backend Python interpreter ──
  if (cmd === "python") {
    const target = args[args.indexOf("python") + 1];
    if (!target) {
      const config = loadConfig();
      console.log(`  Pinned interpreter : ${config.pythonPath || "(none — auto-detect)"}`);
      console.log(`  Resolved           : ${findPython() || "(no Python 3 found)"}`);
      process.exit(0);
    }
    if (target === "clear") {
      const config = loadConfig();
      delete config.pythonPath;
      saveConfig(config);
      console.log("  ✓ Cleared pinned interpreter — auto-detect on next run.");
      process.exit(0);
    }
    if (!isPython3(target)) {
      console.error(`  ✗ Not a working Python 3 interpreter: ${target}`);
      process.exit(1);
    }
    const config = loadConfig();
    config.pythonPath = absInterpreter(target);
    saveConfig(config);
    if (isPyPkgInstalled(target)) {
      console.log(`  ✓ Pinned Python: ${target} (backend found)`);
    } else {
      console.log(`  ✓ Pinned Python: ${target}`);
      console.log(`    ⚠ Backend not installed there yet — it will be installed on next start,`);
      console.log(`      or run: ${target} -m pip install ${PYPI_PACKAGE}`);
    }
    if (getRunningPid()) console.log("    Apply to the running bot: telechat restart");
    process.exit(0);
  }

  // ── Restart ──
  if (cmd === "restart") {
    const python = findPython();
    if (!python) {
      console.error("Error: Python 3.10+ required.");
      process.exit(1);
    }
    stopService();
    await sleep(1000);
    if (!existsSync(ENV_FILE)) {
      console.error("  No .env found. Run: telechat setup");
      process.exit(1);
    }
    const pid = startService(python, debug);
    console.log(`  ✓ telechat restarted (PID ${pid})`);
    console.log(`    Logs: telechat logs`);
    process.exit(0);
  }

  // ── Update ──
  if (cmd === "update") {
    const python = findPython();
    if (!python) {
      console.error("Error: Python 3.10+ required.");
      process.exit(1);
    }
    if (isPyPkgInstalled(python) && isEditableInstall(python)) {
      console.log("  ⚠ Backend is an editable/source install — update it with git pull");
      console.log("    in the source tree instead. Skipping pip to avoid clobbering it.");
      process.exit(1);
    }
    console.log("  Updating telechat...");
    if (!installPyPkg(python)) process.exit(1);
    console.log("  ✓ Updated to latest version.");
    // Restart if running
    if (getRunningPid()) {
      stopService();
      await sleep(1000);
      const pid = startService(python, debug);
      console.log(`  ✓ Restarted with new version (PID ${pid})`);
    }
    process.exit(0);
  }

  // ── Setup (manual wizard) ──
  if (cmd === "setup") {
    const { python } = checkAndFixIssues();
    await setup();
    printTips();
    process.exit(0);
  }

  // ── Claude Desktop bridge (install/uninstall/status/notify/approve) ──
  // Delegates to telechat_pkg.desktop_bridge.cli_dispatch via main.py.
  if (cmd === "bridge") {
    const python = findPython();
    if (!python) {
      console.error("  ✗ Python 3.10+ required.");
      process.exit(1);
    }
    if (!isPyPkgInstalled(python)) {
      console.log("  Installing Python backend...");
      if (!installPyPkg(python)) process.exit(1);
    }
    const subArgs = args.slice(args.indexOf("bridge") + 1);
    const result = spawnSync(python, ["-m", "telechat_pkg.main", "bridge", ...subArgs], {
      stdio: "inherit",
      env: process.env,
    });
    process.exit(result.status ?? 1);
  }

  // ── Normal start (default command or explicit 'start') ──
  if (cmd === "start" || !cmd) {
    const python = findPython();
    if (!python) {
      console.error("  ✗ Python 3.10+ required.\n    Install from https://python.org/downloads");
      process.exit(1);
    }

    if (!isPyPkgInstalled(python)) {
      console.log("  Installing Python backend...");
      if (!installPyPkg(python)) process.exit(1);
      console.log("  ✓ Python backend installed");
    }

    ensureDataHome();
    const envFile = ENV_FILE;

    // No .env — guide the user
    if (!existsSync(envFile)) {
      console.log(`
  Data home: ${DATA_HOME}

  No .env configuration found. Set up your bot first:

    telechat init     AI-guided setup (recommended)
                      Claude walks you through each platform,
                      opens the right pages, validates tokens.
                      Requires: npm i -g @anthropic-ai/claude-code

    telechat setup    Manual setup wizard
                      Step-by-step prompts without Claude.

  Quick start (Telegram only):
    1. Message @BotFather on Telegram → /newbot → copy token
    2. echo "TELEGRAM_BOT_TOKEN=your_token_here" > ${envFile}
       echo "CLAUDE_MODE=cli" >> ${envFile}
    3. telechat start
`);
      process.exit(1);
    }

    // Validate .env has at least one platform configured
    const envContent = fs.readFileSync(envFile, "utf8");
    const envVars = {};
    for (const line of envContent.split("\n")) {
      if (!line.trim() || line.startsWith("#")) continue;
      const eq = line.indexOf("=");
      if (eq > 0) envVars[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
    }

    const hasTelegram = !!envVars.TELEGRAM_BOT_TOKEN;
    const hasWhatsApp = !!envVars.GREEN_API_INSTANCE_ID && !!envVars.GREEN_API_TOKEN;
    const hasSlack = !!envVars.SLACK_BOT_TOKEN && !!envVars.SLACK_APP_TOKEN;
    const botMode = (envVars.BOT_MODE || "telegram").toLowerCase();
    const hasWeb = botMode === "all" || botMode.split(",").map(s => s.trim()).includes("web");

    if (!hasTelegram && !hasWhatsApp && !hasSlack && !hasWeb) {
      console.log(`
  .env exists but no platform credentials found.

  Need at least one of:
    • TELEGRAM_BOT_TOKEN       (Telegram)
    • GREEN_API_INSTANCE_ID    (WhatsApp)
    • SLACK_BOT_TOKEN          (Slack)

  Run 'telechat init' or 'telechat setup' to configure.
`);
      process.exit(1);
    }

    // ── Prompt for missing access control on any platform ──
    await postInitValidation(envFile);

    // Re-read env vars after validation may have updated the file
    const updatedContent = fs.readFileSync(envFile, "utf8");
    for (const line of updatedContent.split("\n")) {
      if (!line.trim() || line.startsWith("#")) continue;
      const eq = line.indexOf("=");
      if (eq > 0) envVars[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
    }

    // Check if already running
    const existingPid = getRunningPid();
    if (existingPid) {
      console.log(`  telechat is already running (PID ${existingPid})`);
      // Show what's running
      const platforms = [];
      if (hasTelegram) platforms.push("Telegram");
      if (hasWhatsApp) platforms.push("WhatsApp");
      if (hasSlack) platforms.push("Slack");
      if (hasWeb) platforms.push("Web");
      console.log(`  Platforms: ${platforms.join(", ")}`);
      if (hasWeb) printWebQR(envVars.WEB_CHAT_PORT || "8585");
      console.log(`\n  Commands:`);
      console.log(`    telechat logs       View live logs`);
      console.log(`    telechat status     Check health`);
      console.log(`    telechat restart    Restart with current config`);
      console.log(`    telechat stop       Stop the bot`);
      process.exit(0);
    }

    // Stop any orphan processes. The pattern requires the `python … -m` form
    // rather than the bare module name: `pkill -f telechat_pkg.main` also
    // matches an editor with the file open, a grep for the string, or a test
    // run, and killed them. -u keeps the sweep inside this user's processes.
    try {
      const uid = typeof process.getuid === "function" ? process.getuid() : null;
      const scope = uid === null ? "" : `-u ${uid} `;
      execSync(`pkill ${scope}-f 'python[^ ]* -m telechat_pkg\\.main'`, { stdio: "ignore" });
      await sleep(1000);
    } catch {}

    const pid = startService(python, debug);

    // Show startup summary
    const platforms = [];
    if (hasTelegram) platforms.push("Telegram");
    if (hasWhatsApp) platforms.push("WhatsApp");
    if (hasSlack) platforms.push("Slack");
    if (hasWeb) platforms.push("Web");
    const claudeMode = envVars.CLAUDE_MODE || "cli";
    const webPort = envVars.WEB_CHAT_PORT || "8585";

    console.log(`  ✓ telechat started (PID ${pid})`);
    console.log(`    Platforms : ${platforms.join(", ")}`);
    console.log(`    Claude    : ${claudeMode} mode`);
    if (hasWeb) console.log(`    Web chat  : http://localhost:${webPort}`);
    if (debug) console.log(`    Debug     : ON`);
    console.log(`    Logs      : telechat logs`);

    if (hasWeb) printWebQR(webPort);

    // Warn about missing access control
    const warnings = [];
    if (hasTelegram && !envVars.TELEGRAM_ALLOWED_USER_IDS)
      warnings.push("Telegram: no user restriction (anyone can message your bot)");
    if (hasWhatsApp && !envVars.WHATSAPP_ALLOWED_NUMBERS)
      warnings.push("WhatsApp: no number restriction (anyone can message your bot)");
    if (hasSlack && !envVars.SLACK_ALLOWED_USER_IDS)
      warnings.push("Slack: no user restriction (anyone in workspace can use the bot)");
    if (hasWeb && !envVars.WEB_CHAT_TOKEN)
      warnings.push("Web: no access token (anyone with the URL can chat)");

    if (warnings.length) {
      console.log(`\n  ⚠ Security:`);
      for (const w of warnings) console.log(`    • ${w}`);
      console.log(`    Fix: telechat init → reconfigure, or edit .env manually`);
    }

    process.exit(0);
  }

  // Unknown command
  console.log(`  Unknown command: ${cmd}`);
  console.log(`  Run 'telechat --help' for available commands.`);
  process.exit(1);
}

main();
