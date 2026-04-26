# SPDX-FileCopyrightText: 2017-2022 Blender Foundation
# SPDX-License-Identifier: GPL-2.0-or-later

import time
import math
import bpy
from bpy.types import Operator, Panel, Scene, PropertyGroup, UIList
from bpy.props import (
    IntProperty,
    FloatProperty,
    EnumProperty,
    BoolProperty,
    StringProperty,
    CollectionProperty
)

AUTO_CONSOLE_OPENED = False

# -------------------------------------------------------------------
#  LOGGING SYSTEM
# -------------------------------------------------------------------

class AutoTrackLogItem(PropertyGroup):
    message: StringProperty()
    icon: StringProperty(default="INFO")

class AutoTrackResultItem(PropertyGroup):
    scenario: StringProperty()
    solve: StringProperty()
    error: FloatProperty(default=0.0)
    bundles: IntProperty(default=0)
    score: FloatProperty(default=0.0)

def log_msg(scene, message, icon='INFO'):
    print(f"[AutoTrack] {message}", flush=True)
    
    try:
        scene.autotrack_status = message
        item = scene.autotrack_log.add()
        item.message = message
        item.icon = icon
    except:
        pass 
    
    if len(scene.autotrack_log) > 50:
        scene.autotrack_log.remove(0)
        
    scene.autotrack_log_index = len(scene.autotrack_log) - 1
    force_ui_update()

def force_ui_update():
    try:
        for window in bpy.context.window_manager.windows:
            screen = window.screen
            for area in screen.areas:
                area.tag_redraw()
    except Exception:
        pass

    try:
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    except Exception:
        pass

def ensure_console_open():
    global AUTO_CONSOLE_OPENED
    if AUTO_CONSOLE_OPENED:
        return

    console_toggle = getattr(bpy.ops.wm, 'console_toggle', None)
    if not console_toggle:
        AUTO_CONSOLE_OPENED = True
        return

    try:
        console_toggle()
        AUTO_CONSOLE_OPENED = True
    except Exception:
        AUTO_CONSOLE_OPENED = True

def get_nearby_marker(track, frame):
    marker = track.markers.find_frame(frame, exact=True)
    if marker:
        return marker
    return track.markers.find_frame(frame - 1, exact=True)

def count_recent_valid_markers(track, start_frame, end_frame):
    count = 0
    for marker in track.markers:
        if start_frame <= marker.frame <= end_frame and not marker.mute:
            count += 1
    return count

def track_key(track):
    return track.as_pointer()

def get_clip_space(context):
    if not context.area:
        return None
    for space in context.area.spaces:
        if space.type == 'CLIP_EDITOR':
            return space
    return None

def get_clip_context(context):
    screen = context.screen
    area = context.area if context.area and context.area.type == 'CLIP_EDITOR' else None

    if not area and screen:
        area = next((item for item in screen.areas if item.type == 'CLIP_EDITOR'), None)

    if not area:
        return None, None, None

    region = next((item for item in area.regions if item.type == 'WINDOW'), None)
    space = next((item for item in area.spaces if item.type == 'CLIP_EDITOR'), None)
    return area, region, space

def call_clip_op(context, operator, *args, **kwargs):
    area, region, space = get_clip_context(context)
    if not area or not region or not space:
        raise RuntimeError("No valid Movie Clip Editor context")

    with context.temp_override(area=area, region=region, space_data=space):
        return operator(*args, **kwargs)

def set_tracking_frame(context, frame):
    context.scene.frame_set(frame)
    space = get_clip_space(context)
    clip_user = getattr(space, 'clip_user', None) if space else None
    if clip_user and hasattr(clip_user, 'frame_current'):
        try:
            clip_user.frame_current = frame
        except Exception:
            pass
    return context.scene.frame_current

def set_clip_frame(context, frame):
    scene_frame = set_tracking_frame(context, frame)
    area, region, space = get_clip_context(context)
    clip_user = getattr(space, 'clip_user', None) if space else None
    if clip_user and hasattr(clip_user, 'frame_current'):
        try:
            clip_user.frame_current = frame
        except Exception:
            pass
    return scene_frame

def count_trackable_tracks(tracks, frame):
    count = 0
    for track in tracks:
        if track.hide or track.lock:
            continue
        marker = get_nearby_marker(track, frame)
        if marker and not marker.mute:
            count += 1
    return count

def set_track_selected(track, selected):
    track.select = selected
    for attr in ('select_anchor', 'select_pattern', 'select_search'):
        if hasattr(track, attr):
            setattr(track, attr, selected)

def delete_tracks(tracks):
    if not tracks:
        return
    bpy.ops.clip.select_all(action='DESELECT')
    for track in tracks:
        set_track_selected(track, True)
    bpy.ops.clip.delete_track()

def delete_tracks_in_context(context, tracks):
    if not tracks:
        return
    call_clip_op(context, bpy.ops.clip.select_all, action='DESELECT')
    for track in tracks:
        set_track_selected(track, True)
    call_clip_op(context, bpy.ops.clip.delete_track)

def detect_trial_features(context, scene, clip, scenario, threshold_scale=1.0, distance_scale=1.0):
    tracks = clip.tracking.tracks
    existing_track_keys = {track_key(track) for track in tracks}
    threshold = max(0.0001, scene.autotrack_detect_threshold * scenario['detect_threshold_scale'] * threshold_scale)
    distance = max(2, int(scene.autotrack_detect_distance * scenario['detect_distance_scale'] * distance_scale))

    call_clip_op(context, bpy.ops.clip.select_all, action='DESELECT')
    call_clip_op(
        context,
        bpy.ops.clip.detect_features,
        threshold=threshold,
        min_distance=distance,
        margin=scene.autotrack_detect_margin,
        placement=scene.autotrack_detect_placement
    )

    new_trackers = [t for t in tracks if track_key(t) not in existing_track_keys]
    if len(new_trackers) > scene.autotrack_max_new_tracks:
        delete_tracks_in_context(context, new_trackers[scene.autotrack_max_new_tracks:])
        new_trackers = new_trackers[:scene.autotrack_max_new_tracks]

    return new_trackers, threshold, distance

def get_setting(settings, attr):
    return getattr(settings, attr) if hasattr(settings, attr) else None

def set_setting(settings, attr, value):
    if not hasattr(settings, attr):
        return False
    try:
        setattr(settings, attr, value)
        return True
    except Exception:
        return False

def save_settings(settings, attrs):
    return {attr: get_setting(settings, attr) for attr in attrs if hasattr(settings, attr)}

def restore_settings(settings, values):
    for attr, value in values.items():
        set_setting(settings, attr, value)

def count_reconstructed_tracks(tracks):
    return sum(1 for track in tracks if track.weight > 0.0 and not track.hide and track.has_bundle)

TRACKING_STRATEGY_PRESETS = {
    'BALANCED': {
        'name': 'Balanced',
        'motion_model': 'LocRotScale',
        'pattern_size': 21,
        'search_size': 71,
        'pattern_match': 'PREV_FRAME',
        'brute': False,
        'normalization': True,
        'correlation_min': 0.75,
    },
    'LOCATION': {
        'name': 'Location',
        'motion_model': 'Loc',
        'pattern_size': 21,
        'search_size': 61,
        'pattern_match': 'PREV_FRAME',
        'brute': False,
        'normalization': True,
        'correlation_min': 0.80,
    },
    'AFFINE': {
        'name': 'Affine',
        'motion_model': 'Affine',
        'pattern_size': 31,
        'search_size': 101,
        'pattern_match': 'PREV_FRAME',
        'brute': True,
        'normalization': True,
        'correlation_min': 0.70,
    },
    'PERSPECTIVE': {
        'name': 'Perspective',
        'motion_model': 'Perspective',
        'pattern_size': 41,
        'search_size': 151,
        'pattern_match': 'PREV_FRAME',
        'brute': True,
        'normalization': True,
        'correlation_min': 0.65,
    },
}

TRACKING_STRATEGY_ORDER = ('BALANCED', 'LOCATION', 'AFFINE', 'PERSPECTIVE')

def apply_tracking_strategy(settings, strategy_id):
    preset = TRACKING_STRATEGY_PRESETS.get(strategy_id)
    if not preset:
        return False

    set_setting(settings, 'default_motion_model', preset['motion_model'])
    set_setting(settings, 'default_pattern_size', preset['pattern_size'])
    set_setting(settings, 'default_search_size', preset['search_size'])
    set_setting(settings, 'default_pattern_match', preset['pattern_match'])
    set_setting(settings, 'use_default_brute', preset['brute'])
    set_setting(settings, 'default_use_brute', preset['brute'])
    set_setting(settings, 'use_default_normalization', preset['normalization'])
    set_setting(settings, 'default_use_normalization', preset['normalization'])
    set_setting(settings, 'default_correlation_min', preset['correlation_min'])
    return True

