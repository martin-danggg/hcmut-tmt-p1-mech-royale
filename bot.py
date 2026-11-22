"""HCMUT TMT - Mech Royale bot v6c (v3 + chase wounded mechs) (rule-based, standard library only).

Ideas: stay inside the zone early, lead gun shots, only fire with a clear line,
laser/lightning as second weapon, face the attacker, back off to regenerate,
take cubes/loot/drops when it is quiet.
v2: when HP < 45% with a known threat, walk to the nearest spot no threat can see
and stand still (silent) until HP > 90%.
v3: with no mech in sight, farm drones/crates/cubes/supply drops inside the safe area
and wander the next circle until 330 s to find them. All rule numbers are read from hello.config.
"""
import math
import os
import random
import zlib
from array import array

import mechfight as mf

HERE = os.path.dirname(os.path.abspath(__file__))
GRID_FILE = os.path.join(HERE, "map", "solid_v2.i16")
TERRAIN_CRC_V2 = 3005214772  # crc32 of arena/v2/terrain.i16 the grid was built from
TAU = 2 * math.pi
THREAT_SOUNDS = {"gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet", "reload"}
UPGRADE_ORDER = ["plating", "armor", "nanites", "laser_damage", "gun_damage", "laser_charge", "gun_rate",
                 "lightning_efficiency", "gyros", "gun_mag", "servos", "sensors", "lightning_range",
                 "lightning_chain", "gun_velocity", "laser_pierce", "capacitor", "reactor", "jets"]
WANT_WEAPONS = ["laser", "lightning"]  # never take a mortar (cook-off, self splash)
FARM_NPC_RANGE = 180.0   # v3: actively farm drones (2 cubes each) and crates (1 cube) when no mech is in sight
FARM_CRATE_RANGE = 150.0
LOOT_RANGE = 180.0
EXPLORE_UNTIL = 330.0
# tuning knobs (one experiment = one change here)
FIRE_SLACK = 0.0        # rad added to the gun's on-target tolerance (torso slews 0.2 rad per tick)
WANT_GUN = 100.0        # preferred fighting distance with only the gun
WANT_LASER = 150.0      # ... when holding a laser
WANT_LIGHTNING = 30.0   # ... when holding lightning (and HP > 50%)
HIDE_HP = 0.45          # hide to regenerate below this HP fraction (0 = never hide)
CHASE_HP = 0.5          # chase a mech that left view if its HP fraction was below this (0 = off)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def dist2(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Grid:
    """Ground + building/stadium roof height (m) on the terrain's 1 m vertex grid."""

    def __init__(self, hello):
        self.ok = False
        self.nx = 1070
        self.ny = 724
        self.x0 = -513.0
        self.y0 = -334.0
        dirs = [hello.get("map", {}).get("dir") or "", "/arena", os.path.join(HERE, "arena")]
        terrain = None
        for d in dirs:
            p = os.path.join(d, "v2", "terrain.i16")
            if d and os.path.isfile(p):
                terrain = p
                break
        try:
            use_built = True
            if terrain is not None:
                raw = open(terrain, "rb").read()
                use_built = zlib.crc32(raw) == TERRAIN_CRC_V2
            src = GRID_FILE if use_built else terrain
            if not use_built:
                mf.log("terrain differs from v2: using terrain-only heights")
            self.h = array("h")
            self.h.frombytes(open(src, "rb").read())
            if len(self.h) == self.nx * self.ny:
                self.ok = True
        except Exception as e:  # no map: play without line-of-sight checks
            mf.log(f"map load failed: {e!r}")

    def height(self, x, y):
        if not self.ok:
            return -1e9
        i = int(round(x - self.x0))
        j = int(round(y - self.y0))
        if i < 0 or j < 0 or i >= self.nx or j >= self.ny:
            return 1e9
        return self.h[j * self.nx + i] * 0.01

    def los(self, a, b, step=1.5):
        if not self.ok:
            return True
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        n = int(math.hypot(dx, dy) / step) + 1
        for k in range(1, n):
            f = k / n
            if self.height(a[0] + dx * f, a[1] + dy * f) > a[2] + dz * f:
                return False
        return True


