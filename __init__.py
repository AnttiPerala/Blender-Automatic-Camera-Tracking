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

# -------------------------------------------------------------------
#  LOGGING SYSTEM
# -------------------------------------------------------------------

class AutoTrackLogItem(PropertyGroup):
    message: StringProperty()
    icon: StringProperty(default="INFO")

def log_msg(scene, message, icon='INFO'):
    print(f"[AutoTrack] {message}")
    
    try:
        item = scene.autotrack_log.add()
        item.message = message
        item.icon = icon
    except:
        pass 
    
    if len(scene.autotrack_log) > 50:
        scene.autotrack_log.remove(0)
        
    scene.autotrack_log_index = len(scene.autotrack_log) - 1

class CLIP_UL_autotrack_log(UIList):
    bl_idname = "CLIP_UL_autotrack_log"
    
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        icon_name = item.icon if item.icon else 'INFO'
        try:
            layout.label(text=item.message, icon=icon_name)
        except:
            layout.label(text=item.message, icon='INFO')

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
        return (context.area.spaces.active.clip is not None)

    def execute(self, context):
        scene = context.scene
        clip = context.area.spaces.active.clip
        tracks = clip.tracking.tracks
        current_frame = scene.frame_current
        
        MIN_TIME = scene.autotrack_filter_mintime
        
        tracks_to_delete = []
        tracks_to_stop = []

        # 1. ANALYZE
        for track in tracks:
            if track.hide or track.lock: continue
            
            # Find markers (Current OR Previous frame to be robust)
            marker = track.markers.find_frame(current_frame, exact=True)
            if not marker:
                marker = track.markers.find_frame(current_frame - 1, exact=True)

            prev_marker = track.markers.find_frame(current_frame - scene.autotrack_rate, exact=True)
            if not prev_marker:
                 # Fallback for prev marker
                 prev_marker = track.markers.find_frame(current_frame - scene.autotrack_rate - 1, exact=True)

            # A. Time Cleanup
            if prev_marker and len(track.markers) < MIN_TIME:
                tracks_to_delete.append(track)
                continue

            # B. Quality Cleanup (Muted = Slipped)
            if marker and marker.mute:
                if len(track.markers) > MIN_TIME:
                    tracks_to_stop.append(track)
                else:
                    tracks_to_delete.append(track)

        # 2. ACT
        bpy.ops.clip.select_all(action='DESELECT')
        
        for track in tracks_to_delete: track.select = True
        if tracks_to_delete:
            log_msg(scene, f'Deleted {len(tracks_to_delete)} garbage tracks', 'TRASH')
            bpy.ops.clip.delete_track()
        
        for track in tracks_to_stop: track.select = False 
        if tracks_to_stop:
            log_msg(scene, f'Stopped {len(tracks_to_stop)} slipping tracks', 'PAUSE')

        # 3. DETECT
        bpy.ops.clip.select_all(action='DESELECT')
        bpy.ops.clip.detect_features(
            threshold=scene.autotrack_detect_threshold,
            min_distance=scene.autotrack_detect_distance,
            margin=scene.autotrack_detect_margin,
            placement=scene.autotrack_detect_placement
        )

        new_trackers = [t for t in tracks if t.select]

        # 4. OVERLAP
        old_trackers = []
        for track in tracks:
            if track not in new_trackers and not track.hide:
                # Check presence on current OR prev frame
                marker = track.markers.find_frame(current_frame, exact=True)
                if not marker: 
                    marker = track.markers.find_frame(current_frame - 1, exact=True)
                
                if marker: old_trackers.append(track)

        trackers_to_remove_overlap = []
        diaglen = math.sqrt(clip.size[0]**2 + clip.size[1]**2)
        
        for new_track in new_trackers:
            new_marker = new_track.markers.find_frame(current_frame, exact=True)
            if new_marker:
                for old_track in old_trackers:
                    old_marker = old_track.markers.find_frame(current_frame, exact=True)
                    if not old_marker: 
                        old_marker = old_track.markers.find_frame(current_frame - 1, exact=True)

                    if old_marker:
                        distance = (new_marker.co - old_marker.co).length * diaglen
                        if distance < scene.autotrack_detect_distance:
                            trackers_to_remove_overlap.append(new_track)
                            break 

        bpy.ops.clip.select_all(action='DESELECT')
        for track in trackers_to_remove_overlap: track.select = True
        bpy.ops.clip.delete_track()

        # 5. TRACK
        context.area.spaces.active.show_disabled = False
        
        count_tracking = 0
        for track in tracks:
            if track.hide or track.lock: continue
            if track in tracks_to_stop: continue 
            
            # Select if it has a marker on current OR previous frame (ready to track)
            marker = track.markers.find_frame(current_frame, exact=True)
            if not marker:
                marker = track.markers.find_frame(current_frame - 1, exact=True)
            
            if marker and not marker.mute:
                track.select = True
                
                # FIX: Add +5 frames buffer to the limit.
                # This ensures the tracker runs PAST the target frame, so the script
                # definitely wakes up. If we set it exactly to 'rate', it often stops
                # 1 frame short, causing a deadlock.
                track.frames_limit = scene.autotrack_rate + 5
                
                count_tracking += 1

        self._frame_redetect = current_frame + scene.autotrack_rate
        log_msg(scene, f'Tracking {count_tracking} feats to frame {self._frame_redetect}...', 'PLAY')
        
        bpy.ops.clip.track_markers('INVOKE_DEFAULT', backwards=False, sequence=True)
        context.area.tag_redraw()
        return {'FINISHED'}

    def modal(self, context, event):
        if event.type in {'ESC'}:
            log_msg(context.scene, 'Auto Track Cancelled', 'CANCEL')
            self.cancel(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            if context.scene.frame_current >= context.scene.frame_end:
                log_msg(context.scene, 'End of clip reached', 'CHECKMARK')
                self.cancel(context)
                return {'FINISHED'}
            
            # Check if we reached the target frame (Allow 1 frame tolerance)
            if self._frame_redetect == -1 or context.scene.frame_current >= (self._frame_redetect - 1):
                self.execute(context)
            
            return {'PASS_THROUGH'}
        
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        wm = context.window_manager
        wm.modal_handler_add(self)
        self._timer = wm.event_timer_add(time_step=0.5, window=context.window)
        self._frame_redetect = -1
        log_msg(context.scene, "Starting Auto Track...", "CON_FOLLOWTRACK")
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
            # STATE: SOLVING
            if self._state == 'SOLVING':
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

                tracks = clip.tracking.tracks
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
        scene = context.scene
        clip = context.area.spaces.active.clip
        if not clip or not clip.tracking.objects.active:
            self.report({'ERROR'}, "No active tracking object")
            return {'CANCELLED'}

        log_msg(scene, "--- Starting Auto Solve ---", "TRIA_RIGHT")
        
        self._iteration = 0
        self._tracks_disabled_count = 0
        self._state = 'SOLVING'
        self._state_from_prune = False
        
        wm = context.window_manager
        self._timer = wm.event_timer_add(time_step=0.1, window=context.window)
        wm.modal_handler_add(self)
        
        return {'RUNNING_MODAL'}

    def finish(self, context):
        scene = context.scene
        clip = context.area.spaces.active.clip
        tracks = clip.tracking.tracks
        
        if self._tracks_disabled_count > 0:
            bpy.ops.clip.select_all(action='DESELECT')
            for track in tracks:
                if track.weight == 0.0:
                    track.select = True
            
            bpy.ops.clip.delete_track()
            log_msg(scene, f"Finished. Deleted {self._tracks_disabled_count} tracks.", "TRASH")
        else:
            log_msg(scene, "Finished. No tracks deleted.", "CHECKMARK")
            
        self.cancel(context)

    def cancel(self, context):
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

        layout.separator()

        # --- TRACKING CONTROLS ---
        col = layout.column(align=True)
        col.scale_y = 1.5
        col.operator('autotrack.auto_track', text='Start Auto Track', icon='CON_FOLLOWTRACK')
        
        col = layout.column(align=True)
        col.separator()
        col.label(text="Main Settings:")
        col.prop(scene, "autotrack_rate")
        col.prop(scene, 'autotrack_filter_mintime', text="Min Duration")

        # --- SOLVING CONTROLS ---
        layout.separator()
        layout.label(text="Solving:")
        
        col = layout.column(align=True)
        col.prop(scene, "autotrack_solve_delete_failed", text="Del. Failed Reconstruct")
        col.prop(scene, "autotrack_solve_delete_count", text="Del. Worst (Batch)")
        
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
    CLIP_UL_autotrack_log,
    CLIP_OT_autotrack_clear_log,
    CLIP_OT_autotrack_autotrack,
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
    
    # Log Properties
    Scene.autotrack_log = CollectionProperty(type=AutoTrackLogItem)
    Scene.autotrack_log_index = IntProperty()


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    del Scene.autotrack_log
    del Scene.autotrack_log_index


if __name__ == '__main__':
    register()