def resolve_tracking_strategy(scene):
    if scene.autotrack_tracking_strategy == 'AUTO':
        return scene.autotrack_active_tracking_strategy or 'BALANCED'
    return scene.autotrack_tracking_strategy

def escalate_tracking_strategy(scene, settings):
    current = resolve_tracking_strategy(scene)
    if current not in TRACKING_STRATEGY_ORDER:
        current = 'BALANCED'

    index = TRACKING_STRATEGY_ORDER.index(current)
    if index >= len(TRACKING_STRATEGY_ORDER) - 1:
        return current

    next_strategy = TRACKING_STRATEGY_ORDER[index + 1]
    scene.autotrack_active_tracking_strategy = next_strategy
    apply_tracking_strategy(settings, next_strategy)
    return next_strategy

def apply_tracking_scenario(settings, scenario):
    set_setting(settings, 'default_motion_model', scenario['motion_model'])
    set_setting(settings, 'default_pattern_size', scenario['pattern_size'])
    set_setting(settings, 'default_search_size', scenario['search_size'])
    set_setting(settings, 'default_pattern_match', scenario['pattern_match'])
    set_setting(settings, 'use_default_brute', scenario['brute'])
    set_setting(settings, 'default_use_brute', scenario['brute'])
    set_setting(settings, 'use_default_normalization', scenario['normalization'])
    set_setting(settings, 'default_use_normalization', scenario['normalization'])
    set_setting(settings, 'default_correlation_min', scenario['correlation_min'])

