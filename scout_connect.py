"""One-link onboarding page for Kestrel's per-user Instagram authorization."""
import html
import os

DEFAULT_PUBLIC_URL = "https://scout.solodac.com"


def public_url():
    return os.environ.get("SCOUT_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")


def page(base_url=None):
    base = html.escape((base_url or public_url()).rstrip("/"), quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Connect Kestrel</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#0b1018;color:#f3f5f8;font:16px/1.6 Inter,-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif}}
main{{max-width:720px;margin:64px auto;padding:0 24px}}.eyebrow{{color:#8bb8ff;font-size:12px;font-weight:700;letter-spacing:.16em;text-transform:uppercase}}
h1{{font-size:clamp(38px,7vw,62px);line-height:1.02;letter-spacing:-.05em;margin:14px 0}}p{{color:#b8c2d1}}.lead{{font-size:19px}}
.panel{{background:#141d2a;border:1px solid #29384d;border-radius:16px;padding:24px;margin:26px 0}}
button{{background:#4d9cff;color:#06101d;border:0;border-radius:9px;padding:14px 20px;font-weight:750;cursor:pointer;font-size:16px}}
li{{margin:9px 0;color:#c5cedb}}a{{color:#9cc8ff}}.muted{{font-size:13px;color:#8996a8}}
</style></head><body><main><div class="eyebrow">Kestrel · Creator research</div>
<h1>Your research. Your account. Your data.</h1>
<p class="lead">Connect Kestrel to your own Instagram professional account. Kestrel creates a private connector credential for you; other users cannot use it to access your account.</p>
<section class="panel"><h2>1. Connect Instagram</h2>
<p>Sign in with Instagram and approve the permissions Kestrel uses. Your password is handled by Instagram; Kestrel never asks you to enter it.</p>
<a href="{base}/auth/instagram/start"><button type="button">Connect Instagram</button></a>
<p class="muted">Requires an Instagram Business or Creator account. The Meta app must be configured for Instagram Business Login and the callback URL must exactly match the app settings.</p></section>
<h2>2. Add your private Kestrel connector</h2>
<ol><li>After Instagram authorization, copy the private MCP URL shown on the confirmation page.</li>
<li>Add it as a custom MCP connector in ChatGPT or Claude.</li><li>Enable Kestrel in a chat and ask: <b>“What can Kestrel do?”</b></li></ol>
<p class="muted">Each person receives a different revocable connector URL. Treat yours like a password. Reconnecting rotates the credential; disconnecting invalidates it.</p>
<h2>What access is requested?</h2>
<p>Profile and media access, account/media insights, publishing, comment management and messaging. Instagram API access is limited to eligible professional accounts and the permissions approved for this Meta app. Public creator research remains a separate feature and uses Kestrel's configured discovery access.</p>
<p class="muted">Kestrel does not receive your Instagram password. You can disconnect using the authenticated account controls provided by your server administrator.</p>
</main></body></html>"""
