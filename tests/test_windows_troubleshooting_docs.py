from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.error_codes import APP_FAILURE_CODES


ROOT = Path(__file__).resolve().parents[1]
DOC_SCRIPT = ROOT / 'public_html' / 'docs' / 'script.js'


_NODE_DOC_SMOKE = r"""
const fs = require('node:fs');
const vm = require('node:vm');

class MiniElement {
    constructor(tagName) {
        this.tagName = String(tagName).toUpperCase();
        this.children = [];
        this.listeners = {};
        this.attributes = {};
        this.className = '';
        this.hidden = false;
        this.value = '';
        this.style = {};
        this._textContent = '';
        const classes = new Set();
        this.classList = {
            toggle: (name, force) => {
                const enabled = force === undefined ? !classes.has(name) : Boolean(force);
                if (enabled) classes.add(name);
                else classes.delete(name);
                return enabled;
            },
            contains: (name) => classes.has(name),
        };
        MiniElement.created.push(this);
    }

    get textContent() {
        return this._textContent + this.children.map((child) => child.textContent || '').join('');
    }

    set textContent(value) {
        this.children = [];
        this._textContent = String(value ?? '');
    }

    append(...nodes) {
        this._textContent = '';
        this.children.push(...nodes);
    }

    prepend(...nodes) {
        this._textContent = '';
        this.children.unshift(...nodes);
    }

    replaceChildren(...nodes) {
        this.children = [];
        this._textContent = '';
        this.append(...nodes);
    }

    setAttribute(name, value) {
        this.attributes[name] = String(value);
    }

    addEventListener(name, callback) {
        this.listeners[name] = callback;
    }

    dispatchEvent(name, event = {}) {
        const callback = this.listeners[name];
        if (!callback) throw new Error(`No ${name} listener on ${this.tagName}`);
        callback(event);
    }

    focus() {}
    remove() {}
    select() {}
}
MiniElement.created = [];

const ids = [
    'categoryNav', 'categoryEyebrow', 'pageTitle', 'pageDescription',
    'searchInput', 'clearSearch', 'resultCount', 'codeList',
];
const elements = new Map(ids.map((id) => [`#${id}`, new MiniElement('div')]));
const body = new MiniElement('body');
const document = {
    body,
    querySelector: (selector) => {
        const element = elements.get(selector);
        if (!element) throw new Error(`Unexpected selector: ${selector}`);
        return element;
    },
    createElement: (tagName) => new MiniElement(tagName),
    createTextNode: (text) => ({textContent: String(text)}),
    execCommand: () => true,
};
const window = {
    location: {hash: ''},
    history: {
        replaceState(_state, _title, hash) { window.location.hash = hash; },
    },
    setTimeout: () => 0,
};
const navigator = {clipboard: {writeText: async () => {}}};
const context = vm.createContext({document, window, navigator});
const source = fs.readFileSync(process.argv[1], 'utf8');
vm.runInContext(source, context, {filename: process.argv[1]});

const registry = vm.runInContext(
    'categories[0].entries.map((entry) => Array.from(entry))', context);
const guidance = vm.runInContext(
    'Array.from(guidanceByCode.entries()).map(([code, value]) => [code, {...value}])',
    context);
const categoryNav = elements.get('#categoryNav');
const searchInput = elements.get('#searchInput');
const codeList = elements.get('#codeList');
const pageTitle = elements.get('#pageTitle');
const resultCount = elements.get('#resultCount');

const initial = {
    count: codeList.children.length,
    text: codeList.textContent,
    cards: codeList.children.map((card) => card.textContent),
    navCount: categoryNav.children.length,
};

searchInput.value = 'recreate lost replay data';
searchInput.dispatchEvent('input', {target: searchInput});
const guidanceSearch = {
    count: codeList.children.length,
    resultCount: resultCount.textContent,
    text: codeList.textContent,
};

searchInput.value = 'requested versus actual codec/backend/resolution/FPS';
searchInput.dispatchEvent('input', {target: searchInput});
const sharedGuidanceSearch = {
    count: codeList.children.length,
    resultCount: resultCount.textContent,
    text: codeList.textContent,
};

searchInput.value = 'window-rename control';
searchInput.dispatchEvent('input', {target: searchInput});
const pathGuidanceSearch = {
    count: codeList.children.length,
    resultCount: resultCount.textContent,
    text: codeList.textContent,
};

searchInput.value = 'later scan';
searchInput.dispatchEvent('input', {target: searchInput});
const uploadGuidanceSearch = {
    count: codeList.children.length,
    resultCount: resultCount.textContent,
    text: codeList.textContent,
};

categoryNav.children[1].dispatchEvent('click');
const windowsBasics = {
    title: pageTitle.textContent,
    count: codeList.children.length,
    text: codeList.textContent,
};
searchInput.value = '100% is unity';
searchInput.dispatchEvent('input', {target: searchInput});
const windowsSearch = {
    count: codeList.children.length,
    resultCount: resultCount.textContent,
    text: codeList.textContent,
};

elements.get('#clearSearch').dispatchEvent('click');
const maliciousText = '<img id="injected" src=x onerror=alert(1)>';
vm.runInContext(
    `categories[1].entries[0][3] = ${JSON.stringify(maliciousText)}; render();`,
    context);
const escaping = {
    text: codeList.textContent,
    imageElements: MiniElement.created.filter((element) => element.tagName === 'IMG').length,
};

categoryNav.children[2].dispatchEvent('click');
const support = {
    title: pageTitle.textContent,
    count: codeList.children.length,
    text: codeList.textContent,
    hash: window.location.hash,
};

console.log(JSON.stringify({registry, guidance, initial, guidanceSearch,
                            sharedGuidanceSearch, pathGuidanceSearch,
                            uploadGuidanceSearch, windowsBasics, windowsSearch,
                            escaping, support}));
"""


