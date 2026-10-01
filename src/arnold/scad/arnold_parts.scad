// arnold_parts.scad - tested mechanisms for A.R.N.O.L.D.'s designed parts.
//
// The design model chooses one of these and sets its numbers, rather than
// inventing a hinge from nothing: the geometry here is checked by the test
// suite (tests/test_scad_library.py), including that a hinged lid swings
// through its whole range without touching the box. Decoration is added as
// children, in the frame of the face it sits on.
//
// Conventions: millimetres; the body stands on z=0, centred on x=0, its
// front face at y=0 and its back at y=d. Everything is built "closed" and
// then laid out: layout="print" (default) puts every piece flat on the plate
// in the orientation that prints without supports; "closed" and "open" are
// for looking at it assembled.
//
// OpenSCAD 2021.01, no other libraries.

$fn = 64;

// -- small helpers ----------------------------------------------------------

// A rounded rectangle in XY: x centred, y from y0 to y1.
module _rrect(w, y0, y1, r) {
    rr = max(0.01, min(r, w / 2 - 0.01, (y1 - y0) / 2 - 0.01));
    hull() for (x = [-w / 2 + rr, w / 2 - rr], y = [y0 + rr, y1 - rr])
        translate([x, y]) circle(r = rr);
}

// A block with rounded vertical edges: x centred, y from 0 to d, z from 0 to h.
module rounded_box(size, corner = 3) {
    linear_extrude(size[2]) _rrect(size[0], 0, size[1], corner);
}

// Raised relief from 2D shapes or text: relief(0.8) text("X", 8, halign="center", valign="center");
module relief(h = 0.8) {
    linear_extrude(h) children();
}

// A traced picture (emblem.py writes the SVG beside the part), w wide and
// centred on the origin, raised by h: emblem("part.svg", 30);
module emblem(file, w, h = 0.8) {
    relief(h) resize([w, 0], auto = true) import(file, center = true);
}

// A ring for a key ring or lanyard, lying flat, centred on the origin.
module keyring_loop(hole = 5, band = 2.5, t = 3) {
    difference() {
        cylinder(d = hole + 2 * band, h = t);
        translate([0, 0, -1]) cylinder(d = hole, h = t + 2);
    }
}

// A pocket for a round magnet, to difference() away: opening at z=0, going up.
module magnet_pocket(d = 10, h = 3, clearance = 0.2) {
    translate([0, 0, -0.01]) cylinder(d = d + 2 * clearance, h = h + clearance + 0.01);
}

// A countersunk screw hole, to difference() away: head at z=0, shank going down.
module screw_hole(d = 3.2, head = 6.5, depth = 20) {
    translate([0, 0, -depth]) cylinder(d = d, h = depth + 0.01);
    translate([0, 0, -(head - d) / 2]) cylinder(d1 = d, d2 = head, h = (head - d) / 2 + 0.01);
    cylinder(d = head, h = 5);
}

// -- faces, for decoration ------------------------------------------------------
// Each puts its children on a face: local x across the face, local y up the
// face (or towards the back, on a top face), local z out of it. Give reliefs
// 0.6-1.2 mm of height from z=0. A lid prints upside down, so what is put on
// a lid's top is ENGRAVED into it (ENGRAVE deep) rather than raised.

ENGRAVE = 0.8;

// Sunk 0.05 mm into the face, so a relief always fuses with it.
module _on_front(y, zmid) {
    translate([0, y, zmid]) rotate([90, 0, 0]) translate([0, 0, -0.05]) children();
}

module _on_top(ymid, z) {
    translate([0, ymid, z]) children();
}

// -- the hinged box and the pouch ------------------------------------------------
//
// A box with a lid on a knuckle hinge along the top of the back. The pin is a
// length of 1.75 mm filament pushed through the knuckles (melt or glue the
// ends): box and lid print as separate flat pieces, which is what makes the
// hinge dependable on any printer. flap > 0 turns the lid into a pouch flap
// that folds down over the front, with a snap ridge to hold it shut. belt > 0
// adds a loop on the back for a belt that wide.
//
// Children: 0 = on the lid's top, 1 = on the front (the flap's face when there
// is a flap, else the box's front), 2 = on the box's front below the flap.