class Bot:
    name = "hcmut-tmt"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.rng = random.Random(hello.get("seed", 0))
        self.grid = Grid(hello)
        self.hz = self.cfg["match"]["tick_hz"]
        self.strafe = 1
        self.strafe_until = 0.0
        self.hist = []  # (t, x, y) for stuck detection
        self.climb_until = 0.0
        self.side = 1
        self.threat_yaw = None
        self.threat_until = 0.0
        self.scan_phase = self.rng.random() * TAU
        self.last_hp = None
        self.seen = {}  # mech id -> (t, pos) last time we knew where it was
        self.hiding = False
        self.cover = None
        self.cover_t = -1.0
        self.wander = None
        self.wander_until = 0.0
        self.wounded = {}

    # ------------------------------------------------------------------ zone
    def zone_now(self, z, t):
        c, r = z["center"], z["radius"]
        nc, nr = z["next_center"], z["next_radius"]
        ss, se = z["shrink_start"], z["shrink_end"]
        if t <= ss or se <= ss:
            return c, r
        f = min(1.0, (t - ss) / (se - ss))
        return [c[0] + (nc[0] - c[0]) * f, c[1] + (nc[1] - c[1]) * f], r + (nr - r) * f

    def safe(self, p, z, t, margin=8.0):
        """Is p a good place to be given the zone (now and after the next shrink)."""
        c, r = self.zone_now(z, t)
        if dist2(p, c) > r - margin:
            return False
        travel = dist2(p, z["next_center"]) - z["next_radius"]
        return travel <= 0 or (z["shrink_start"] - t) > travel / 10.0 + 10

    def zone_goal(self, me, z, t):
        nc, nr = z["next_center"], z["next_radius"]
        d = dist2(me, nc)
        hold = max(nr * 0.55, nr - 40) if nr > 30 else nr * 0.3
        if d <= hold:
            return None
        s = hold / d
        return [nc[0] + (me[0] - nc[0]) * s, nc[1] + (me[1] - nc[1]) * s]

    # -------------------------------------------------------------- movement
    def steer(self, me, yaw, t, energy):
        """Pick a walkable direction near yaw; returns (yaw, climb)."""
        g = self.grid
        if not g.ok:
            return yaw, False
        feet = me[2]
        lim = feet + self.cfg["mech"]["step_up"] + 0.3

        def blocked(a):
            ca, sa = math.cos(a), math.sin(a)
            for r in (3.0, 5.5, 8.0):
                if g.height(me[0] + ca * r, me[1] + sa * r) > lim:
                    return True
            return False

        if not blocked(yaw):
            return yaw, False
        # obstacle ahead: climb if it is low enough for our energy, else go around
        ahead = max(g.height(me[0] + math.cos(yaw) * r, me[1] + math.sin(yaw) * r) for r in (3.0, 5.5, 8.0, 12.0))
        rise = ahead - feet
        if rise < energy * 0.5 - 6 and rise < 45:
            return yaw, True
        for k in range(1, 9):
            for s in (self.side, -self.side):
                a = wrap(yaw + s * k * math.pi / 8)
                if not blocked(a):
                    return a, False
        return yaw, energy > 25

    def walkable_line(self, a, b, feet):
        g = self.grid
        n = int(dist2(a, b) / 2.0) + 1
        lim = feet + self.cfg["mech"]["step_up"] + 0.3
        for k in range(1, n + 1):
            f = k / n
            if g.height(a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f) > lim:
                return False
        return True

    def find_cover(self, me, threats, z, t):
        """Closest reachable spot where no known threat's eye can see our top."""
        g = self.grid
        if not g.ok or not threats:
            return None
        away = math.atan2(sum(me[1] - p[1] for p in threats), sum(me[0] - p[0] for p in threats))
        best = None
        for r in (7.0, 12.0, 18.0, 25.0, 33.0):
            for k in range(16):
                a = away + (k - 8) * TAU / 16
                p = [me[0] + math.cos(a) * r, me[1] + math.sin(a) * r]
                pz = g.height(p[0], p[1])
                if pz > me[2] + 1.5 or not self.safe(p, z, t):
                    continue
                if not self.walkable_line(me, p, me[2]):
                    continue
                top = [p[0], p[1], pz + 8.5]
                if any(g.los([q[0], q[1], q[2] + 8.0], top) for q in threats):
                    continue
                cost = r + 6.0 * abs(wrap(a - away)) / math.pi
                if best is None or cost < best[0]:
                    best = (cost, p)
            if best is not None:
                return best[1]
        return None

    # ------------------------------------------------------------- shooting
    def lead(self, me_muzzle, tpos, tvel, speed, zoff):
        c = [tpos[0], tpos[1], tpos[2] + zoff]
        v = tvel or [0, 0, 0]
        t = 0.0
        p = c
        for _ in range(4):
            p = [c[0] + v[0] * t, c[1] + v[1] * t, c[2] + max(-2.0, min(2.0, v[2] * t))]
            d = math.dist(p, me_muzzle)
            t = max(0.0, d - 3.0) / speed
        return p

    @staticmethod
    def angles(src, dst):
        dx, dy, dz = dst[0] - src[0], dst[1] - src[1], dst[2] - src[2]
        return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))

    def act(self, obs):
        t = obs["t"]
        me_s = obs["self"]
        me = me_s["pos"]
        st = me_s["stats"]
        scale = st.get("height", 9.0) / 9.0
        muzzle = [me[0], me[1], me[2] + st.get("muzzle_height", 7.0)]
        z = obs["zone"]
        hp_frac = me_s["hp"] / max(1.0, me_s["max_hp"])
        energy = me_s["energy"]
        act = {"tick": obs["tick"]}

        # ---- upgrades
        offer = obs.get("offer")
        if offer:
            ids = [c["id"] for c in offer["choices"]]
            best = min(range(len(ids)), key=lambda i: UPGRADE_ORDER.index(ids[i]) if ids[i] in UPGRADE_ORDER else 99)
            act["pick"] = best

        weapons = me_s["weapons"]
        slot = {w["kind"]: i for i, w in enumerate(weapons) if w}

        # ---- loot within reach
        reach = self.cfg["loot"]["pickup_radius"]
        if weapons[1] is None:
            for p in obs["pickups"]:
                if p["kind"] in WANT_WEAPONS and p["kind"] not in slot and dist2(p["pos"], me) < reach - 0.3 \
                        and abs(p["pos"][2] - me[2]) < 3.8:
                    act["take"] = p["id"]
                    break

        # ---- threats seen
        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for m in mechs:
            self.seen[m["id"]] = (t, m["pos"])
        for k in [k for k, v in self.seen.items() if t - v[0] > 10.0]:
            del self.seen[k]
        for e in obs["events"]:
            if e["type"] == "damaged" and e.get("by") is not None and e["by"] not in self.seen and e.get("bearing") is not None:
                b = e["bearing"]
                self.seen[e["by"]] = (t, [me[0] + math.cos(b) * 80, me[1] + math.sin(b) * 80, me[2]])
            if e["type"] == "damaged" and e.get("bearing") is not None:
                self.threat_yaw = e["bearing"]
                self.threat_until = t + 2.5
        if not mechs:
            best_s = None
            for s in obs["sounds"]:
                if s["kind"] in THREAT_SOUNDS and s["dist"] in ("near", "mid") and (best_s is None or s["dist"] == "near"):
                    best_s = s
            if best_s is not None and t >= self.threat_until - 1.0:
                self.threat_yaw = best_s["bearing"]
                self.threat_until = max(self.threat_until, t + 1.5)

        def score(m):
            d = dist2(m["pos"], me)
            vis = self.grid.los(muzzle, [m["pos"][0], m["pos"][1], m["pos"][2] + 4.5])
            return d + (0 if vis else 150) + 0.05 * m.get("hp", 1400), vis

        target = None
        tvis = False
        if mechs:
            target, tvis = None, False
            best = 1e18
            for m in mechs:
                sc, vis = score(m)
                if sc < best:
                    best, target, tvis = sc, m, vis
        tkind = "mech"
        if target is None and npcs and hp_frac > 0.4:
            n = min(npcs, key=lambda m: dist2(m["pos"], me))
            if dist2(n["pos"], me) < FARM_NPC_RANGE and self.safe(n["pos"], z, t):
                target, tkind = n, "npc"
                tvis = self.grid.los(muzzle, [n["pos"][0], n["pos"][1], n["pos"][2] + 1.75])
        crate = None
        if target is None and obs["crates"]:
            c = min(obs["crates"], key=lambda c: dist2(c["pos"], me))
            if dist2(c["pos"], me) < FARM_CRATE_RANGE and self.safe(c["pos"], z, t) and self.grid.los(muzzle, [c["pos"][0], c["pos"][1], c["pos"][2] + 1.2]):
                crate = c

        # ---- aim & fire
        fire = [False, False]
        aim_yaw, aim_pitch = me_s["aim"][0], 0.0
        gun = weapons[slot["gun"]] if "gun" in slot else None
        gspeed = gun["stats"]["speed"] if gun else 252.0
        grange = gun["stats"]["range"] if gun else 230.0
        if target is not None:
            zoff = (4.5 if tkind == "mech" else 1.75) * (1 + 0.05 * target.get("cubes", 0))
            d = dist2(target["pos"], me)
            aimp = self.lead(muzzle, target["pos"], target.get("vel"), gspeed, zoff)
            aim_yaw, aim_pitch = self.angles(muzzle, aimp)
            # laser: hitscan, aim at where it will be at release
            if "laser" in slot:
                lw = weapons[slot["laser"]]
                ls = lw["stats"]
                charged = lw.get("charge", 0) >= ls["charge"] - 1e-3
                if charged:
                    v = target.get("vel") or [0, 0, 0]
                    lp = [target["pos"][0] + v[0] * 0.1, target["pos"][1] + v[1] * 0.1, target["pos"][2] + zoff]
                    aim_yaw, aim_pitch = self.angles(muzzle, lp)
            ay, ap = me_s["aim"]
            rad = 2.5 if tkind == "mech" else 1.8
            tol = max(0.006, 0.7 * math.atan2(rad, max(d, 1.0)))
            err_y = abs(wrap(ay - aim_yaw))
            err_p = abs(ap - aim_pitch)
            on = err_y < tol + FIRE_SLACK and err_p < tol * 1.6 + FIRE_SLACK * 0.5
            if gun is not None and tvis and on and d < grange - 5 and gun.get("reload", 0) == 0:
                fire[slot["gun"]] = True
            if "laser" in slot:
                lw = weapons[slot["laser"]]
                ls = lw["stats"]
                i = slot["laser"]
                if lw.get("cooldown", 0) <= 0 and d < ls["range"] - 5 and (tvis or lw.get("charge", 0) > 0):
                    if lw.get("charge", 0) < ls["charge"] - 1e-3:
                        fire[i] = True  # keep charging
                    else:
                        fire[i] = not (tvis and err_y < tol * 0.8 and err_p < tol * 1.5)  # release = shoot
            if "lightning" in slot:
                lw = weapons[slot["lightning"]]
                ls = lw["stats"]
                if d < ls["range"] - 2 and err_y < math.radians(ls["cone_deg"]) * 0.4 and energy >= ls["energy"] + 6 and tvis:
                    fire[slot["lightning"]] = True
        elif crate is not None:
            cp = [crate["pos"][0], crate["pos"][1], crate["pos"][2] + 1.2]
            aim_yaw, aim_pitch = self.angles(muzzle, cp)
            ay, ap = me_s["aim"]
            d = dist2(cp, me)
            tol = max(0.006, 0.7 * math.atan2(1.4, max(d, 1.0)))
            if gun is not None and abs(wrap(ay - aim_yaw)) < tol and abs(ap - aim_pitch) < tol * 1.6 and gun.get("reload", 0) == 0:
                fire[slot["gun"]] = True
            if "lightning" in slot and d < 40 and abs(wrap(ay - aim_yaw)) < 0.15 and energy > 40:
                fire[slot["lightning"]] = True
        else:
            if self.threat_yaw is not None and t < self.threat_until:
                aim_yaw = self.threat_yaw
            else:
                # sweep around the travel/zone direction to see who is coming
                base = math.atan2(z["next_center"][1] - me[1], z["next_center"][0] - me[0])
                if self.wander is not None and t < EXPLORE_UNTIL:
                    base = math.atan2(self.wander[1] - me[1], self.wander[0] - me[0]) + math.pi
                aim_yaw = wrap(base + math.pi + 1.6 * math.sin(t * 0.9 + self.scan_phase))
            aim_pitch = 0.0
            if gun is not None and gun.get("ammo", 99) < gun["stats"]["mag"] * 0.6 and gun.get("reload", 0) == 0:
                act["reload"] = True
            # a charged laser with nothing to shoot: keep it ready only while it will not auto-fire
            if "laser" in slot:
                lw = weapons[slot["laser"]]
                if 0 < lw.get("charge", 0) < lw["stats"]["charge"]:
                    fire[slot["laser"]] = True

        # wounded mech that just left view: its last known spot (v6c)
        chase = None
        if CHASE_HP > 0 and not mechs:
            for mid, (ts, pos, hpf) in self.wounded.items():
                if t - ts < 6.0 and hpf < CHASE_HP and dist2(pos, me) > 6 and self.safe(pos, z, t):
                    chase = pos
                    break
        for m in mechs:
            self.wounded[m["id"]] = (t, m["pos"], m.get("hp", 1400) / max(1.0, m.get("max_hp", 1400)))
        # ---- where to go
        goal = self.zone_goal(me, z, t)
        dodge = None
        for m in mechs:
            if m.get("charging"):
                to_me = math.atan2(me[1] - m["pos"][1], me[0] - m["pos"][0])
                if abs(wrap(m["aim"][0] - to_me)) < 0.12:
                    dodge = to_me + math.pi / 2 * self.strafe
        move_yaw = None
        speed = 1.0
        boost = False
        c_now, r_now = self.zone_now(z, t)
        outside = dist2(me, c_now) > r_now - 3
        if outside:
            goal = goal or z["next_center"]
            boost = True
        if t > self.strafe_until:
            self.strafe = -self.strafe
            self.strafe_until = t + self.rng.uniform(0.8, 1.8)

        if target is not None and tkind == "mech" and not outside:
            tp = target["pos"]
            to_t = math.atan2(tp[1] - me[1], tp[0] - me[0])
            d = dist2(tp, me)
            want = WANT_LIGHTNING if ("lightning" in slot and hp_frac > 0.5) else (WANT_LASER if "laser" in slot else WANT_GUN)
            radial = 0.0
            if d < want - 15:
                radial = -1.0
            elif d > want + 15 and want < 1e8:
                radial = 0.7
            vx = math.cos(to_t) * radial + math.cos(to_t + math.pi / 2) * self.strafe * 0.8
            vy = math.sin(to_t) * radial + math.sin(to_t + math.pi / 2) * self.strafe * 0.8
            if goal is not None:
                gy = math.atan2(goal[1] - me[1], goal[0] - me[0])
                vx += math.cos(gy) * 0.8
                vy += math.sin(gy) * 0.8
            move_yaw = math.atan2(vy, vx)
            boost = hp_frac < 0.35 and energy > 25
        else:
            # v3: no mech in sight -> farm cubes, loot and drops that are safe to reach, then the zone
            pts = []
            if not mechs:
                for c in obs["cubes"]:
                    pts.append((dist2(c["pos"], me), c["pos"]))
                if weapons[1] is None:
                    for p in obs["pickups"]:
                        if p["kind"] in WANT_WEAPONS and p["kind"] not in slot:
                            pts.append((dist2(p["pos"], me) - 20, p["pos"]))
                for dr in obs["drops"]:
                    dd = dist2(dr["pos"], me)
                    if dd < 300 and dr["lands_in"] < dd / 11.0 + 3 and dr.get("holder") in (None, me_s["id"]) \
                            and hp_frac > 0.3:
                        pts.append((dd - 60, dr["pos"]))
            pts = [p for p in pts if p[0] < LOOT_RANGE and self.safe(p[1], z, t)]
            if pts and not outside:
                _, gp = min(pts, key=lambda p: p[0])
                dd = dist2(gp, me)
                if dd > 1.0:
                    move_yaw = math.atan2(gp[1] - me[1], gp[0] - me[0])
                    speed = min(1.0, dd / 4.0)
                    boost = energy > 70 and dd > 40
            elif target is not None and tkind == "npc" and not outside:
                tp = target["pos"]
                to_t = math.atan2(tp[1] - me[1], tp[0] - me[0])
                d = dist2(tp, me)
                radial = 1.0 if d > 45 else (-0.6 if d < 25 else 0.0)
                vx = math.cos(to_t) * radial + math.cos(to_t + math.pi / 2) * self.strafe * 0.7
                vy = math.sin(to_t) * radial + math.sin(to_t + math.pi / 2) * self.strafe * 0.7
                move_yaw = math.atan2(vy, vx)
            elif crate is not None and not outside and dist2(crate["pos"], me) > 30:
                move_yaw = math.atan2(crate["pos"][1] - me[1], crate["pos"][0] - me[0])
            elif chase is not None and not outside:
                move_yaw = math.atan2(chase[1] - me[1], chase[0] - me[0])
                boost = energy > 40
            elif goal is not None:
                move_yaw = math.atan2(goal[1] - me[1], goal[0] - me[0])
                boost = boost or (energy > 60 and (z["shrink_start"] - t) < dist2(me, goal) / 11.0 + 5)
            elif t < EXPLORE_UNTIL:
                # wander inside the next circle to find drones, crates and cubes
                nc, nr = z["next_center"], z["next_radius"]
                if self.wander is None or t > self.wander_until or dist2(self.wander, me) < 8 \
                        or dist2(self.wander, nc) > nr * 0.7:
                    a = self.rng.random() * TAU
                    r = nr * 0.6 * math.sqrt(self.rng.random())
                    self.wander = [nc[0] + math.cos(a) * r, nc[1] + math.sin(a) * r]
                    self.wander_until = t + 25.0
                move_yaw = math.atan2(self.wander[1] - me[1], self.wander[0] - me[0])
            else:
                # late game: drift toward the middle of the next circle, slowly (silent below step speed)
                nc = z["next_center"]
                dd = dist2(me, nc)
                if dd > 10:
                    move_yaw = math.atan2(nc[1] - me[1], nc[0] - me[0])
                    speed = 0.4
        # ---- hide behind a building to regenerate (v2)
        threats = [v[1] for v in self.seen.values()]
        if not self.hiding and hp_frac < HIDE_HP and threats:
            self.hiding = True
        if self.hiding and (hp_frac > 0.9 or not threats):
            self.hiding = False
            self.cover = None
        if self.hiding and not outside:
            if self.cover is None or t - self.cover_t > 0.6:
                self.cover = self.find_cover(me, threats, z, t)
                self.cover_t = t
            if self.cover is not None:
                dd = dist2(self.cover, me)
                if dd > 1.5:
                    move_yaw = math.atan2(self.cover[1] - me[1], self.cover[0] - me[0])
                    speed = 1.0
                    boost = energy > 20 and dd > 6
                else:
                    move_yaw = None  # stand still: silent and out of sight
                    boost = False
            elif target is not None and tkind == "mech":
                tp = target["pos"]
                move_yaw = math.atan2(me[1] - tp[1], me[0] - tp[0]) + 0.5 * self.strafe
                boost = energy > 25
        if dodge is not None:
            move_yaw = dodge
            speed = 1.0
            boost = energy > 15

        # ---- stuck detection and steering
        jump = False
        self.hist.append((t, me[0], me[1]))
        while self.hist and self.hist[0][0] < t - 1.2:
            self.hist.pop(0)
        if move_yaw is not None:
            move_yaw, climb = self.steer(me, move_yaw, t, energy)
            if speed > 0.5 and len(self.hist) > 8 and t - self.hist[0][0] > 1.0 and \
                    dist2(me, self.hist[0][1:]) < 2.0 and t > self.climb_until + 0.5:
                self.climb_until = t + 1.5
                self.side = -self.side
            if climb or t < self.climb_until:
                if me_s.get("grounded"):
                    jump = True
                else:
                    boost = energy > 5
                speed = 1.0
            act["move"] = [math.cos(move_yaw) * speed, math.sin(move_yaw) * speed]
        else:
            act["move"] = [0.0, 0.0]
        act["boost"] = bool(boost)
        act["jump"] = bool(jump and me_s.get("grounded"))
        pmin, pmax = st.get("min_pitch", -0.7), st.get("max_pitch", 1.3)
        act["aim"] = [wrap(aim_yaw), max(pmin, min(pmax, aim_pitch))]
        act["fire"] = fire

        if obs["tick"] % 10 == 0:
            mf.draw.label(f"hp {me_s['hp']:.0f} e {energy:.0f} {'T' + str(target['id']) if target else '-'}{' HIDE' if self.hiding else ''}")
        return act


mf.run(Bot)
