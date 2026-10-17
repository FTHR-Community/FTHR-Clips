# SFTP remembered host verification — 13 October 2026

An empty Host key field now checks trusted host keys from `~/.ssh/known_hosts`
and `~/.fthr/uploader/known_hosts`. Non-default ports use `[hostname]:port`, and
hashed OpenSSH hostnames are supported. Unknown hosts and changed keys fail
before authentication. An explicit SHA256 fingerprint remains authoritative;
a mismatch never falls back to a trusted-host entry.

After a successful pinned connection, authentication and remote-folder check,
FTHR atomically remembers the verified public key in its own known_hosts file.
Other hosts are preserved. Connection tests report a failure if the verified key
cannot be saved; uploads may continue with their explicit pin if this optional
cache write fails. No unverified key is automatically accepted. Stored passwords
remain in Windows Credential Manager, and their existing identity is unchanged.

The SFTP UI now labels Host key as optional and explains the first-use and
remembered-key workflows. Saving credentials no longer requires a fingerprint.

Validation: 61 SFTP, upload-settings and extension-boundary tests passed. Tests
use a real local SSH/SFTP server and verify trusted user/app stores, hashed hosts,
port scoping, rejection before credentials, explicit-pin precedence, and
preservation of unrelated host entries. Ruff passed for changed modules.

The user's configured partner host was tested using its saved fingerprint and
credentials, then tested again with an empty fingerprint using the remembered
key. Both authenticated successfully and verified the configured folder. These
checks did not upload or modify server files. The original saved configuration
was retained for compatibility with the currently built uploader.

Application and uploader source are updated. Existing frozen executables and
optional extension packages have not been rebuilt; release packaging must rebuild
the uploader package as well as the core UI to expose this behavior in the
installed app.