module hinged_box(size = [60, 40, 30], wall = 2, floor = 2, lid = 3, corner = 3,
                  flap = 0, catch = true, clearance = 0.3, pin = 1.75, knuckle = 3.5,
                  knuckles = 5, belt = 0, belt_t = 4, loop_w = 20, loop_top = 0, strap = 3,
                  layout = "print", angle = 100, part = "all") {
    w = size[0]; d = size[1]; h = size[2];
    c = clearance;
    r = knuckle;
    zl = h + c;                    // the lid's underside
    zt = zl + lid;                 // the lid's top
    ya = d + c + r;                // the hinge axis: behind the back, level with the lid's underside
    za = zl;
    ft = wall;                     // the flap's thickness
    yf = flap > 0 ? -(c + ft) : 0; // the lid's front edge
    L = w - 2 * corner;            // hinge length, clear of the rounded corners
    n = knuckles % 2 == 1 ? knuckles : knuckles + 1;  // odd: the box has both ends
    s = (L - (n - 1) * c) / n;
    hole = pin + 0.25;
    ridge = 0.8;
    zc = h - flap + 4;             // the snap ridge's height on the front
    has_catch = catch && flap >= 9;
    top = loop_top > 0 ? loop_top : h - 2 * r - c - 2;
    gap = belt_t + 2;              // how far the loop stands off the back

    assert(wall >= 1.2, "walls under 1.2 mm will not print well");
    assert(s >= 3, "too many knuckles for this width - use fewer");
    assert(flap == 0 || flap <= h - floor, "the flap is longer than the box is tall");
    assert(belt == 0 || top - 2 * strap >= belt + 2,
           str("the belt loop is too short for a ", belt, " mm belt: make the box taller"));
    assert(belt == 0 || top <= h - 2 * r - c - 1, "the belt loop would run into the hinge");

    module seg(i) translate([-L / 2 + i * (s + c), 0, 0]) children();

    module axis_cyl(rad, len) translate([0, ya, za]) rotate([0, 90, 0]) cylinder(r = rad, h = len);

    module body() {
        difference() {
            union() {
                rounded_box([w, d, h], corner);
                // Knuckles on the box: the even ones, braced to the back at 45 degrees.
                // The brace stops at the axis: above it is where the lid's edge swings.
                for (i = [0 : 2 : n - 1]) seg(i) intersection() {
                    union() {
                        axis_cyl(r, s);
                        intersection() {
                            hull() {
                                axis_cyl(r, s);
                                translate([0, d - 1, za - r - (ya - d)]) cube([s, 1, r + (ya - d)]);
                            }
                            translate([-1, d - 1, 0]) cube([s + 2, ya + r + 2 - d, h]);
                        }
                    }
                    translate([-1, d - 1, 0]) cube([s + 2, ya + r + 2 - d, zt]);
                }
                if (has_catch)
                    translate([-(w / 2 - corner - 2), 0, zc]) rotate([0, 90, 0])
                        cylinder(r = ridge, h = w - 2 * corner - 4);
                if (belt > 0) {
                    translate([-loop_w / 2, d + gap, 0]) cube([loop_w, strap, top]);
                    translate([-loop_w / 2, d - 1, 0]) cube([loop_w, gap + 1 + strap, strap]);
                    translate([-loop_w / 2, d - 1, top - strap]) cube([loop_w, gap + 1 + strap, strap]);
                }
            }
            // The cavity.
            translate([0, wall, floor])
                linear_extrude(h) _rrect(w - 2 * wall, 0, d - 2 * wall, max(0.5, corner - wall));
            translate([-L / 2 - 1, 0, 0]) axis_cyl(hole / 2, L + 2);
        }
        if ($children > 2) _on_front(0, (flap > 0 ? (h - flap) : h) / 2) children(2);
    }

    module lid_part() {
        difference() {
            union() {
                translate([0, 0, zl]) linear_extrude(lid) _rrect(w, yf, d, corner);
                if (flap > 0)
                    intersection() {
                        translate([0, 0, h - flap]) linear_extrude(flap + c + lid) _rrect(w, yf, d, corner);
                        translate([-w, yf, h - flap]) cube([2 * w, ft, flap + c + lid]);
                    }
                // Knuckles on the lid: the odd ones, joined to it by a bar.
                for (i = [1 : 2 : n - 1]) seg(i) intersection() {
                    union() {
                        axis_cyl(r, s);
                        translate([0, d - 0.01, zl]) cube([s, ya - d + 0.01, lid]);
                    }
                    translate([-1, d - 1, za - r - 1]) cube([s + 2, ya + r + 2 - d, zt - (za - r - 1)]);
                }
            }
            translate([-L / 2 - 1, 0, 0]) axis_cyl(hole / 2, L + 2);
            if (has_catch)
                translate([-(w / 2 - corner - 2) - c, 0, zc]) rotate([0, 90, 0])
                    cylinder(r = ridge + c, h = w - 2 * corner - 4 + 2 * c);
            if ($children > 0) _on_top((yf + d) / 2, zt - ENGRAVE) children(0);
        }
        if ($children > 1) {
            if (flap > 0) _on_front(yf, h - flap / 2) children(1);
        }
    }

    // Front decoration on the box itself, when there is no flap, belongs to the body.
    module lid_placed() {
        if (layout == "print")
            translate([w + 2 * r + 8, 0, zt]) rotate([180, 0, 0]) children();
        else if (layout == "open")
            translate([0, ya, za]) rotate([-angle, 0, 0]) translate([0, -ya, -za]) children();
        else
            children();
    }

    // part: "all", or "body" / "lid" alone (for checking one against the other).
    show_body = part != "lid";
    show_lid = part != "body";
    if (flap == 0 && $children > 1) {
        if (show_body) body() { union() {} union() {} children(1); }
        if (show_lid) lid_placed() lid_part() children(0);
    } else {
        if (show_body) { if ($children > 2) body() { union() {} union() {} children(2); } else body(); }
        if (show_lid) {
            if ($children > 1) lid_placed() lid_part() { children(0); children(1); }
            else if ($children > 0) lid_placed() lid_part() children(0);
            else lid_placed() lid_part();
        }
    }
}

