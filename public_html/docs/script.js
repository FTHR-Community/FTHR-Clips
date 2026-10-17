const categories = [
    {
        id: 'app-failure-codes',
        label: 'App Failure Codes',
        description: 'Codes displayed in the FTHRClips bottom error bar. The message detail contains the incident-specific reason.',
        entries: [
            ['Error 001', 'CAPTURE FAILED', 'Capture', 'Capture health reported a backend failure or the capture startup sequence failed. The detail text contains the reported reason.'],
            ['Error 002', 'CAPTURE UNAVAILABLE', 'Capture', 'A save was requested while the capture engine was not connected, so there was no active replay buffer to address.'],
            ['Error 003', 'ENGINE NOT FOUND', 'Capture', 'The FTHRcapture executable could not be found at the expected install or development path.'],
            ['Error 004', 'ENGINE NOT RESPONDING', 'Capture', 'The capture process was launched but did not complete its IPC startup handshake.'],
            ['Error 005', 'ENGINE COULD NOT START', 'Capture', 'A capture startup attempt failed before a usable engine connection was established.'],
            ['Error 006', 'ENGINE STOPPED', 'Capture', 'The capture process exited unexpectedly. The process exit code is included in the message detail.'],
            ['Error 007', 'MANUAL RECORDING FAILED', 'Recording', 'A manual recording could not be completed or finalized. Recoverable video fragments may be mentioned in the detail.'],
            ['Error 008', 'RECORDING COULD NOT START', 'Recording', 'The manual recording request could not be handed to the capture engine.'],
            ['Error 009', 'RECORDING COULD NOT STOP', 'Recording', 'The capture engine was unavailable when a manual recording stop was requested.'],
            ['Error 010', 'RECORDING FOLDER UNAVAILABLE', 'Recording', 'The configured manual-recording folder could not be created or accessed.'],
            ['Error 011', 'RECORDING PATH TOO LONG', 'Recording', 'The manual-recording output path exceeds the native engine path limit.'],
            ['Error 012', 'NOT ENOUGH DISK SPACE', 'Recording', 'Manual recording was blocked by the preflight free-space threshold.'],
            ['Error 013', 'CLIP NOT SAVED', 'Clip saving', 'Capture health rejected the save because fresh replay data was not currently usable. The detail says whether capture is starting, recovering, or stale.'],
            ['Error 014', 'CLIP SAVE FAILED', 'Clip saving', 'The local clip folder, save command submission, or save setup failed before a completed clip was produced.'],
            ['Error 015', 'CLIP WAS NOT SAVED', 'Clip saving', 'The capture engine reported a terminal save failure, an incomplete path, or a missing/empty output file.'],
            ['Error 016', 'CLIP PATH TOO LONG', 'Clip saving', 'The requested clip path exceeds the Windows shared-memory path field limit.'],
            ['Error 017', 'CLIP FINALIZATION FAILED', 'Clip saving', 'The base clip could not be made available as a completed final file after the save operation.'],
            ['Error 018', 'EXPORT FAILED', 'Export and sharing', 'An edited clip export failed validation, encoding, folder creation, or file publication.'],
            ['Error 019', 'SHARE FAILED', 'Export and sharing', 'The share workflow could not create or publish its exported clip.'],
            ['Error 020', 'UPLOAD FAILED', 'Upload', 'The upload provider rejected or could not complete the upload. The provider or retry detail is passed through when available.'],
            ['Error 021', 'UPLOAD NOT CONFIGURED', 'Upload', 'An upload was requested without an installed and enabled Upload Extension.'],
            ['Error 022', 'UPLOAD CONNECTION FAILED', 'Upload', 'The upload settings Test Connection request failed. The provider response is used as the detail.'],
            ['Error 023', 'COMPRESSION FAILED', 'Upload', 'The optional compressed upload copy could not be created, verified, or brought under the provider size limit.'],
        ],
    },
    {
        id: 'windows-basics',
        label: 'Windows Basics',
        description: 'Check the active source, storage, audio input, and recovery state. A healthy frame counter does not prove that the picture or sound is correct.',
        entries: [
            ['Capture source', 'SOURCE → Desktop → monitor → APPLY', 'Source and monitor', 'In the main window, open SOURCE, choose Desktop, select the active monitor, then choose APPLY. For Window / Game, select a listed window. Confirm the intended display and Windows scaling before reproducing a problem. FTHR requires same-adapter ownership for capture and encoding; a mismatch in diagnostic adapter IDs is not a supported fallback.'],
            ['Clip storage', 'Settings → Clip → Folders', 'Storage', 'The Clips Storage Folder holds saved clips, screenshots, exports, and shares. USE DEFAULT resolves the current Windows Videos known folder (including a Windows relocation) and adds FTHR Clips. An existing installation keeps its configured folder unless you choose a new root. Changing the root does not move existing files. Choose a writable local folder and check its free space.'],
            ['Manual recordings', 'Settings → Clip → Folders', 'Storage', 'By default, the Manual Recording Folder is <Clips Storage Folder>\\Recordings. If you explicitly choose a different folder, it remains custom when the Clips Storage Folder changes. FTHR checks that it can create and write to the selected folder and requires at least 512 MB free before starting a manual recording.'],
            ['Microphone input', 'Settings → Audio', 'Audio', 'Select the intended input device and use the device refresh control if it is missing. The microphone gain slider runs from 0 to 200: 100% is unity gain; values above 100% are a digital boost and can distort loud input. This slider is not the Windows device-volume control. Windows capture uses the selected native microphone endpoint.'],
            ['Hardware encoder', 'Settings → Performance', 'Capture and encoding', 'A requested codec or encoder choice is not proof that it started. Check the active encoder shown by FTHR and the diagnostic report. Encoder-init recovery requires a newer engine process, the native hardware-active flag, STRICT HEALTHY capture, and at least 3 advancing polls over 3 seconds. If those conditions are not met, restart capture after correcting the selected hardware/codec; FTHR does not silently replace the engine process.'],
            ['Capture recovery', 'Wait for capture progress', 'Recovery', 'A stalled capture can receive one bounded internal backend-recovery attempt per incident. 30 healthy seconds resets that recovery budget. The keyed backend incident clears only after repeated frame progress (at least 3 advancing polls over at least 3 seconds). CONTENT_SUSPECT frames may count as progress for this backend incident; that is not proof that the captured image is correct or non-black. Engine-process exits and lost terminal saves are not automatically restarted or recreated.'],
            ['Mixed-DPI monitor capture', 'Known limitation', 'Monitor and scaling', 'Monitor capture through Windows Graphics Capture has an unqualified mixed-DPI boundary between the initial GraphicsCaptureItem.Size and the DXGI output extent. This is a validation gap, not proof of a geometry mismatch. Window capture separately checks size agreement. If monitor capture looks cropped across mixed-DPI displays, include the display arrangement and scaling in a report; do not infer a root cause from size values alone.'],
            ['Hardware qualification', 'Known limitation', 'Capture and encoding', 'Physical qualification for AMD, Intel, and hybrid/multi-GPU hardware paths is still pending. The report can show the actual capture backend, encoder, and adapter information; FTHR requires same-adapter ownership for capture and encoding, and does not support an adapter mismatch as a fallback. A requested setting or successful startup alone does not prove that a specific game/display path is correct.'],
            ['Save qualification', 'Error 015 validation', 'Known limitation', 'Automated replay-policy tests do not substitute for a physical Windows save. The static-scene Error 015 save and full physical A/V comparison remain unverified. An error-free engine start or test run does not prove a clip was written; confirm a new, non-empty completed file and preserve the matching diagnostic report.'],
            ['Playback and effects', 'Performance notes', 'Known limitations', 'Performance observations are environment-specific, not universal guarantees: software-rendered active effects have been observed around 20 FPS; cold playback startup around 1.7 seconds is not fully resolved; paused seeking has improved to roughly 625 ms but does not automatically resume playback. GPU, display, and media affect results.'],
        ],
    },
    {
        id: 'diagnostics-support',
        label: 'Diagnostics & Support',
        description: 'Export a local diagnostic ZIP after reproducing an issue, then include the incident details needed to investigate it.',
        entries: [
            ['Diagnostic ZIP', 'EXPORT DIAGNOSTIC REPORT', 'When to export', 'After reproducing an alpha issue, open Settings → General → Troubleshooting and choose EXPORT DIAGNOSTIC REPORT. Export promptly and, when practical, before restarting capture or repeating attempts so the report retains the relevant session context. The report is privacy-redacted and contains bounded hardware, capture, audio, playback, export, and library diagnostics. Nothing is uploaded automatically; inspect the ZIP before sharing it.'],
            ['Attach the report', 'Use DRAG ZIP · <name>', 'Support', 'After export, use the DRAG ZIP · <name> control to drag a copy into your bug report; clicking the control opens the ZIP folder. The report is a diagnostic snapshot, not a saved clip or proof that a capture/save succeeded.'],
            ['Ticket details', 'Include the exact incident', 'Support', 'Include the Error number and full detail exactly as shown; reproduction steps and expected versus actual result; date/time and time zone; FTHR version/build and Windows version/build; session or operation/save IDs if known; GPU model and driver; requested versus actual codec, capture backend, resolution, and FPS; selected source, monitor/display, and scaling; and storage destination/free space when relevant. Say whether capture was restarted and whether a new completed clip was produced. Do not include passwords, tokens, or media by default; share media only if support asks for it.'],
            ['Error 4551', 'Keep the diagnosis neutral', 'Support', 'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION is a symbolic name for a Windows error value. The name alone does not identify which API call failed or establish the root cause. Include the complete diagnostic detail and report; do not disable antivirus or other security protections as a troubleshooting step.'],
        ],
    },
];

