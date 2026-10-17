# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.1.x (alpha) | ✅ |

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Send a report to: **community@fthrclips.com**

Include:
- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Your suggested fix (optional)

we will most likely reply within 48 hours though we cannot guarantee that our laziness doesn't overcome our striving for improvement.
in case your report leads to a change in the app you may be listed in the apps credits unless you do not want to.

## Scope

Things we care about:

- Code execution via malicious clip files or settings
- Privilege escalation (the capture engine runs as the user, not root)
- Data leakage from the local clip library
- Hotkey hijacking or command injection via the Unix socket

Out of scope:

- Issues requiring physical access to the machine
- Social engineering
- Theoretical vulnerabilities without a proof of concept