def test_windows_troubleshooting_docs_registry_render_search_and_escape():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required for the docs browser-script smoke test')

    result = subprocess.run(
        [node, '-e', _NODE_DOC_SMOKE, str(DOC_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
        encoding='utf-8',
    )
    rendered = json.loads(result.stdout)

    expected_registry = [
        [item.code, item.title, item.category, item.explanation]
        for item in APP_FAILURE_CODES
    ]
    assert rendered['registry'] == expected_registry
    assert all(len(entry) == 4 for entry in rendered['registry'])
    assert len(rendered['registry']) == len(APP_FAILURE_CODES) == 23

    guidance = dict(rendered['guidance'])
    assert set(guidance) == {item.code for item in APP_FAILURE_CODES}
    assert all(
        set(fields) == {'cause', 'automatic', 'action'}
        and all(fields.values())
        for fields in guidance.values()
    )
    assert rendered['initial']['count'] == 23
    assert rendered['initial']['navCount'] == 3
    assert 'FTHR attempt:' in rendered['initial']['text']
    assert 'What to do:' in rendered['initial']['text']
    assert len(rendered['initial']['cards']) == 23
    assert all(
        'When to export:' in card
        and 'Ticket checklist:' in card
        and 'GPU and driver' in card
        for card in rendered['initial']['cards']
    )

    assert rendered['guidanceSearch']['count'] == 1
    assert rendered['guidanceSearch']['resultCount'] == '1 of 23 entries'
    assert 'Error 015' in rendered['guidanceSearch']['text']
    assert 'Error 014' not in rendered['guidanceSearch']['text']

    assert rendered['sharedGuidanceSearch']['count'] == 23
    assert rendered['sharedGuidanceSearch']['resultCount'] == '23 of 23 entries'
    assert 'session/operation IDs if known' in rendered['sharedGuidanceSearch']['text']
    assert rendered['pathGuidanceSearch']['count'] == 1
    assert 'Error 016' in rendered['pathGuidanceSearch']['text']
    assert 'window-rename control in Folders' in rendered['pathGuidanceSearch']['text']
    assert rendered['uploadGuidanceSearch']['count'] == 1
    assert 'Error 020' in rendered['uploadGuidanceSearch']['text']
    assert 'later scan' in rendered['uploadGuidanceSearch']['text']

    assert rendered['windowsBasics']['title'] == 'Windows Basics'
    assert rendered['windowsBasics']['count'] == 10
    assert 'Settings → Clip → Folders' in rendered['windowsBasics']['text']
    assert 'selected native microphone endpoint' in rendered['windowsBasics']['text']
    assert 'static-scene Error 015 save' in rendered['windowsBasics']['text']
    assert 'SOURCE → Desktop → monitor → APPLY' in rendered['windowsBasics']['text']
    assert 'same-adapter ownership' in rendered['windowsBasics']['text']
    assert '<Clips Storage Folder>\\Recordings' in rendered['windowsBasics']['text']
    assert 'USE DEFAULT resolves the current Windows Videos known folder' in rendered['windowsBasics']['text']
    assert 'Changing the root does not move existing files' in rendered['windowsBasics']['text']
    assert '30 healthy seconds resets that recovery budget' in rendered['windowsBasics']['text']
    assert 'initial GraphicsCaptureItem.Size and the DXGI output extent' in rendered['windowsBasics']['text']
    assert 'not proof of a geometry mismatch' in rendered['windowsBasics']['text']
    assert rendered['windowsSearch']['count'] == 1
    assert 'Microphone input' in rendered['windowsSearch']['text']

    assert rendered['escaping']['text'].find('<img id="injected"') >= 0
    assert rendered['escaping']['imageElements'] == 0

    assert rendered['support']['title'] == 'Diagnostics & Support'
    assert rendered['support']['count'] == 4
    assert 'Settings → General → Troubleshooting' in rendered['support']['text']
    assert 'DRAG ZIP · <name>' in rendered['support']['text']
    assert 'Nothing is uploaded automatically' in rendered['support']['text']
    assert rendered['support']['hash'] == '#diagnostics-support'


def test_windows_troubleshooting_docs_script_parses_with_node():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to parse the docs browser script')

    subprocess.run(
        [node, '--check', str(DOC_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
        encoding='utf-8',
    )
