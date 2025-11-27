# SPDX-FileCopyrightText: 2017-2022 Blender Foundation
#
# SPDX-License-Identifier: GPL-2.0-or-later

import time
import math
import bpy
from bpy.types import Operator, Panel, Scene
from bpy.props import (
    IntProperty,
    FloatProperty,
    EnumProperty
)


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

        # 1. Filter short tracks (Time based)
        context.area.spaces.active.show_disabled = True
        filtered_trackers = []
        for track in tracks:
            if track.hide or track.lock:
                continue
            # Check if track existed 'rate' frames ago
            marker = track.markers.find_frame(current_frame - scene.autotrack_rate, exact=True)
            if marker and len(track.markers) < scene.autotrack_filter_mintime:
                filtered_trackers.append(track)

        bpy.ops.clip.select_all(action='DESELECT')
        for track in filtered_trackers:
            track.select = True
        bpy.ops.clip.delete_track()
        if filtered_trackers:
            print('Filtered %s short trackers' % len(filtered_trackers))
        filtered_trackers.clear()

        # 2. Filter High Error tracks (Quality based)
        # This was missing from the loop previously
        bpy.ops.clip.select_all(action='SELECT')
        bpy.ops.clip.filter_tracks(track_threshold=scene.autotrack_filter_threshold)
        bpy.ops.clip.delete_track()

        # 3. Detect new features
        bpy.ops.clip.select_all(action='DESELECT')
        bpy.ops.clip.detect_features(
            threshold=scene.autotrack_detect_threshold,
            min_distance=scene.autotrack_detect_distance,
            margin=scene.autotrack_detect_margin,
            placement=scene.autotrack_detect_placement
        )

        # Store new trackers
        new_trackers = []
        for track in tracks:
            if track.select:
                new_trackers.append(track)
                track.frames_limit = scene.autotrack_rate
        
        # 4. Filter overlapping trackers
        bpy.ops.clip.select_all(action='INVERT')
        old_trackers = []
        for track in tracks:
            if track.select and not (track.hide or track.lock):
                marker = track.markers.find_frame(current_frame, exact=True)
                if marker and not marker.mute:
                    old_trackers.append(track)

        filtered_trackers = []
        diaglen = math.sqrt(clip.size[0]**2 + clip.size[1]**2)
        
        for new_track in new_trackers:
            new_marker = new_track.markers.find_frame(current_frame, exact=True)
            if new_marker:
                for old_track in old_trackers:
                    old_marker = old_track.markers.find_frame(current_frame, exact=True)
                    if old_marker:
                        distance = (new_marker.co - old_marker.co).length * diaglen
                        if distance < scene.autotrack_detect_distance:
                            filtered_trackers.append(new_track)
                            break # Optimization: Found one overlap, stop checking this new track

        bpy.ops.clip.select_all(action='DESELECT')
        for track in filtered_trackers:
            track.select = True
        bpy.ops.clip.delete_track()
        if filtered_trackers:
            print('Filtered %s overlapping trackers' % len(filtered_trackers))

        # 5. Start tracking
        context.area.spaces.active.show_disabled = False
        bpy.ops.clip.select_all(action='SELECT')
        
        # Calculate next stop point
        self._frame_redetect = current_frame + scene.autotrack_rate
        
        print(f'Tracking forward... (Next Detect: Frame {self._frame_redetect})')
        bpy.ops.clip.track_markers('INVOKE_DEFAULT', backwards=False, sequence=True)

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
                
                # CRITICAL FIX: Only run logic if we reached the target frame.
                # If user hit ESC during tracking, frame_current will be < _frame_redetect.
                # In that case, we simply wait (do nothing) until user manually tracks 
                # or scrubs to the target frame, effectively pausing the process.
                if self._frame_redetect == -1 or context.scene.frame_current >= self._frame_redetect:
                    self.execute(context)
                
            return {'PASS_THROUGH'}
        
        return {'RUNNING_MODAL'}

    def invoke(self, context, event):
        wm = context.window_manager
        wm.modal_handler_add(self)
        bpy.app.handlers.frame_change_post.append(self._frame_change_event)
        self._timer = wm.event_timer_add(time_step=0.5, window=context.window) # Faster timer check
        self._frame_changed = False
        self._frame_redetect = -1
        self.execute(context)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        # Safety cleanup
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
        print("Auto Track Finished/Cancelled")


