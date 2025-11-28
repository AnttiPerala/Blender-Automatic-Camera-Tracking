# SPDX-FileCopyrightText: 2017-2022 Blender Foundation
# SPDX-License-Identifier: GPL-2.0-or-later

import time
import math
import bpy
from bpy.types import Operator, Panel, Scene
from bpy.props import (
    IntProperty,
    FloatProperty,
    EnumProperty,
    BoolProperty
)

# -------------------------------------------------------------------
#  OPERATOR: MAIN AUTO TRACKER
# -------------------------------------------------------------------

class CLIP_OT_autotrack_autotrack(Operator):
    bl_idname = 'autotrack.auto_track'
    bl_label = 'Auto Track'
    bl_description = 'Automatically use Detect Features and filtering to motion track the timeline forward'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING', 'PRESET'}

    _frame_changed = False
    _frame_redetect = -1  # Initialize to -1

    def _frame_change_event(self, scene, depsgraph):
        self._frame_changed = True

    @classmethod
    def poll(cls, context):
        return (context.area.spaces.active.clip is not None)

    def execute(self, context):
        time_start = time.time()
        scene = context.scene
        clip = context.area.spaces.active.clip
        tracks = clip.tracking.tracks
        current_frame = scene.frame_current

        # --- SETTINGS ---
        CORRELATION_MIN = 0.75 
        MIN_TIME = scene.autotrack_filter_mintime

        # Lists for action
        tracks_to_delete = []
        tracks_to_stop = []

        # 1. ANALYZE EXISTING TRACKS
        for track in tracks:
            if track.hide or track.lock:
                continue
            
            # Check if track is active on this frame
            marker = track.markers.find_frame(current_frame, exact=True)
            
            # Check A: Is the track too short? (Time based cleanup)
            prev_marker = track.markers.find_frame(current_frame - scene.autotrack_rate, exact=True)
            if prev_marker and len(track.markers) < MIN_TIME:
                tracks_to_delete.append(track)
                continue

            # Check B: Is the track slipping? (Quality based cleanup)
            if marker and not marker.mute:
                if track.average_correlation < CORRELATION_MIN:
                    # Logic: If it's long enough, keep it but stop tracking (save history).
                    # If it's too short, kill it.
                    if len(track.markers) > MIN_TIME:
                        tracks_to_stop.append(track)
                    else:
                        tracks_to_delete.append(track)

        # 2. APPLY DELETIONS
        bpy.ops.clip.select_all(action='DESELECT')
        for track in tracks_to_delete:
            track.select = True
        if tracks_to_delete:
            print(f'Deleting {len(tracks_to_delete)} garbage tracks')
            bpy.ops.clip.delete_track()
        
        # 3. APPLY STOPS (DISABLE)
        for track in tracks_to_stop:
            track.select = False # Deselecting stops the tracker from updating it next pass
        if tracks_to_stop:
            print(f'Stopping {len(tracks_to_stop)} slipping tracks')

        # 4. DETECT NEW FEATURES
        # Deselect everything first so detect_features runs cleanly
        bpy.ops.clip.select_all(action='DESELECT')
        bpy.ops.clip.detect_features(
            threshold=scene.autotrack_detect_threshold,
            min_distance=scene.autotrack_detect_distance,
            margin=scene.autotrack_detect_margin,
            placement=scene.autotrack_detect_placement
        )

        # Identify "New" tracks (selected by detect_features)
        new_trackers = [t for t in tracks if t.select]

        # 5. FILTER OVERLAPPING
        # Compare New Trackers vs ALL existing visible trackers (Active OR Stopped)
        old_trackers = []
        for track in tracks:
            if track not in new_trackers and not track.hide:
                marker = track.markers.find_frame(current_frame, exact=True)
                if marker:
                    old_trackers.append(track)

        trackers_to_remove_overlap = []
        diaglen = math.sqrt(clip.size[0]**2 + clip.size[1]**2)
        
        for new_track in new_trackers:
            new_marker = new_track.markers.find_frame(current_frame, exact=True)
            if new_marker:
                for old_track in old_trackers:
                    old_marker = old_track.markers.find_frame(current_frame, exact=True)
                    if old_marker:
                        distance = (new_marker.co - old_marker.co).length * diaglen
                        if distance < scene.autotrack_detect_distance:
                            trackers_to_remove_overlap.append(new_track)
                            break 

        bpy.ops.clip.select_all(action='DESELECT')
        for track in trackers_to_remove_overlap:
            track.select = True
        bpy.ops.clip.delete_track()
        if trackers_to_remove_overlap:
            print(f'Removed {len(trackers_to_remove_overlap)} overlapping candidates')

        # 6. START TRACKING
        context.area.spaces.active.show_disabled = False
        
        # Select valid tracks for the next tracking pass
        for track in tracks:
            if track.hide or track.lock:
                continue
            if track in tracks_to_stop:
                continue # Keep these unselected/stopped
            
            # If it exists on this frame, select it for tracking
            marker = track.markers.find_frame(current_frame, exact=True)
            if marker:
                track.select = True

        # Calculate next stop point
        self._frame_redetect = current_frame + scene.autotrack_rate
        
        print(f'Tracking forward... (Next Detect: Frame {self._frame_redetect})')
        bpy.ops.clip.track_markers('INVOKE_DEFAULT', backwards=False, sequence=True)
        
        # Force UI update for stats
        context.area.tag_redraw()

        return {'FINISHED'}

    def modal(self, context, event):
        if event.type in {'ESC'}:
            print('Cancelling Auto Track...')
            self.cancel(context)
            return {'CANCELLED'}

        if event.type == 'TIMER':
            if context.scene.frame_current >= context.scene.frame_end:
                print('End of clip reached')
                self.cancel(context)
                return {'FINISHED'}
            
            if self._frame_changed:
                self._frame_changed = False
                if self._frame_redetect == -1 or context.scene.frame_current >= self._frame_redetect:
                    self.execute(context)
            return {'PASS_THROUGH'}
        
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        wm = context.window_manager
        wm.modal_handler_add(self)
        bpy.app.handlers.frame_change_post.append(self._frame_change_event)
        self._timer = wm.event_timer_add(time_step=0.5, window=context.window)
        self._frame_changed = False
        self._frame_redetect = -1
        self.execute(context)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        try:
            bpy.app.handlers.frame_change_post.remove(self._frame_change_event)
        except:
            pass
        wm = context.window_manager
        if hasattr(self, '_timer'):
            wm.event_timer_remove(self._timer)
        
        if context.area and context.area.spaces.active and context.area.spaces.active.clip:
            for track in context.area.spaces.active.clip.tracking.tracks:
                track.frames_limit = 0
        context.area.tag_redraw()
        print("Auto Track Finished/Cancelled")


