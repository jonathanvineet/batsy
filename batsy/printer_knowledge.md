# Jonathan's 3D printer: what Batsy knows, and what to change for what

## The machine
- Creality Ender-5 Pro (2019), Marlin 1.1.6, CH340 USB at 115200 baud, prints from the SD (TF) card.
- Bed 220 x 220, height 300, 0.4 mm nozzle. Ender-5 Pro is a Bowden extruder (metal extruder, Capricorn tube).
- Slicer: OrcaSlicer (flatpak), presets from the "Creality Ender-5 S1 0.4 nozzle" bundle in ~/.local/share/batsy/printer/.
  Caution: that bundle is the S1 (direct drive) machine profile: retraction 2 mm @ 60 mm/s, which is short for a Bowden.
  If stringing: retraction_length 4-6 mm, retraction_speed 40-45 mm/s.
- Machine limits in the profile: accel 500 extruding, 1500 travel, jerk 8 X/Y, Z max 5 mm/s.
- Filament: "Creality Generic PLA- main": nozzle 190, first layer 195 (bed from the system PLA profile, about 60).

## Your process presets (default "nicee")
- nicee: 0.14 layers, 3 walls, lightning infill, top 10 / bottom 4, outer wall 25, ironing top, tree supports, brim 5 mm, seam random.
- fast: 0.16 layers, 4 walls, lightning, top/bottom 6, infill 120 mm/s, travel 300, no brim, seam nearest.
- fineeee: 0.10 layers, 4 walls, gyroid, top/bottom 6, outer 20 / inner 30, ironing, supports.
- fineeee-car: 0.12 layers, 3 walls, gyroid, top/bottom 10, fuzzy skin all walls, ironing.
- keychain: 0.08 layers, 8 walls, 100% gyroid, top/bottom 14, outer wall line 0.32, ironing (flow 10%, spacing 0.08), seam aligned back.
- litho: 0.12 layers, 1000 walls (solid lithophane), brim 25 inner+outer, slow (outer 25, travel 100).
- litho2: 0.10 layers, 4 walls, lightning, top/bottom 10, ironing, brim 15.
- base vadar: 0.20 base, gyroid, outer 45 / inner 80, brim ears 15, fuzzy skin outside, supports.

## OrcaSlicer keys (process), values as strings
- layer_height (0.08-0.28 on a 0.4 nozzle; below 0.25 x nozzle is pointless), initial_layer_print_height (0.2-0.28).
- line_width / outer_wall_line_width / inner_wall_line_width (0.4-0.45; up to 0.5 for strength).
- wall_loops, top_shell_layers, bottom_shell_layers, top_shell_thickness, bottom_shell_thickness.
- sparse_infill_density ("15%"), sparse_infill_pattern: gyroid, grid, cubic, adaptivecubic, lightning, honeycomb,
  3dhoneycomb, rectilinear, line, triangles, crosshatch. top_surface_pattern / bottom_surface_pattern: monotonic,
  monotonicline, alignedrectilinear, concentric, hilbertcurve, archimedeanchords, octagramspiral.
- Speeds mm/s: outer_wall_speed, inner_wall_speed, sparse_infill_speed, internal_solid_infill_speed, top_surface_speed,
  gap_infill_speed, support_speed, bridge_speed, initial_layer_speed, initial_layer_infill_speed, travel_speed.
- Accelerations: default_acceleration, outer_wall_acceleration, inner_wall_acceleration, top_surface_acceleration,
  initial_layer_acceleration, travel_acceleration (the firmware caps extruding moves at 500 anyway).
- Supports: enable_support "1"/"0", support_type "normal(auto)" / "tree(auto)" / "normal(manual)" / "tree(manual)",
  support_threshold_angle (30 default; higher = less support), support_on_build_plate_only, support_top_z_distance
  (0.2-0.3, bigger = easier removal), support_object_xy_distance, support_interface_top_layers, support_style.
- Adhesion: brim_type "no_brim" / "outer_only" / "inner_only" / "outer_and_inner" / "brim_ears" / "auto_brim",
  brim_width, brim_object_gap, raft_layers, skirt_loops, skirt_distance.
- Seam: seam_position "aligned" / "aligned_back" / "nearest" / "back" / "random"; seam_gap; seam_slope_type (scarf).
- Surface: ironing_type "no ironing" / "top" / "topmost" / "solid", ironing_flow, ironing_spacing, ironing_speed;
  only_one_wall_top; fuzzy_skin "none" / "external" / "all" / "allwalls", fuzzy_skin_thickness, fuzzy_skin_point_distance.
- Walls: wall_generator "classic" / "arachne", wall_sequence "inner wall/outer wall" / "outer wall/inner wall",
  precise_outer_wall, detect_thin_wall, infill_wall_overlap, ensure_vertical_shell_thickness.
