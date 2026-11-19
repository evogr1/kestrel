"""Public legal pages for your Kestrel server: /privacy, /terms and /data-deletion.

Meta asks for these URLs if you submit your app for review. They are a starting template, not legal advice:
read them, edit them for your situation, and set SCOUT_OPERATOR (your name or company) and SCOUT_CONTACT_EMAIL.
Served without the API key (scout_server.py). Plain HTML, no scripts, no tracking.
"""
import html
import os

OPERATOR = os.environ.get("SCOUT_OPERATOR") or "the operator of this server"
CONTACT = os.environ.get("SCOUT_CONTACT_EMAIL") or "the operator"
UPDATED = os.environ.get("SCOUT_LEGAL_UPDATED") or "17 November 2026"

_STYLE = ("body{font:16px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#1b1b1b}"
          "h1{font-size:28px;margin-bottom:4px}h2{font-size:19px;margin-top:28px}.u{color:#666;font-size:14px}"
          "a{color:#1a5fd0}@media(prefers-color-scheme:dark){body{background:#111;color:#e8e8e8}.u{color:#aaa}a{color:#7fb0ff}}")


def _contact():
    return ('<a href="mailto:%s">%s</a>' % (html.escape(CONTACT), html.escape(CONTACT))) if "@" in CONTACT else html.escape(CONTACT)


def _page(title, sections):
    body = "".join("<h2>%s</h2>%s" % (html.escape(h), "".join("<p>%s</p>" % p for p in ps)) for h, ps in sections)
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>%s | Kestrel</title><style>%s</style></head><body><h1>%s</h1><div class=\"u\">Kestrel, run by %s. Last updated %s.</div>%s"
            "<p class=\"u\">Questions: %s</p></body></html>") % (
        html.escape(title), _STYLE, html.escape(title), html.escape(OPERATOR), html.escape(UPDATED), body, _contact())


def privacy():
    op = html.escape(OPERATOR)
    return _page("Privacy Policy", [
        ("Who we are", ["This Kestrel server is a research tool run by %s for planning content on the YouTube channels, Instagram accounts and "
                        "Facebook Pages they own or manage." % op]),
        ("What we read", ["Insights for accounts we manage, through the official YouTube and Meta APIs, after the account owner signs in and grants access: "
                          "views, reach, watch time, follower counts and audience breakdowns that the platforms report in aggregate.",
                          "Public information about other public Business and Creator accounts and public posts, such as follower counts, likes and "
                          "comment counts, as the platforms' official APIs make it available."]),
        ("What we do not do", ["We do not sell, rent or share data. We do not build advertising profiles. We do not collect passwords or read "
                               "private messages. Audience data comes only as aggregated statistics, never as information about individual people."]),
        ("How long we keep it", ["Answers from the platforms are cached on this server for a few hours. Research results are kept for up to 90 days "
                                 "and then deleted. Access tokens stay on this server, are never shown in any answer or log, and can be revoked at "
                                 "any time from the platform's own settings."]),
        ("Your choices", ["You can remove this app's access at any time in your Facebook, Instagram or Google account settings, and ask for "
                          "deletion as described on the <a href=\"/data-deletion\">data deletion page</a>."]),
    ])


def terms():
    return _page("Terms of Service", [
        ("Use", ["This Kestrel server is a private research tool. Access needs a key issued by %s. It is provided as it is, without any promise "
                 "that it is available or error free." % html.escape(OPERATOR)]),
        ("Platforms", ["Kestrel uses the official YouTube and Meta APIs and their rate limits for accounts we manage and for public accounts."]),
        ("Data", ["How data is handled is described in the <a href=\"/privacy\">privacy policy</a>. These terms may change; the date at the top "
                  "shows the latest version."]),
    ])


def data_deletion():
    return _page("Data Deletion", [
        ("Remove access", ["To stop this app from reading your account, remove it in your Facebook or Instagram settings (Settings, Apps and "
                           "Websites) or in your Google account permissions. Access ends immediately."]),
        ("Delete your data", ["Contact %s from the address linked to your account, with the account name, and ask for deletion. Everything held "
                              "about that account, including cached answers, research results and tokens, is deleted within 30 days." % _contact()]),
    ])


PAGES = {"/privacy": privacy, "/terms": terms, "/data-deletion": data_deletion}