# -------------------------------------------------------------------
#  OPERATOR: AUTO SOLVE & CLEAN
# -------------------------------------------------------------------

class CLIP_OT_autotrack_autosolve(Operator):
    bl_idname = 'autotrack.auto_solve'
    bl_label = 'Auto Solve & Clean'
    bl_description = 'Iteratively solves and removes worst/failed trackers until error stops improving'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        clip = context.area.spaces.active.clip
        if not clip:
            self.report({'ERROR'}, "No clip active")
            return {'CANCELLED'}
        
        active_obj = clip.tracking.objects.active
        if not active_obj:
            self.report({'ERROR'}, "No active tracking object found")
            return {'CANCELLED'}

        tracks = clip.tracking.tracks
        
        MAX_ITERATIONS = 20
        TARGET_ERROR = 0.3
        
        # User settings
        delete_failed = scene.autotrack_solve_delete_failed
        delete_count = scene.autotrack_solve_delete_count
        
        print(f"--- Starting Auto Solve (Remove {delete_count} worst + Failed: {delete_failed}) ---")
        
        # Initial Solve
        bpy.ops.clip.solve_camera()
        
        if not active_obj.reconstruction.is_valid:
            self.report({'ERROR'}, "Initial solve failed. Need at least 8 good tracks.")
            return {'CANCELLED'}

        best_error = active_obj.reconstruction.average_error
        print(f"Initial Error: {best_error:.4f}")

        tracks_disabled_count = 0

        for i in range(MAX_ITERATIONS):
            if best_error <= TARGET_ERROR:
                print(f"Target error {TARGET_ERROR} reached.")
                break

            # --- IDENTIFY TRACKS TO PRUNE ---
            candidates_to_prune = [] # List of tuples (track, reason_string)
            
            # 1. Identify Failed Reconstructions (Has no 3D Bundle)
            if delete_failed:
                for track in tracks:
                    # weight > 0 means it's currently used. 
                    # has_bundle == False means solve failed for this specific track.
                    if track.weight > 0.0 and not track.hide and not track.has_bundle:
                        candidates_to_prune.append(track)

            # 2. Identify Worst N Trackers
            # Filter only valid tracks that HAVE a bundle (otherwise they have no error to measure)
            valid_tracks = [t for t in tracks if t.weight > 0.0 and not t.hide and t.has_bundle]
            
            # Sort by error (Descending)
            valid_tracks.sort(key=lambda t: t.average_error, reverse=True)
            
            # Take the top N
            worst_n = valid_tracks[:delete_count]
            for t in worst_n:
                if t not in candidates_to_prune:
                    candidates_to_prune.append(t)

            if not candidates_to_prune:
                print("No more tracks to prune.")
                break

            # --- SOFT DELETE & BACKUP ---
            # Store original weights to revert if needed
            history = {track: track.weight for track in candidates_to_prune}
            
            # Set weight to 0
            for track in candidates_to_prune:
                track.weight = 0.0
            
            # --- SOLVE AGAIN ---
            bpy.ops.clip.solve_camera()
            new_error = active_obj.reconstruction.average_error
            
            # --- CHECK RESULT ---
            if new_error < best_error:
                # IMPROVEMENT: Keep changes
                track_names = ", ".join([t.name for t in candidates_to_prune])
                print(f"Iter {i+1}: Disabled {len(candidates_to_prune)} tracks. Improvement: {best_error:.4f} -> {new_error:.4f}")
                best_error = new_error
                tracks_disabled_count += len(candidates_to_prune)
            else:
                # WORSE: Revert changes
                print(f"Iter {i+1}: Removing {len(candidates_to_prune)} tracks made it worse ({new_error:.4f}). Reverting & Stopping.")
                for track, old_weight in history.items():
                    track.weight = old_weight
                
                bpy.ops.clip.solve_camera() # Restore solver state
                break

        # Final Cleanup
        if tracks_disabled_count > 0:
            bpy.ops.clip.select_all(action='DESELECT')
            count_deleted = 0
            for track in tracks:
                if track.weight == 0.0:
                    track.select = True
                    count_deleted += 1
            
            bpy.ops.clip.delete_track()
            self.report({'INFO'}, f"Finished. Error: {best_error:.4f}. Deleted {count_deleted} tracks.")
        else:
            self.report({'INFO'}, "Finished. No tracks removed.")
        
        context.area.tag_redraw()
        return {'FINISHED'}


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
        time_start = time.time()
        
        bpy.ops.clip.filter_tracks(
            track_threshold=scene.autotrack_filter_threshold,
        )
        
        count = sum(1 for t in tracks if t.select)
        print(f'Selected {count} high-error tracks in {time.time() - time_start:.4f} sec')
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
        
        # New Solver Settings
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


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()