def build_tracking_scenarios(iterations):
    motion_models = ('Loc', 'LocRotScale', 'Affine', 'Perspective')
    pattern_matches = ('PREV_FRAME', 'KEYFRAME')
    sizes = ((21, 61), (31, 101), (41, 151), (51, 201))
    scenarios = []

    for i in range(iterations):
        motion_model = motion_models[i % len(motion_models)]
        pattern_match = pattern_matches[(i // len(motion_models)) % len(pattern_matches)]
        pattern_size, search_size = sizes[(i // (len(motion_models) * len(pattern_matches))) % len(sizes)]
        hard_motion = motion_model in {'Affine', 'Perspective'}

        scenarios.append({
            'index': i + 1,
            'name': f'{motion_model} / {pattern_match} / {pattern_size}-{search_size}',
            'motion_model': motion_model,
            'pattern_match': pattern_match,
            'pattern_size': pattern_size,
            'search_size': search_size,
            'brute': hard_motion or search_size >= 151,
            'normalization': True,
            'correlation_min': max(0.55, 0.82 - (0.04 * (i % 6))),
            'detect_threshold_scale': max(0.35, 1.0 - (0.08 * (i % 5))),
            'detect_distance_scale': max(0.45, 1.0 - (0.07 * ((i // 2) % 5))),
        })

    return scenarios

def clear_tracking_tracks():
    bpy.ops.clip.select_all(action='SELECT')
    bpy.ops.clip.delete_track()

def clear_tracking_tracks_in_context(context):
    call_clip_op(context, bpy.ops.clip.select_all, action='SELECT')
    call_clip_op(context, bpy.ops.clip.delete_track)

def tracking_scenario_summary(scenario):
    return (
        f"{scenario['name']}, corr {scenario['correlation_min']:.2f}, "
        f"detect x{scenario['detect_threshold_scale']:.2f}/{scenario['detect_distance_scale']:.2f}"
    )

def apply_solve_preset(settings, preset):
    if hasattr(settings, 'refine_intrinsics'):
        set_setting(settings, 'refine_intrinsics', preset.get('legacy_refine', 'NONE'))
    else:
        set_setting(settings, 'refine_intrinsics_focal_length', preset.get('focal', False))
        set_setting(settings, 'refine_intrinsics_principal_point', preset.get('principal', False))
        set_setting(settings, 'refine_intrinsics_radial_distortion', preset.get('radial', False))
        set_setting(settings, 'refine_intrinsics_tangential_distortion', preset.get('tangential', False))

    if 'auto_keyframes' in preset:
        set_setting(settings, 'use_keyframe_selection', preset['auto_keyframes'])

def solve_score(error, reconstructed_count, minimum_tracks):
    if error is None:
        return float('inf')
    missing_tracks = max(0, minimum_tracks - reconstructed_count)
    return error + (missing_tracks * 0.05)

SOLVE_SETTING_ATTRS = (
    'refine_intrinsics',
    'refine_intrinsics_focal_length',
    'refine_intrinsics_principal_point',
    'refine_intrinsics_radial_distortion',
    'refine_intrinsics_tangential_distortion',
    'use_keyframe_selection',
)

SOLVE_PRESETS = [
    {
        'name': 'No refinement',
        'legacy_refine': 'NONE',
        'focal': False,
        'principal': False,
        'radial': False,
        'tangential': False,
        'auto_keyframes': True,
    },
    {
        'name': 'Focal length',
        'legacy_refine': 'FOCAL_LENGTH',
        'focal': True,
        'principal': False,
        'radial': False,
        'tangential': False,
        'auto_keyframes': True,
    },
    {
        'name': 'Focal + radial distortion',
        'legacy_refine': 'FOCAL_LENGTH_RADIAL_K1_K2',
        'focal': True,
        'principal': False,
        'radial': True,
        'tangential': False,
        'auto_keyframes': True,
    },
    {
        'name': 'Focal + optical center',
        'legacy_refine': 'FOCAL_LENGTH_PRINCIPAL_POINT',
        'focal': True,
        'principal': True,
        'radial': False,
        'tangential': False,
        'auto_keyframes': True,
    },
    {
        'name': 'Focal + optical center + distortion',
        'legacy_refine': 'FOCAL_LENGTH_PRINCIPAL_POINT_RADIAL_K1_K2',
        'focal': True,
        'principal': True,
        'radial': True,
        'tangential': True,
        'auto_keyframes': True,
    },
    {
        'name': 'Focal + distortion, fixed keyframes',
        'legacy_refine': 'FOCAL_LENGTH_RADIAL_K1_K2',
        'focal': True,
        'principal': False,
        'radial': True,
        'tangential': False,
        'auto_keyframes': False,
    },
]

def optimize_solve_settings(context, scene, clip, active_obj):
    settings = clip.tracking.settings
    original = save_settings(settings, SOLVE_SETTING_ATTRS)
    original_tripod = get_setting(settings, 'use_tripod_solver')
    minimum_tracks = max(8, min(scene.autotrack_min_tracks, 40))

    best = None
    best_snapshot = None
    log_msg(scene, f'Trying {len(SOLVE_PRESETS)} solve setting presets...', 'SETTINGS')

    for preset in SOLVE_PRESETS:
        restore_settings(settings, original)
        if original_tripod is not None:
            set_setting(settings, 'use_tripod_solver', original_tripod)

        try:
            apply_solve_preset(settings, preset)
            bpy.ops.clip.solve_camera()
        except Exception as exc:
            log_msg(scene, f"{preset['name']}: skipped ({exc})", 'ERROR')
            continue

        if not active_obj.reconstruction.is_valid:
            log_msg(scene, f"{preset['name']}: invalid solve", 'X')
            continue

        error = active_obj.reconstruction.average_error
        reconstructed = count_reconstructed_tracks(active_obj.tracks)
        score = solve_score(error, reconstructed, minimum_tracks)
        log_msg(scene, f"{preset['name']}: err {error:.3f}, bundles {reconstructed}", 'DOT')

        if best is None or score < best['score']:
            best = {
                'name': preset['name'],
                'score': score,
                'error': error,
                'reconstructed': reconstructed,
                'preset': preset,
            }
            best_snapshot = save_settings(settings, SOLVE_SETTING_ATTRS)

    if not best:
        restore_settings(settings, original)
        if original_tripod is not None:
            set_setting(settings, 'use_tripod_solver', original_tripod)
        log_msg(scene, 'No valid optimized solve preset found; using current settings', 'ERROR')
        return None

    restore_settings(settings, best_snapshot)
    if original_tripod is not None:
        set_setting(settings, 'use_tripod_solver', original_tripod)
    bpy.ops.clip.solve_camera()
    log_msg(scene, f"Best solve preset: {best['name']} ({best['error']:.3f}px)", 'CHECKMARK')
    return best

class CLIP_UL_autotrack_log(UIList):
    bl_idname = "CLIP_UL_autotrack_log"
    
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        icon_name = item.icon if item.icon else 'INFO'
        try:
            layout.label(text=item.message, icon=icon_name)
        except:
            layout.label(text=item.message, icon='INFO')

class CLIP_UL_autotrack_results(UIList):
    bl_idname = "CLIP_UL_autotrack_results"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=f"{index + 1}: {item.error:.3f}px", icon='CHECKMARK')
        row.label(text=f"{item.bundles} bundles")
        row.label(text=item.scenario)

def add_result_item(scene, result):
    item = scene.autotrack_results.add()
    item.scenario = f"{result['scenario']['name']} / {result['solve_name']}"
    item.solve = result['solve_name']
    item.error = result['error']
    item.bundles = result['bundles']
    item.score = result['score']
    scene.autotrack_results_index = len(scene.autotrack_results) - 1

class CLIP_OT_autotrack_clear_log(Operator):
    bl_idname = 'autotrack.clear_log'
    bl_label = 'Clear Log'
    bl_description = 'Clear the status log'
    
    def execute(self, context):
        context.scene.autotrack_log.clear()
        return {'FINISHED'}


# -------------------------------------------------------------------
#  OPERATOR: MAIN AUTO TRACKER (MODAL - TIMER BASED)
# -------------------------------------------------------------------

class CLIP_OT_autotrack_autotrack(Operator):
    bl_idname = 'autotrack.auto_track'
    bl_label = 'Auto Track'
    bl_description = 'Automatically use Detect Features and filtering to motion track the timeline forward'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING', 'PRESET'}

    _frame_redetect = -1 
    _timer = None

    @classmethod
    def poll(cls, context):
        return (
            context.area is not None and
            context.area.spaces.active is not None and
            context.area.spaces.active.clip is not None
        )

    def execute(self, context):
        scene = context.scene
        clip = context.area.spaces.active.clip
        tracks = clip.tracking.tracks
        settings = clip.tracking.settings
        current_frame = scene.frame_current
        frame_end = scene.frame_end

        if current_frame < scene.frame_start:
            current_frame = set_tracking_frame(context, scene.frame_start)
        elif current_frame > frame_end:
            log_msg(scene, 'Current frame is past the render end frame', 'ERROR')
            self.cancel(context)
            return {'CANCELLED'}
        
        MIN_TIME = scene.autotrack_filter_mintime
        
        tracks_to_delete = []
        tracks_to_stop = []

        # 1. ANALYZE
        for track in tracks:
            if track.hide or track.lock: continue
            
            # Find markers (Current OR Previous frame to be robust)
            marker = get_nearby_marker(track, current_frame)
            prev_marker = get_nearby_marker(track, current_frame - scene.autotrack_rate)

            # A. Time Cleanup
            valid_duration = count_recent_valid_markers(track, current_frame - scene.autotrack_rate, current_frame)
            if prev_marker and valid_duration < MIN_TIME:
                tracks_to_delete.append(track)
                continue

            # B. Quality Cleanup (Muted = Slipped)
            if marker and marker.mute:
                if valid_duration >= MIN_TIME:
                    tracks_to_stop.append(track)
                else:
                    tracks_to_delete.append(track)

        # 2. ACT
        if tracks_to_delete:
            log_msg(scene, f'Deleted {len(tracks_to_delete)} garbage tracks', 'TRASH')
            delete_tracks(tracks_to_delete)
        
        for track in tracks_to_stop:
            set_track_selected(track, False)
            track.lock = True
        if tracks_to_stop:
            log_msg(scene, f'Stopped {len(tracks_to_stop)} slipping tracks', 'PAUSE')

        # 3. DETECT
        existing_track_keys = {track_key(track) for track in tracks}
        active_before_detect = count_trackable_tracks(tracks, current_frame)
        if scene.autotrack_tracking_strategy != 'MANUAL':
            apply_tracking_strategy(settings, resolve_tracking_strategy(scene))

        bpy.ops.clip.select_all(action='DESELECT')
        bpy.ops.clip.detect_features(
            threshold=scene.autotrack_detect_threshold,
            min_distance=scene.autotrack_detect_distance,
            margin=scene.autotrack_detect_margin,
            placement=scene.autotrack_detect_placement
        )

        new_trackers = [t for t in tracks if track_key(t) not in existing_track_keys]

        if not new_trackers and active_before_detect < scene.autotrack_min_tracks:
            loose_threshold = max(0.001, scene.autotrack_detect_threshold * 0.5)
            loose_distance = max(5, int(scene.autotrack_detect_distance * 0.75))
            if scene.autotrack_tracking_strategy == 'AUTO':
                next_strategy = escalate_tracking_strategy(scene, settings)
                log_msg(scene, f'Few active tracks; retrying as {TRACKING_STRATEGY_PRESETS[next_strategy]["name"]}', 'VIEWZOOM')
            else:
                log_msg(scene, 'Few active tracks; retrying detection with looser settings', 'VIEWZOOM')
            bpy.ops.clip.select_all(action='DESELECT')
            bpy.ops.clip.detect_features(
                threshold=loose_threshold,
                min_distance=loose_distance,
                margin=scene.autotrack_detect_margin,
                placement=scene.autotrack_detect_placement
            )
            new_trackers = [t for t in tracks if track_key(t) not in existing_track_keys]

        if len(new_trackers) > scene.autotrack_max_new_tracks:
            overflow = new_trackers[scene.autotrack_max_new_tracks:]
            delete_tracks(overflow)
            new_trackers = new_trackers[:scene.autotrack_max_new_tracks]
            log_msg(scene, f'Capped new tracks to {scene.autotrack_max_new_tracks}', 'FILTER')

        # 4. OVERLAP
        old_trackers = []
        for track in tracks:
            if track not in new_trackers and not track.hide and not track.lock:
                # Check presence on current OR prev frame
                marker = get_nearby_marker(track, current_frame)
                
                if marker: old_trackers.append(track)

        trackers_to_remove_overlap = []
        diaglen = math.sqrt(clip.size[0]**2 + clip.size[1]**2)
        
        for new_track in new_trackers:
            new_marker = new_track.markers.find_frame(current_frame, exact=True)
            if new_marker:
                for old_track in old_trackers:
                    old_marker = old_track.markers.find_frame(current_frame, exact=True)
                    if not old_marker:
                        old_marker = get_nearby_marker(old_track, current_frame)

                    if old_marker:
                        distance = (new_marker.co - old_marker.co).length * diaglen
                        if distance < scene.autotrack_detect_distance:
                            trackers_to_remove_overlap.append(new_track)
                            break 

        if trackers_to_remove_overlap:
            delete_tracks(trackers_to_remove_overlap)
            log_msg(scene, f'Removed {len(trackers_to_remove_overlap)} overlapping new tracks', 'X')

        # 5. TRACK
        context.area.spaces.active.show_disabled = False
        
        count_tracking = 0
        for track in tracks:
            if track.hide or track.lock: continue
            if track in tracks_to_stop: continue 
            
            # Select if it has a marker on current OR previous frame (ready to track)
            marker = get_nearby_marker(track, current_frame)
            
            if marker and not marker.mute:
                set_track_selected(track, True)
                
                # FIX: Add +5 frames buffer to the limit.
                # This ensures the tracker runs PAST the target frame, so the script
                # definitely wakes up. If we set it exactly to 'rate', it often stops
                # 1 frame short, causing a deadlock.
                frames_remaining = max(1, frame_end - current_frame)
                track.frames_limit = min(scene.autotrack_rate + 5, frames_remaining)
                
                count_tracking += 1

        if count_tracking == 0:
            log_msg(scene, 'No valid tracks available to continue', 'ERROR')
            self.cancel(context)
            return {'CANCELLED'}

        self._frame_redetect = min(current_frame + scene.autotrack_rate, frame_end)
        log_msg(scene, f'Tracking {count_tracking} feats to frame {self._frame_redetect}...', 'PLAY')
        
        bpy.ops.clip.track_markers('INVOKE_DEFAULT', backwards=False, sequence=True)
        context.area.tag_redraw()
        return {'FINISHED'}

    def modal(self, context, event):
        if event.type in {'ESC'}:
            log_msg(context.scene, 'Auto Track Cancelled', 'CANCEL')
            self.cancel(context)
            context.scene.autotrack_auto_solve_after_track = False
            return {'CANCELLED'}

        if event.type == 'TIMER':
            if context.scene.frame_current >= context.scene.frame_end:
                log_msg(context.scene, 'End of clip reached', 'CHECKMARK')
                run_solve = context.scene.autotrack_auto_solve_after_track
                self.cancel(context)
                context.scene.autotrack_auto_solve_after_track = False
                if run_solve:
                    log_msg(context.scene, 'Starting Auto Solve & Clean...', 'TRIA_RIGHT')
                    bpy.ops.autotrack.auto_solve('INVOKE_DEFAULT')
                return {'FINISHED'}
            
            # Check if we reached the target frame (Allow 1 frame tolerance)
            if self._frame_redetect == -1 or context.scene.frame_current >= (self._frame_redetect - 1):
                self.execute(context)
            
            return {'PASS_THROUGH'}
        
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        ensure_console_open()
        scene = context.scene
        set_tracking_frame(context, scene.frame_start)

        clip = context.area.spaces.active.clip
        if scene.autotrack_tracking_strategy == 'AUTO':
            scene.autotrack_active_tracking_strategy = 'BALANCED'
        elif scene.autotrack_tracking_strategy != 'MANUAL':
            scene.autotrack_active_tracking_strategy = scene.autotrack_tracking_strategy

        if scene.autotrack_tracking_strategy != 'MANUAL':
            strategy = resolve_tracking_strategy(scene)
            if apply_tracking_strategy(clip.tracking.settings, strategy):
                log_msg(scene, f'Using {TRACKING_STRATEGY_PRESETS[strategy]["name"]} tracking strategy', 'SETTINGS')

        wm = context.window_manager
        wm.modal_handler_add(self)
        self._timer = wm.event_timer_add(time_step=0.5, window=context.window)
        self._frame_redetect = -1
        log_msg(scene, f"Starting Auto Track over frames {scene.frame_start}-{scene.frame_end}...", "CON_FOLLOWTRACK")
        self.execute(context)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        wm = context.window_manager
        if hasattr(self, '_timer'): wm.event_timer_remove(self._timer)
        
        # Reset frames_limit so manual tracking works normally again
        if context.area and context.area.spaces.active and context.area.spaces.active.clip:
            for track in context.area.spaces.active.clip.tracking.tracks:
                track.frames_limit = 0
        context.area.tag_redraw()


# -------------------------------------------------------------------
#  OPERATOR: ONE-CLICK TRACK THEN SOLVE
# -------------------------------------------------------------------

class CLIP_OT_autotrack_track_and_solve(Operator):
    bl_idname = 'autotrack.track_and_solve'
    bl_label = 'Auto Track + Solve'
    bl_description = 'Track to the end of the clip, then run Auto Solve & Clean'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return CLIP_OT_autotrack_autotrack.poll(context)

    def execute(self, context):
        context.scene.autotrack_auto_solve_after_track = True
        return bpy.ops.autotrack.auto_track('INVOKE_DEFAULT')


# -------------------------------------------------------------------
#  OPERATOR: HEAVY TRACKING/SOLVE SCENARIO OPTIMIZER
# -------------------------------------------------------------------

class CLIP_OT_autotrack_optimize_track_and_solve(Operator):
    bl_idname = 'autotrack.optimize_track_and_solve'
    bl_label = 'Optimize Track + Solve'
    bl_description = 'Try multiple tracking scenarios and solve presets, then keep the best trial clip active'
    bl_options = {'REGISTER', 'UNDO'}

    _timer = None
    _state = 'IDLE'
    _scenarios = None
    _scenario_index = 0
    _original_clip = None
    _trial_clips = None
    _best = None
    _current_clip = None
    _current_scenario = None
    _solve_index = 0
    _solve_original = None
    _solve_original_tripod = None
    _solve_minimum_tracks = 8
    _scenario_best = None
    _scenario_best_snapshot = None

    @classmethod
    def poll(cls, context):
        return CLIP_OT_autotrack_autotrack.poll(context)

    def execute(self, context):
        return self.invoke(context, None)

    def invoke(self, context, event):
        ensure_console_open()
        scene = context.scene
        space = context.area.spaces.active
        self._original_clip = space.clip

        if not self._original_clip:
            self.report({'ERROR'}, "No clip loaded")
            return {'CANCELLED'}

        self._scenarios = build_tracking_scenarios(scene.autotrack_tracking_iterations)
        if not self._scenarios:
            self.report({'ERROR'}, "No tracking scenarios generated")
            return {'CANCELLED'}

        scene.autotrack_results.clear()
        self._trial_clips = []
        self._best = None
        self._scenario_index = 0
        self._state = 'SCENARIO'
        log_msg(scene, f"Starting heavy optimizer: {len(self._scenarios)} tracking scenarios x {len(SOLVE_PRESETS)} solve presets", 'SETTINGS')

        wm = context.window_manager
        self._timer = wm.event_timer_add(time_step=0.1, window=context.window)
        wm.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        scene = context.scene

        if event.type == 'ESC':
            log_msg(scene, "Optimize Track + Solve cancelled", "CANCEL")
            self.cancel(context)
            return {'CANCELLED'}

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if self._state == 'SCENARIO':
            return self.step_scenario(context)

        if self._state == 'SOLVE':
            return self.step_solve(context)

        if self._state == 'FINISH':
            return self.finish_optimizer(context)

        return {'PASS_THROUGH'}

    def step_scenario(self, context):
        scene = context.scene
        space = context.area.spaces.active

        if self._scenario_index >= len(self._scenarios):
            self._state = 'FINISH'
            return {'PASS_THROUGH'}

        scenario = self._scenarios[self._scenario_index]
        self._scenario_index += 1

        trial_clip = self._original_clip.copy()
        trial_clip.name = f"{self._original_clip.name}_AutoTrackTrial_{scenario['index']:02d}"
        self._trial_clips.append(trial_clip)
        self._current_clip = trial_clip
        self._current_scenario = scenario
        space.clip = trial_clip
        set_tracking_frame(context, scene.frame_start)

        log_msg(scene, f"Scenario {scenario['index']}/{len(self._scenarios)}: {tracking_scenario_summary(scenario)}", 'PLAY')

        try:
            clear_tracking_tracks_in_context(context)
            tracked = self.run_tracking_trial(context, scene, trial_clip, scenario)
        except Exception as exc:
            log_msg(scene, f"Scenario {scenario['index']} failed: {exc}", 'ERROR')
            return {'PASS_THROUGH'}

        if not tracked:
            return {'PASS_THROUGH'}

        self.start_solve_trial(scene, trial_clip)
        self._state = 'SOLVE'
        return {'PASS_THROUGH'}

    def start_solve_trial(self, scene, clip):
        settings = clip.tracking.settings
        self._solve_index = 0
        self._solve_original = save_settings(settings, SOLVE_SETTING_ATTRS)
        self._solve_original_tripod = get_setting(settings, 'use_tripod_solver')
        self._solve_minimum_tracks = max(8, min(scene.autotrack_min_tracks, 40))
        self._scenario_best = None
        self._scenario_best_snapshot = None

    def step_solve(self, context):
        scene = context.scene
        clip = self._current_clip
        scenario = self._current_scenario
        active_obj = clip.tracking.objects.active
        settings = clip.tracking.settings

        if not active_obj:
            log_msg(scene, f"Scenario {scenario['index']}: no active tracking object", 'ERROR')
            self._state = 'SCENARIO'
            return {'PASS_THROUGH'}

        if self._solve_index >= len(SOLVE_PRESETS):
            return self.finish_scenario_solve(context)

        preset = SOLVE_PRESETS[self._solve_index]
        self._solve_index += 1

        restore_settings(settings, self._solve_original or {})
        if self._solve_original_tripod is not None:
            set_setting(settings, 'use_tripod_solver', self._solve_original_tripod)

        log_msg(scene, f"Scenario {scenario['index']}: solve {self._solve_index}/{len(SOLVE_PRESETS)} {preset['name']}", 'SETTINGS')
        try:
            apply_solve_preset(settings, preset)
            call_clip_op(context, bpy.ops.clip.solve_camera)
        except Exception as exc:
            log_msg(scene, f"Scenario {scenario['index']} solve skipped: {exc}", 'ERROR')
            return {'PASS_THROUGH'}

        if not active_obj.reconstruction.is_valid:
            log_msg(scene, f"Scenario {scenario['index']} solve invalid: {preset['name']}", 'X')
            return {'PASS_THROUGH'}

        error = active_obj.reconstruction.average_error
        bundles = count_reconstructed_tracks(active_obj.tracks)
        score = solve_score(error, bundles, self._solve_minimum_tracks)
        log_msg(scene, f"Scenario {scenario['index']} solve result: {preset['name']} err {error:.3f}, bundles {bundles}", 'DOT')

        if self._scenario_best is None or score < self._scenario_best['score']:
            self._scenario_best = {
                'scenario': scenario,
                'solve_name': preset['name'],
                'score': score,
                'error': error,
                'bundles': bundles,
            }
            self._scenario_best_snapshot = save_settings(settings, SOLVE_SETTING_ATTRS)
            log_msg(scene, f"Scenario {scenario['index']}: new best solve preset", 'CHECKMARK')

        return {'PASS_THROUGH'}

    def finish_scenario_solve(self, context):
        scene = context.scene
        clip = self._current_clip
        scenario = self._current_scenario
        settings = clip.tracking.settings

        if not self._scenario_best:
            restore_settings(settings, self._solve_original or {})
            if self._solve_original_tripod is not None:
                set_setting(settings, 'use_tripod_solver', self._solve_original_tripod)
            log_msg(scene, f"Scenario {scenario['index']}: no valid solve", 'X')
            self._state = 'SCENARIO'
            return {'PASS_THROUGH'}

        restore_settings(settings, self._scenario_best_snapshot or {})
        if self._solve_original_tripod is not None:
            set_setting(settings, 'use_tripod_solver', self._solve_original_tripod)
        call_clip_op(context, bpy.ops.clip.solve_camera)

        scenario_best = self._scenario_best
        scenario_best['clip'] = clip
        log_msg(
            scene,
            f"Scenario {scenario['index']} best: {scenario_best['solve_name']} err {scenario_best['error']:.3f}, bundles {scenario_best['bundles']}",
            'DOT'
        )
        add_result_item(scene, scenario_best)

        if self._best is None or scenario_best['score'] < self._best['score']:
            self._best = scenario_best
            log_msg(scene, f"Scenario {scenario['index']} is new overall best", 'CHECKMARK')

        self._state = 'SCENARIO'
        return {'PASS_THROUGH'}

    def finish_optimizer(self, context):
        scene = context.scene
        space = context.area.spaces.active

        if not self._best:
            space.clip = self._original_clip
            log_msg(scene, "No valid optimized tracking result found", 'ERROR')
            self.report({'ERROR'}, "No valid optimized tracking result found")
            self.cancel(context)
            return {'CANCELLED'}

        space.clip = self._best['clip']
        scene.autotrack_best_clip_name = self._best['clip'].name
        set_tracking_frame(context, scene.frame_start)
        log_msg(
            scene,
            f"Winner: scenario {self._best['scenario']['index']} ({self._best['scenario']['name']}), solve {self._best['solve_name']}, error {self._best['error']:.3f}",
            'CHECKMARK'
        )

        if not scene.autotrack_keep_trial_clips:
            for clip in self._trial_clips:
                if clip != self._best['clip'] and clip.users == 0:
                    bpy.data.movieclips.remove(clip)

        self.cancel(context)
        return {'FINISHED'}

    def cancel(self, context):
        wm = context.window_manager
        if self._timer:
            wm.event_timer_remove(self._timer)
            self._timer = None
        if context.area:
            context.area.tag_redraw()

    def run_tracking_trial(self, context, scene, clip, scenario):
        settings = clip.tracking.settings
        apply_tracking_scenario(settings, scenario)

        frame = scene.frame_start
        frame_end = scene.frame_end
        max_cycles = max(1, math.ceil((frame_end - frame) / max(1, scene.autotrack_rate)) + 2)
        total_tracking_steps = 0

        for cycle in range(max_cycles):
            frame = set_clip_frame(context, min(frame, frame_end))
            if frame >= frame_end:
                break

            tracks = clip.tracking.tracks
            active_before = count_trackable_tracks(tracks, frame)
            retries = ((1.0, 1.0), (0.5, 0.7), (0.25, 0.45), (0.1, 0.25))
            new_trackers = []
            threshold = scene.autotrack_detect_threshold
            distance = scene.autotrack_detect_distance

            if active_before < scene.autotrack_min_tracks:
                for threshold_scale, distance_scale in retries:
                    new_trackers, threshold, distance = detect_trial_features(
                        context,
                        scene,
                        clip,
                        scenario,
                        threshold_scale=threshold_scale,
                        distance_scale=distance_scale
                    )
                    log_msg(
                        scene,
                        f"Scenario {scenario['index']}: detected {len(new_trackers)} features at frame {frame} (threshold {threshold:.4f}, distance {distance})",
                        'FILTER'
                    )
                    if new_trackers:
                        break

            call_clip_op(context, bpy.ops.clip.select_all, action='DESELECT')
            count_tracking = 0
            for track in tracks:
                if track.hide or track.lock:
                    continue

                marker = get_nearby_marker(track, frame)
                if marker and not marker.mute:
                    set_track_selected(track, True)
                    track.frames_limit = min(scene.autotrack_rate, max(1, frame_end - frame))
                    count_tracking += 1

            if count_tracking == 0:
                log_msg(scene, f"Scenario {scenario['index']}: no tracks at frame {frame}", 'ERROR')
                break

            target = min(frame + scene.autotrack_rate, frame_end)
            log_msg(scene, f"Scenario {scenario['index']}: tracking {count_tracking} tracks {frame}->{target}", 'PLAY')
            call_clip_op(context, bpy.ops.clip.track_markers, backwards=False, sequence=True)
            total_tracking_steps += 1

            next_frame = scene.frame_current
            if next_frame <= frame:
                next_frame = target
            frame = next_frame

        tracked_markers = sum(1 for track in clip.tracking.tracks if len(track.markers) > 1)
        if total_tracking_steps == 0 or tracked_markers < 8:
            log_msg(scene, f"Scenario {scenario['index']}: skipped solve, only {tracked_markers} tracks have multiple markers", 'ERROR')
            return False

        return True

    def solve_trial(self, context, scene, clip, scenario):
        active_obj = clip.tracking.objects.active
        if not active_obj:
            return None

        settings = clip.tracking.settings
        original = save_settings(settings, SOLVE_SETTING_ATTRS)
        original_tripod = get_setting(settings, 'use_tripod_solver')
        minimum_tracks = max(8, min(scene.autotrack_min_tracks, 40))
        best = None
        best_snapshot = None

        for solve_index, preset in enumerate(SOLVE_PRESETS, start=1):
            restore_settings(settings, original)
            if original_tripod is not None:
                set_setting(settings, 'use_tripod_solver', original_tripod)

            log_msg(scene, f"Scenario {scenario['index']}: solve {solve_index}/{len(SOLVE_PRESETS)} {preset['name']}", 'SETTINGS')
            try:
                apply_solve_preset(settings, preset)
                call_clip_op(context, bpy.ops.clip.solve_camera)
            except Exception as exc:
                log_msg(scene, f"Scenario {scenario['index']} solve skipped: {exc}", 'ERROR')
                continue

            if not active_obj.reconstruction.is_valid:
                continue

            error = active_obj.reconstruction.average_error
            bundles = count_reconstructed_tracks(active_obj.tracks)
            score = solve_score(error, bundles, minimum_tracks)

            if best is None or score < best['score']:
                best = {
                    'scenario': scenario,
                    'solve_name': preset['name'],
                    'score': score,
                    'error': error,
                    'bundles': bundles,
                }
                best_snapshot = save_settings(settings, SOLVE_SETTING_ATTRS)

        if not best:
            restore_settings(settings, original)
            if original_tripod is not None:
                set_setting(settings, 'use_tripod_solver', original_tripod)
            return None

        restore_settings(settings, best_snapshot)
        if original_tripod is not None:
            set_setting(settings, 'use_tripod_solver', original_tripod)
        call_clip_op(context, bpy.ops.clip.solve_camera)
        return best


class CLIP_OT_autotrack_restore_best(Operator):
    bl_idname = 'autotrack.restore_best'
    bl_label = 'Restore Best Result'
    bl_description = 'Switch the Movie Clip Editor back to the best optimized tracking result'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (
            context.area is not None and
            context.area.spaces.active is not None and
            context.area.spaces.active.type == 'CLIP_EDITOR'
        )

    def execute(self, context):
        scene = context.scene
        clip_name = scene.autotrack_best_clip_name
        if not clip_name:
            self.report({'ERROR'}, "No best result has been stored yet")
            log_msg(scene, "No best result has been stored yet", "ERROR")
            return {'CANCELLED'}

        clip = bpy.data.movieclips.get(clip_name)
        if not clip:
            self.report({'ERROR'}, f"Best result clip not found: {clip_name}")
            log_msg(scene, f"Best result clip not found: {clip_name}", "ERROR")
            return {'CANCELLED'}

        context.area.spaces.active.clip = clip
        set_tracking_frame(context, scene.frame_start)
        log_msg(scene, f"Restored best result: {clip.name}", "CHECKMARK")
        return {'FINISHED'}


# -------------------------------------------------------------------
#  OPERATOR: AUTO SOLVE & CLEAN (MODAL)
# -------------------------------------------------------------------

class CLIP_OT_autotrack_autosolve(Operator):
    bl_idname = 'autotrack.auto_solve'
    bl_label = 'Auto Solve & Clean'
    bl_description = 'Iteratively solves and removes worst/failed trackers'
    bl_options = {'REGISTER', 'UNDO'}

    # Internal state variables
    _timer = None
    _iteration = 0
    _max_iterations = 20
    _target_error = 0.3
    _best_error = 9999.9
    _tracks_disabled_count = 0
    _candidates_to_prune = [] 
    _history = {} 
    _state = 'IDLE' 
    _optimized_solve_settings = False
    _solve_opt_index = 0
    _solve_opt_original = None
    _solve_opt_original_tripod = None
    _solve_opt_minimum_tracks = 8
    _solve_opt_best = None
    _solve_opt_best_snapshot = None

    def start_solve_optimization(self, scene, clip):
        settings = clip.tracking.settings
        self._solve_opt_index = 0
        self._solve_opt_original = save_settings(settings, SOLVE_SETTING_ATTRS)
        self._solve_opt_original_tripod = get_setting(settings, 'use_tripod_solver')
        self._solve_opt_minimum_tracks = max(8, min(scene.autotrack_min_tracks, 40))
        self._solve_opt_best = None
        self._solve_opt_best_snapshot = None
        self._optimized_solve_settings = True
        log_msg(scene, f'Trying {len(SOLVE_PRESETS)} solve setting presets...', 'SETTINGS')

    def step_solve_optimization(self, context, scene, clip, active_obj):
        settings = clip.tracking.settings

        if self._solve_opt_index >= len(SOLVE_PRESETS):
            if not self._solve_opt_best:
                restore_settings(settings, self._solve_opt_original or {})
                if self._solve_opt_original_tripod is not None:
                    set_setting(settings, 'use_tripod_solver', self._solve_opt_original_tripod)
                log_msg(scene, 'No valid optimized solve preset found; using current settings', 'ERROR')
            else:
                restore_settings(settings, self._solve_opt_best_snapshot or {})
                if self._solve_opt_original_tripod is not None:
                    set_setting(settings, 'use_tripod_solver', self._solve_opt_original_tripod)
                best = self._solve_opt_best
                log_msg(scene, f"Best solve preset: {best['name']} ({best['error']:.3f}px)", 'CHECKMARK')

            self._state = 'SOLVING'
            context.area.tag_redraw()
            return

        preset = SOLVE_PRESETS[self._solve_opt_index]
        self._solve_opt_index += 1

        restore_settings(settings, self._solve_opt_original or {})
        if self._solve_opt_original_tripod is not None:
            set_setting(settings, 'use_tripod_solver', self._solve_opt_original_tripod)

        log_msg(scene, f"Solve preset {self._solve_opt_index}/{len(SOLVE_PRESETS)}: {preset['name']}", 'SETTINGS')
        try:
            apply_solve_preset(settings, preset)
            bpy.ops.clip.solve_camera()
        except Exception as exc:
            log_msg(scene, f"{preset['name']}: skipped ({exc})", 'ERROR')
            context.area.tag_redraw()
            return

        if not active_obj.reconstruction.is_valid:
            log_msg(scene, f"{preset['name']}: invalid solve", 'X')
            context.area.tag_redraw()
            return

        error = active_obj.reconstruction.average_error
        reconstructed = count_reconstructed_tracks(active_obj.tracks)
        score = solve_score(error, reconstructed, self._solve_opt_minimum_tracks)
        log_msg(scene, f"{preset['name']}: err {error:.3f}, bundles {reconstructed}", 'DOT')

        if self._solve_opt_best is None or score < self._solve_opt_best['score']:
            self._solve_opt_best = {
                'name': preset['name'],
                'score': score,
                'error': error,
                'reconstructed': reconstructed,
            }
            self._solve_opt_best_snapshot = save_settings(settings, SOLVE_SETTING_ATTRS)
            log_msg(scene, f"{preset['name']}: new best", 'CHECKMARK')

        context.area.tag_redraw()

    def execute(self, context):
        return self.invoke(context, None)

    def modal(self, context, event):
        scene = context.scene
        clip = context.area.spaces.active.clip
        active_obj = clip.tracking.objects.active

        if event.type == 'ESC':
            log_msg(scene, "Auto Solve Cancelled by User", "CANCEL")
            self.cancel(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            # STATE: OPTIMIZING SOLVE SETTINGS
            if self._state == 'OPTIMIZING':
                self.step_solve_optimization(context, scene, clip, active_obj)
                return {'PASS_THROUGH'}

            # STATE: SOLVING
            if self._state == 'SOLVING':
                log_msg(scene, f"Solve iteration {self._iteration + 1}/{self._max_iterations}", "INFO")
                bpy.ops.clip.solve_camera()
                
                if not active_obj.reconstruction.is_valid:
                    log_msg(scene, "Solve Failed (Not enough tracks?)", "ERROR")
                    self.cancel(context)
                    return {'CANCELLED'}
                
                new_error = active_obj.reconstruction.average_error

                if self._iteration == 0:
                    self._best_error = new_error
                    log_msg(scene, f"Initial Error: {self._best_error:.4f}", "INFO")
                    self._state = 'PRUNING'
                
                elif self._state_from_prune:
                    if new_error < self._best_error:
                        log_msg(scene, f"Iter {self._iteration}: -{len(self._candidates_to_prune)} tracks. Err: {self._best_error:.3f}->{new_error:.3f}", "DOT")
                        self._best_error = new_error
                        self._tracks_disabled_count += len(self._candidates_to_prune)
                        self._state = 'PRUNING' 
                    else:
                        log_msg(scene, f"Iter {self._iteration}: Worse ({new_error:.3f}). Reverting.", "X")
                        for track, old_weight in self._history.items():
                            track.weight = old_weight
                        bpy.ops.clip.solve_camera()
                        self.finish(context)
                        return {'FINISHED'}

                self._state_from_prune = False
                
                if self._best_error <= self._target_error:
                    log_msg(scene, f"Target error {self._target_error} reached.", "CHECKMARK")
                    self.finish(context)
                    return {'FINISHED'}
                
                context.area.tag_redraw()

            # STATE: PRUNING
            elif self._state == 'PRUNING':
                self._iteration += 1
                if self._iteration > self._max_iterations:
                    self.finish(context)
                    return {'FINISHED'}

                tracks = active_obj.tracks
                delete_failed = scene.autotrack_solve_delete_failed
                delete_count = scene.autotrack_solve_delete_count
                
                self._candidates_to_prune = []
                
                # 1. Failed
                if delete_failed:
                    for track in tracks:
                        if track.weight > 0.0 and not track.hide and not track.has_bundle:
                            self._candidates_to_prune.append(track)

                # 2. Worst
                valid_tracks = [t for t in tracks if t.weight > 0.0 and not t.hide and t.has_bundle]
                valid_tracks.sort(key=lambda t: t.average_error, reverse=True)
                
                worst_n = valid_tracks[:delete_count]
                for t in worst_n:
                    if t not in self._candidates_to_prune:
                        self._candidates_to_prune.append(t)

                if not self._candidates_to_prune:
                    log_msg(scene, "No more tracks to prune.", "CHECKMARK")
                    self.finish(context)
                    return {'FINISHED'}

                self._history = {track: track.weight for track in self._candidates_to_prune}
                for track in self._candidates_to_prune:
                    track.weight = 0.0
                
                self._state = 'SOLVING'
                self._state_from_prune = True
                context.area.tag_redraw()

        return {'PASS_THROUGH'}

    def invoke(self, context, event):
        ensure_console_open()
        scene = context.scene
        clip = context.area.spaces.active.clip
        if not clip or not clip.tracking.objects.active:
            self.report({'ERROR'}, "No active tracking object")
            return {'CANCELLED'}

        track_count = len(clip.tracking.objects.active.tracks)
        if track_count > scene.autotrack_solve_max_input_tracks:
            message = (
                f"Too many tracks to auto-solve safely ({track_count}). "
                f"Clean tracks or raise Max Solve Tracks."
            )
            log_msg(scene, message, "ERROR")
            self.report({'ERROR'}, message)
            return {'CANCELLED'}

        log_msg(scene, "--- Starting Auto Solve ---", "TRIA_RIGHT")
        
        self._iteration = 0
        self._tracks_disabled_count = 0
        self._max_iterations = scene.autotrack_solve_max_iterations
        self._target_error = scene.autotrack_solve_target_error
        self._state_from_prune = False
        self._optimized_solve_settings = False
        if scene.autotrack_solve_optimize_settings:
            self._state = 'OPTIMIZING'
            self.start_solve_optimization(scene, clip)
        else:
            self._state = 'SOLVING'
        
        wm = context.window_manager
        self._timer = wm.event_timer_add(time_step=0.1, window=context.window)
        wm.modal_handler_add(self)
        
        return {'RUNNING_MODAL'}

    def finish(self, context):
        scene = context.scene
        clip = context.area.spaces.active.clip
        active_obj = clip.tracking.objects.active
        tracks = active_obj.tracks if active_obj else clip.tracking.tracks
        if scene.autotrack_tracking_strategy != 'MANUAL':
            strategy = resolve_tracking_strategy(scene)
            preset = TRACKING_STRATEGY_PRESETS.get(strategy)
            if preset:
                if self._best_error <= self._target_error:
                    log_msg(scene, f"Tracking strategy result: {preset['name']} reached target", "CHECKMARK")
                elif scene.autotrack_tracking_strategy == 'AUTO' and strategy != TRACKING_STRATEGY_ORDER[-1]:
                    next_index = TRACKING_STRATEGY_ORDER.index(strategy) + 1
                    next_name = TRACKING_STRATEGY_PRESETS[TRACKING_STRATEGY_ORDER[next_index]]['name']
                    log_msg(scene, f"Target not reached; next stronger strategy would be {next_name}", "INFO")
                else:
                    log_msg(scene, f"Tracking strategy result: {preset['name']} was best attempted", "INFO")
        
        if self._tracks_disabled_count > 0:
            bpy.ops.clip.select_all(action='DESELECT')
            for track in tracks:
                if track.weight == 0.0:
                    set_track_selected(track, True)
            
            bpy.ops.clip.delete_track()
            log_msg(scene, f"Finished. Deleted {self._tracks_disabled_count} tracks.", "TRASH")
        else:
            log_msg(scene, "Finished. No tracks deleted.", "CHECKMARK")
            
        self.cancel(context)

    def cancel(self, context):
        if self._state == 'OPTIMIZING' and self._solve_opt_original is not None:
            clip = context.area.spaces.active.clip if context.area and context.area.spaces.active else None
            if clip:
                restore_settings(clip.tracking.settings, self._solve_opt_original)
                if self._solve_opt_original_tripod is not None:
                    set_setting(clip.tracking.settings, 'use_tripod_solver', self._solve_opt_original_tripod)

        wm = context.window_manager
        if self._timer: wm.event_timer_remove(self._timer)
        context.area.tag_redraw()


# -------------------------------------------------------------------
#  OPERATOR: MANUAL FILTER
# -------------------------------------------------------------------

class CLIP_OT_autotrack_filter(Operator):
    bl_idname = 'autotrack.filter'
    bl_label = 'Filter All Tracks'
    bl_description = 'Select tracks based on the Error Threshold setting'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (context.area.spaces.active.clip is not None)

    def execute(self, context):
        scene = context.scene
        clip = context.area.spaces.active.clip
        tracks = clip.tracking.tracks
        
        bpy.ops.clip.filter_tracks(
            track_threshold=scene.autotrack_filter_threshold,
        )
        
        count = sum(1 for t in tracks if t.select)
        log_msg(scene, f'Selected {count} high-error tracks', 'FILTER')
        return {'FINISHED'}


# -------------------------------------------------------------------
#  PANELS
# -------------------------------------------------------------------

class CLIP_PT_autotrack_main(Panel):
    bl_label = 'Auto-track'
    bl_space_type = 'CLIP_EDITOR'
    bl_region_type = 'TOOLS'
    bl_category = 'Auto-track'

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        # --- STATISTICS SECTION ---
        clip = context.area.spaces.active.clip
        count_total = 0
        count_active = 0
        count_finished = 0
        solve_error = 0.0
        
        if clip:
            tracks = clip.tracking.tracks
            count_total = len(tracks)
            
            active_obj = clip.tracking.objects.active
            if active_obj and active_obj.reconstruction.is_valid:
                solve_error = active_obj.reconstruction.average_error

            for t in tracks:
                if t.hide: continue 
                if t.select: count_active += 1
                else: count_finished += 1

        box = layout.box()
        col = box.column(align=True)
        
        row = col.row()
        row.alignment = 'EXPAND'
        row.label(text=f"Total: {count_total}")
        if solve_error > 0:
            row.label(text=f"Error: {solve_error:.2f}px")
        
        row = col.row()
        row.alignment = 'EXPAND'
        row.label(text=f"Active: {count_active}", icon='DOT')
        row.label(text=f"Finished: {count_finished}", icon='SOLO_OFF')

        if scene.autotrack_status:
            box.label(text=scene.autotrack_status, icon='INFO')

        # --- LOG SECTION ---
        layout.separator()
        row = layout.row()
        row.label(text="Status Log:")
        row.operator("autotrack.clear_log", text="", icon="TRASH")
        
        if len(scene.autotrack_log) > 0:
            layout.template_list(
                "CLIP_UL_autotrack_log", "", 
                scene, "autotrack_log", 
                scene, "autotrack_log_index", 
                rows=5
            )
        else:
            box = layout.box()
            box.label(text="No logs yet...", icon="INFO")

        if len(scene.autotrack_results) > 0:
            layout.separator()
            layout.label(text="Optimization Results:")
            layout.template_list(
                "CLIP_UL_autotrack_results", "",
                scene, "autotrack_results",
                scene, "autotrack_results_index",
                rows=5
            )

        layout.separator()

        # --- TRACKING CONTROLS ---
        col = layout.column(align=True)
        col.scale_y = 1.5
        col.operator('autotrack.optimize_track_and_solve', text='Optimize Track + Solve', icon='MODIFIER')
        col.operator('autotrack.restore_best', text='Restore Best Result', icon='RECOVER_LAST')
        col.operator('autotrack.track_and_solve', text='Auto Track + Solve', icon='PLAY')
        col.operator('autotrack.auto_track', text='Start Auto Track', icon='CON_FOLLOWTRACK')
        
        col = layout.column(align=True)
        col.separator()
        col.label(text="Main Settings:")
        col.prop(scene, "autotrack_tracking_strategy", text="Tracking Strategy")
        col.prop(scene, "autotrack_tracking_iterations", text="Tracking Iterations")
        col.prop(scene, "autotrack_keep_trial_clips", text="Keep Trial Clips")
        col.prop(scene, "autotrack_rate")
        col.prop(scene, "autotrack_min_tracks")
        col.prop(scene, "autotrack_max_new_tracks")
        col.prop(scene, 'autotrack_filter_mintime', text="Min Duration")

        # --- SOLVING CONTROLS ---
        layout.separator()
        layout.label(text="Solving:")
        
        col = layout.column(align=True)
        col.prop(scene, "autotrack_solve_delete_failed", text="Del. Failed Reconstruct")
        col.prop(scene, "autotrack_solve_delete_count", text="Del. Worst (Batch)")
        col.prop(scene, "autotrack_solve_optimize_settings", text="Try Solve Settings")
        col.prop(scene, "autotrack_solve_target_error", text="Target Error")
        col.prop(scene, "autotrack_solve_max_iterations", text="Max Iterations")
        col.prop(scene, "autotrack_solve_max_input_tracks", text="Max Solve Tracks")
        
        col.separator()
        col.scale_y = 1.5
        col.operator('autotrack.auto_solve', text='Auto Solve & Clean', icon='TRIA_RIGHT')


class CLIP_PT_autotrack_tracker_settings(Panel):
    bl_label = 'Tracking Settings'
    bl_space_type = 'CLIP_EDITOR'
    bl_region_type = 'TOOLS'
    bl_category = 'Auto-track'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        sc = context.space_data
        clip = sc.clip
        settings = clip.tracking.settings

        col = layout.column(align=True)
        col.prop(settings, "default_pattern_size")
        col.prop(settings, "default_search_size")
        col.separator()
        col.prop(settings, "default_motion_model")
        col.prop(settings, "default_pattern_match", text="Match")
        col.prop(settings, "use_default_brute")
        col.prop(settings, "use_default_normalization")
        col = layout.column(align=True)
        col.prop(settings, "default_correlation_min")
        col.prop(settings, "default_margin")


class CLIP_PT_autotrack_detect_settings(Panel):
    bl_label = 'Feature Detection Settings'
    bl_space_type = 'CLIP_EDITOR'
    bl_region_type = 'TOOLS'
    bl_category = 'Auto-track'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        layout.use_property_split = True

        col = layout.column(align=True)
        col.prop(scene, 'autotrack_detect_margin')
        col.prop(scene, 'autotrack_detect_threshold')
        col.prop(scene, 'autotrack_detect_distance')
        col.prop(scene, 'autotrack_detect_placement')


class CLIP_PT_autotrack_filter_settings(Panel):
    bl_label = 'Manual Filter Tools'
    bl_space_type = 'CLIP_EDITOR'
    bl_region_type = 'TOOLS'
    bl_category = 'Auto-track'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        scene = context.scene
        layout = self.layout
        layout.use_property_split = True
        col = layout.column(align=True)
        col.scale_y = 1.5
        col.prop(scene, "autotrack_filter_threshold", text="Error Threshold") 
        col.operator('autotrack.filter', text='Select High Error', icon='FILTER')


# -------------------------------------------------------------------
#  REGISTRATION
# -------------------------------------------------------------------

classes = (
    AutoTrackLogItem,
    AutoTrackResultItem,
    CLIP_UL_autotrack_log,
    CLIP_UL_autotrack_results,
    CLIP_OT_autotrack_clear_log,
    CLIP_OT_autotrack_autotrack,
    CLIP_OT_autotrack_track_and_solve,
    CLIP_OT_autotrack_optimize_track_and_solve,
    CLIP_OT_autotrack_restore_best,
    CLIP_OT_autotrack_autosolve,
    CLIP_OT_autotrack_filter,
    CLIP_PT_autotrack_main,
    CLIP_PT_autotrack_tracker_settings,
    CLIP_PT_autotrack_detect_settings,
    CLIP_PT_autotrack_filter_settings
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Autotrack Properties
    Scene.autotrack_rate = IntProperty(
        name='Update Interval',
        description='How many frames to track before cleaning up and detecting new features',
        default=30,
        min=1
    )
    Scene.autotrack_min_tracks = IntProperty(
        name='Minimum Active Tracks',
        description='Retry feature detection with looser settings when active tracks fall below this count',
        default=20,
        min=0
    )
    Scene.autotrack_max_new_tracks = IntProperty(
        name='Max New Tracks',
        description='Maximum number of newly detected trackers to keep per cleanup cycle',
        default=80,
        min=1,
        max=1000
    )
    Scene.autotrack_auto_solve_after_track = BoolProperty(
        name='Auto Solve After Track',
        description='Internal flag used by Auto Track + Solve',
        default=False,
        options={'HIDDEN'}
    )
    Scene.autotrack_tracking_strategy = EnumProperty(
        name='Tracking Strategy',
        description='Tracking defaults to use while adding and tracking features',
        items=(
            ('AUTO', 'Auto', 'Start balanced and escalate when feature density gets too low'),
            ('MANUAL', 'Manual', 'Use the current Blender tracking settings without changing them'),
            ('BALANCED', 'Balanced', 'Location, rotation, and scale with medium tracking areas'),
            ('LOCATION', 'Location', 'Translation-only tracking for stable, clean footage'),
            ('AFFINE', 'Affine', 'Larger areas and affine model for harder motion'),
            ('PERSPECTIVE', 'Perspective', 'Largest areas and perspective model for strong deformation'),
        ),
        default='AUTO'
    )
    Scene.autotrack_tracking_iterations = IntProperty(
        name='Tracking Iterations',
        description='Number of tracking scenarios Optimize Track + Solve should test',
        default=8,
        min=1,
        max=64
    )
    Scene.autotrack_keep_trial_clips = BoolProperty(
        name='Keep Trial Clips',
        description='Keep non-winning trial clip copies after Optimize Track + Solve finishes',
        default=False
    )
    Scene.autotrack_best_clip_name = StringProperty(
        name='Best Result Clip',
        description='Movie clip datablock name of the best optimized tracking result',
        default='',
        options={'HIDDEN'}
    )
    Scene.autotrack_active_tracking_strategy = StringProperty(
        name='Active Tracking Strategy',
        description='Internal strategy used by Auto mode',
        default='BALANCED',
        options={'HIDDEN'}
    )

    # Feature Detection Properties
    Scene.autotrack_detect_margin = IntProperty(
        name='Margin',
        description='Distance from edge of image detected features must be',
        subtype='PIXEL',
        default=0,
        min=0
    )
    Scene.autotrack_detect_threshold = FloatProperty(
        name='Detect Threshold',
        description='Minimum threshold value for a feature to be considered',
        precision=3,
        default=0.1,
        min=0.001,
    )
    Scene.autotrack_detect_distance = IntProperty(
        name='Distance',
        description='Minimum distance detected features must be from each other',
        subtype='PIXEL',
        default=60,
        min=5
    )
    Scene.autotrack_detect_placement = EnumProperty(
        name='Allowed Placement',
        description='Allowed areas to detect new features',
        items=(
                ("FRAME", "Whole Frame", "The entire frame can be used for feature detection"),
                ("INSIDE_GPENCIL", "Inside Grease Pencil",
                    "Only areas inside the grease mask can be used for feature detection"),
                ("OUTSIDE_GPENCIL", "Outside Grease Pencil",
                    "Only areas outside the grease mask can be used for feature detection")
        ),
        default='FRAME'
    )

    # Filter Properties
    Scene.autotrack_filter_threshold = FloatProperty(
        name='Threshold',
        description='Max Reprojection Error allowed (used by Auto Solve & Manual Filter)',
        precision=3,
        default=5.0,
        min=0.0,
    )
    Scene.autotrack_filter_mintime = IntProperty(
        name='Minimum Track Time',
        description='Minimum amount of frames a tracker should have a valid track to be kept',
        default=15,
        min=0
    )
    
    # Auto Solve Properties
    Scene.autotrack_solve_delete_failed = BoolProperty(
        name='Delete Failed',
        description='Delete tracks that Blender failed to reconstruct 3D positions for',
        default=True
    )
    Scene.autotrack_solve_delete_count = IntProperty(
        name='Delete Count',
        description='Number of worst tracks to remove per iteration',
        default=1,
        min=1,
        max=50
    )
    Scene.autotrack_solve_optimize_settings = BoolProperty(
        name='Try Solve Settings',
        description='Try multiple camera solve refinement settings and keep the best valid result',
        default=True
    )
    Scene.autotrack_solve_target_error = FloatProperty(
        name='Target Error',
        description='Stop Auto Solve once the average solve error reaches this value',
        precision=3,
        default=0.3,
        min=0.0
    )
    Scene.autotrack_solve_max_iterations = IntProperty(
        name='Max Iterations',
        description='Maximum Auto Solve prune/solve attempts',
        default=20,
        min=1,
        max=200
    )
    Scene.autotrack_solve_max_input_tracks = IntProperty(
        name='Max Solve Tracks',
        description='Cancel Auto Solve before launching if the active object has more tracks than this',
        default=300,
        min=20,
        max=5000
    )
    
    # Log Properties
    Scene.autotrack_status = StringProperty(
        name='Status',
        description='Latest Auto-track status message',
        default=''
    )
    Scene.autotrack_log = CollectionProperty(type=AutoTrackLogItem)
    Scene.autotrack_log_index = IntProperty()
    Scene.autotrack_results = CollectionProperty(type=AutoTrackResultItem)
    Scene.autotrack_results_index = IntProperty()


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    for prop_name in (
        'autotrack_rate',
        'autotrack_min_tracks',
        'autotrack_max_new_tracks',
        'autotrack_auto_solve_after_track',
        'autotrack_tracking_strategy',
        'autotrack_tracking_iterations',
        'autotrack_keep_trial_clips',
        'autotrack_best_clip_name',
        'autotrack_active_tracking_strategy',
        'autotrack_detect_margin',
        'autotrack_detect_threshold',
        'autotrack_detect_distance',
        'autotrack_detect_placement',
        'autotrack_filter_threshold',
        'autotrack_filter_mintime',
        'autotrack_solve_delete_failed',
        'autotrack_solve_delete_count',
        'autotrack_solve_optimize_settings',
        'autotrack_solve_target_error',
        'autotrack_solve_max_iterations',
        'autotrack_solve_max_input_tracks',
        'autotrack_status',
        'autotrack_log',
        'autotrack_log_index',
        'autotrack_results',
        'autotrack_results_index',
    ):
        if hasattr(Scene, prop_name):
            delattr(Scene, prop_name)


if __name__ == '__main__':
    register()
