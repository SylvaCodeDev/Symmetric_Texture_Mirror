bl_info = {
    "name": "Symmetric Texture Mirror",
    "author": "SylvaCode",
    "version": (1, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar (N) > Symmetry",
    "description": "Bisect and mirror a textured mesh with live preview",
    "category": "Mesh",
}

"""Mirror textured meshes across a selectable local axis."""

import bpy
import bmesh
from mathutils import Vector, Matrix
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty


_GUIDES = {}
_DRAW_HANDLE = None
_IN_UPDATE = False


def _axis_idx(settings):
    return 'XYZ'.index(settings.axis)


def _is_valid_source(obj):
    try:
        return (obj is not None and obj.type == 'MESH' and
                obj.data is not None and len(obj.data.vertices) > 0 and
                len(obj.data.polygons) > 0 and
                not obj.get('_symtex_preview'))
    except ReferenceError:
        return False


def _tag_viewports():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def _clear_obj(obj):
    if obj is None:
        return
    try:
        mesh = obj.data if obj.type == 'MESH' else None
        if obj.name in bpy.data.objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    except (ReferenceError, RuntimeError):
        pass


def _restore_hidden_source(settings):
    source = settings.hidden_source
    if source is not None:
        try:
            if source.name in bpy.data.objects:
                source.hide_set(settings.was_hidden)
                source.hide_render = settings.was_render_hidden
        except (ReferenceError, RuntimeError):
            pass
    settings.hidden_source = None


def _remove_preview(scene, restore_source=True):
    s = scene.symtex
    key = scene.as_pointer()
    _GUIDES.pop(key, None)

    old_preview, old_helper = s.preview, s.helper
    s.preview = None
    s.helper = None
    _clear_obj(old_preview)
    _clear_obj(old_helper)
    if restore_source:
        _restore_hidden_source(s)
    _tag_viewports()


def _original_mesh_data(source, context):
    """Preserve all evaluated details when input has modifiers/shape keys."""
    if source.modifiers or source.data.shape_keys:
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = source.evaluated_get(depsgraph)
        mesh = bpy.data.meshes.new_from_object(
            evaluated, preserve_all_data_layers=True, depsgraph=depsgraph
        )
        if mesh is None:
            raise RuntimeError('Cannot evaluate this source mesh.')
        return mesh
    return source.data.copy()


def _plane_info(mesh, axis_index, fraction):
    coords = [v.co[axis_index] for v in mesh.vertices]
    low, high = min(coords), max(coords)
    span = high - low
    if span < 1e-10:
        raise RuntimeError('No thickness along selected axis. Pick X, Y or Z.')
    plane = low + span * fraction
    tol = max(span * 1e-6, 1e-9)
    merge_tol = max(span * 5e-6, 1e-8)
    return plane, tol, merge_tol


def _world_frame_and_cut(source, bounds, axis_index, plane, cut_segments):
    """World-space rectangle for the cut plane plus exact bisect contours."""
    axes = [i for i in range(3) if i != axis_index]
    extent = []
    for i in axes:
        lo, hi = bounds[i]
        margin = max((hi - lo) * 0.06, 0.001)
        extent.append((lo - margin, hi + margin))
    a, b = axes
    points = []
    for v0, v1 in (
        (extent[0][0], extent[1][0]),
        (extent[0][1], extent[1][0]),
        (extent[0][1], extent[1][1]),
        (extent[0][0], extent[1][1]),
    ):
        vec = Vector((0, 0, 0))
        vec[axis_index] = plane
        vec[a], vec[b] = v0, v1
        points.append(tuple(source.matrix_world @ vec))
    rect_lines = []
    for n in range(4):
        rect_lines.extend((points[n], points[(n + 1) % 4]))

    contour_lines = []
    for first, second in cut_segments:
        contour_lines.extend((
            tuple(source.matrix_world @ first),
            tuple(source.matrix_world @ second)
        ))
    return rect_lines, contour_lines


def _make_preview(scene, source, context):
    s = scene.symtex
    if context.mode != 'OBJECT':
        raise RuntimeError('Switch to Object Mode first.')
    if source.library is not None or source.data.library is not None:
        raise RuntimeError('Make the linked source mesh local before mirroring.')
    axis = _axis_idx(s)
    result = None
    helper = None
    mesh = None

    try:
        mesh = _original_mesh_data(source, context)
        result = source.copy()
        result.data = mesh
        mesh = None
        result.name = source.name + '_SYMTEX_PREVIEW'
        result['_symtex_preview'] = True
        result.animation_data_clear()
        result.modifiers.clear()

        collection = source.users_collection[0] if source.users_collection else scene.collection
        collection.objects.link(result)
        result.matrix_world = source.matrix_world.copy()
        result.hide_viewport = False
        result.hide_render = False
        result.hide_set(False)
        result.hide_select = False

        bounds = [
            (min(v.co[i] for v in result.data.vertices),
             max(v.co[i] for v in result.data.vertices))
            for i in range(3)
        ]
        plane, tolerance, merge_tolerance = _plane_info(
            result.data, axis, s.cut_position
        )
        normal = Vector(tuple(1.0 if i == axis else 0.0 for i in range(3)))
        point = normal * plane
        bm = bmesh.new()
        try:
            bm.from_mesh(result.data)
            bisected = bmesh.ops.bisect_plane(
                bm,
                geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                dist=tolerance,
                plane_co=point,
                plane_no=normal,
                clear_inner=s.keep_positive,
                clear_outer=not s.keep_positive,
            )
            for v in bisected['geom_cut']:
                if isinstance(v, bmesh.types.BMVert) and v.is_valid:
                    v.co[axis] = plane
            for v in bm.verts:
                if abs(v.co[axis] - plane) <= tolerance:
                    v.co[axis] = plane

            if not bm.faces:
                raise RuntimeError('No faces remain after cutting. Adjust the plane.')

            cut_segments = []
            for edge in bm.edges:
                if edge.is_valid and all(
                    abs(v.co[axis] - plane) <= tolerance * 2 for v in edge.verts
                ):
                    cut_segments.append((edge.verts[0].co.copy(),
                                         edge.verts[1].co.copy()))
            bm.to_mesh(result.data)
        finally:
            bm.free()
        result.data.update()

        helper = bpy.data.objects.new(result.name + '_MIRROR_PLANE', None)
        collection.objects.link(helper)
        helper.matrix_world = result.matrix_world @ Matrix.Translation(point)
        helper.hide_render = True
        helper.hide_set(True)
        helper.hide_select = True

        modifier = result.modifiers.new('Symmetric Texture Mirror', 'MIRROR')
        for i in range(3):
            modifier.use_axis[i] = (i == axis)
        modifier.mirror_object = helper
        modifier.use_mirror_merge = True
        modifier.use_clip = True
        modifier.merge_threshold = merge_tolerance
        # Preserve UV orientation from the retained half.
        modifier.use_mirror_u = False
        modifier.use_mirror_v = False

        frame, contour = _world_frame_and_cut(
            source, bounds, axis, plane, cut_segments
        )
        return result, helper, frame, contour

    except Exception:
        _clear_obj(result)
        _clear_obj(helper)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
        raise


def _refresh_preview(scene, context):
    global _IN_UPDATE
    if _IN_UPDATE:
        return
    _IN_UPDATE = True
    try:
        s = scene.symtex
        _remove_preview(scene)
        if not s.preview_enabled:
            if _is_valid_source(s.source) and not s.source.hide_get():
                bpy.ops.object.select_all(action='DESELECT')
                s.source.select_set(True)
                context.view_layer.objects.active = s.source
            s.status = 'Preview off. Original mesh restored.'
            return
        if not _is_valid_source(s.source):
            s.status = 'Select a textured mesh, then click Use Selected.'
            return
        source = s.source
        result, helper, frame, contour = _make_preview(scene, source, context)
        s.was_hidden = source.hide_get()
        s.was_render_hidden = source.hide_render
        s.hidden_source = source
        s.preview = result
        s.helper = helper
        source.hide_set(True)
        source.hide_render = True
        bpy.ops.object.select_all(action='DESELECT')
        result.select_set(True)
        context.view_layer.objects.active = result
        _GUIDES[scene.as_pointer()] = (frame, contour)
        kept = '+ positive' if s.keep_positive else '- negative'
        s.status = f'LIVE: {s.axis} axis | keeping {kept} side'
    except Exception as exc:
        _remove_preview(scene)
        s.status = 'Preview error: ' + str(exc)
        print('[Symmetric Texture Mirror]', s.status)
    finally:
        _IN_UPDATE = False
        _tag_viewports()


def _settings_updated(self, context):
    if context is None or context.scene is None:
        return
    _refresh_preview(context.scene, context)


def _draw_guide():
    try:
        scene = bpy.context.scene
        if scene is None or not hasattr(scene, 'symtex'):
            return
        s = scene.symtex
        if not s.preview_enabled or s.preview is None or not s.show_guide:
            return
        lines = _GUIDES.get(scene.as_pointer())
        if not lines:
            return
        import gpu
        from gpu_extras.batch import batch_for_shader

        try:
            shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
            polyline = True
        except Exception:
            shader = gpu.shader.from_builtin('UNIFORM_COLOR')
            polyline = False

        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('NONE')
        for coords, color, width in (
            (lines[0], (1.0, 0.55, 0.03, 0.70), 1.6),
            (lines[1], (0.05, 1.0, 0.80, 0.95), 3.0),
        ):
            if not coords:
                continue
            batch = batch_for_shader(shader, 'LINES', {'pos': coords})
            shader.bind()
            if polyline:
                shader.uniform_float('viewportSize', gpu.state.viewport_get()[2:])
                shader.uniform_float('lineWidth', width)
            shader.uniform_float('color', color)
            batch.draw(shader)
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')
    except Exception as exc:
        print('[Symmetric Texture Mirror] Guide drawing:', exc)


class SYMTEX_Settings(bpy.types.PropertyGroup):
    source: PointerProperty(
        name='Source Model', type=bpy.types.Object,
        description='Original mesh used for preview and final geometry',
        update=_settings_updated,
    )
    axis: EnumProperty(
        name='Mirror Axis',
        items=[('X', 'X', 'Cut along local X'),
               ('Y', 'Y', 'Cut along local Y'),
               ('Z', 'Z', 'Cut along local Z')],
        default='Y', update=_settings_updated,
    )
    keep_positive: BoolProperty(
        name='Swap Sides',
        description='Off keeps the negative side, on keeps the positive side',
        default=False, update=_settings_updated,
    )
    cut_position: FloatProperty(
        name='Cut Position',
        description='Position of cutting plane between minimum and maximum model bounds',
        min=0.01, max=0.99, default=0.50, subtype='FACTOR',
        update=_settings_updated,
    )
    preview_enabled: BoolProperty(
        name='Live Preview', default=True, update=_settings_updated,
    )
    show_guide: BoolProperty(name='Show Cut Guide', default=True,
                             update=lambda self, context: _tag_viewports())
    status: StringProperty(name='Status', default='Select a mesh to start.', options={'HIDDEN'})

    preview: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    helper: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    hidden_source: PointerProperty(type=bpy.types.Object, options={'HIDDEN'})
    was_hidden: BoolProperty(default=False, options={'HIDDEN'})
    was_render_hidden: BoolProperty(default=False, options={'HIDDEN'})


class SYMTEX_OT_use_selected(bpy.types.Operator):
    bl_idname = 'symtex.use_selected'
    bl_label = 'Use Selected Mesh'
    bl_description = 'Capture the selected source mesh and start live preview'
    bl_options = {'REGISTER'}

    def execute(self, context):
        active = context.view_layer.objects.active
        s = context.scene.symtex
        source = active if _is_valid_source(active) else None
        if source is None and active is not None and active.type == 'EMPTY':
            children = []
            todo = list(active.children)
            while todo:
                child = todo.pop()
                todo.extend(child.children)
                if _is_valid_source(child):
                    children.append(child)
            if len(children) == 1:
                source = children[0]
        if source is None:
            self.report({'WARNING'}, 'Select the original mesh (not its preview).')
            return {'CANCELLED'}
        if context.mode != 'OBJECT':
            self.report({'WARNING'}, 'Switch to Object Mode before previewing.')
            return {'CANCELLED'}
        if s.source == source:
            _refresh_preview(context.scene, context)
        else:
            s.source = source
        if s.status.startswith('Preview error:'):
            self.report({'ERROR'}, s.status[:200])
            return {'CANCELLED'}
        return {'FINISHED'}


class SYMTEX_OT_generate(bpy.types.Operator):
    bl_idname = 'symtex.generate'
    bl_label = 'Generate'
    bl_description = 'Apply mirrored geometry and permanently delete the original mesh and preview helper'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        s = getattr(context.scene, 'symtex', None) if context.scene else None
        return s is not None and _is_valid_source(s.source)

    def execute(self, context):
        global _IN_UPDATE
        scene = context.scene
        s = scene.symtex
        source = s.source
        if context.mode != 'OBJECT':
            self.report({'ERROR'}, 'Switch to Object Mode first.')
            return {'CANCELLED'}

        if s.preview is None:
            was_enabled = s.preview_enabled
            if not was_enabled:
                s.preview_enabled = True
            else:
                _refresh_preview(scene, context)
        if s.preview is None:
            self.report({'ERROR'}, s.status[:200])
            return {'CANCELLED'}

        result, helper = s.preview, s.helper
        modifier = next((m for m in result.modifiers if m.type == 'MIRROR' and
                         m.name == 'Symmetric Texture Mirror'), None)
        if modifier is None:
            self.report({'ERROR'}, 'Preview mirror modifier is missing. Refresh preview.')
            return {'CANCELLED'}

        bpy.ops.object.select_all(action='DESELECT')
        result.hide_set(False)
        result.select_set(True)
        context.view_layer.objects.active = result
        try:
            result_code = bpy.ops.object.modifier_apply(modifier=modifier.name)
            if 'FINISHED' not in result_code:
                raise RuntimeError('Blender cancelled modifier application.')
        except Exception as exc:
            self.report({'ERROR'}, 'Could not apply mirror: ' + str(exc))
            return {'CANCELLED'}

        # Verify the applied modifier produced geometry on both sides.
        axis_idx = _axis_idx(s)
        span_values = [v.co[axis_idx] for v in result.data.vertices]
        if not span_values or helper is None:
            self.report({'ERROR'}, 'Mirror output or plane helper is missing.')
            return {'CANCELLED'}
        local_plane = (result.matrix_world.inverted() @
                       helper.matrix_world.translation)[axis_idx]
        eps = max((max(span_values) - min(span_values)) * 1e-6, 1e-8)
        if (min(span_values) >= local_plane - eps or
                max(span_values) <= local_plane + eps):
            self.report({'ERROR'}, 'Mirror produced only one half. Original retained.')
            return {'CANCELLED'}

        # Delete the original only after a successful mirror operation.
        _IN_UPDATE = True
        try:
            original_name = source.name
            original_mesh = source.data
            if source != result:
                for child in list(source.children):
                    if child == result or child == helper:
                        continue
                    world = child.matrix_world.copy()
                    child.parent = result
                    child.matrix_world = world
                bpy.data.objects.remove(source, do_unlink=True)
                if original_mesh.users == 0:
                    bpy.data.meshes.remove(original_mesh)

            if helper is not None:
                _clear_obj(helper)
            result.name = original_name + '_Mirrored'
            if '_symtex_preview' in result:
                del result['_symtex_preview']
            result.hide_render = False
            result.hide_select = False
            result.hide_set(False)
            s.preview = None
            s.helper = None
            s.hidden_source = None
            s.source = None
            s.status = 'Generated: ' + result.name + '. Original deleted.'
            _GUIDES.pop(scene.as_pointer(), None)
            bpy.ops.object.select_all(action='DESELECT')
            result.select_set(True)
            context.view_layer.objects.active = result
        finally:
            _IN_UPDATE = False
            _tag_viewports()
        self.report({'INFO'}, 'Mirrored mesh generated; original deleted.')
        return {'FINISHED'}


class SYMTEX_PT_panel(bpy.types.Panel):
    bl_label = 'Symmetric Texture Mirror'
    bl_idname = 'SYMTEX_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Symmetry'

    def draw(self, context):
        layout = self.layout
        s = context.scene.symtex
        layout.prop(s, 'source', text='Model')
        layout.operator('symtex.use_selected', icon='EYEDROPPER')
        col = layout.column(align=True)
        col.label(text='Mirror Axis (local)')
        col.prop(s, 'axis', expand=True)
        col.prop(s, 'keep_positive', toggle=True, icon='ARROW_LEFTRIGHT')
        kept = 'Positive (+)' if s.keep_positive else 'Negative (-)'
        col.label(text='Keep: ' + kept)
        col.prop(s, 'cut_position', slider=True)
        layout.separator()
        layout.prop(s, 'preview_enabled', icon='HIDE_OFF')
        layout.prop(s, 'show_guide', icon='OVERLAY')
        if s.preview is not None:
            box = layout.box()
            box.label(text='Preview shows final mirrored shape', icon='CHECKMARK')
            box.label(text='Orange: plane | Cyan: cut edges')
        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator('symtex.generate', icon='MOD_MIRROR')
        layout.separator()
        layout.label(text=s.status[:100], icon='INFO')
        if _is_valid_source(s.source) and (s.source.modifiers or s.source.data.shape_keys):
            layout.label(text='Source modifiers/keys baked to static mesh', icon='ERROR')
        layout.label(text='Symmetric geometry only; does not repair holes.')


_CLASSES = (
    SYMTEX_Settings,
    SYMTEX_OT_use_selected,
    SYMTEX_OT_generate,
    SYMTEX_PT_panel,
)


def register():
    global _DRAW_HANDLE
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.symtex = PointerProperty(type=SYMTEX_Settings)
    if not bpy.app.background:
        _DRAW_HANDLE = bpy.types.SpaceView3D.draw_handler_add(
            _draw_guide, (), 'WINDOW', 'POST_VIEW'
        )


def unregister():
    global _DRAW_HANDLE, _IN_UPDATE
    _IN_UPDATE = True
    try:
        for scene in bpy.data.scenes:
            _remove_preview(scene)
        if _DRAW_HANDLE is not None:
            bpy.types.SpaceView3D.draw_handler_remove(_DRAW_HANDLE, 'WINDOW')
            _DRAW_HANDLE = None
        if hasattr(bpy.types.Scene, 'symtex'):
            del bpy.types.Scene.symtex
        for cls in reversed(_CLASSES):
            bpy.utils.unregister_class(cls)
        _GUIDES.clear()
    finally:
        _IN_UPDATE = False


if __name__ == '__main__':
    register()
