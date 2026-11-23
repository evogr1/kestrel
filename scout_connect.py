"""Public, AI-readable one-link onboarding page for Kestrel's remote MCP connector."""
import html
import os

DEFAULT_PUBLIC_URL = "https://scout.solodac.com"


def page(mcp_url):
    endpoint = html.escape(mcp_url, quote=True)
    prompt = (
        "Please help me add Kestrel as a custom MCP connector/plugin. "
        "Use the official setup instructions for my AI client. "
        "The Kestrel setup page is the source of truth: " + mcp_url.replace("/mcp/", "/connect/") + ". "
        "Do not ask me to clone GitHub, install Python, use Terminal, or install Xcode. "
        "You cannot silently change my account settings; guide me through the required approval steps. "
        "After I approve the connector, verify that its tools are available and show me how to use Kestrel."
    )
    prompt_html = html.escape(prompt)
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect Kestrel to your AI</title>
<meta name="description" content="Add Kestrel to ChatGPT or Claude as a remote MCP connector. No terminal or local installation.">
<meta name="robots" content="noindex,nofollow">
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1018;color:#f3f5f8;font:16px/1.6 Inter,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}
main{max-width:760px;margin:64px auto;padding:0 24px}.eyebrow{color:#8bb8ff;font-size:12px;font-weight:700;letter-spacing:.16em;text-transform:uppercase}
h1{font-size:clamp(38px,7vw,64px);line-height:1.02;letter-spacing:-.05em;margin:14px 0}p{color:#b8c2d1}.lead{font-size:19px;max-width:620px}
.panel{background:#141d2a;border:1px solid #29384d;border-radius:16px;padding:22px;margin:26px 0}
code{overflow-wrap:anywhere;color:#a9ceff}.url{background:#080d14;padding:14px;border-radius:9px;margin:12px 0}
button{background:#4d9cff;color:#06101d;border:0;border-radius:9px;padding:12px 18px;font-weight:750;cursor:pointer}
h2{font-size:20px;margin:26px 0 8px}li{margin:8px 0;color:#c5cedb}a{color:#9cc8ff}.muted{font-size:13px;color:#8996a8}
</style>
</head>
<body><main>
<div class="eyebrow">Kestrel · Creator research</div>
<h1>Give your AI a research team.</h1>
<p class="lead">Connect Kestrel to ChatGPT or Claude. Your AI gets creator research, outlier discovery, channel analysis, content ideas and more. No GitHub clone, local Python, Terminal or Xcode required.</p>
<section class="panel">
<h2>1. Add Kestrel as a connector</h2>
<p>Use this remote MCP server URL in your AI app's custom connector setup:</p>
<div class="url"><code id="endpoint">""" + endpoint + """</code></div>
<button type="button" onclick="navigator.clipboard.writeText(document.getElementById('endpoint').textContent).then(()=>this.textContent='Copied')">Copy connector URL</button>
<p class="muted">This URL contains your Kestrel access credential. Share it only with people you intend to give access. If it leaks, rotate SCOUT_MCP_SECRET.</p>
</section>
<h2>ChatGPT</h2>
<ol>
<li>Open ChatGPT on the web and go to <b>Plugins</b>.</li>
<li>Select <b>+</b> → <b>Add custom MCP server</b>.</li>
<li>Name it <b>Kestrel</b> and paste the connector URL above.</li>
<li>Choose the authentication option matching this URL-based credential setup, acknowledge the risk notice, and create the plugin.</li>
<li>Open a chat, enable Kestrel, and ask: <b>“What can Kestrel do?”</b></li>
</ol>
<p class="muted">Availability and workspace permissions depend on your ChatGPT plan and admin settings. Current instructions: <a href="https://help.openai.com/en/articles/12584461-connecting-custom-mcp-servers-in-chatgpt">OpenAI Help</a>.</p>
<h2>Claude</h2>
<ol>
<li>Open <b>Settings → Connectors</b> (or <b>Customize → Connectors</b>).</li>
<li>Select <b>Add</b> → <b>Add custom connector</b>.</li>
<li>Name it <b>Kestrel</b> and paste the connector URL above.</li>
<li>Review the detected authentication settings and add the connector.</li>
<li>Enable Kestrel in a chat and ask: <b>“What can Kestrel do?”</b></li>
</ol>
<p class="muted">Current instructions: <a href="https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp">Claude Help</a>.</p>
<section class="panel">
<h2>2. Or give this page to your AI</h2>
<p>Paste this page's URL into ChatGPT or Claude and ask it to help you add Kestrel. The AI can explain and guide the setup, but you must approve the connector in your account settings.</p>
<div class="url"><code id="prompt">""" + prompt_html + """</code></div>
<button type="button" onclick="navigator.clipboard.writeText(document.getElementById('prompt').textContent).then(()=>this.textContent='Copied')">Copy setup prompt</button>
</section>
<p class="muted">Kestrel is a third-party MCP server. Review the tools and permissions before enabling it. Connecting the server does not sign you into your own Instagram account; account-specific Instagram OAuth is a separate feature.</p>
</main></body></html>"""


def public_url():
    return os.environ.get("SCOUT_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")
