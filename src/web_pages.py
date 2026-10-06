"""Small, cache-resistant browser pages for account connection flows."""

from html import escape

from fastapi.responses import HTMLResponse

SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _document(
    *,
    title: str,
    eyebrow: str,
    heading: str,
    body: str,
    content: str = "",
    tone: str = "neutral",
) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)} | AI Mail</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17201d;
      --muted: #60706a;
      --line: #d8dfdc;
      --surface: #ffffff;
      --canvas: #f3f6f4;
      --accent: #087f5b;
      --accent-hover: #066747;
      --success-bg: #e8f7ef;
      --success-line: #8bd3ad;
      --warning-bg: #fff7df;
      --warning-line: #dfbd5b;
      --danger-bg: #fff0ed;
      --danger-line: #e6a196;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      background: var(--canvas);
      color: var(--ink);
      font: 16px/1.5 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }}
    main {{
      width: min(100% - 32px, 560px);
      margin: 0 auto;
      padding: clamp(48px, 10vh, 96px) 0 48px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 28px;
      font-size: 17px;
      font-weight: 750;
    }}
    .mark {{
      display: grid;
      width: 32px;
      height: 32px;
      place-items: center;
      border-radius: 7px;
      background: var(--ink);
      color: white;
      font-size: 14px;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      padding: clamp(24px, 5vw, 40px);
      box-shadow: 0 12px 32px rgba(23, 32, 29, 0.07);
    }}
    .status {{
      display: inline-flex;
      align-items: center;
      min-height: 28px;
      margin-bottom: 20px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 10px;
      color: var(--muted);
      background: #f8faf9;
      font-size: 13px;
      font-weight: 700;
    }}
    .success .status {{ border-color: var(--success-line); background: var(--success-bg); color: #17613f; }}
    .warning .status {{ border-color: var(--warning-line); background: var(--warning-bg); color: #72590d; }}
    .danger .status {{ border-color: var(--danger-line); background: var(--danger-bg); color: #8c3529; }}
    h1 {{
      margin: 0 0 12px;
      font-size: clamp(28px, 7vw, 38px);
      line-height: 1.12;
      letter-spacing: 0;
    }}
    p {{ margin: 0; color: var(--muted); }}
    form {{ margin-top: 28px; }}
    label {{
      display: block;
      margin-bottom: 8px;
      font-size: 14px;
      font-weight: 700;
    }}
    input {{
      width: 100%;
      height: 48px;
      border: 1px solid #aab7b2;
      border-radius: 6px;
      padding: 0 13px;
      color: var(--ink);
      background: white;
      font: inherit;
    }}
    input:focus {{
      outline: 3px solid rgba(8, 127, 91, 0.18);
      border-color: var(--accent);
    }}
    button {{
      width: 100%;
      min-height: 48px;
      margin-top: 16px;
      border: 0;
      border-radius: 6px;
      padding: 11px 18px;
      background: var(--accent);
      color: white;
      font: inherit;
      font-weight: 750;
      cursor: pointer;
    }}
    button:hover {{ background: var(--accent-hover); }}
    .context {{
      margin-top: 24px;
      border-top: 1px solid var(--line);
      padding-top: 18px;
      color: var(--muted);
      font-size: 14px;
      overflow-wrap: anywhere;
    }}
    .privacy {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
    }}
  </style>
</head>
<body>
  <main>
    <div class="brand"><span class="mark">AI</span><span>AI Mail</span></div>
    <section class="panel {escape(tone)}">
      <div class="status">{escape(eyebrow)}</div>
      <h1>{escape(heading)}</h1>
      <p>{escape(body)}</p>
      {content}
    </section>
  </main>
</body>
</html>"""


def html_page(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content=content,
        status_code=status_code,
        headers=SECURITY_HEADERS,
    )


# What a connection that did not fully work needs from its owner. Chosen by a
# fixed key from the query string, never echoed from it.
MICROSOFT_PROBLEMS = {
    "imap": (
        "Microsoft accepted your sign-in, but the mailbox refused an IMAP connection, so nothing can be "
        "synchronized yet. IMAP is probably switched off for this mailbox; a Microsoft 365 administrator can "
        "allow it with Set-CASMailbox -Identity <address> -ImapEnabled $true."
    ),
    "send": (
        "Reading mail will work, but sending will not: Microsoft did not give this connection permission to "
        "send mail. Connect the mailbox again and accept every permission that is requested."
    ),
    "both": (
        "Microsoft accepted your sign-in, but neither reading nor sending works yet. IMAP may be switched off "
        "for this mailbox (an administrator can allow it with Set-CASMailbox -Identity <address> "
        "-ImapEnabled $true), and sending needs the mail permissions to be accepted. Connect the mailbox "
        "again once that is sorted out."
    ),
}


def service_page(connected: str | None = None, problem: str | None = None) -> HTMLResponse:
    if connected == "gmail":
        return html_page(
            _document(
                title="Gmail connected",
                eyebrow="Connected",
                heading="Gmail is ready",
                body="Initial synchronization has started. You can close this tab and return to the conversation.",
                tone="success",
            )
        )
    if connected == "microsoft":
        if problem in MICROSOFT_PROBLEMS:
            return html_page(
                _document(
                    title="Microsoft connected with a problem",
                    eyebrow="Needs attention",
                    heading="Microsoft 365 is connected, but not working yet",
                    body=MICROSOFT_PROBLEMS[problem],
                    tone="warning",
                )
            )
        return html_page(
            _document(
                title="Microsoft connected",
                eyebrow="Connected",
                heading="Microsoft 365 is ready",
                body="Initial synchronization has started. You can close this tab and return to the conversation.",
                tone="success",
            )
        )
    return html_page(
        _document(
            title="Service online",
            eyebrow="Service online",
            heading="AI Mail is running",
            body="The authenticated mail connector is available and ready for MCP clients.",
            tone="success",
        )
    )


def password_form_page(
    *,
    account_name: str,
    address: str,
    error: str | None = None,
) -> HTMLResponse:
    content = f"""
      <form method="post" autocomplete="off">
        <label for="password">Mailbox password or app password</label>
        <input id="password" name="password" type="password" required autofocus
               minlength="1" maxlength="1024" autocomplete="current-password">
        <button type="submit">Save password securely</button>
      </form>
      <div class="context">{escape(account_name)} &middot; {escape(address)}</div>
      <p class="privacy">The password is sent directly to AI Mail and is never returned to the MCP client.</p>
    """
    return html_page(
        _document(
            title="Set mailbox password",
            eyebrow="Credential setup" if not error else "Check password",
            heading="Set mailbox password",
            body=error or "Enter only the credential for this mailbox. Other connection settings remain unchanged.",
            content=content,
            tone="warning" if error else "neutral",
        ),
        status_code=422 if error else 200,
    )


def password_saved_page() -> HTMLResponse:
    return html_page(
        _document(
            title="Password saved",
            eyebrow="Credential saved",
            heading="Mailbox password saved",
            body="The encrypted credential is stored. AI Mail can now test and adjust the remaining connection settings without asking for it again.",
            tone="success",
        )
    )


def invalid_setup_page() -> HTMLResponse:
    return html_page(
        _document(
            title="Setup link expired",
            eyebrow="Link unavailable",
            heading="Request a new setup link",
            body="This password setup link is invalid, expired, or has already been used.",
            tone="danger",
        ),
        status_code=401,
    )


# AADSTS codes that mean "an administrator has to approve this app first", as
# opposed to the user having said no.
_ADMIN_APPROVAL_CODES = ("AADSTS65001", "AADSTS90094", "AADSTS650056", "AADSTS700016")


def microsoft_denied_page(error: str, description: str) -> HTMLResponse:
    """The consent screen ended without a code: declined, blocked, or needing an administrator."""
    needs_admin = any(code in description for code in _ADMIN_APPROVAL_CODES)
    if needs_admin:
        body = (
            "Your organization requires an administrator to approve this app before it can be used. "
            "Ask them to grant consent for it, then start the connection again."
        )
    elif error == "access_denied":
        body = "The request was declined, so nothing was connected. Start the connection again to retry."
    else:
        body = f"Microsoft reported: {' '.join(description.split())[:300] or error}"
    return html_page(
        _document(
            title="Microsoft did not connect",
            eyebrow="Not connected",
            heading="The mailbox was not connected",
            body=body,
            tone="danger",
        ),
        status_code=400,
    )
