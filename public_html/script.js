const ROADMAP_FALLBACK = [
        {
            "phase": "01",
            "title": "Foundation",
            "status": "done",
            "note": "Core pipeline is solid",
            "items": [
                { "text": "DXGI screen capture (NVENC path)", "done": true },
                { "text": "Encoded ring buffer \u2014 no raw frames in RAM", "done": true },
                { "text": "Hotkey \u2192 instant clip save (mux only, no re-encode)", "done": true },
                { "text": "x264 software fallback for non-NVIDIA GPUs", "done": true },
                { "text": "PySide6 UI \u2014 clip grid, settings, hotkey config", "done": true },
                { "text": "Python \u2194 C++ IPC via shared memory", "done": true }
            ]
        },
        {
            "phase": "02",
            "title": "Alpha Prep",
            "status": "active",
            "note": "Q3 2026 target",
            "items": [
                { "text": "In-app clip editor (trim, export)", "done": true },
                { "text": "Multiband audio control", "done": true },
                { "text": "UI stability and quality-of-life improvements", "done": false },
                { "text": "Screenshot and extended clip capture", "done": true },
                { "text": "UI customization", "done": true },
                { "text": "Camera capture", "done": true },
                { "text": "Linux support", "done": false },
                { "text": "Installer / first public build", "done": false }
            ]
        },
        {
            "phase": "03",
            "title": "Post-Alpha",
            "status": "upcoming",
            "note": null,
            "items": [
                { "text": "AMD AMF hardware encoding support", "done": false },
                { "text": "Intel QuickSync (QSV) support", "done": false },
                { "text": "Plugin support", "done": false },
                { "text": "Auto-clipping support", "done": false },
                { "text": "Advanced customization options", "done": false }
            ]
        }
    ];

async function loadRoadmapData() {
    try {
        const response = await fetch('roadmap.json', { cache: 'no-store' });
        if (!response.ok) throw new Error(`Roadmap request failed: ${response.status}`);
        return await response.json();
    } catch (error) {
        console.warn('Using embedded roadmap fallback.', error);
        return ROADMAP_FALLBACK;
    }
}