// Keep the public four-field registry above stable. Additional troubleshooting
// guidance is keyed separately so old consumers can continue reading entries.
const sharedSupportGuidance = {
    whenToExport: 'After reproducing this error, export a diagnostic ZIP from Settings → General → Troubleshooting. Export promptly and, when practical, before restarting capture or repeating attempts. The report is privacy-redacted and nothing is uploaded automatically; inspect it and attach it yourself.',
    ticketDetails: 'For a useful ticket, include the exact Error number/detail, reproduction steps, expected versus actual result, date/time/time zone, FTHR and Windows versions/builds, session/operation IDs if known, GPU and driver, requested versus actual codec/backend/resolution/FPS, selected monitor/display/scaling, storage/free space when relevant, and whether capture was restarted. Do not include secrets or media by default.',
};
const guidanceByCode = new Map([
    ['Error 001', {
        cause: 'Capture health or a native startup check reported a backend failure. The detail text identifies the incident-specific reason when available.',
        automatic: 'Only a keyed capture-backend incident may receive one bounded backend recovery attempt per incident. It clears after the same current lineage shows at least 3 advancing polls over at least 3 seconds; 30 healthy seconds resets the recovery budget. CONTENT_SUSPECT progress can clear that incident, but does not validate picture quality. Startup and encoder-init failures do not automatically restart the engine process.',
        action: 'Check the full detail and selected source/display. If capture remains failed, use RESTART ENGINE once. Export a report if the error returns.',
    }],
    ['Error 002', {
        cause: 'A save was requested while the capture engine was disconnected, so no active replay buffer was available to save.',
        automatic: 'The save is refused; FTHR does not reconstruct replay footage that was not available at the request.',
        action: 'Wait for capture to reconnect and show progress, or use RESTART CAPTURE if it remains disconnected. Then allow the buffer to fill before saving again.',
    }],
    ['Error 003', {
        cause: 'FTHR could not find its capture-engine executable at the expected install or development path.',
        automatic: 'No replacement executable is downloaded or installed automatically.',
        action: 'Confirm the application was fully installed/extracted and that its capture-engine file is present. Repair or reinstall the same intended FTHR package if needed; export a report if the file is present but still not found.',
    }],
    ['Error 004', {
        cause: 'The engine process launched but FTHR did not complete the IPC startup handshake.',
        automatic: 'Startup makes bounded connection/handshake attempts. A failed handshake does not automatically replace or relaunch the engine process.',
        action: 'Wait for the startup attempt to finish, then use RESTART ENGINE once. If it repeats, note the selected capture source and attach a diagnostic report.',
    }],
    ['Error 005', {
        cause: 'The process could not be launched or the engine failed before a usable connection was established; the detail may include the native Windows error.',
        automatic: 'FTHR records the reported launch/startup details but does not automatically replace the process after a failed start.',
        action: 'Read the full detail and check the selected monitor, capture mode, and hardware encoder. If ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION appears, include it in the report; the symbol alone is not a confirmed root cause. Do not disable antivirus or other security protections.',
    }],
    ['Error 006', {
        cause: 'The capture-engine process exited unexpectedly; the detail includes its exit code.',
        automatic: 'FTHR disconnects the exited process. It does not silently create a replacement engine process.',
        action: 'Use RESTART ENGINE, then confirm capture is progressing before saving. Export a diagnostic report if the process exits again.',
    }],
    ['Error 007', {
        cause: 'A manual recording could not finish or finalize; the detail may identify a recoverable video fragment.',
        automatic: 'A staged fragment may be retained or recovered during shutdown when the engine reports one, but a complete recording is not guaranteed.',
        action: 'Read the detail and use OPEN RECORDINGS to inspect the configured folder. Keep any fragment unchanged and include its name and the report in a ticket.',
    }],
    ['Error 008', {
        cause: 'The manual-recording request could not be handed to a connected capture engine.',
        automatic: 'FTHR does not start a replacement engine as part of this failed recording request.',
        action: 'Confirm capture is connected and progressing, then start the recording again. If capture is disconnected, resolve that state first.',
    }],
    ['Error 009', {
        cause: 'The capture engine was unavailable when the manual-recording stop was requested.',
        automatic: 'FTHR may try to recover a staged recording during shutdown, but this does not guarantee that the recording can be finalized.',
        action: 'Do not assume the recording completed. After FTHR has shut down, inspect the configured Manual Recording Folder and preserve any fragment; attach a report if it recurs.',
    }],
    ['Error 010', {
        cause: 'The configured manual-recording folder could not be created or accessed.',
        automatic: 'FTHR does not move recordings to a different folder automatically.',
        action: 'Open Settings → Clip → Folders, choose a valid writable Manual Recording Folder, and check that the drive is available. Existing files are not moved when you change the folder.',
    }],
    ['Error 011', {
        cause: 'The manual-recording output path exceeds the native engine limit of 255 characters.',
        automatic: 'FTHR does not shorten or redirect the path automatically.',
        action: 'Choose a shorter Manual Recording Folder path and shorter folder names, then retry.',
    }],
    ['Error 012', {
        cause: 'Manual recording preflight requires at least 512 MB free in its configured destination.',
        automatic: 'FTHR blocks the recording before it starts; it does not delete files to make room.',
        action: 'Free space on that drive or choose another writable Manual Recording Folder, then try again.',
    }],
    ['Error 013', {
        cause: 'The UI save guard refuses when capture is STOPPED, FAILED, or RECOVERING. INITIALIZING and STALLED states are delegated to native replay-ring validity checks; the detail identifies the actual rejection.',
        automatic: 'If the incident is a stalled capture backend, FTHR may make one bounded internal recovery attempt per incident; 30 healthy seconds resets that recovery budget. This attempt is separate from the rejected save and does not recover the missed moment.',
        action: 'Wait until capture is receiving new frames; if the detail says capture is recovering, wait briefly and try once after it resumes. If it stays stopped/failed, restart capture. A new replay buffer must accumulate before the next save.',
    }],
    ['Error 014', {
        cause: 'Clip-folder creation, save-command submission, or save setup failed before FTHR confirmed a completed clip.',
        automatic: 'The failed setup is not automatically retried as a new save.',
        action: 'Check the Clips Storage Folder in Settings → Clip → Folders, its write access and free space, then try one new save after capture is progressing. Use a shorter folder/source name if the full path is long.',
    }],
    ['Error 015', {
        cause: 'The engine reported a terminal save failure, returned an incomplete path, or FTHR found the output file missing or empty.',
        automatic: 'This save is terminal; FTHR does not automatically retry it or recreate lost replay data. A bounded backend recovery is not a save recovery.',
        action: 'Check the full detail, destination, and free space. Wait for a fresh progressing buffer before making one new save. Preserve the report from the failed attempt; do not assume a clip exists unless a non-empty completed file is present.',
    }],
    ['Error 016', {
        cause: 'The requested Windows clip path exceeds the shared-memory path field limit of 255 characters.',
        automatic: 'FTHR does not shorten or redirect the destination automatically.',
        action: 'Choose a shorter Clips Storage Folder path in Settings → Clip → Folders, then save again. For Window / Game capture the subfolder name comes from the selected window; there is no window-rename control in Folders.',
    }],
    ['Error 017', {
        cause: 'The base save could not be made available as a completed final clip; the final file was unavailable after saving.',
        automatic: 'FTHR does not guarantee automatic re-finalization or publication of an unavailable base file.',
        action: 'Check the configured Clips Storage Folder for the output and preserve any file/fragment. Export a diagnostic report before repeating the capture if practical.',
    }],
    ['Error 018', {
        cause: 'An edited export failed validation, encoding, destination-folder creation, or file publication. The export detail identifies the failed step when available.',
        automatic: 'FTHR does not silently repeat a failed export.',
        action: 'Check that the edit has a non-empty kept segment, the Exported folder is writable, and the source clip remains available. Correct the reported issue and start a new export.',
    }],
    ['Error 019', {
        cause: 'The share workflow could not prepare, export, or publish its shared clip.',
        automatic: 'The share export is not automatically restarted after a terminal failure.',
        action: 'Read the detail, check the source clip and Shared folder, and try the share action again after correcting the reported issue. Include the report if the failure repeats.',
    }],
    ['Error 020', {
        cause: 'The upload extension/provider rejected the upload or could not complete it; the provider response is shown when available.',
        automatic: 'FTHR does not claim a successful upload after failure. If interval scanning is enabled, eligible completed files not marked uploaded may be tried in a later scan; this is not an immediate retry or a guarantee of success. The error bar offers RETRY NOW when the source clip is available.',
        action: 'Check the provider response and connection, then use RETRY NOW or try again from the clip. Include the provider message in the ticket.',
    }],
    ['Error 021', {
        cause: 'An upload was requested without an installed and enabled Upload Extension.',
        automatic: 'No upload or provider retry starts until an uploader is configured.',
        action: 'Open Upload Settings, install and enable the intended extension, then retry the upload.',
    }],
    ['Error 022', {
        cause: 'The Upload Settings Test Connection request failed; the provider response is passed through as the detail.',
        automatic: 'FTHR reports the failed test and does not silently change provider credentials or configuration.',
        action: 'Check the selected provider and its required settings, then run Test Connection again. Include the provider response in the ticket.',
    }],
    ['Error 023', {
        cause: 'The optional compressed upload copy could not be created or verified, or it still exceeded the provider size limit.',
        automatic: 'Compression is used only when configured/confirmed. The original clip is preserved; if the compressed copy is still too large, the upload does not start.',
        action: 'Check free space and the provider limit, then retry compression/upload if appropriate. Do not delete or overwrite the original clip.',
    }],
]);