// A belt pouch: a hinged box whose lid folds down over the front and snaps
// shut, with a loop on the back. size is the box without the lid.
module pouch(size = [40, 25, 60], belt = 40, belt_t = 4, flap = 0, wall = 2, corner = 4,
             clearance = 0.3, loop_w = 0, layout = "print", angle = 100, part = "all") {
    fl = flap > 0 ? flap : round(size[2] * 0.35);
    lw = loop_w > 0 ? loop_w : min(size[0] - 2 * corner, 24);
    if ($children > 2)
        hinged_box(size, wall = wall, corner = corner, flap = fl, belt = belt, belt_t = belt_t,
                   loop_w = lw, clearance = clearance, layout = layout, angle = angle, part = part)
            { children(0); children(1); children(2); }
    else if ($children > 1)
        hinged_box(size, wall = wall, corner = corner, flap = fl, belt = belt, belt_t = belt_t,
                   loop_w = lw, clearance = clearance, layout = layout, angle = angle, part = part)
            { children(0); children(1); }
    else if ($children > 0)
        hinged_box(size, wall = wall, corner = corner, flap = fl, belt = belt, belt_t = belt_t,
                   loop_w = lw, clearance = clearance, layout = layout, angle = angle, part = part)
            children(0);
    else
        hinged_box(size, wall = wall, corner = corner, flap = fl, belt = belt, belt_t = belt_t,
                   loop_w = lw, clearance = clearance, layout = layout, angle = angle, part = part);
}

// -- the lidded box -----------------------------------------------------------------
//
// A box with a press-on lid: a lip under the lid fits inside the walls with
// `clearance` all round and holds by friction.
// Children: 0 = on the lid's top, 1 = on the box's front.

module lidded_box(size = [60, 40, 30], wall = 2, floor = 2, lid = 2.4, corner = 3,
                  lip = 5, clearance = 0.25, layout = "print", part = "all") {
    w = size[0]; d = size[1]; h = size[2];
    c = clearance;
    lt = 1.6;  // the lip's thickness
    assert(wall >= 1.2, "walls under 1.2 mm will not print well");
    assert(lip < h - floor, "the lip is deeper than the box");
    if (part != "lid") {
        difference() {
            rounded_box([w, d, h], corner);
            translate([0, wall, floor]) linear_extrude(h) _rrect(w - 2 * wall, 0, d - 2 * wall, max(0.5, corner - wall));
        }
        if ($children > 1) _on_front(0, h / 2) children(1);
    }

    module lid_part() {
        difference() {
            translate([0, 0, h]) linear_extrude(lid) _rrect(w, 0, d, corner);
            if ($children > 0) _on_top(d / 2, h + lid - ENGRAVE) children(0);
        }
        // The lip hangs down inside the walls.
        translate([0, 0, h - lip]) difference() {
            translate([0, wall + c, 0]) linear_extrude(lip) _rrect(w - 2 * (wall + c), 0, d - 2 * (wall + c), max(0.5, corner - wall - c));
            translate([0, wall + c + lt, -1]) linear_extrude(lip + 2) _rrect(w - 2 * (wall + c + lt), 0, d - 2 * (wall + c + lt), max(0.3, corner - wall - c - lt));
        }
    }
    if (part == "body") {
    } else if (layout == "print")
        translate([w + 8, 0, h + lid]) rotate([180, 0, 0]) lid_part() if ($children > 0) children(0);
    else
        translate([0, 0, layout == "open" ? 15 : 0]) lid_part() if ($children > 0) children(0);
}

// -- the tray -------------------------------------------------------------------------
//
// An open tray divided into rows x cols compartments, for an organiser or a
// drawer insert. Children: 0 = on the front.

module tray(size = [120, 80, 30], rows = 2, cols = 3, wall = 1.6, floor = 1.6, corner = 3, divider = 1.2) {
    w = size[0]; d = size[1]; h = size[2];
    assert(wall >= 1.2, "walls under 1.2 mm will not print well");
    iw = w - 2 * wall; id = d - 2 * wall;
    difference() {
        rounded_box([w, d, h], corner);
        for (i = [0 : cols - 1], j = [0 : rows - 1]) {
            cw = (iw - (cols - 1) * divider) / cols;
            cd = (id - (rows - 1) * divider) / rows;
            translate([-iw / 2 + i * (cw + divider), wall + j * (cd + divider), floor])
                translate([cw / 2, 0, 0]) linear_extrude(h) _rrect(cw, 0, cd, max(0.5, corner - wall));
        }
    }
    if ($children > 0) _on_front(0, h / 2) children(0);
}