const HCMode = {
            _btns() { return document.querySelectorAll('#hcToggle, #hcToggleMobile'); },
            toggle() {
                const active = document.body.classList.toggle('high-contrast');
                this._btns().forEach(b => b.classList.toggle('active', active));
                localStorage.setItem('fthr-hc', active ? '1' : '0');
            },
            init() {
                if (localStorage.getItem('fthr-hc') === '1') {
                    document.body.classList.add('high-contrast');
                    this._btns().forEach(b => b.classList.add('active'));
                }
            }
        };
        HCMode.init();

        if (history.scrollRestoration) history.scrollRestoration = 'manual';
        window.scrollTo(0, 0);

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

        const SectionNav = {
            sequence: ['hero', 'features', 'how-it-works', 'privacy', 'team', 'coming-soon', 'site-footer'],
            targetIndex: 0,

            getCurrentIndex() {
                const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
                if (window.scrollY >= maxScroll - 8) return this.sequence.length - 1;

                const viewportAnchor = fixedHeaderOffset() + Math.min(window.innerHeight * 0.42, 360);
                let best = this.targetIndex;
                let bestDistance = Infinity;
                this.sequence.forEach((id, i) => {
                    const el = document.getElementById(id);
                    if (!el) return;
                    const rect = el.getBoundingClientRect();
                    const isVisible = rect.bottom > fixedHeaderOffset() && rect.top < window.innerHeight;
                    if (!isVisible) return;

                    const distance = Math.abs(rect.top - viewportAnchor);
                    if (distance < bestDistance) {
                        best = i;
                        bestDistance = distance;
                    }
                });
                return best;
            },

            next() {
                if (this.targetIndex >= this.sequence.length - 1) return;
                scrollToSection(this.sequence[++this.targetIndex]);
            },

            prev() {
                if (this.targetIndex <= 0) return;
                scrollToSection(this.sequence[--this.targetIndex]);
            },

            syncIndex() {
                this.targetIndex = this.getCurrentIndex();
                this.updateArrows();
            },

            updateArrows() {
                const scrollPos = window.scrollY;
                const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
                document.getElementById('btnUp').classList.toggle('hidden', scrollPos < 100);
                document.getElementById('btnDown').classList.toggle('hidden',
                    scrollPos > maxScroll - 100 || this.targetIndex >= this.sequence.length - 1
                );
            }
        };

        const TeamCarousel = {
            current: 0,
            animating: false,
            touchStart: null,

            init() {
                this.root = document.querySelector('[data-team-carousel]');
                if (!this.root) return;

                this.members = [...this.root.querySelectorAll('[data-team-member]')];
                this.dots = [...document.querySelectorAll('[data-team-dot]')];
                this.currentLabel = document.querySelector('[data-team-current]');

                this.root.querySelector('[data-team-prev]')?.addEventListener('click', () => this.prev());
                this.root.querySelector('[data-team-next]')?.addEventListener('click', () => this.next());
                this.dots.forEach((dot, index) => dot.addEventListener('click', () => {
                    if (index === this.current) return;
                    this.show(index, index > this.current ? 1 : -1);
                }));

                document.addEventListener('keydown', (event) => {
                    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
                    if (!this.isActiveSection() || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
                    if (event.key === 'ArrowLeft') {
                        event.preventDefault();
                        this.prev();
                    } else if (event.key === 'ArrowRight') {
                        event.preventDefault();
                        this.next();
                    }
                });

                this.root.addEventListener('touchstart', (event) => {
                    const touch = event.changedTouches[0];
                    this.touchStart = { x: touch.clientX, y: touch.clientY };
                }, { passive: true });

                this.root.addEventListener('touchend', (event) => {
                    if (!this.touchStart) return;
                    const touch = event.changedTouches[0];
                    const dx = touch.clientX - this.touchStart.x;
                    const dy = touch.clientY - this.touchStart.y;
                    this.touchStart = null;
                    if (Math.abs(dx) < 45 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
                    dx < 0 ? this.next() : this.prev();
                }, { passive: true });

                this.updateControls();
            },

            isActiveSection() {
                const section = document.getElementById('team');
                if (!section) return false;
                const rect = section.getBoundingClientRect();
                return rect.top < window.innerHeight * 0.66 && rect.bottom > window.innerHeight * 0.34;
            },

            next() {
                this.show((this.current + 1) % this.members.length, 1);
            },

            prev() {
                this.show((this.current - 1 + this.members.length) % this.members.length, -1);
            },

            show(nextIndex, direction) {
                if (this.animating || nextIndex === this.current || !this.members.length) return;
                this.animating = true;

                const previous = this.members[this.current];
                const next = this.members[nextIndex];
                const enterClass = direction > 0 ? 'team-enter-next' : 'team-enter-prev';
                const exitClass = direction > 0 ? 'team-exit-next' : 'team-exit-prev';

                next.setAttribute('aria-hidden', 'false');
                next.classList.add('active', enterClass);
                previous.classList.add(exitClass);
                this.current = nextIndex;
                this.updateControls();

                window.setTimeout(() => {
                    previous.classList.remove('active', exitClass);
                    previous.setAttribute('aria-hidden', 'true');
                    next.classList.remove(enterClass);
                    this.animating = false;
                }, window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 20 : 650);
            },

            updateControls() {
                if (this.currentLabel) this.currentLabel.textContent = String(this.current + 1).padStart(2, '0');
                this.dots.forEach((dot, index) => {
                    const active = index === this.current;
                    dot.classList.toggle('active', active);
                    dot.setAttribute('aria-selected', String(active));
                    dot.tabIndex = active ? 0 : -1;
                });
            }
        };

        const PrivacyCarousel = {
            current: 0,
            touchStart: null,

            init() {
                this.root = document.querySelector('[data-privacy-carousel]');
                if (!this.root) return;

                this.cards = [...this.root.querySelectorAll('[data-privacy-card]')];
                this.title = this.root.querySelector('[data-privacy-title]');
                this.description = this.root.querySelector('[data-privacy-description]');
                this.eyebrow = this.root.querySelector('[data-privacy-eyebrow]');
                this.count = this.root.querySelector('[data-privacy-count]');
                this.cardsData = [
                    {
                        title: 'No Data Collection',
                        eyebrow: '01 // LOCAL BY DEFAULT',
                        description: 'FTHR collects your clips for you, not what you do with them. There is no analytics pipeline and no telemetry. Your captures stay in the folder you chose unless you explicitly send one through the optional uploader.'
                    },
                    {
                        title: 'No Telemetry',
                        eyebrow: '02 // NOTHING PHONES HOME',
                        description: 'No crash reports, usage pings, diagnostic payloads, or heartbeat requests leave the app. If FTHR crashes, the report stays on your device until you decide to send it.'
                    },
                    {
                        title: 'No Required Account',
                        eyebrow: '03 // INSTALL AND RUN',
                        description: 'Capturing, editing, exporting, and managing local clips requires no sign-up, login, email, or cloud profile. The optional upload provider identity stays separate from your local capture workflow.'
                    },
                    {
                        title: 'Network Only On Your Command',
                        eyebrow: '04 // YOU PICK THE DESTINATION',
                        description: 'FTHR’s core capture workflow makes no outbound connection. The separate uploader connects only after you enable it, and only to the Lustful, Catbox, or custom server you selected.'
                    }
                ];

                this.cards.forEach((card, index) => card.addEventListener('click', () => this.show(index)));
                this.root.querySelector('[data-privacy-prev]')?.addEventListener('click', () => this.prev());
                this.root.querySelector('[data-privacy-next]')?.addEventListener('click', () => this.next());

                document.addEventListener('keydown', (event) => {
                    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
                    if (!this.isActiveSection() || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
                    if (event.key === 'ArrowLeft') {
                        event.preventDefault();
                        this.prev();
                    } else if (event.key === 'ArrowRight') {
                        event.preventDefault();
                        this.next();
                    }
                });

                this.root.addEventListener('touchstart', (event) => {
                    const touch = event.changedTouches[0];
                    this.touchStart = { x: touch.clientX, y: touch.clientY };
                }, { passive: true });

                this.root.addEventListener('touchend', (event) => {
                    if (!this.touchStart) return;
                    const touch = event.changedTouches[0];
                    const dx = touch.clientX - this.touchStart.x;
                    const dy = touch.clientY - this.touchStart.y;
                    this.touchStart = null;
                    if (Math.abs(dx) < 45 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
                    dx < 0 ? this.next() : this.prev();
                }, { passive: true });

                this.updateControls();
            },

            isActiveSection() {
                const section = document.getElementById('privacy');
                if (!section) return false;
                const rect = section.getBoundingClientRect();
                return rect.top < window.innerHeight * 0.7 && rect.bottom > window.innerHeight * 0.3;
            },

            next() { this.show((this.current + 1) % this.cards.length); },
            prev() { this.show((this.current - 1 + this.cards.length) % this.cards.length); },

            show(index) {
                if (!this.cards.length) return;
                this.current = index;
                const data = this.cardsData[index];
                this.cards.forEach((card, cardIndex) => {
                    const active = cardIndex === index;
                    card.classList.toggle('is-active', active);
                    card.setAttribute('aria-pressed', String(active));
                });
                if (this.title) this.title.textContent = data.title;
                if (this.description) this.description.textContent = data.description;
                if (this.eyebrow) this.eyebrow.textContent = data.eyebrow;
                if (this.count) this.count.textContent = `${String(index + 1).padStart(2, '0')} / 04`;

            },

            updateControls() { this.show(this.current); }
        };

        function fixedHeaderOffset() {
            const nav = document.querySelector('nav');
            return nav ? Math.ceil(nav.getBoundingClientRect().bottom) : 0;
        }

        let accordionScrollFrame = null;
        let accordionScrollSequence = 0;
        function accordionScrollTarget(panel, mode) {
            const section = panel.closest('section');
            if (!section) return null;

            const rect = mode === 'collapse' ? section.getBoundingClientRect() : panel.getBoundingClientRect();
            const topLimit = fixedHeaderOffset() + 12;
            const bottomLimit = window.innerHeight - 16;
            const usableHeight = Math.max(0, bottomLimit - topLimit);
            const desiredTop = rect.height <= usableHeight
                ? topLimit + Math.max(0, (usableHeight - rect.height) / 2)
                : topLimit;

            return Math.max(0, window.scrollY + rect.top - desiredTop);
        }

        function scheduleAccordionScroll(panel, mode = 'expand', duration = 460) {
            if (!panel || window.innerWidth > 768) return;
            if (accordionScrollFrame) cancelAnimationFrame(accordionScrollFrame);

            const sequence = ++accordionScrollSequence;
            const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
            const startY = window.scrollY;
            const startTime = performance.now();
            const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
            const settleDuration = Math.max(duration, 520);
            const maxDuration = reducedMotion ? settleDuration + 1100 : settleDuration + 800;
            let previousHeight = null;
            let stableFrames = 0;

            const follow = (now) => {
                if (sequence !== accordionScrollSequence) return;
                const targetTop = accordionScrollTarget(panel, mode);
                if (targetTop === null) return;
                const section = panel.closest('section');
                const targetElement = mode === 'collapse' ? section : panel;
                const targetHeight = targetElement?.getBoundingClientRect().height ?? 0;
                const layoutSettled = previousHeight !== null && Math.abs(targetHeight - previousHeight) < 0.25;
                stableFrames = layoutSettled ? stableFrames + 1 : 0;
                previousHeight = targetHeight;

                const elapsed = now - startTime;
                if (reducedMotion) {
                    if (elapsed < settleDuration || stableFrames < 8) {
                        accordionScrollFrame = requestAnimationFrame(follow);
                        return;
                    }
                    window.scrollTo({ top: targetTop, behavior: 'auto' });
                    accordionScrollFrame = null;
                    return;
                }

                const progress = Math.min(1, elapsed / duration);

                const nextTop = startY + (targetTop - startY) * easeOutCubic(progress);
                window.scrollTo({ top: Math.max(0, nextTop), behavior: 'auto' });

                if (elapsed < settleDuration || stableFrames < 8) {
                    accordionScrollFrame = requestAnimationFrame(follow);
                } else {
                    accordionScrollFrame = null;
                    window.scrollTo({ top: targetTop, behavior: 'auto' });
                }

                if (elapsed >= maxDuration) {
                    accordionScrollFrame = null;
                    window.scrollTo({ top: targetTop, behavior: 'auto' });
                }
            };

            accordionScrollFrame = requestAnimationFrame(follow);
        }

        let activeScrollAnimation = null;
        function smoothWindowScrollTo(top, duration = 380, done = () => {}) {
            if (activeScrollAnimation) cancelAnimationFrame(activeScrollAnimation);

            const start = window.scrollY;
            const change = top - start;
            if (Math.abs(change) < 2) {
                done();
                return;
            }

            const startTime = performance.now();
            const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
            const step = (now) => {
                const progress = Math.min(1, (now - startTime) / duration);
                window.scrollTo(0, start + change * easeOutCubic(progress));

                if (progress < 1) {
                    activeScrollAnimation = requestAnimationFrame(step);
                } else {
                    activeScrollAnimation = null;
                    done();
                }
            };

            activeScrollAnimation = requestAnimationFrame(step);
        }

        const AnimationManager = {
            sections: new Map(),
            wheelTimeout: null,

            init() {
                document.querySelectorAll('.animate-section').forEach(el => {
                    this.sections.set(el.id, { element: el, hasBeenVisible: false });
                });

                this.observer = new IntersectionObserver((entries) => {
                    entries.forEach(entry => {
                        const sec = this.sections.get(entry.target.id);
                        if (sec && entry.isIntersecting) this.reveal(sec);
                    });
                }, { rootMargin: '-10% 0px -10% 0px', threshold: 0.1 });

                this.sections.forEach(sec => this.observer.observe(sec.element));

                setTimeout(() => {
                    this.sections.forEach(sec => {
                        if (sec.element.getBoundingClientRect().top < window.innerHeight * 0.9) this.reveal(sec);
                    });
                }, 100);

                this.setupScrollSquare();
                this.setupWheelNav();
                this.setupRotatingText();
            },

            reveal(sec) {
                if (sec.hasBeenVisible) return;
                sec.hasBeenVisible = true;
                sec.element.classList.add('has-been-visible');
            },

            setupScrollSquare() {
                const square = document.getElementById('scrollSquare');
                if (!square) return;
                const update = () => {
                    const scrollHeight = document.documentElement.scrollHeight - window.innerHeight;
                    const pct = scrollHeight > 0 ? window.scrollY / scrollHeight : 0;
                    square.style.setProperty('--square-position', `${5 + pct * 90}%`);
                    SectionNav.syncIndex();
                };
                window.addEventListener('scroll', update, { passive: true });
                update();
            },

            setupWheelNav() {
                const canUseSectionWheel = window.matchMedia('(min-width: 961px) and (hover: hover) and (pointer: fine)');
                const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
                if (!canUseSectionWheel.matches || reduceMotion.matches) return;

                window.addEventListener('wheel', (e) => {
                    const current = document.getElementById(SectionNav.sequence[SectionNav.getCurrentIndex()]);
                    const tallSection = current && current.scrollHeight > window.innerHeight + 80;
                    if (tallSection || Math.abs(e.deltaY) < 45 || Math.abs(e.deltaY) < Math.abs(e.deltaX)) return;

                    e.preventDefault();
                    clearTimeout(this.wheelTimeout);
                    this.wheelTimeout = setTimeout(() => {
                        e.deltaY > 0 ? SectionNav.next() : SectionNav.prev();
                    }, 30);
                }, { passive: false });
            },

            setupRotatingText() {
                const spans = document.querySelectorAll('.rotating-text span');
                if (spans.length < 2) return;

                let current = 0;
                setInterval(() => {
                    spans[current].classList.remove('active');
                    setTimeout(() => {
                        current = (current + 1) % spans.length;
                        spans[current].classList.add('active');
                    }, 500);
                }, 8500);
            }
        };

        const AccordionManager = {
            _collapse(step) {
                const body = step.querySelector('.step-body');
                body.style.maxHeight = body.scrollHeight + 'px';
                body.offsetHeight; // force reflow
                body.style.maxHeight = '0';
                step.classList.remove('open');
                step.querySelector('.step-trigger')?.setAttribute('aria-expanded', 'false');
            },
            _expand(step) {
                const body = step.querySelector('.step-body');
                step.classList.add('open');
                step.querySelector('.step-trigger')?.setAttribute('aria-expanded', 'true');
                body.style.maxHeight = body.scrollHeight + 'px';
            },
            toggle(trigger) {
                const step = trigger.closest('.flow-step');
                const container = step.closest('.flow-steps');
                const wasOpen = step.classList.contains('open');
                const openSteps = [...container.querySelectorAll('.flow-step.open')];
                openSteps.forEach(s => this._collapse(s));
                if (!wasOpen) {
                    this._expand(step);
                    scheduleAccordionScroll(step, 'expand', 460);
                } else {
                    scheduleAccordionScroll(step, 'collapse', 420);
                }
            }
        };

        const PlatformTabs = {
            init() {
                const section = document.getElementById('how-it-works');
                if (!section) return;

                const buttons = [...section.querySelectorAll('[data-platform-tab]')];
                const panels = [...section.querySelectorAll('[data-platform-panel]')];
                if (!buttons.length || !panels.length) return;

                const show = (platform, moveFocus = false) => {
                    const activeButton = buttons.find(button => button.dataset.platformTab === platform);
                    const activePanel = panels.find(panel => panel.dataset.platformPanel === platform);
                    if (!activeButton || !activePanel) return;

                    buttons.forEach(button => {
                        const isActive = button === activeButton;
                        button.classList.toggle('is-active', isActive);
                        button.setAttribute('aria-selected', isActive ? 'true' : 'false');
                        button.tabIndex = isActive ? 0 : -1;
                    });
                    panels.forEach(panel => {
                        panel.hidden = panel !== activePanel;
                    });
                    if (moveFocus) activeButton.focus();
                };

                buttons.forEach((button, index) => {
                    button.addEventListener('click', () => show(button.dataset.platformTab));
                    button.addEventListener('keydown', event => {
                        const key = event.key;
                        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key)) return;
                        event.preventDefault();
                        const direction = key === 'ArrowLeft' || key === 'Home' ? -1 : 1;
                        const nextIndex = key === 'Home' ? 0
                            : key === 'End' ? buttons.length - 1
                            : (index + direction + buttons.length) % buttons.length;
                        show(buttons[nextIndex].dataset.platformTab, true);
                    });
                });

                const initial = buttons.find(button => button.getAttribute('aria-selected') === 'true') || buttons[0];
                show(initial.dataset.platformTab);
            }
        };

        const FeatureTabs = {
            minHeightTimer: null,

            show(cat) {
                const features = document.getElementById('features');
                if (!features) return;
                if (document.querySelector(`.feature-tab-btn[data-cat="${cat}"]`)?.classList.contains('active')) return;

                const categoriesWrap = features.querySelector('.feature-categories-wrap');
                if (window.innerWidth <= 768 && categoriesWrap) {
                    clearTimeout(this.minHeightTimer);
                    document.body.style.minHeight = `${document.documentElement.scrollHeight}px`;
                    features.style.minHeight = `${features.getBoundingClientRect().height}px`;
                    categoriesWrap.style.minHeight = `${categoriesWrap.getBoundingClientRect().height}px`;
                }

                document.querySelectorAll('.feature-tab-btn').forEach(b =>
                    b.classList.toggle('active', b.dataset.cat === cat)
                );
                document.querySelectorAll('.feature-block.open').forEach(b => FeatureManager._collapse(b, true));
                document.querySelectorAll('.feature-category').forEach(c =>
                    c.classList.toggle('active', c.dataset.cat === cat)
                );

                requestAnimationFrame(() => {
                    const navBottom = fixedHeaderOffset();
                    const tabs = features.querySelector('.feature-tabs');
                    const tabsTop = tabs?.getBoundingClientRect().top ?? features.getBoundingClientRect().top;
                    const desiredTop = navBottom + 16;
                    const releaseReservedHeight = () => {
                        if (!categoriesWrap) return;
                        clearTimeout(this.minHeightTimer);
                        this.minHeightTimer = window.setTimeout(() => {
                            document.body.style.minHeight = '';
                            features.style.minHeight = '';
                            categoriesWrap.style.minHeight = '';
                        }, 80);
                    };

                    if (window.innerWidth <= 768) {
                        const targetTop = Math.min(window.scrollY, Math.max(0, features.offsetTop - desiredTop));
                        smoothWindowScrollTo(
                            targetTop,
                            420,
                            releaseReservedHeight
                        );
                    } else if (window.innerWidth > 768) {
                        features.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        releaseReservedHeight();
                    } else {
                        releaseReservedHeight();
                    }

                    SectionNav.targetIndex = SectionNav.sequence.indexOf('features');
                    SectionNav.updateArrows();
                });
            }
        };

        const FeatureDemos = {
            keys: {
                capture: 'fthr-feature-capture',
                upload: 'fthr-feature-upload',
                audio: 'fthr-feature-audio',
                theme: 'fthr-feature-theme'
            },
            timers: {},

            read(key, fallback) {
                try {
                    const raw = localStorage.getItem(key);
                    return raw ? JSON.parse(raw) : fallback;
                } catch (error) {
                    return fallback;
                }
            },

            write(key, value) {
                try {
                    localStorage.setItem(key, JSON.stringify(value));
                } catch (error) {
                    // Ignore private-mode storage failures; the demo still works for the session.
                }
            },

            markSaved(name) {
                const el = document.querySelector(`[data-save-state="${name}"]`);
                if (!el) return;
                el.textContent = 'Saved';
                el.classList.add('flash');
                clearTimeout(this.timers[name]);
                this.timers[name] = setTimeout(() => el.classList.remove('flash'), 700);
            },

            refresh(root) {
                const block = root.closest('.feature-block');
                if (typeof FeatureManager !== 'undefined') FeatureManager.refresh(block);
            },

            initCapture() {
                const root = document.querySelector('[data-demo="capture"]');
                if (!root) return;

                const defaults = { framerate: '60 FPS', resolution: 'Source', bitrate: 'High' };
                const saved = { ...defaults, ...this.read(this.keys.capture, {}) };
                const selects = root.querySelectorAll('[data-capture-setting]');
                const aliases = {
                    framerate: {
                        '30fps': '60 FPS',
                        '30 FPS': '60 FPS',
                        '60fps': '60 FPS',
                        '120fps': '120 FPS',
                        '240fps': '240 FPS',
                        '360fps': '360 FPS'
                    },
                    resolution: {
                        '1920x1080': '1080p',
                        '2560x1440': '1440p',
                        '3440x1440': '1440p',
                        '3840x2160': 'Source'
                    },
                    bitrate: {
                        '18 Mbps': 'Low',
                        '35 Mbps': 'High',
                        '60 Mbps': 'Custom',
                        '100 Mbps': 'Custom',
                        'Lossless': 'Custom'
                    }
                };

                selects.forEach(select => {
                    const key = select.dataset.captureSetting;
                    const value = aliases[key]?.[saved[key]] || saved[key];
                    if ([...select.options].some(option => option.value === value)) {
                        select.value = value;
                    }
                    select.addEventListener('change', () => render(true));
                });

                const values = () => {
                    const current = {};
                    selects.forEach(select => { current[select.dataset.captureSetting] = select.value; });
                    return current;
                };

                const render = (persist) => {
                    const current = values();

                    if (persist) {
                        this.write(this.keys.capture, current);
                        this.markSaved('capture');
                    }
                    this.refresh(root);
                };

                render(false);
            },

            initUpload() {
                const root = document.querySelector('[data-demo="upload"]');
                if (!root) return;

                const select = root.querySelector('[data-upload-provider]');
                const profiles = {
                    lustful: {
                        url: 'https://fthr.lustful.wtf',
                        hideUrl: true,
                        extraLabel: 'Account',
                        extra: 'Hardware-bound account',
                        extraReadonly: true
                    },
                    catbox: {
                        url: 'https://catbox.moe/user/api.php',
                        hideUrl: true,
                        extraLabel: 'Userhash',
                        extra: 'Catbox userhash (optional)',
                        extraReadonly: false
                    },
                    custom: {
                        url: 'https://your-server.example.com/upload',
                        hideUrl: false,
                        extraLabel: 'Client ID',
                        extra: 'client_id',
                        extraReadonly: false
                    }
                };

                const saved = this.read(this.keys.upload, { provider: 'lustful' });
                const savedProvider = saved.provider === 'server' ? 'custom' : saved.provider;
                if (select && profiles[savedProvider]) select.value = savedProvider;

                const render = (persist) => {
                    const provider = select?.value || 'lustful';
                    const profile = profiles[provider] || profiles.lustful;
                    ['url', 'extra'].forEach(key => {
                        const el = root.querySelector(`[data-upload-value="${key}"]`);
                        if (el) el.value = profile[key];
                    });
                    const urlRow = root.querySelector('[data-upload-url-row]');
                    if (urlRow) urlRow.hidden = profile.hideUrl;
                    const extraLabel = root.querySelector('[data-upload-extra-label]');
                    if (extraLabel) extraLabel.textContent = profile.extraLabel;
                    const extraInput = root.querySelector('[data-upload-value="extra"]');
                    if (extraInput) {
                        extraInput.readOnly = profile.extraReadonly;
                        extraInput.setAttribute('aria-readonly', String(profile.extraReadonly));
                    }
                    if (persist) {
                        this.write(this.keys.upload, { provider });
                        this.markSaved('upload');
                    }
                    this.refresh(root);
                };

                select?.addEventListener('change', () => render(true));
                render(false);
            },

            initAudio() {
                const root = document.querySelector('[data-demo="audio"]');
                const list = root?.querySelector('[data-audio-list]');
                if (!root || !list) return;

                const saved = this.read(this.keys.audio, { volumes: {} });

                const save = () => {
                    const payload = { volumes: {} };
                    list.querySelectorAll('.audio-track').forEach(row => {
                        payload.volumes[row.dataset.track] = row.querySelector('input[type="range"]').value;
                    });
                    this.write(this.keys.audio, payload);
                    this.markSaved('audio');
                    this.refresh(root);
                };

                const updateRangeLabel = (range) => {
                    const value = range.closest('.audio-track')?.querySelector('.track-value');
                    if (value) value.textContent = `${range.value}%`;
                    range.style.setProperty('--slider-value', `${range.value}%`);
                };

                list.querySelectorAll('input[type="range"]').forEach(range => {
                    const id = range.closest('.audio-track')?.dataset.track;
                    if (id && saved.volumes?.[id] !== undefined) range.value = saved.volumes[id];
                    updateRangeLabel(range);
                    range.addEventListener('input', () => {
                        updateRangeLabel(range);
                        save();
                    });
                });
            },

            initTheme() {
                const root = document.querySelector('[data-demo="theme"]');
                const target = document.querySelector('[data-theme-target]');
                if (!root || !target) return;

                const defaults = { bg: '#001a12', text: '#ffffff', highlight: '#00ffaa', icon: '#9a9a9a' };
                const saved = { ...defaults, ...this.read(this.keys.theme, {}) };
                const inputs = root.querySelectorAll('[data-theme-token]');

                const isColor = (value) => /^#[0-9a-f]{6}$/i.test(value);
                inputs.forEach(input => {
                    const key = input.dataset.themeToken;
                    input.value = isColor(saved[key]) ? saved[key] : defaults[key];
                    input.addEventListener('input', () => apply(true));
                    input.addEventListener('change', () => apply(true));
                });

                const values = () => {
                    const current = {};
                    inputs.forEach(input => { current[input.dataset.themeToken] = input.value; });
                    return current;
                };

                const apply = (persist) => {
                    const current = values();
                    target.style.setProperty('--theme-row-bg', current.bg);
                    target.style.setProperty('--theme-row-text', current.text);
                    target.style.setProperty('--theme-row-highlight', current.highlight);
                    target.style.setProperty('--theme-row-icon', current.icon);
                    if (persist) {
                        this.write(this.keys.theme, current);
                        this.markSaved('theme');
                    }
                    this.refresh(root);
                };

                root.querySelector('[data-theme-reset]')?.addEventListener('click', () => {
                    inputs.forEach(input => { input.value = defaults[input.dataset.themeToken]; });
                    apply(true);
                });

                apply(false);
            },

            initCustomSelects() {
                const selects = document.querySelectorAll('.feature-demo select');
                const closeSelect = (root) => {
                    root.classList.remove('open');
                    root.querySelector('.mock-select-btn')?.setAttribute('aria-expanded', 'false');
                    root.closest('.feature-block')?.classList.remove('dropdown-open');
                };
                const closeAll = (except = null) => {
                    document.querySelectorAll('.mock-select.open').forEach(root => {
                        if (root !== except) closeSelect(root);
                    });
                };

                selects.forEach(select => {
                    if (select.dataset.enhanced === '1') return;
                    select.dataset.enhanced = '1';
                    select.classList.add('native-select-hidden');

                    const root = document.createElement('div');
                    root.className = 'mock-select';

                    const button = document.createElement('button');
                    button.type = 'button';
                    button.className = 'mock-select-btn';
                    button.setAttribute('aria-haspopup', 'listbox');
                    button.setAttribute('aria-expanded', 'false');

                    const list = document.createElement('div');
                    list.className = 'mock-select-options';
                    list.setAttribute('role', 'listbox');

                    const optionButtons = [...select.options].map(option => {
                        const optionButton = document.createElement('button');
                        optionButton.type = 'button';
                        optionButton.className = 'mock-select-option';
                        optionButton.setAttribute('role', 'option');
                        optionButton.dataset.value = option.value;
                        optionButton.textContent = option.textContent;
                        optionButton.addEventListener('click', () => {
                            select.value = option.value;
                            select.dispatchEvent(new Event('change', { bubbles: true }));
                            closeSelect(root);
                        });
                        list.appendChild(optionButton);
                        return optionButton;
                    });

                    const sync = () => {
                        const selected = select.selectedOptions[0];
                        button.textContent = selected ? selected.textContent : '';
                        optionButtons.forEach(optionButton => {
                            const active = optionButton.dataset.value === select.value;
                            optionButton.classList.toggle('active', active);
                            optionButton.setAttribute('aria-selected', active ? 'true' : 'false');
                        });
                    };

                    button.addEventListener('click', () => {
                        const willOpen = !root.classList.contains('open');
                        closeAll(root);
                        root.classList.toggle('open', willOpen);
                        button.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
                        root.closest('.feature-block')?.classList.toggle('dropdown-open', willOpen);
                    });

                    root.addEventListener('keydown', (event) => {
                        if (event.key === 'Escape') {
                            closeSelect(root);
                            button.focus();
                        }
                    });

                    select.addEventListener('change', sync);
                    root.append(button, list);
                    select.insertAdjacentElement('afterend', root);
                    sync();
                });

                document.addEventListener('click', (event) => {
                    if (!event.target.closest('.mock-select')) closeAll();
                });
            },

            init() {
                this.initCapture();
                this.initUpload();
                this.initAudio();
                this.initTheme();
                this.initCustomSelects();
            }
        };

        const FeatureManager = {
            _collapse(block, immediate = false) {
                const body = block.querySelector('.feature-body');
                if (immediate) {
                    const previousTransition = body.style.transition;
                    body.style.transition = 'none';
                    body.style.maxHeight = '0';
                    block.classList.remove('open', 'dropdown-open');
                    block.querySelector('.feature-trigger')?.setAttribute('aria-expanded', 'false');
                    body.offsetHeight;
                    body.style.transition = previousTransition;
                    return;
                }
                body.style.maxHeight = body.scrollHeight + 'px';
                body.offsetHeight; // force reflow
                body.style.maxHeight = '0';
                block.classList.remove('open', 'dropdown-open');
                block.querySelector('.feature-trigger')?.setAttribute('aria-expanded', 'false');
            },
            _expand(block) {
                const body = block.querySelector('.feature-body');
                block.classList.add('open');
                block.querySelector('.feature-trigger')?.setAttribute('aria-expanded', 'true');
                body.style.maxHeight = body.scrollHeight + 'px';
            },
            refresh(block) {
                if (!block || !block.classList.contains('open')) return;
                const body = block.querySelector('.feature-body');
                body.style.maxHeight = body.scrollHeight + 'px';
            },
            toggle(trigger) {
                const block = trigger.closest('.feature-block');
                const container = block.closest('.feature-blocks');
                const wasOpen = block.classList.contains('open');
                const openBlocks = [...container.querySelectorAll('.feature-block.open')];
                openBlocks.forEach(b => this._collapse(b));
                if (!wasOpen) {
                    this._expand(block);
                    scheduleAccordionScroll(block, 'expand', 460);
                } else {
                    scheduleAccordionScroll(block, 'collapse', 420);
                }
            }
        };

        let isFlipping = false;
        function handleLogoClick() {
            const logo = document.getElementById('logo');
            if (window.scrollY < 50) {
                if (!isFlipping) {
                    isFlipping = true;
                    logo.classList.add('flipping');
                    setTimeout(() => { logo.classList.remove('flipping'); isFlipping = false; }, 600);
                }
            } else {
                window.scrollTo({ top: 0, behavior: 'smooth' });
                SectionNav.targetIndex = 0;
            }
        }

        function scrollToSection(id) {
            const el = document.getElementById(id);
            if (!el) return;
            const block = id === 'site-footer' ? 'end' : id === 'coming-soon' ? 'start' : 'center';
            el.scrollIntoView({ behavior: 'smooth', block });
            const idx = SectionNav.sequence.indexOf(id);
            if (idx !== -1) SectionNav.targetIndex = idx;
            if (!el.classList.contains('animate-section')) return;
            el.classList.remove('section-jump-target');
            void el.offsetWidth;
            el.classList.add('section-jump-target');
            el.addEventListener('animationend', () => el.classList.remove('section-jump-target'), { once: true });
        }

        document.addEventListener('DOMContentLoaded', async () => {
            document.querySelectorAll('.step-trigger, .feature-trigger').forEach(button => {
                button.setAttribute('aria-expanded', 'false');
            });

            DotField.init();
            AnimationManager.init();
            FeatureDemos.init();
            PrivacyCarousel.init();
            TeamCarousel.init();
            PlatformTabs.init();

            // Build roadmap from JSON data block above
            const phases = await loadRoadmapData();
            const container = document.getElementById('roadmapContainer');
            const statusLabel = { done: 'Complete', active: 'In Progress', upcoming: 'Upcoming' };

            phases.forEach(phase => {
                const phaseEl = document.createElement('div');
                phaseEl.className = `roadmap-phase status-${phase.status}`;

                const header = `
                    <div class="roadmap-phase-header">
                        <span class="roadmap-phase-num">${phase.phase}</span>
                        <div class="roadmap-phase-title-wrap">
                            <span class="roadmap-phase-title">${phase.title}</span>
                            ${phase.note ? `<span class="roadmap-phase-note">${phase.note}</span>` : ''}
                        </div>
                        <span class="roadmap-status-badge ${phase.status}">${statusLabel[phase.status]}</span>
                    </div>`;

                const items = phase.items.map(item => `
                    <div class="roadmap-item">
                        <div class="roadmap-item-check ${item.done ? 'checked' : ''}"></div>
                        <span class="roadmap-item-text ${item.done ? 'done-text' : ''}">${item.text}</span>
                    </div>`).join('');

                phaseEl.innerHTML = header + `<div class="roadmap-items">${items}</div>`;
                container.appendChild(phaseEl);
            });
        });