const categoryById = new Map(categories.map((category) => [category.id, category]));
const state = {
    categoryId: categoryById.has(window.location.hash.slice(1))
        ? window.location.hash.slice(1)
        : categories[0].id,
    query: '',
};

const categoryNav = document.querySelector('#categoryNav');
const categoryEyebrow = document.querySelector('#categoryEyebrow');
const pageTitle = document.querySelector('#pageTitle');
const pageDescription = document.querySelector('#pageDescription');
const searchInput = document.querySelector('#searchInput');
const clearSearch = document.querySelector('#clearSearch');
const resultCount = document.querySelector('#resultCount');
const codeList = document.querySelector('#codeList');

function renderCategoryNav() {
    categoryNav.replaceChildren();
    categories.forEach((category) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'category-button';
        button.classList.toggle('is-active', category.id === state.categoryId);
        button.setAttribute('aria-pressed', category.id === state.categoryId ? 'true' : 'false');
        button.append(document.createTextNode(category.label));

        const count = document.createElement('span');
        count.className = 'category-count';
        count.textContent = ` (${category.entries.length})`;
        button.append(count);
        button.addEventListener('click', () => selectCategory(category.id));
        categoryNav.append(button);
    });
}

function selectCategory(categoryId) {
    if (!categoryById.has(categoryId)) return;
    state.categoryId = categoryId;
    state.query = '';
    searchInput.value = '';
    window.history.replaceState(null, '', `#${categoryId}`);
    render();
}

