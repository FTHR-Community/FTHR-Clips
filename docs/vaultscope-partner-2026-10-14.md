# VaultScope partner setup — 14 October 2026

The upload provider selector now includes VaultScope. Its signup buttons open
https://vaultscope.de/ps/fthr and describe the partner-supplied one-month-free offer.

Choose VaultScope, copy its login JSON, and click **Paste VaultScope login**.
The importer requires providerName VaultScope, domain sftp.vaultscope.de, an integer
port from 1–65535, and nonempty username/password strings. Invalid input leaves
the form unchanged and error messages never echo credentials. Password whitespace
is preserved. Imported credentials remain in the masked form until saved through
the existing Windows Credential Manager flow.

Import resets the remote folder to the account home directory, clears any previous
host-key pin, and disables/clears public URL mapping. Existing trusted known_hosts
verification remains in effect. The supplied schema does not specify a public
sharing URL or remote folder, so neither is inferred from the domain.

VaultScope uses upload_provider=sftp for compatibility with the existing extension.
The sftp_provider_name setting preserves the named UI selection across restarts.
Connection tests also dispatch to SFTP. No uploader runtime changes are required.

Validation: new UI coverage exercises signup links, full import, password vault
handoff, reload, generic-SFTP switching, malformed payloads, atomic rejection,
and SFTP connection-test dispatch. Ruff passed. Source tests passed; the separate
existing frozen-extension integration test failed to connect to its local test
server with a generic connection/transfer error. Executables were not rebuilt.
The partner signup page could not be retrieved by the web tool; offer wording and
schema follow the partner details supplied for this task.
