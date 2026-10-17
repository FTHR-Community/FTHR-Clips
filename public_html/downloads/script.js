// Replace these values when the release artifacts or source repository change.
const DOWNLOAD_CONFIG = {
    windows: {
        href: 'assets/FTHRClipsInstaller.exe',
        filename: 'FTHRClipsInstaller.exe'
    },
    linux: {
        href: 'assets/FTHRClips.AppImage',
        filename: 'FTHRClips.AppImage'
    },
    source: 'https://github.com/FTHR-Community/FTHR-Clips'
};

const HCMode = {
    button: null,

    init() {
        this.button = document.getElementById('hcToggle');
        if (localStorage.getItem('fthr-hc') === '1') this.set(true);
        this.button?.addEventListener('click', () => {
            this.set(!document.body.classList.contains('high-contrast'));
        });
    },

    set(active) {
        document.body.classList.toggle('high-contrast', active);
        this.button?.classList.toggle('active', active);
        localStorage.setItem('fthr-hc', active ? '1' : '0');
    }
};

// This is the same cursor-reactive dot field used by the main site.
const DotField = {
    canvas: null,
    ctx: null,
    dots: [],
    shields: [],
    target: { x: -9999, y: -9999, active: false },
    pointer: { x: -9999, y: -9999, active: false },
    raf: null,
    frame: 0,

    init() {
        this.canvas = document.getElementById('dotField');
        if (!this.canvas) return;
        this.ctx = this.canvas.getContext('2d', { alpha: true });
        this.resize();
        this.updateShields();

        window.addEventListener('resize', () => {
            this.resize();
            this.updateShields();
        }, { passive: true });
        window.addEventListener('scroll', () => this.updateShields(), { passive: true });
        window.addEventListener('pointermove', (event) => {
            this.target.x = event.clientX;
            this.target.y = event.clientY;
            this.target.active = true;
        }, { passive: true });
        window.addEventListener('pointerleave', () => {
            this.target.active = false;
            this.target.x = -9999;
            this.target.y = -9999;
        }, { passive: true });

        this.draw();
    },

    resize() {
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const width = window.innerWidth;
        const height = window.innerHeight;
        this.canvas.width = Math.ceil(width * dpr);
        this.canvas.height = Math.ceil(height * dpr);
        this.canvas.style.width = `${width}px`;
        this.canvas.style.height = `${height}px`;
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        const spacing = Math.max(18, Math.min(width, height) * 0.03);
        const cols = Math.ceil(width / spacing) + 2;
        const rows = Math.ceil(height / spacing) + 2;
        this.dots = [];

        for (let y = -1; y < rows; y++) {
            for (let x = -1; x < cols; x++) {
                this.dots.push({ x: x * spacing, y: y * spacing });
            }
        }
    },

    updateShields() {
        const shielded = document.querySelectorAll('.hero-subtitle, .alpha-notice, .section-header, .launch-status, .footer-note');
        this.shields = Array.from(shielded).map(el => {
            const rect = el.getBoundingClientRect();
            return {
                left: rect.left - 22,
                right: rect.right + 22,
                top: rect.top - 12,
                bottom: rect.bottom + 12
            };
        });
    },

    shieldAmount(dot) {
        let amount = 0;
        this.shields.forEach(rect => {
            const dx = dot.x < rect.left ? rect.left - dot.x : dot.x > rect.right ? dot.x - rect.right : 0;
            const dy = dot.y < rect.top ? rect.top - dot.y : dot.y > rect.bottom ? dot.y - rect.bottom : 0;
            const distance = Math.sqrt(dx * dx + dy * dy);
            const fade = Math.max(0, 1 - distance / 30);
            if (fade > amount) amount = fade;
        });
        return amount;
    },

    draw() {
        const width = window.innerWidth;
        const height = window.innerHeight;
        const radius = Math.max(95, Math.min(width, height) * 0.15);
        const radiusSq = radius * radius;
        if (this.frame++ % 18 === 0) this.updateShields();

        this.pointer.x += (this.target.x - this.pointer.x) * 0.16;
        this.pointer.y += (this.target.y - this.pointer.y) * 0.16;
        this.pointer.active = this.target.active;

        this.ctx.clearRect(0, 0, width, height);

        this.dots.forEach(dot => {
            const dx = dot.x - this.pointer.x;
            const dy = dot.y - this.pointer.y;
            const distSq = dx * dx + dy * dy;
            const pull = this.pointer.active && distSq < radiusSq
                ? 1 - Math.sqrt(distSq) / radius
                : 0;
            const eased = pull * pull * (3 - 2 * pull);
            const shield = this.shieldAmount(dot);
            const visibility = 1 - shield * 0.82;
            const size = (1 + eased * 2.6) * visibility;
            const whiteAlpha = (0.16 + eased * 0.12) * visibility;
            const tealAlpha = eased * 0.92 * visibility;
            if (whiteAlpha < 0.012 && tealAlpha < 0.012) return;

            this.ctx.beginPath();
            this.ctx.arc(dot.x, dot.y, size, 0, Math.PI * 2);
            this.ctx.fillStyle = tealAlpha > 0.01
                ? `rgba(0, 255, 170, ${tealAlpha})`
                : `rgba(255, 255, 255, ${whiteAlpha})`;
            this.ctx.fill();
        });

        this.raf = requestAnimationFrame(() => this.draw());
    }
};

function applyReleaseLinks() {
    document.querySelectorAll('[data-download-link]').forEach(link => {
        const platform = link.dataset.downloadLink;
        const config = DOWNLOAD_CONFIG[platform];
        if (!config) return;
        link.href = config.href;
        link.download = config.filename;
        link.setAttribute('aria-label', `Download ${platform} version`);
        const label = document.querySelector(`[data-file-label="${platform}"]`);
        if (label) label.textContent = config.filename;
    });

    document.querySelectorAll('[data-source-link]').forEach(link => {
        link.href = DOWNLOAD_CONFIG.source;
    });
}

document.addEventListener('DOMContentLoaded', () => {
    HCMode.init();
    DotField.init();
    applyReleaseLinks();
});