function makeCodeEntry(entry) {
    const [code, title, category, explanation] = entry;
    const article = document.createElement('article');
    article.className = 'code-entry';

    const codeValue = document.createElement('code');
    codeValue.className = 'code-value';
    codeValue.textContent = code;
    article.append(codeValue);

    const copy = document.createElement('div');
    copy.className = 'code-copy';
    const heading = document.createElement('h2');
    heading.className = 'code-title';
    heading.textContent = title;
    copy.append(heading);

    const categoryLabel = document.createElement('p');
    categoryLabel.className = 'eyebrow';
    categoryLabel.textContent = category;
    copy.prepend(categoryLabel);

    const detail = document.createElement('p');
    detail.className = 'code-explanation';
    detail.textContent = explanation;
    copy.append(detail);

    const guidance = guidanceByCode.get(code);
    if (guidance) {
        const guidanceLabel = document.createElement('p');
        guidanceLabel.className = 'eyebrow';
        guidanceLabel.textContent = 'Troubleshooting';
        copy.append(guidanceLabel);

        [
            ['Likely cause', guidance.cause],
            ['FTHR attempt', guidance.automatic],
            ['What to do', guidance.action],
        ].forEach(([label, value]) => {
            const item = document.createElement('p');
            item.className = 'code-explanation';
            item.textContent = `${label}: ${value}`;
            copy.append(item);
        });

        const supportLabel = document.createElement('p');
        supportLabel.className = 'eyebrow';
        supportLabel.textContent = 'Report this error';
        copy.append(supportLabel);
        [
            ['When to export', sharedSupportGuidance.whenToExport],
            ['Ticket checklist', sharedSupportGuidance.ticketDetails],
        ].forEach(([label, value]) => {
            const item = document.createElement('p');
            item.className = 'code-explanation';
            item.textContent = `${label}: ${value}`;
            copy.append(item);
        });
    }
    article.append(copy);

    const copyButton = document.createElement('button');
    copyButton.type = 'button';
    copyButton.className = 'copy-code';
    copyButton.textContent = 'Copy';
    copyButton.setAttribute('aria-label', `Copy ${code}`);
    copyButton.addEventListener('click', () => copyCode(code, copyButton));
    article.append(copyButton);
    return article;
}

