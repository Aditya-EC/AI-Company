// Three.js virtual office. Rooms = departments, avatars = AI employees; visuals are driven
// only by backend state delivered through VCOOSOffice (polling + WebSocket refresh).
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const Office = window.VCOOSOffice;
const container = document.getElementById("office-canvas");

function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
  } catch (e) { return false; }
}

if (window.VCOOS_OFFICE.force2d) {
  // 2D mode requested explicitly; common.js renders it.
} else if (!webglAvailable()) {
  Office.fallback2d("WebGL not supported on this device");
} else {
  init();
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function init() {
  const ROOM_W = 12, ROOM_D = 10, GAP = 2;
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.prepend(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 500);
  camera.position.set(28, 30, 38);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.maxPolarAngle = Math.PI / 2.15;
  controls.minDistance = 8;
  controls.maxDistance = 140;

  scene.add(new THREE.HemisphereLight(0xffffff, 0x8899aa, 0.9));
  const sun = new THREE.DirectionalLight(0xffffff, 1.1);
  sun.position.set(30, 50, 20);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -80, right: 80, top: 80, bottom: -80 });
  scene.add(sun);

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400), new THREE.MeshStandardMaterial({ color: 0xcfd6e3, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.05;
  ground.receiveShadow = true;
  scene.add(ground);

  function applyTheme() {
    const dark = getComputedStyle(document.documentElement).colorScheme === "dark";
    scene.background = new THREE.Color(cssVar("--surface-2") || (dark ? "#18223a" : "#f0f3f9"));
    ground.material.color.set(dark ? 0x1b2438 : 0xdfe5ee);
  }
  applyTheme();
  window.addEventListener("vcoos-theme", applyTheme);

  const officeGroup = new THREE.Group();
  scene.add(officeGroup);
  const labelLayer = container;
  const labels = []; // {el, obj, offsetY}
  const pickables = [];
  const avatars = new Map(); // employee id -> {group, ring, body, data}
  const mats = {
    desk: new THREE.MeshStandardMaterial({ color: 0x8b6b4a, roughness: 0.6 }),
    deskTop: new THREE.MeshStandardMaterial({ color: 0xe9e4dc, roughness: 0.4 }),
    screen: new THREE.MeshStandardMaterial({ color: 0x111827, emissive: 0x000000, roughness: 0.3 }),
    chair: new THREE.MeshStandardMaterial({ color: 0x374151, roughness: 0.7 }),
    glass: new THREE.MeshStandardMaterial({ color: 0x9ec5e8, transparent: true, opacity: 0.22, roughness: 0.1 }),
    frame: new THREE.MeshStandardMaterial({ color: 0x6b7280, metalness: 0.4, roughness: 0.4 }),
    skin: new THREE.MeshStandardMaterial({ color: 0xf1c7a0, roughness: 0.8 }),
    plant: new THREE.MeshStandardMaterial({ color: 0x2f855a }),
    pot: new THREE.MeshStandardMaterial({ color: 0xd6d3d1 }),
  };

  function addLabel(text, obj, offsetY, cls) {
    const el = document.createElement("div");
    el.className = "office-label" + (cls ? " " + cls : "");
    el.textContent = text;
    labelLayer.appendChild(el);
    labels.push({ el, obj, offsetY });
    return el;
  }

  function clearOffice() {
    officeGroup.clear();
    labels.forEach((l) => l.el.remove());
    labels.length = 0;
    pickables.length = 0;
    avatars.clear();
  }

  function placeRooms(departments) {
    // Honor configured grid cells; resolve collisions by moving to the next free cell.
    const used = new Set();
    return departments.map((d) => {
      let x = d.x, z = d.z;
      while (used.has(`${x},${z}`)) { x += 1; if (x > 3) { x = 0; z += 1; } }
      used.add(`${x},${z}`);
      return { ...d, cx: x * (ROOM_W + GAP), cz: z * (ROOM_D + GAP) };
    });
  }

  function buildDesk(group, x, z) {
    const desk = new THREE.Group();
    const top = new THREE.Mesh(new THREE.BoxGeometry(1.8, 0.08, 0.9), mats.deskTop);
    top.position.y = 0.75; top.castShadow = true; top.receiveShadow = true;
    desk.add(top);
    for (const [lx, lz] of [[-0.8, -0.38], [0.8, -0.38], [-0.8, 0.38], [0.8, 0.38]]) {
      const leg = new THREE.Mesh(new THREE.BoxGeometry(0.06, 0.75, 0.06), mats.frame);
      leg.position.set(lx, 0.375, lz); desk.add(leg);
    }
    const screen = new THREE.Mesh(new THREE.BoxGeometry(0.9, 0.5, 0.04), mats.screen.clone());
    screen.position.set(0, 1.1, -0.3); screen.castShadow = true;
    desk.add(screen);
    const chair = new THREE.Mesh(new THREE.BoxGeometry(0.55, 0.08, 0.55), mats.chair);
    chair.position.set(0, 0.48, 0.75);
    desk.add(chair);
    const back = new THREE.Mesh(new THREE.BoxGeometry(0.55, 0.6, 0.06), mats.chair);
    back.position.set(0, 0.8, 1.02);
    desk.add(back);
    desk.position.set(x, 0, z);
    desk.scale.setScalar(1.35);
    group.add(desk);
    return { desk, screen };
  }

  function buildAvatar(e, x, z, screen) {
    const g = new THREE.Group();
    const body = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.28, 0.75, 16), new THREE.MeshStandardMaterial({ color: e.color, roughness: 0.6 }));
    body.position.y = 0.9; body.castShadow = true;
    const head = new THREE.Mesh(new THREE.SphereGeometry(0.2, 20, 16), mats.skin);
    head.position.y = 1.45; head.castShadow = true;
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.42, 0.55, 32), new THREE.MeshBasicMaterial({ color: 0x94a3b8, side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2; ring.position.y = 0.02;
    const marker = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 10), new THREE.MeshBasicMaterial({ color: 0x94a3b8 }));
    marker.position.y = 1.85;
    g.add(body, head, ring, marker);
    g.position.set(x, 0, z + 1.0);
    g.scale.setScalar(1.6);
    g.userData = { kind: "employee", id: e.id };
    [body, head, ring, marker].forEach((m) => { m.userData = g.userData; pickables.push(m); });
    officeGroup.add(g);
    const label = addLabel(e.name, marker, 0.55);
    avatars.set(e.id, { group: g, ring, marker, body, screen, label, data: e, phase: Math.random() * Math.PI * 2 });
    applyAvatarState(e.id, e);
  }

  function applyAvatarState(id, e) {
    const a = avatars.get(id);
    if (!a) return;
    a.data = e;
    const color = new THREE.Color((Office.STATES[e.state] || ["", "#94a3b8"])[1]);
    a.ring.material.color.copy(color);
    a.marker.material.color.copy(color);
    a.screen.material.emissive.set(e.state === "working" ? 0x2563eb : e.state === "failed" ? 0x7f1d1d : 0x000000);
    a.body.material.opacity = e.state === "offline" ? 0.35 : 1;
    a.body.material.transparent = e.state === "offline";
    a.label.textContent = `${e.name}${e.task && e.state !== "idle" ? " · " + e.task.code : ""}`;
  }

  function buildRoom(d) {
    const room = new THREE.Group();
    room.position.set(d.cx, 0, d.cz);
    const floor = new THREE.Mesh(new THREE.BoxGeometry(ROOM_W, 0.1, ROOM_D),
      new THREE.MeshStandardMaterial({ color: new THREE.Color(d.color).lerp(new THREE.Color(0xffffff), 0.72), roughness: 0.9 }));
    floor.receiveShadow = true;
    floor.userData = { kind: "room", id: d.id };
    pickables.push(floor);
    room.add(floor);
    const stripe = new THREE.Mesh(new THREE.BoxGeometry(ROOM_W, 0.12, 0.35), new THREE.MeshStandardMaterial({ color: d.color }));
    stripe.position.set(0, 0.01, -ROOM_D / 2 + 0.175);
    room.add(stripe);
    // Glass walls with an entrance gap at the front.
    const h = 1.6;
    const walls = [
      [ROOM_W, h, 0.06, 0, -ROOM_D / 2], [0.06, h, ROOM_D, -ROOM_W / 2, 0], [0.06, h, ROOM_D, ROOM_W / 2, 0],
      [ROOM_W / 2 - 1.2, h, 0.06, -ROOM_W / 4 - 0.6, ROOM_D / 2], [ROOM_W / 2 - 1.2, h, 0.06, ROOM_W / 4 + 0.6, ROOM_D / 2],
    ];
    for (const [w, hh, dd, x, z] of walls) {
      const wall = new THREE.Mesh(new THREE.BoxGeometry(w, hh, dd), mats.glass);
      wall.position.set(x, hh / 2, z);
      room.add(wall);
      const cap = new THREE.Mesh(new THREE.BoxGeometry(w + 0.02, 0.05, dd + 0.04), mats.frame);
      cap.position.set(x, hh, z);
      room.add(cap);
    }
    const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.25, 0.2, 0.4, 12), mats.pot);
    pot.position.set(ROOM_W / 2 - 0.6, 0.2, -ROOM_D / 2 + 0.6);
    const leaves = new THREE.Mesh(new THREE.SphereGeometry(0.45, 12, 10), mats.plant);
    leaves.position.set(ROOM_W / 2 - 0.6, 0.8, -ROOM_D / 2 + 0.6);
    room.add(pot, leaves);
    officeGroup.add(room);
    const anchor = new THREE.Object3D();
    anchor.position.set(d.cx, 2.2, d.cz - ROOM_D / 2);
    officeGroup.add(anchor);
    addLabel(d.name + (d.paused ? " (paused)" : ""), anchor, 0, "room");
    // Desks: up to 3 per row, rows as needed; employees beyond capacity share the last rows.
    const n = Math.max(d.employees.length, 1);
    const cols = Math.min(3, n), rows = Math.ceil(n / 3);
    d.employees.forEach((e, i) => {
      const c = i % 3, r = Math.floor(i / 3);
      const x = d.cx + (c - (cols - 1) / 2) * 3.4;
      const z = d.cz - ROOM_D / 2 + 2.2 + r * (Math.min(2.6, (ROOM_D - 3) / Math.max(rows, 1)));
      const { screen } = buildDesk(officeGroup, x, z);
      buildAvatar(e, x, z, screen);
    });
  }

  let layoutKey = "";
  let framed = false;
  Office.onState((state) => {
    window.VCOOS_3D_READY = true;
    // Rebuild geometry only when the set of rooms/employees changes; otherwise update visuals in place.
    const key = JSON.stringify(state.departments.map((d) => [d.id, d.x, d.z, d.color, d.paused, d.name, d.employees.map((e) => e.id)]));
    if (key !== layoutKey) {
      layoutKey = key;
      clearOffice();
      const placed = placeRooms(state.departments);
      placed.forEach(buildRoom);
      if (!framed && placed.length) {
        const occupied = placed.filter((d) => d.employees.length);
        const box = new THREE.Box3();
        (occupied.length ? occupied : placed).forEach((d) => {
          box.expandByPoint(new THREE.Vector3(d.cx - ROOM_W / 2, 0, d.cz - ROOM_D / 2));
          box.expandByPoint(new THREE.Vector3(d.cx + ROOM_W / 2, 2, d.cz + ROOM_D / 2));
        });
        const center = box.getCenter(new THREE.Vector3());
        const size = Math.max(box.getSize(new THREE.Vector3()).length(), 20);
        controls.target.copy(center);
        camera.position.copy(center).add(new THREE.Vector3(size * 0.32, size * 0.62, size * 0.72));
        framed = true;
      }
    } else {
      state.departments.forEach((d) => d.employees.forEach((e) => applyAvatarState(e.id, e)));
    }
  });

  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let downAt = null;
  renderer.domElement.addEventListener("pointerdown", (ev) => { downAt = [ev.clientX, ev.clientY]; });
  renderer.domElement.addEventListener("pointerup", (ev) => {
    if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 5) return; // was a drag
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(pickables, false)[0];
    if (!hit) return;
    const { kind, id } = hit.object.userData;
    if (kind === "employee") Office.showEmployee(id);
    else if (kind === "room") Office.showRoom(Office.getState().departments.find((d) => d.id === id));
  });

  let hovered = null;
  renderer.domElement.addEventListener("pointermove", (ev) => {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set(((ev.clientX - rect.left) / rect.width) * 2 - 1, -((ev.clientY - rect.top) / rect.height) * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(pickables, false)[0];
    const next = hit ? hit.object.userData : null;
    renderer.domElement.style.cursor = next ? "pointer" : "grab";
    hovered = next;
  });

  function resize() {
    const w = container.clientWidth, h = container.clientHeight;
    renderer.setSize(w, h, false);
    renderer.domElement.style.width = w + "px";
    renderer.domElement.style.height = h + "px";
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);
  resize();

  const tmp = new THREE.Vector3();
  const clock = new THREE.Clock();
  renderer.setAnimationLoop(() => {
    const t = clock.getElapsedTime();
    // Motion encodes real state only: working avatars bob gently; failed/blocked rings pulse.
    avatars.forEach((a) => {
      const s = a.data.state;
      a.group.position.y = s === "working" ? Math.abs(Math.sin(t * 3 + a.phase)) * 0.06 : 0;
      const pulse = s === "failed" || s === "blocked" || s === "awaiting_approval" ? 1 + Math.sin(t * 4 + a.phase) * 0.12 : 1;
      a.ring.scale.set(pulse, pulse, pulse);
    });
    controls.update();
    renderer.render(scene, camera);
    const w = container.clientWidth, h = container.clientHeight;
    for (const l of labels) {
      l.obj.getWorldPosition(tmp);
      tmp.y += l.offsetY;
      tmp.project(camera);
      const visible = tmp.z < 1 && tmp.x > -1.1 && tmp.x < 1.1 && tmp.y > -1.1 && tmp.y < 1.1;
      l.el.style.display = visible ? "" : "none";
      if (visible) l.el.style.transform = `translate(-50%, -100%) translate(${(tmp.x * 0.5 + 0.5) * w}px, ${(-tmp.y * 0.5 + 0.5) * h}px)`;
    }
  });
}
