"""Public onboarding page for the hosted Kestrel MCP service."""
import html
import os

DEFAULT_PUBLIC_URL = "https://scout.solodac.com"


def public_url():
    return os.environ.get("SCOUT_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")


def page(base_url=None):
    base = html.escape((base_url or public_url()).rstrip("/"), quote=True)
    endpoint = html.escape((base_url or public_url()).rstrip("/") + "/mcp", quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Connect Kestrel</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#0b1018;color:#f3f5f8;font:16px/1.6 Inter,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}}
main{{max-width:720px;margin:64px auto;padding:0 24px}}.eyebrow{{color:#8bb8ff;font-size:12px;font-weight:700;letter-spacing:.16em;text-transform:uppercase}}
h1{{font-size:clamp(38px,7vw,62px);line-height:1.02;letter-spacing:-.05em;margin:14px 0}}p{{color:#b8c2d1}}.lead{{font-size:19px}}
.panel{{background:#141d2a;border:1px solid #29384d;border-radius:16px;padding:24px;margin:26px 0}}
code{{display:block;overflow-wrap:anywhere;background:#080d14;padding:14px;border-radius:8px;color:#a9ceff}}
li{{margin:9px 0;color:#c5cedb}}a{{color:#9cc8ff}}.muted{{font-size:13px;color:#8996a8}}
</style></head><body><main><div class="eyebrow">Kestrel · Creator intelligence</div>
<h1>Creator intelligence, inside your AI.</h1>
<p class="lead">Connect this MCP server to Claude or a supported ChatGPT workspace. Each person signs in separately; Kestrel issues an OAuth token for that person and keeps their Instagram credentials on the server.</p>
<section class="panel"><h2>Connector URL</h2><code>{endpoint}</code>
<p>Copy this URL into your AI assistant's custom MCP connector. Choose OAuth / sign-in when prompted. Do not choose “No sign in”.</p></section>
<h2>Claude</h2><ol><li>Open Customize → Connectors → Add custom connector.</li>
<li>Use the connector URL above and choose Sign in now or Sign in when needed.</li>
<li>For OAuth client registration, choose “Register automatically”.</li>
<li>Complete the Instagram authorization. Use an eligible Business or Creator account.</li></ol>
<h2>ChatGPT</h2><p>Use a ChatGPT workspace that supports custom MCP apps. Add a custom app with the URL above, choose OAuth, and complete sign-in. Custom MCP availability depends on workspace plan and administrator settings.</p>
<h2>What happens to your data?</h2><ul><li>Your Instagram access token stays encrypted on Kestrel's server and is never returned to Claude or ChatGPT.</li>
<li>Each signed-in user gets an isolated local database for saved profiles, watchlists, analytics caches and workspace state.</li>
<li>Public creator research remains separate from your private connected-account data.</li></ul>
<p class="muted">Instagram Login requires a professional account and Meta app access. The first release connects one Instagram account per Kestrel identity. You can revoke access by disconnecting the connector or using the account-disconnect endpoint.</p>
<p><a href="{base}/privacy">Privacy policy</a> · <a href="{base}/terms">Terms</a> · <a href="{base}/data-deletion">Data deletion</a></p>
</main></body></html>"""