async function copyCode(code, button) {
    try {
        await navigator.clipboard.writeText(code);
    } catch (_error) {
        const helper = document.createElement('textarea');
        helper.value = code;
        helper.setAttribute('readonly', '');
        helper.style.position = 'fixed';
        helper.style.opacity = '0';
        document.body.append(helper);
        helper.select();
        document.execCommand('copy');
        helper.remove();
    }
    const original = button.textContent;
    button.textContent = 'Copied';
    window.setTimeout(() => { button.textContent = original; }, 1200);
}

function render() {
    const category = categoryById.get(state.categoryId);
    const query = state.query.trim().toLowerCase();
    const entries = category.entries.filter((entry) => (
        !query || [
            entry.join(' '),
            ...Object.values(guidanceByCode.get(entry[0]) || {}),
            ...(guidanceByCode.has(entry[0])
                ? Object.values(sharedSupportGuidance)
                : []),
        ].join(' ').toLowerCase().includes(query)
    ));

    categoryEyebrow.textContent = category.label;
    pageTitle.textContent = category.label;
    pageDescription.textContent = category.description;
    resultCount.textContent = query
        ? `${entries.length} of ${category.entries.length} entries`
        : `${category.entries.length} entr${category.entries.length === 1 ? 'y' : 'ies'}`;
    clearSearch.hidden = !state.query;
    codeList.replaceChildren();

    if (!entries.length) {
        const empty = document.createElement('p');
        empty.className = 'empty-state';
        empty.textContent = category.entries.length
            ? 'No entries match this filter.'
            : 'No entries have been documented in this category yet.';
        codeList.append(empty);
    } else {
        entries.forEach((entry) => codeList.append(makeCodeEntry(entry)));
    }
    renderCategoryNav();
}

searchInput.addEventListener('input', (event) => {
    state.query = event.target.value;
    render();
});

clearSearch.addEventListener('click', () => {
    state.query = '';
    searchInput.value = '';
    render();
    searchInput.focus();
});

render();
