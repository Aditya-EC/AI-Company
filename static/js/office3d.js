// VCoOS HQ: a Three.js virtual office. Department rooms sit in a ring around the shared orchestration
// engine. Every visual state comes from the backend (VCOOSOffice state): light flows only to rooms that
// have running tasks, avatars show their real status, and an empty chair means the employee is offline.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { EffectComposer } from "three/addons/postprocessing/EffectComposer.js";
import { RenderPass } from "three/addons/postprocessing/RenderPass.js";
import { UnrealBloomPass } from "three/addons/postprocessing/UnrealBloomPass.js";
import { OutputPass } from "three/addons/postprocessing/OutputPass.js";

const Office = window.VCOOSOffice;
const container = document.getElementById("office-canvas");
const REDUCED_MOTION = matchMedia("(prefers-reduced-motion: reduce)").matches;

function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
  } catch (e) { return false; }
}

if (!window.VCOOS_OFFICE.force2d) {
  if (!webglAvailable()) Office.fallback2d("WebGL not supported on this device");
  else init();
}

function init() {
  // ---------------------------------------------------------------- constants & helpers
  const W = 11, D = 9, WALL_H = 2.6, WALL_T = 0.25, SLAB = 0.55, HUB_R = 7.5, MAX_SEATS = 9;
  const ISO = new THREE.Vector3(1, 1.05, 1).normalize();
  const SKIN = [0xf1c7a0, 0xe0ac69, 0xc68642, 0x8d5524, 0xffdbac, 0xd9a066];
  const HAIR = [0x2b1d14, 0x4a2e1a, 0x0f0f10, 0x8a5a2b, 0xc9a36b, 0x6b2f1a, 0x9ca3af];
  const hash = (s) => { let h = 2166136261; for (const c of String(s)) h = Math.imul(h ^ c.charCodeAt(0), 16777619); return h >>> 0; };
  const shade = (hex, f) => new THREE.Color(hex).lerp(new THREE.Color(f > 0 ? 0xffffff : 0x000000), Math.abs(f));
  const stateColor = (k) => new THREE.Color((Office.STATES[k] || ["", "#94a3b8"])[1]);

  const fxStored = (() => { try { return localStorage.getItem("vcoos-office-fx"); } catch (e) { return null; } })();
  let effects = fxStored ? fxStored === "on" : !(innerWidth < 800 || (navigator.hardwareConcurrency || 8) <= 4);

  // ---------------------------------------------------------------- renderer, scene, camera
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.75));
  renderer.shadowMap.enabled = effects;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  container.prepend(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a0f1d);
  scene.fog = new THREE.Fog(0x0a0f1d, 90, 210);
  const camera = new THREE.PerspectiveCamera(38, 1, 0.5, 600);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.maxPolarAngle = Math.PI / 2.4;
  controls.minDistance = 10;
  controls.maxDistance = 190;

  scene.add(new THREE.HemisphereLight(0x9fb4ff, 0x0b1020, 0.55));
  scene.add(new THREE.AmbientLight(0x1e293b, 0.6));
  const moon = new THREE.DirectionalLight(0xdbe4ff, 1.25);
  moon.position.set(40, 70, 30);
  moon.castShadow = true;
  moon.shadow.mapSize.set(2048, 2048);
  moon.shadow.bias = -0.0004;
  Object.assign(moon.shadow.camera, { left: -70, right: 70, top: 70, bottom: -70, near: 1, far: 220 });
  scene.add(moon);

  // Composer with bloom for emissive materials (hub, lamps, screens).
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 0.75, 0.5, 0.82);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  // Ground: dark plate with a faint dot grid.
  const dots = document.createElement("canvas");
  dots.width = dots.height = 64;
  const dctx = dots.getContext("2d");
  dctx.fillStyle = "#0d1424"; dctx.fillRect(0, 0, 64, 64);
  dctx.fillStyle = "rgba(148,163,184,0.18)"; dctx.beginPath(); dctx.arc(32, 32, 1.4, 0, Math.PI * 2); dctx.fill();
  const dotTex = new THREE.CanvasTexture(dots);
  dotTex.wrapS = dotTex.wrapT = THREE.RepeatWrapping;
  dotTex.repeat.set(120, 120);
  dotTex.colorSpace = THREE.SRGBColorSpace;
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(600, 600), new THREE.MeshStandardMaterial({ map: dotTex, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -SLAB;
  ground.receiveShadow = true;
  scene.add(ground);

  // Shared glow sprite texture.
  const glowCanvas = document.createElement("canvas");
  glowCanvas.width = glowCanvas.height = 64;
  const gctx = glowCanvas.getContext("2d");
  const grad = gctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  grad.addColorStop(0, "rgba(255,255,255,1)"); grad.addColorStop(0.35, "rgba(255,255,255,0.45)"); grad.addColorStop(1, "rgba(255,255,255,0)");
  gctx.fillStyle = grad; gctx.fillRect(0, 0, 64, 64);
  const glowTex = new THREE.CanvasTexture(glowCanvas);
  const glowSprite = (color, size, opacity = 1) => {
    const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: glowTex, color, transparent: true, opacity, depthWrite: false, blending: THREE.AdditiveBlending }));
    s.scale.set(size, size, size);
    return s;
  };

  // Geometry/material caches keep hundreds of meshes cheap.
  const G = {}, Mt = {};
  const geo = (key, make) => G[key] || (G[key] = make());
  const mat = (key, make) => Mt[key] || (Mt[key] = make());
  const std = (color, extra = {}) => mat(`s:${color}:${JSON.stringify(extra)}`, () => new THREE.MeshStandardMaterial({ color, roughness: 0.75, ...extra }));
  const mesh = (g, m, { cast = true, receive = true } = {}) => { const o = new THREE.Mesh(g, m); o.castShadow = cast; o.receiveShadow = receive; return o; };
  const box = (w, h, d) => geo(`b:${w}:${h}:${d}`, () => new THREE.BoxGeometry(w, h, d));

  // ---------------------------------------------------------------- scene graph state
  const officeGroup = new THREE.Group();
  scene.add(officeGroup);
  const labels = [];   // {el, obj, dy, kind}
  const pickables = [];
  const avatars = new Map();
  const rooms = new Map();
  let hub = null, layoutKey = "", placed = [], focusedRoom = null;

  function addLabel(html, obj, dy, cls) {
    const el = document.createElement("div");
    el.className = "office-label " + (cls || "");
    el.innerHTML = html;
    container.appendChild(el);
    labels.push({ el, obj, dy, kind: cls });
    return el;
  }

  function clearOffice() {
    officeGroup.clear();
    labels.forEach((l) => l.el.remove());
    labels.length = 0;
    pickables.length = 0;
    avatars.clear();
    rooms.clear();
    hub = null;
  }

  // ---------------------------------------------------------------- the engine hub
  function buildHub() {
    const g = new THREE.Group();
    const base = mesh(geo("hubBase", () => new THREE.CylinderGeometry(HUB_R, HUB_R + 0.4, SLAB, 64)), std(0x0f172a, { roughness: 0.6, metalness: 0.3 }));
    base.position.y = -SLAB / 2;
    const top = mesh(geo("hubTop", () => new THREE.CylinderGeometry(HUB_R - 1.6, HUB_R - 1.4, 0.18, 64)), std(0x111c33, { roughness: 0.4, metalness: 0.5 }));
    top.position.y = 0.09;
    g.add(base, top);
    const ringMat = new THREE.MeshStandardMaterial({ color: 0x22d3ee, emissive: 0x22d3ee, emissiveIntensity: 1.6, roughness: 0.3 });
    ringMat.userData = { base: 1.6 };
    const rings = [2.4, 3.6, 4.9, 6.6].map((r, i) => {
      const t = new THREE.Mesh(geo(`hr${r}`, () => new THREE.TorusGeometry(r, i === 3 ? 0.05 : 0.07, 8, 96)), ringMat);
      t.rotation.x = -Math.PI / 2;
      t.position.y = 0.2 + i * 0.01;
      g.add(t);
      return t;
    });
    const coreMat = new THREE.MeshStandardMaterial({ color: 0xa5f3fc, emissive: 0x67e8f9, emissiveIntensity: 2.2, roughness: 0.2 });
    const core = new THREE.Mesh(geo("hubCore", () => new THREE.SphereGeometry(1.05, 40, 28)), coreMat);
    core.position.y = 2.3;
    const orbitA = new THREE.Mesh(geo("orbA", () => new THREE.TorusGeometry(1.7, 0.03, 6, 80)), ringMat);
    const orbitB = orbitA.clone();
    orbitA.position.y = orbitB.position.y = 2.3;
    orbitA.rotation.set(1.1, 0.2, 0);
    orbitB.rotation.set(-0.9, 0.6, 0.4);
    const beamMat = new THREE.MeshBasicMaterial({ color: 0x67e8f9, transparent: true, opacity: 0.18, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });
    const beam = new THREE.Mesh(geo("beam", () => new THREE.CylinderGeometry(0.32, 0.5, 22, 24, 1, true)), beamMat);
    beam.position.y = 11.5;
    const halo = glowSprite(0x67e8f9, 9, 0.55);
    halo.position.y = 2.3;
    g.add(core, orbitA, orbitB, beam, halo);
    [base, top, core].forEach((m) => { m.userData = { kind: "hub" }; pickables.push(m); });
    officeGroup.add(g);
    const anchor = new THREE.Object3D();
    anchor.position.set(0, 5.4, 0);
    g.add(anchor);
    const label = addLabel("", anchor, 0, "hub");
    hub = { g, rings, core, coreMat, ringMat, orbitA, orbitB, beam, beamMat, halo, label };
  }

  function applyHub(h) {
    if (!hub) return;
    const alive = (h.workers || 0) > 0;
    const c = new THREE.Color(alive ? 0x22d3ee : 0xf59e0b);
    hub.ringMat.color.copy(c); hub.ringMat.emissive.copy(c);
    hub.coreMat.emissive.copy(alive ? new THREE.Color(0x67e8f9) : new THREE.Color(0xb45309));
    hub.beamMat.color.copy(c);
    hub.beam.visible = alive;
    hub.halo.material.color.copy(c);
    hub.activity = alive ? Math.min(h.running || 0, 10) : 0;
    hub.label.innerHTML = `<div class="ol-kicker">THE ENGINE</div><div class="ol-sub">${alive ? `${h.running || 0} running · ${h.queued || 0} queued` : "no worker running"}</div>`;
  }

  // ---------------------------------------------------------------- rooms
  function wallScreenTexture(d) {
    const c = document.createElement("canvas");
    c.width = 256; c.height = 128;
    const x = c.getContext("2d");
    x.fillStyle = "#0b1222"; x.fillRect(0, 0, 256, 128);
    x.fillStyle = d.color; x.fillRect(0, 0, 256, 6);
    x.fillStyle = "#e2e8f0"; x.font = "bold 20px sans-serif"; x.fillText(d.name.slice(0, 18), 12, 34);
    const s = d.stats || {};
    const vals = [["run", s.running || 0, "#22c55e"], ["queue", s.queued || 0, "#38bdf8"], ["review", s.review || 0, "#a78bfa"], ["done", s.completed_today || 0, "#e2e8f0"]];
    const max = Math.max(1, ...vals.map((v) => v[1]));
    vals.forEach(([label, v, col], i) => {
      const bx = 14 + i * 60, h = 54 * (v / max);
      x.fillStyle = "rgba(148,163,184,0.15)"; x.fillRect(bx, 50, 40, 54);
      x.fillStyle = col; x.fillRect(bx, 104 - h, 40, h);
      x.fillStyle = "#94a3b8"; x.font = "12px sans-serif"; x.fillText(`${label} ${v}`, bx, 120);
    });
    const tex = new THREE.CanvasTexture(c);
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  function seatLayout(n) {
    const count = Math.min(Math.max(n, 1), MAX_SEATS);
    const cols = Math.min(3, count), rows = Math.ceil(count / 3);
    const zs = rows === 1 ? [0.4] : rows === 2 ? [-1.3, 2.0] : [-2.3, 0.3, 2.9];
    const seats = [];
    for (let i = 0; i < count; i++) {
      const r = Math.floor(i / 3), c = i % 3, inRow = Math.min(3, count - r * 3);
      const xs = inRow === 1 ? [0.6] : inRow === 2 ? [-1.0, 2.2] : [-2.3, 0.6, 3.5];
      seats.push({ x: xs[c], z: zs[r] });
    }
    return seats;
  }

  function buildDesk(parent, x, z) {
    const g = new THREE.Group();
    g.position.set(x, 0, z);
    const top = mesh(box(1.7, 0.07, 0.8), std(0xf1f5f9, { roughness: 0.4 }));
    top.position.y = 0.78;
    g.add(top);
    for (const [lx, lz] of [[-0.78, -0.33], [0.78, -0.33], [-0.78, 0.33], [0.78, 0.33]]) {
      const leg = mesh(box(0.05, 0.78, 0.05), std(0x111827));
      leg.position.set(lx, 0.39, lz);
      g.add(leg);
    }
    const stand = mesh(box(0.06, 0.32, 0.06), std(0x111827));
    stand.position.set(0, 0.97, -0.22);
    const frame = mesh(box(0.9, 0.52, 0.05), std(0x0b0f19, { roughness: 0.3 }));
    frame.position.set(0, 1.36, -0.24);
    const screenMat = new THREE.MeshStandardMaterial({ color: 0x0b1222, emissive: 0x000000, emissiveIntensity: 1.4, roughness: 0.25 });
    const screen = new THREE.Mesh(geo("screen", () => new THREE.PlaneGeometry(0.82, 0.44)), screenMat);
    screen.position.set(0, 1.36, -0.21);
    const kb = mesh(box(0.55, 0.03, 0.18), std(0x1f2937));
    kb.position.set(0, 0.83, 0.12);
    g.add(stand, frame, screen, kb);
    // Chair (behind the desk, facing it)
    const seat = mesh(box(0.6, 0.09, 0.56), std(0x1f2937));
    seat.position.set(0, 0.55, 0.95);
    const back = mesh(box(0.58, 0.62, 0.08), std(0x1f2937));
    back.position.set(0, 0.92, 1.24);
    const pole = mesh(geo("pole", () => new THREE.CylinderGeometry(0.04, 0.04, 0.45, 8)), std(0x111827));
    pole.position.set(0, 0.3, 0.95);
    const foot = mesh(geo("foot", () => new THREE.CylinderGeometry(0.32, 0.32, 0.05, 10)), std(0x111827));
    foot.position.set(0, 0.04, 0.95);
    g.add(seat, back, pole, foot);
    parent.add(g);
    return { group: g, screenMat };
  }

  function buildAvatar(parent, e, x, z) {
    const g = new THREE.Group();
    g.position.set(x, 0, z + 0.95);
    const h = hash(e.employee_id || e.id);
    const shirt = new THREE.MeshStandardMaterial({ color: e.color, roughness: 0.7 });
    const skin = std(SKIN[h % SKIN.length], { roughness: 0.85 });
    const hairM = std(HAIR[(h >> 4) % HAIR.length], { roughness: 0.9 });
    const pants = std(0x1e293b);
    const torso = mesh(geo("torso", () => new THREE.CapsuleGeometry(0.24, 0.32, 6, 12)), shirt);
    torso.position.y = 1.0;
    const head = mesh(geo("head", () => new THREE.SphereGeometry(0.25, 20, 16)), skin);
    head.position.y = 1.56;
    const hair = mesh(geo("hair", () => new THREE.SphereGeometry(0.265, 20, 12, 0, Math.PI * 2, 0, Math.PI * 0.55)), hairM);
    hair.position.set(0, 1.6, 0.02);
    const armG = geo("arm", () => new THREE.CapsuleGeometry(0.07, 0.36, 4, 8));
    const armL = mesh(armG, shirt), armR = mesh(armG, shirt);
    armL.position.set(-0.27, 0.98, -0.18); armR.position.set(0.27, 0.98, -0.18);
    armL.rotation.x = armR.rotation.x = Math.PI / 2.4;
    const legG = box(0.15, 0.13, 0.5);
    const legL = mesh(legG, pants), legR = mesh(legG, pants);
    legL.position.set(-0.11, 0.66, -0.2); legR.position.set(0.11, 0.66, -0.2);
    g.add(torso, head, hair, armL, armR, legL, legR);
    const ringMat = new THREE.MeshBasicMaterial({ color: 0x94a3b8, transparent: true, opacity: 0.85, blending: THREE.AdditiveBlending, depthWrite: false });
    const ring = new THREE.Mesh(geo("statusRing", () => new THREE.RingGeometry(0.52, 0.66, 40)), ringMat);
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.03;
    g.add(ring);
    const tagAnchor = new THREE.Object3D();
    tagAnchor.position.y = 2.05;
    g.add(tagAnchor);
    const ud = { kind: "employee", id: e.id };
    [torso, head, hair].forEach((m) => { m.userData = ud; pickables.push(m); });
    parent.add(g);
    const tag = addLabel("", tagAnchor, 0, "agent");
    return { group: g, ring, ringMat, shirt, parts: [torso, head, hair, armL, armR, legL, legR], tag, phase: (h % 628) / 100 };
  }

  function applyAvatar(a, e, screenMat) {
    a.data = e;
    const c = stateColor(e.state);
    a.ringMat.color.copy(c);
    a.group.visible = true;
    const offline = e.state === "offline";
    a.parts.forEach((p) => { p.visible = !offline; });  // offline: empty chair, status ring stays
    a.shirt.color.set(e.state === "paused" ? 0x64748b : e.color);
    screenMat.emissive.set({ working: 0x2563eb, failed: 0x7f1d1d, blocked: 0x9a3412, awaiting_approval: 0x6d28d9, under_review: 0x1d4ed8, waiting: 0x854d0e }[e.state] || 0x000000);
    const task = e.task && e.state !== "idle" ? ` · ${Office.esc(e.task.code)}` : "";
    a.tag.innerHTML = `<span class="dot" style="background:${(Office.STATES[e.state] || ["", "#94a3b8"])[1]}"></span>${Office.esc(e.name)}${task}`;
  }

  function buildRoom(d) {
    const room = new THREE.Group();
    room.position.set(d.cx, 0, d.cz);
    const base = new THREE.Color(d.color);
    const slab = mesh(box(W, SLAB, D), std(shade(d.color, -0.35).getHex(), { roughness: 0.8 }));
    slab.position.y = -SLAB / 2;
    const floor = mesh(box(W - 0.02, 0.04, D - 0.02), std(shade(d.color, -0.12).getHex(), { roughness: 0.85 }));
    floor.position.y = 0.02;
    const rug = mesh(box(W * 0.66, 0.02, D * 0.6), std(shade(d.color, 0.55).getHex(), { roughness: 1 }), { cast: false });
    rug.position.set(0.6, 0.05, 0.5);
    const wallMat = std(base.getHex(), { roughness: 0.7 });
    const wallN = mesh(box(W, WALL_H, WALL_T), wallMat);
    wallN.position.set(0, WALL_H / 2, -D / 2 + WALL_T / 2);
    const wallW = mesh(box(WALL_T, WALL_H, D), wallMat);
    wallW.position.set(-W / 2 + WALL_T / 2, WALL_H / 2, 0);
    room.add(slab, floor, rug, wallN, wallW);
    [slab, floor, rug, wallN, wallW].forEach((m) => { m.userData = { kind: "room", id: d.id }; pickables.push(m); });

    // Decor: wall screen with the team's real numbers, bookshelf, plants, floor lamp with a light pool.
    const screen = new THREE.Mesh(geo("wallScreen", () => new THREE.PlaneGeometry(2.6, 1.3)),
      new THREE.MeshStandardMaterial({ map: wallScreenTexture(d), emissive: 0xffffff, emissiveMap: null, emissiveIntensity: 0.0, roughness: 0.4 }));
    screen.material.emissiveMap = screen.material.map;
    screen.material.emissiveIntensity = 0.9;
    screen.userData.ownMaterial = true;
    screen.position.set(1.4, 1.65, -D / 2 + WALL_T + 0.02);
    const shelf = mesh(box(0.35, 1.1, 2.0), std(0xe5e7eb));
    shelf.position.set(-W / 2 + WALL_T + 0.18, 1.3, -1.8);
    room.add(screen, shelf);
    const bookColors = [0xef4444, 0xf59e0b, 0x3b82f6, 0x10b981, 0x8b5cf6];
    for (let i = 0; i < 7; i++) {
      const b = mesh(box(0.22, 0.32, 0.12), std(bookColors[(hash(d.name) + i) % bookColors.length]));
      b.position.set(-W / 2 + WALL_T + 0.2, 1.62 - (i > 3 ? 0.42 : 0), -2.55 + (i % 4) * 0.3 + (i > 3 ? 0.15 : 0));
      room.add(b);
    }
    const plant = (x, z, s = 1) => {
      const pot = mesh(geo("pot", () => new THREE.CylinderGeometry(0.3, 0.24, 0.45, 12)), std(0xf8fafc));
      pot.position.set(x, 0.23, z);
      const leaves = mesh(geo("leaves", () => new THREE.IcosahedronGeometry(0.5, 1)), std(0x16a34a, { flatShading: true }));
      leaves.position.set(x, 0.85 * s, z);
      leaves.scale.setScalar(s);
      room.add(pot, leaves);
    };
    plant(W / 2 - 0.7, -D / 2 + 0.9, 1.1);
    plant(-W / 2 + 0.8, D / 2 - 0.8, 0.9);
    const lampX = -W / 2 + 1.0, lampZ = -D / 2 + 1.0;
    const lampPole = mesh(geo("lampPole", () => new THREE.CylinderGeometry(0.035, 0.035, 2.3, 8)), std(0x111827));
    lampPole.position.set(lampX, 1.15, lampZ);
    const bulb = new THREE.Mesh(geo("bulb", () => new THREE.SphereGeometry(0.2, 16, 12)),
      mat("bulb", () => new THREE.MeshStandardMaterial({ color: 0xfff7d6, emissive: 0xffe9a8, emissiveIntensity: 2.4 })));
    bulb.position.set(lampX, 2.35, lampZ);
    const bulbGlow = glowSprite(0xffe2a0, 2.6, 0.55);
    bulbGlow.position.copy(bulb.position);
    const pool = new THREE.Mesh(geo("pool", () => new THREE.CircleGeometry(2.6, 32)),
      mat("pool", () => new THREE.MeshBasicMaterial({ map: glowTex, color: 0xffd38a, transparent: true, opacity: 0.28, blending: THREE.AdditiveBlending, depthWrite: false })));
    pool.rotation.x = -Math.PI / 2;
    pool.position.set(lampX + 0.6, 0.07, lampZ + 0.6);
    room.add(lampPole, bulb, bulbGlow, pool);

    // Desks + avatars bound to real employees.
    const seats = seatLayout(d.employees.length);
    const desks = seats.map((s) => buildDesk(room, s.x, s.z));
    d.employees.slice(0, MAX_SEATS).forEach((e, i) => {
      const a = buildAvatar(room, e, seats[i].x, seats[i].z);
      a.screenMat = desks[i].screenMat;
      avatars.set(e.id, a);
      applyAvatar(a, e, desks[i].screenMat);
    });
    if (!d.employees.length) desks.forEach((k) => k.group.visible = false);

    officeGroup.add(room);
    const anchor = new THREE.Object3D();
    anchor.position.set(d.cx - 1.2, WALL_H + 1.4, d.cz - D / 2);
    officeGroup.add(anchor);
    const label = addLabel("", anchor, 0, "room");
    label.addEventListener("click", () => Office.showRoom(d.id));
    const r = { d, group: room, wallMat, screen, label, flow: null };
    rooms.set(d.id, r);
    buildFlow(r);
    applyRoom(r, d);
  }

  function applyRoom(r, d) {
    r.d = d;
    const s = d.stats || {};
    const extra = d.employees.length > MAX_SEATS ? ` · +${d.employees.length - MAX_SEATS} more` : "";
    r.label.innerHTML = `<span class="ol-icon" style="background:${Office.esc(d.color)}">${Office.esc(Office.initials(d.name))}</span>
      <span><span class="ol-name">${Office.esc(d.name)}${d.paused ? ' <em>paused</em>' : ""}</span>
      <span class="ol-sub"><span class="dot" style="background:${s.running ? "#22c55e" : "#64748b"}"></span>${d.employees.length} agent${d.employees.length === 1 ? "" : "s"} · ${s.running || 0} running${extra}</span></span>`;
    const old = r.screen.material.map;
    r.screen.material.map = r.screen.material.emissiveMap = wallScreenTexture(d);
    r.screen.material.needsUpdate = true;
    if (old) old.dispose();
    r.wallMat.color.set(d.paused ? shade(d.color, -0.45) : new THREE.Color(d.color));
    if (r.flow) r.flow.setActive(Math.min(s.running || 0, 6), d.color);
  }

  // ---------------------------------------------------------------- paths & data flow (real running work only)
  function buildFlow(r) {
    const { cx, cz, color } = r.d;
    const dir = new THREE.Vector3(cx, 0, cz).normalize();
    const start = dir.clone().multiplyScalar(HUB_R + 0.2);
    const end = new THREE.Vector3(cx, 0, cz).sub(dir.clone().multiplyScalar(Math.max(W, D) / 2 + 0.3));
    const len = start.distanceTo(end);
    if (len > 0.5) {
      const road = mesh(box(1.5, 0.06, len), std(0x111827, { roughness: 0.9 }), { cast: false });
      road.position.copy(start.clone().add(end).multiplyScalar(0.5)).setY(-0.02);
      road.lookAt(end.x, -0.02, end.z);
      officeGroup.add(road);
      for (let t = 0.6; t < len - 0.4; t += 1.3) {
        const dash = new THREE.Mesh(box(0.08, 0.02, 0.55), mat("dash", () => new THREE.MeshStandardMaterial({ color: 0x475569, emissive: 0x334155, emissiveIntensity: 0.6 })));
        dash.position.copy(start.clone().add(end.clone().sub(start).normalize().multiplyScalar(t))).setY(0.02);
        dash.lookAt(end.x, 0.02, end.z);
        officeGroup.add(dash);
      }
    }
    const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(0, 2.3, 0),
      new THREE.Vector3(cx * 0.5, 7.5, cz * 0.5), new THREE.Vector3(cx, WALL_H + 0.4, cz));
    const lineMat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.12, blending: THREE.AdditiveBlending, depthWrite: false });
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(48)), lineMat);
    officeGroup.add(line);
    const sparks = [];
    r.flow = {
      curve, line, lineMat, sparks,
      setActive(n, col) {
        lineMat.color.set(col);
        lineMat.opacity = n ? 0.55 : 0.12;
        while (sparks.length < n) { const sp = glowSprite(col, 0.9, 0.95); officeGroup.add(sp); sparks.push(sp); }
        while (sparks.length > n) officeGroup.remove(sparks.pop());
        sparks.forEach((sp, i) => { sp.material.color.set(col); sp.userData.offset = i / Math.max(n, 1); });
      },
    };
  }

  // ---------------------------------------------------------------- layout
  function placeRooms(departments) {
    // Ring order follows the Founder's configured grid cells (row, then column), then id.
    const ordered = [...departments].sort((a, b) => (a.z - b.z) || (a.x - b.x) || (a.id - b.id));
    const n = ordered.length;
    const R = Math.max(HUB_R + 11, (n * (Math.max(W, D) + 8)) / (2 * Math.PI));
    return ordered.map((d, i) => {
      const a = -Math.PI / 2 + (i / Math.max(n, 1)) * Math.PI * 2;
      return { ...d, cx: Math.cos(a) * R, cz: Math.sin(a) * R, ringRadius: R };
    });
  }

  // ---------------------------------------------------------------- camera flights
  let flight = null;
  function flyTo(target, distance, ms = 900) {
    const fromPos = camera.position.clone(), fromTarget = controls.target.clone();
    const toTarget = target.clone(), toPos = target.clone().add(ISO.clone().multiplyScalar(distance));
    if (REDUCED_MOTION) { camera.position.copy(toPos); controls.target.copy(toTarget); return; }
    flight = { fromPos, fromTarget, toPos, toTarget, t0: performance.now(), ms };
  }
  function fitDistance() {
    // Fit the whole ring (plus room size) inside the narrower of the vertical/horizontal field of view.
    const R = (placed.length ? placed[0].ringRadius : 20) + Math.max(W, D) * 0.8;
    const vfov = THREE.MathUtils.degToRad(camera.fov);
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * camera.aspect);
    return THREE.MathUtils.clamp(R / Math.sin(Math.min(vfov, hfov) / 2) * 0.82, 30, 190);
  }
  function overview() {
    focusedRoom = null;
    document.getElementById("hq-home").hidden = true;
    flyTo(new THREE.Vector3(0, 0, 0), fitDistance());
  }
  Office.hooks.focusRoom = (id) => {
    const r = rooms.get(Number(id));
    if (!r) return;
    focusedRoom = r.d.id;
    document.getElementById("hq-home").hidden = false;
    flyTo(new THREE.Vector3(r.d.cx, 0.5, r.d.cz), 22);
  };
  Office.hooks.focusHub = () => { focusedRoom = null; document.getElementById("hq-home").hidden = false; flyTo(new THREE.Vector3(0, 1.5, 0), 26); };
  Office.hooks.home = overview;
  document.getElementById("hq-home").addEventListener("click", () => Office.closePanel());
  document.getElementById("zoom-in").addEventListener("click", () => dolly(0.75));
  document.getElementById("zoom-out").addEventListener("click", () => dolly(1.33));
  document.getElementById("zoom-fit").addEventListener("click", () => Office.closePanel());
  function dolly(f) {
    const off = camera.position.clone().sub(controls.target);
    const len = THREE.MathUtils.clamp(off.length() * f, controls.minDistance, controls.maxDistance);
    flyTo(controls.target.clone(), len, 350);
  }
  const fxBtn = document.getElementById("hq-fx");
  const setFx = (on) => {
    effects = on;
    renderer.shadowMap.enabled = on;
    scene.traverse((o) => { if (o.material) o.material.needsUpdate = true; });
    fxBtn.classList.toggle("on", on);
    fxBtn.textContent = on ? "Effects: on" : "Effects: off";
    try { localStorage.setItem("vcoos-office-fx", on ? "on" : "off"); } catch (e) { /* private mode */ }
  };
  fxBtn.addEventListener("click", () => setFx(!effects));
  setFx(effects);

  // ---------------------------------------------------------------- minimap
  const mini = document.getElementById("minimap");
  const miniCtx = mini.getContext("2d");
  document.getElementById("hq-minimap").hidden = false;
  function drawMinimap() {
    const w = mini.width, h = mini.height;
    miniCtx.clearRect(0, 0, w, h);
    const R = placed.length ? placed[0].ringRadius + W : 30;
    const sc = Math.min(w, h) / (2 * R + 4);
    miniCtx.save();
    miniCtx.translate(w / 2, h / 2);
    miniCtx.strokeStyle = "rgba(34,211,238,0.8)";
    miniCtx.beginPath(); miniCtx.arc(0, 0, HUB_R * sc, 0, Math.PI * 2); miniCtx.stroke();
    placed.forEach((d) => {
      miniCtx.fillStyle = d.color;
      miniCtx.globalAlpha = focusedRoom && focusedRoom !== d.id ? 0.45 : 1;
      miniCtx.fillRect(d.cx * sc - (W * sc) / 2, d.cz * sc - (D * sc) / 2, W * sc, D * sc);
      if ((d.stats || {}).running) { miniCtx.fillStyle = "#22c55e"; miniCtx.fillRect(d.cx * sc - 2, d.cz * sc - 2, 4, 4); }
    });
    miniCtx.globalAlpha = 1;
    const t = controls.target;
    miniCtx.strokeStyle = "#ffffff";
    miniCtx.strokeRect(t.x * sc - 6, t.z * sc - 6, 12, 12);
    miniCtx.restore();
  }
  mini.addEventListener("click", (ev) => {
    const rect = mini.getBoundingClientRect();
    const R = placed.length ? placed[0].ringRadius + W : 30;
    const sc = Math.min(mini.width, mini.height) / (2 * R + 4);
    const x = ((ev.clientX - rect.left) * (mini.width / rect.width) - mini.width / 2) / sc;
    const z = ((ev.clientY - rect.top) * (mini.height / rect.height) - mini.height / 2) / sc;
    if (Math.hypot(x, z) < HUB_R) return Office.showHub();
    let best = null, bestD = Infinity;
    placed.forEach((d) => { const dd = Math.hypot(d.cx - x, d.cz - z); if (dd < bestD) { best = d; bestD = dd; } });
    if (best && bestD < Math.max(W, D)) Office.showRoom(best.id);
  });

  // ---------------------------------------------------------------- state updates
  Office.onState((state) => {
    window.VCOOS_3D_READY = true;
    const key = JSON.stringify(state.departments.map((d) => [d.id, d.x, d.z, d.color, d.name, d.employees.map((e) => e.id)]));
    if (key !== layoutKey) {
      const first = layoutKey === "";
      layoutKey = key;
      clearOffice();
      placed = placeRooms(state.departments);
      buildHub();
      placed.forEach(buildRoom);
      if (first) {
        camera.position.copy(ISO.clone().multiplyScalar(fitDistance()));
        controls.target.set(0, 0, 0);
      }
    } else {
      state.departments.forEach((d) => {
        const r = rooms.get(d.id);
        if (r) applyRoom(r, { ...d, cx: r.d.cx, cz: r.d.cz, ringRadius: r.d.ringRadius });
        d.employees.forEach((e) => { const a = avatars.get(e.id); if (a) applyAvatar(a, e, a.screenMat); });
      });
      placed = placed.map((p) => ({ ...p, stats: (state.departments.find((d) => d.id === p.id) || {}).stats }));
    }
    applyHub(state.hub || {});
  });

  // ---------------------------------------------------------------- picking
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let downAt = null, hoverRoom = null;
  const pick = (ev) => {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(pickables, false)[0];
    return hit ? hit.object.userData : null;
  };
  renderer.domElement.addEventListener("pointerdown", (ev) => { downAt = [ev.clientX, ev.clientY]; });
  renderer.domElement.addEventListener("pointerup", (ev) => {
    if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 5) return;  // drag, not click
    const hit = pick(ev);
    if (!hit) return;
    if (hit.kind === "employee") {
      const owner = [...rooms.values()].find((r) => r.d.employees.some((e) => e.id === hit.id));
      if (owner) Office.showRoom(owner.d.id);  // sets the team context (back button) and flies there
      Office.showEmployee(hit.id);
    } else if (hit.kind === "room") Office.showRoom(hit.id);
    else if (hit.kind === "hub") Office.showHub();
  });
  renderer.domElement.addEventListener("pointermove", (ev) => {
    const hit = pick(ev);
    renderer.domElement.style.cursor = hit ? "pointer" : "grab";
    const next = hit && hit.kind === "room" ? hit.id : null;
    if (next !== hoverRoom) {
      if (hoverRoom && rooms.get(hoverRoom)) rooms.get(hoverRoom).wallMat.emissive.set(0x000000);
      if (next && rooms.get(next)) rooms.get(next).wallMat.emissive.set(new THREE.Color(rooms.get(next).d.color).multiplyScalar(0.25));
      hoverRoom = next;
    }
  });

  // ---------------------------------------------------------------- resize & render loop
  function resize() {
    const w = container.clientWidth, h = container.clientHeight;
    renderer.setSize(w, h, false);
    renderer.domElement.style.width = w + "px";
    renderer.domElement.style.height = h + "px";
    composer.setSize(w, h);
    bloom.setSize(w, h);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);
  resize();

  const tmp = new THREE.Vector3();
  const clock = new THREE.Clock();
  let frame = 0;
  renderer.setAnimationLoop(() => {
    const t = clock.getElapsedTime();
    if (flight) {
      const k = Math.min((performance.now() - flight.t0) / flight.ms, 1);
      const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
      camera.position.lerpVectors(flight.fromPos, flight.toPos, e);
      controls.target.lerpVectors(flight.fromTarget, flight.toTarget, e);
      if (k >= 1) flight = null;
    }
    if (!REDUCED_MOTION) {
      if (hub) {
        const speed = 0.15 + (hub.activity || 0) * 0.08;  // spins faster with more real running work
        hub.rings.forEach((r, i) => { r.rotation.z += (i % 2 ? -1 : 1) * speed * 0.01; });
        hub.orbitA.rotation.y += speed * 0.02; hub.orbitB.rotation.x += speed * 0.015;
        hub.ringMat.emissiveIntensity = 1.3 + Math.sin(t * 2) * 0.25 * Math.min(1, hub.activity || 0);
      }
      avatars.forEach((a) => {
        const s = a.data && a.data.state;
        a.group.position.y = s === "working" ? Math.abs(Math.sin(t * 4 + a.phase)) * 0.03 : 0;
        const pulse = s === "failed" || s === "blocked" || s === "awaiting_approval" ? 1 + Math.sin(t * 4 + a.phase) * 0.15 : 1;
        a.ring.scale.set(pulse, pulse, pulse);
      });
    }
    rooms.forEach((r) => {
      if (!r.flow) return;
      r.flow.sparks.forEach((sp) => {
        const u = REDUCED_MOTION ? sp.userData.offset : (t * 0.22 + sp.userData.offset) % 1;
        sp.position.copy(r.flow.curve.getPoint(u));
      });
    });
    controls.update();
    if (effects) composer.render(); else renderer.render(scene, camera);

    // HTML labels follow their 3D anchors; agent tags only when close enough to read.
    const w = container.clientWidth, h = container.clientHeight;
    const dist = camera.position.distanceTo(controls.target);
    for (const l of labels) {
      if (l.kind === "agent" && dist > 48) { l.el.style.display = "none"; continue; }
      l.obj.getWorldPosition(tmp);
      tmp.y += l.dy;
      tmp.project(camera);
      const visible = tmp.z < 1 && tmp.x > -1.1 && tmp.x < 1.1 && tmp.y > -1.1 && tmp.y < 1.1;
      l.el.style.display = visible ? "" : "none";
      if (visible) l.el.style.transform = `translate(-50%, -100%) translate(${(tmp.x * 0.5 + 0.5) * w}px, ${(-tmp.y * 0.5 + 0.5) * h}px)`;
    }
    if (++frame % 10 === 0) drawMinimap();
  });
}
