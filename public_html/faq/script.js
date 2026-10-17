const HCMode = {
    button: null,
    toggle() {
        const active = document.body.classList.toggle('high-contrast');
        if (this.button) this.button.classList.toggle('active', active);
        localStorage.setItem('fthr-hc', active ? '1' : '0');
    },
    init() {
        this.button = document.getElementById('hcToggle');
        if (this.button) this.button.addEventListener('click', () => this.toggle());
        const active = localStorage.getItem('fthr-hc') === '1';
        document.body.classList.toggle('high-contrast', active);
        if (this.button) this.button.classList.toggle('active', active);
    }
};

const DotField = {
    canvas: null,
    ctx: null,
    dots: [],
    pointer: { x: -9999, y: -9999, active: false },
    target: { x: -9999, y: -9999, active: false },

    init() {
        this.canvas = document.getElementById('dotField');
        if (!this.canvas) return;
        this.ctx = this.canvas.getContext('2d', { alpha: true });
        this.resize();

        window.addEventListener('resize', () => this.resize(), { passive: true });
        window.addEventListener('pointermove', (event) => {
            this.target.x = event.clientX;
            this.target.y = event.clientY;
            this.target.active = true;
        }, { passive: true });
        window.addEventListener('pointerleave', () => {
            this.target.x = -9999;
            this.target.y = -9999;
            this.target.active = false;
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

    draw() {
        const width = window.innerWidth;
        const height = window.innerHeight;
        const radius = Math.max(95, Math.min(width, height) * 0.15);
        const radiusSq = radius * radius;

        this.pointer.x += (this.target.x - this.pointer.x) * 0.16;
        this.pointer.y += (this.target.y - this.pointer.y) * 0.16;
        this.pointer.active = this.target.active;

        this.ctx.clearRect(0, 0, width, height);

        this.dots.forEach((dot) => {
            const dx = dot.x - this.pointer.x;
            const dy = dot.y - this.pointer.y;
            const distSq = dx * dx + dy * dy;
            const pull = this.pointer.active && distSq < radiusSq
                ? 1 - Math.sqrt(distSq) / radius
                : 0;
            const eased = pull * pull * (3 - 2 * pull);
            const size = 1 + eased * 2.6;
            const whiteAlpha = 0.16 + eased * 0.12;
            const tealAlpha = eased * 0.92;

            this.ctx.beginPath();
            this.ctx.arc(dot.x, dot.y, size, 0, Math.PI * 2);
            this.ctx.fillStyle = tealAlpha > 0.01
                ? `rgba(0, 255, 170, ${tealAlpha})`
                : `rgba(255, 255, 255, ${whiteAlpha})`;
            this.ctx.fill();
        });

        requestAnimationFrame(() => this.draw());
    }
};

document.addEventListener('DOMContentLoaded', () => {
    HCMode.init();
    DotField.init();
});