class CLIP_OT_autotrack_filter(Operator):
    bl_idname = 'autotrack.filter'
    bl_label = 'Filter All Tracks'
    bl_description = 'Apply filters to all tracks'
    bl_options = {'REGISTER', 'UNDO'}

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
        # Hardcoded correlation threshold (0.75 is standard for "good")
        CORRELATION_MIN = 0.75 
        MIN_TIME = scene.autotrack_filter_mintime

        # List to hold tracks we want to DELETE (Garbage)
        tracks_to_delete = []
        # List to hold tracks we want to STOP (Finished/Slipping)
        tracks_to_stop = []

        # 1. ANALYZE EXISTING TRACKS
        for track in tracks:
            if track.hide or track.lock:
                continue
            
            # Get the marker for the current frame
            marker = track.markers.find_frame(current_frame, exact=True)
            
            # Check A: Is the track too short? (Time based)
            # We look back 'rate' frames. If it existed then, but total len is small, it's garbage.
            prev_marker = track.markers.find_frame(current_frame - scene.autotrack_rate, exact=True)
            if prev_marker and len(track.markers) < MIN_TIME:
                tracks_to_delete.append(track)
                continue

            # Check B: Is the track slipping? (Quality based)
            # If marker exists and is active, check quality
            if marker and not marker.mute:
                if track.average_correlation < CORRELATION_MIN:
                    # Logic: If it's long enough, keep it but stop tracking.
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
        # We don't delete these, we just ensure they are NOT selected for the next pass
        for track in tracks_to_stop:
            track.select = False
            # Optional: Lock it so we know it's "Done"
            # track.lock = True 
        if tracks_to_stop:
            print(f'Stopping {len(tracks_to_stop)} slipping tracks (kept history)')

        # 4. DETECT NEW FEATURES
        # Deselect everything first so detect_features runs cleanly
        bpy.ops.clip.select_all(action='DESELECT')
        bpy.ops.clip.detect_features(
            threshold=scene.autotrack_detect_threshold,
            min_distance=scene.autotrack_detect_distance,
            margin=scene.autotrack_detect_margin,
            placement=scene.autotrack_detect_placement
        )

        # Identify which tracks are the "New" ones (they are selected by detect_features)
        new_trackers = [t for t in tracks if t.select]

        # 5. FILTER OVERLAPPING
        # We compare New Trackers vs ALL existing visible trackers (Active OR Stopped)
        # This prevents spawning a new track on top of one we just stopped.
        
        # Invert selection to get the "Old" tracks (Active ones)
        # But we also need to include the ones we just "Stopped" (which are unselected)
        old_trackers = []
        for track in tracks:
            # If it's not a new track, and not hidden, it's an obstacle
            if track not in new_trackers and not track.hide:
                # Does it exist on this frame?
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

        # Delete the overlapping new ones
        bpy.ops.clip.select_all(action='DESELECT')
        for track in trackers_to_remove_overlap:
            track.select = True
        bpy.ops.clip.delete_track()
        if trackers_to_remove_overlap:
            print(f'Removed {len(trackers_to_remove_overlap)} overlapping candidates')

        # 6. START TRACKING
        context.area.spaces.active.show_disabled = False
        
        # Important: We need to make sure we select the VALID existing tracks + VALID new tracks
        # The "Stopped" tracks are currently unselected, which is exactly what we want.
        # But we need to make sure the "Active" old tracks are re-selected.
        
        for track in tracks:
            if track.hide or track.lock:
                continue
            if track in tracks_to_stop:
                continue # Keep these unselected
            
            # If it exists on this frame, select it for tracking
            marker = track.markers.find_frame(current_frame, exact=True)
            if marker:
                track.select = True

        # Calculate next stop point
        self._frame_redetect = current_frame + scene.autotrack_rate
        
        print(f'Tracking forward... (Next Detect: Frame {self._frame_redetect})')
        bpy.ops.clip.track_markers('INVOKE_DEFAULT', backwards=False, sequence=True)

        return {'FINISHED'}


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

        col = layout.column(align=True)
        col.scale_y = 1.5
        col.operator('autotrack.auto_track', text='Start Auto Track', icon='CON_FOLLOWTRACK')
        
        col = layout.column(align=True)
        col.separator()
        col.label(text="Main Settings:")
        col.prop(scene, "autotrack_rate")
        
        # EXPOSED: The filtering thresholds are now in the main panel
        col.separator()
        col.label(text="Deletion Criteria:")
        col.prop(scene, "autotrack_filter_threshold", text="Error Threshold")
        col.prop(scene, 'autotrack_filter_mintime', text="Min Duration")


class CLIP_PT_autotrack_tracker_settings(Panel):
    bl_label = 'Tracking Settings'
    bl_space_type = 'CLIP_EDITOR'
    bl_region_type = 'TOOLS'
    bl_category = 'Auto-track'
    bl_options = {'DEFAULT_CLOSED'} # Collapsed by default to clean up UI

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
        col.operator('autotrack.filter', text='Select High Error', icon='FILTER')


classes = (
    CLIP_OT_autotrack_autotrack,
    CLIP_OT_autotrack_filter,
    CLIP_PT_autotrack_main,
    CLIP_PT_autotrack_tracker_settings,
    CLIP_PT_autotrack_detect_settings,
    CLIP_PT_autotrack_filter_settings
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    Scene.autotrack_rate = IntProperty(
        name='Update Interval',
        description='How many frames to track before cleaning up and detecting new features',
        default=30,
        min=1
    )

    Scene.autotrack_detect_margin = IntProperty(
        name='Margin',
        description='Distance from edge of image detected features must be',
        subtype='PIXEL',
        default=0,
        min=0
    )
    Scene.autotrack_detect_threshold = FloatProperty(
        name='Detect Threshold',
        description='Quality threshold for feature detection (Lower = more features)',
        precision=3,
        default=0.1,
        min=0.001,
    )
    Scene.autotrack_detect_distance = IntProperty(
        name='Distance',
        description='Minimum distance between detected features',
        subtype='PIXEL',
        default=60,
        min=5
    )
    Scene.autotrack_detect_placement = EnumProperty(
        name='Allowed Placement',
        items=(
                ("FRAME", "Whole Frame", ""),
                ("INSIDE_GPENCIL", "Inside Grease Pencil", ""),
                ("OUTSIDE_GPENCIL", "Outside Grease Pencil", "")
        ),
        default='FRAME'
    )

    Scene.autotrack_filter_threshold = FloatProperty(
        name='Error Threshold',
        description='Maximum allowed reprojection error. Tracks above this are deleted automatically',
        precision=2,
        default=5.0,
        min=0.0,
    )
    Scene.autotrack_filter_mintime = IntProperty(
        name='Min Duration',
        description='Tracks shorter than this (in frames) are deleted during cleanup',
        default=15,
        min=0
    )


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()