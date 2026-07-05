"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ApiError, getApiKey, setApiKey, fetchHealth } from "@/lib/api";
import { provisionApiKey } from "@/lib/use-api-key";

export function ApiKeyForm() {
  const [key, setKey] = useState("");
  const [saved, setSaved] = useState(false);
  const [testStatus, setTestStatus] = useState<"idle" | "testing" | "success" | "error">("idle");
  const [testMessage, setTestMessage] = useState("");
  const [showHowTo, setShowHowTo] = useState(false);
  const [generatedKey, setGeneratedKey] = useState("");
  const [provisioning, setProvisioning] = useState(false);
  const [provisionMessage, setProvisionMessage] = useState<
    { kind: "success" | "error"; text: string } | null
  >(null);

  useEffect(() => {
    // Seed the input from localStorage after mount (external store sync);
    // deferred to a microtask so hydration output stays deterministic.
    let cancelled = false;
    void Promise.resolve().then(() => {
      if (!cancelled) setKey(getApiKey() ?? "");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSave = () => {
    setApiKey(key || null);
    setSaved(true);
    setTimeout(() => setSaved(false), 2200);
  };

  const handleTest = async () => {
    const currentKey = getApiKey();
    if (!currentKey) {
      setTestStatus("error");
      setTestMessage("No key saved yet. Paste and save a key first.");
      return;
    }
    setTestStatus("testing");
    setTestMessage("");

    try {
      // We call fetchHealth — it will use the Authorization header from the interceptor
      await fetchHealth();
      setTestStatus("success");
      setTestMessage("✅ Connection successful! Your Railway backend accepted the key.");
    } catch (err: unknown) {
      setTestStatus("error");
      const status = err instanceof ApiError ? err.status : 0;
      const detail = err instanceof ApiError ? err.detail : undefined;
      if (status === 401 || status === 403) {
        setTestMessage("❌ Invalid or rejected key (401/403). Double-check you pasted the full key and that TRADING_API_KEY (or the manifest) is set correctly on Railway.");
      } else if (status === 503) {
        setTestMessage("❌ Backend says no API key is configured on the server yet (503). Set TRADING_API_KEY on Railway or run the issuance command.");
      } else {
        setTestMessage(`❌ Connection failed (${status || "network"}). ${detail || "Check your Railway backend is running and reachable."}`);
      }
    }
  };

  // Server-side provisioning — issues (or rotates) the account's key via
  // the backend and stores it locally in one click.
  const handleGenerateKey = async () => {
    setProvisioning(true);
    setProvisionMessage(null);
    try {
      const result = await provisionApiKey();
      setKey(result.api_key);
      setSaved(true);
      setTimeout(() => setSaved(false), 2200);
      setProvisionMessage({
        kind: "success",
        text: result.rotated
          ? "New key generated and saved — your previous key has been replaced and will no longer work."
          : "Key generated and saved to this browser.",
      });
    } catch (err: unknown) {
      setProvisionMessage({
        kind: "error",
        text:
          err instanceof Error
            ? err.message
            : "Key generation failed. Sign in and try again.",
      });
    } finally {
      setProvisioning(false);
    }
  };

  // Client-side generation for convenience (matches server generation strength)
  const generateAndShow = () => {
    // 32 bytes → url-safe base64, same as secrets.token_urlsafe(32)
    const array = new Uint8Array(32);
    crypto.getRandomValues(array);
    const keyStr = btoa(String.fromCharCode(...array))
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
    setGeneratedKey(keyStr);
    setKey(keyStr); // prefill the input for easy save
  };

  const copyToClipboard = (text: string) => {
    navigator.clipboard.writeText(text).catch(() => {});
  };

  const currentSavedKey = getApiKey();

  return (
    <div className="space-y-6">
      {/* Current key + primary actions */}
      <div>
        <div className="mb-2 flex items-center justify-between">
          <div className="text-sm text-zinc-400">
            {currentSavedKey ? (
              <>Saved key (masked): <span className="font-mono text-zinc-300">{currentSavedKey.slice(0, 8)}••••{currentSavedKey.slice(-4)}</span></>
            ) : (
              "No key saved yet in this browser"
            )}
          </div>
          {currentSavedKey && (
            <Button variant="outline" size="sm" onClick={handleTest} disabled={testStatus === "testing"}>
              {testStatus === "testing" ? "Testing..." : "Test connection"}
            </Button>
          )}
        </div>

        <div className="flex gap-2">
          <Input
            type="password"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="Paste your Railway API key here (Bearer token)"
            className="flex-1 font-mono text-sm"
          />
          <Button onClick={handleSave} className="min-w-[92px]">
            {saved ? "Saved ✓" : "Save"}
          </Button>
        </div>

        <div className="mt-3 rounded-xl border border-cyan-500/20 bg-cyan-500/[0.04] p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm text-zinc-300">
              Signed in? Generate your key automatically — no Railway steps
              needed.
            </div>
            <Button
              size="sm"
              onClick={handleGenerateKey}
              disabled={provisioning}
            >
              {provisioning ? "Generating..." : "Generate key"}
            </Button>
          </div>
          <p className="mt-2 text-[11px] text-amber-400/90">
            Warning: generating a new key replaces your old one — any other
            browser or device using the previous key will need this new key.
          </p>
          {provisionMessage && (
            <div
              className={`mt-2 rounded-md border px-3 py-2 text-sm ${
                provisionMessage.kind === "success"
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-400"
                  : "border-red-500/40 bg-red-500/10 text-red-400"
              }`}
            >
              {provisionMessage.text}
            </div>
          )}
        </div>

        {testStatus !== "idle" && (
          <div className={`mt-2 rounded-md border px-3 py-2 text-sm ${testStatus === "success" ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-400" : "border-red-500/40 bg-red-500/10 text-red-400"}`}>
            {testMessage}
          </div>
        )}
      </div>

      {/* Big friendly "Get your key" section */}
      <div className="rounded-lg border border-white/10 bg-white/[0.015] p-4">
        <button
          onClick={() => setShowHowTo(!showHowTo)}
          className="flex w-full items-center justify-between text-left text-sm font-medium text-white hover:text-cyan-300"
        >
          <span>How do I get the API key from Railway?</span>
          <span className="text-xs text-zinc-500">{showHowTo ? "Hide" : "Show steps"}</span>
        </button>

        {showHowTo && (
          <div className="mt-4 space-y-6 text-sm">
            {/* Quick path */}
            <div>
              <div className="mb-2 font-semibold text-emerald-400">Fastest way (recommended to start)</div>
              <ol className="ml-4 list-decimal space-y-1.5 text-zinc-300">
                <li>
                  Generate a key (or use the one below) and set it on Railway:
                  <div className="my-2 rounded bg-black/40 p-3 font-mono text-xs">
                    TRADING_API_KEY=your-key-here
                  </div>
                </li>
                <li>
                  In Railway dashboard → your service → <strong>Variables</strong> tab → Add Variable:
                  <br />
                  <strong>Name:</strong> <code className="text-cyan-300">TRADING_API_KEY</code><br />
                  <strong>Value:</strong> paste the full key
                </li>
                <li>Redeploy (or wait for auto-deploy), then paste the same key here and hit Save.</li>
              </ol>

              <div className="mt-3 flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={generateAndShow}>
                  Generate a fresh key + show Railway command
                </Button>
                {generatedKey && (
                  <Button size="sm" variant="outline" onClick={() => copyToClipboard(`TRADING_API_KEY=${generatedKey}`)}>
                    Copy railway variables command
                  </Button>
                )}
              </div>

              {generatedKey && (
                <div className="mt-3 rounded-md border border-white/10 bg-black/60 p-3">
                  <div className="mb-1 text-[10px] uppercase tracking-widest text-zinc-500">Fresh key (copy now)</div>
                  <div className="mb-2 break-all font-mono text-xs text-white">{generatedKey}</div>
                  <Button size="sm" onClick={() => copyToClipboard(generatedKey)}>
                    Copy key only
                  </Button>
                  <div className="mt-2 text-[10px] text-zinc-500">
                    CLI one-liner (if you have Railway CLI):<br />
                    <code className="text-amber-300">railway variables --set TRADING_API_KEY=&quot;{generatedKey}&quot;</code>
                  </div>
                </div>
              )}
            </div>

            {/* Production / multi-user path */}
            <div className="border-t border-white/10 pt-4">
              <div className="mb-2 font-semibold text-violet-400">Production path (multiple users, revokable keys)</div>
              <p className="text-zinc-400">
                Use the helper script from the repo (best experience):
              </p>
              <pre className="my-2 overflow-x-auto rounded bg-black/60 p-3 text-xs text-zinc-200">
{`python scripts/momentumforge_api_key.py

# or after pip install -e ".[dev]"
trading-bot-api-issue-key`}
              </pre>
              <p className="text-xs text-zinc-400">
                The script prints the exact shell command for your Railway volume
                (<code>/app/data</code>) and the simple env-var command.
              </p>
            </div>

            <div className="text-xs text-zinc-500">
              After setting the key on Railway, come back here, paste it, Save, then click “Test connection”.
              The key is stored only in your browser (localStorage).
            </div>
          </div>
        )}
      </div>

      <div className="text-[11px] text-zinc-500">
        The key is sent as <code>Authorization: Bearer ...</code> to your Railway backend.
        Need to revoke a key later? Use the <code>python -m trading_bot.api.keys revoke</code> command in a Railway shell.
      </div>
    </div>
  );
}