- Other: spiral_mode "1" (vase), enable_arc_fitting (G2/G3), reduce_crossing_wall (avoid crossing walls),
  slow_down_layers, enable_overhang_speed, overhang_1_4_speed ... overhang_4_4_speed, xy_hole_compensation,
  xy_contour_compensation, elefant_foot_compensation, infill_direction, minimum_sparse_infill_area.
- Filament keys: nozzle_temperature, nozzle_temperature_initial_layer, hot_plate_temp / hot_plate_temp_initial_layer
  (bed), filament_flow_ratio (0.95-1.0), filament_max_volumetric_speed (PLA on stock hotend about 10-12 mm3/s),
  fan_min_speed, fan_max_speed, overhang_fan_speed, close_fan_the_first_x_layers, slow_down_layer_time,
  filament_retraction_length, filament_retraction_speed, pressure_advance (Marlin 1.1.6 has no Linear Advance by default).
- Machine keys: retraction_length, retraction_speed, deretraction_speed, z_hop, z_hop_types, retract_when_changing_layer,
  wipe, machine_start_gcode, machine_end_gcode, z_offset, printable_area, printable_height.

## Problem -> what to change
- Stringing / hairs: retraction up (Bowden 4-6 mm, 40-45 mm/s), nozzle 5-10 C cooler, travel faster, enable wipe.
- Warping / corners lifting: brim (outer_only, 5-8 mm) or brim ears, bed 60-65, fan off first 2-3 layers, clean bed (IPA),
  slower first layer (15-20 mm/s), initial layer 0.24-0.28.
- Not sticking: re-level / lower Z offset, initial layer slower and hotter (+5 C nozzle, +5 C bed), first-layer line 0.45-0.5.
- Elephant foot: elefant_foot_compensation 0.1-0.2, bed a little cooler, Z offset slightly up.
- Weak part / snaps: more walls (4-6) beats more infill, gyroid or cubic 30-50%, thicker lines (0.45-0.5), hotter (205-210),
  orient so layers aren't loaded in tension.
- Rough / droopy overhangs: more fan (100%), slower overhangs, lower layer height, supports at the threshold angle.
- Pillowing / holes in the top: more top layers (5+), monotonic top pattern, ironing.
- Under-extrusion / gaps: hotter, slower (stay under the volumetric limit), flow ratio up 2-5%, check the extruder arm and tube.
- Over-extrusion / blobs: flow ratio down to 0.95, seam aligned/back, lower temperature.
- Visible seam / zit: seam_position back or aligned, scarf seam, seam_gap 5-10%.
- Ringing / ghosting: lower outer wall speed and acceleration (outer 25-40 mm/s, 300-500 accel).
- Layer shifts: travel accel down, belts tight, nothing hitting the nozzle (z hop 0.4), motor current.
- Holes too small / parts don't fit: xy_hole_compensation 0.1-0.2, xy_contour_compensation -0.05 to -0.1.
- Supports hard to remove: support_top_z_distance 0.25-0.3, tree supports, fewer interface layers, support_object_xy_distance 0.5+.
- Faster prints: 0.24-0.28 layers, lightning infill 10-12%, 2-3 walls, faster infill, fewer top/bottom layers.
- Smoother / finer: 0.08-0.12 layers, slow outer wall (20-25), ironing top, monotonic top, seam back.
- PETG: nozzle 230-245, bed 75-85, fan 30-50%, slower (40-50 mm/s), retraction a bit less than PLA, z hop; on smooth PEI use glue stick as a release layer or it can tear the sheet.

## Marlin 1.1.6 commands over USB
- M105 temps, M155 S2 auto-report, M104/M109 S nozzle (set/wait), M140/M190 S bed, M106 S0-255 / M107 fan,
  G28 home, G1 move, G90/G91 abs/rel, M82/M83 extruder abs/rel, M84 motors off, M114 position, M220 S feedrate %,
  M221 S flow %, M290 / M851 Z offset (probe), M92 steps/mm (E steps 93 stock), M500 save EEPROM, M501 load, M502 defaults,
  M503 report settings, M303 E0 S200 C8 PID autotune hotend (E-1 bed), M301 / M304 set PID, M600 filament change
  (only if ADVANCED_PAUSE is enabled), M20 list SD, M23 select, M24 start/resume, M25 pause, M26 S position, M27 progress,
  M28/M29 write file to SD, M30 delete, M117 message on screen, M201 / M203 / M204 / M205 limits, M112 emergency stop.
- Live tuning during a print: M220 speed %, M221 flow %, M104 temperature, M106 fan. Everything else needs a re-slice.
