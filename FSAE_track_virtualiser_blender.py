bl_info = {
    "name": "FSAE Track Virtualiser",
    "author": "cakemun",
    "version": (1, 0, 0),
    "blender": (4, 2, 23),
    "location": "View3D > Sidebar (N-panel) > FSAE Track",
    "description": "Take KML data points and virtualise them within Blender",
    "category": "Import-Export",
}

import os
import math
import random
import xml.etree.ElementTree as ET

try:
    import bpy
    from bpy.props import (
        StringProperty,
        FloatProperty,
        FloatVectorProperty,
        BoolProperty,
        EnumProperty,
        PointerProperty,
    )
    from mathutils import Vector, Matrix, Quaternion
except ImportError:
    bpy = None
    Vector = None
    Matrix = None
    Quaternion = None

# Try importing fastkml if available, but fallback to built-in ElementTree parser
try:
    import fastkml
    HAS_FASTKML = True
except ImportError:
    HAS_FASTKML = False


# =========================================================================
# GPS & KML UTILITIES
# =========================================================================

def gps_to_cartesian(lat, lon, alt, ref_lat, ref_lon, ref_alt=0.0):
    """
    Convert GPS (WGS84) lat/lon/alt to local Cartesian coordinates (x=East, y=North, z=Up)
    using tangent plane equirectangular projection relative to a reference GPS point.
    """
    R = 6371000.0  # Earth radius in meters
    dlat = math.radians(lat - ref_lat)
    dlon = math.radians(lon - ref_lon)
    ref_lat_rad = math.radians(ref_lat)

    x = R * math.cos(ref_lat_rad) * dlon
    y = R * dlat
    z = alt - ref_alt
    return x, y, z


def _parse_name_prefix_idx_group(name):
    """
    Splits a placemark name like "purple_cone-1" into (prefix, idx, group).
    An optional "_<group>" suffix on the index (e.g. "left_cone-3_2") tags which
    chicane set a left/right cone belongs to; group is None when absent/invalid.
    """
    prefix = name
    idx = 0
    group = None
    if '-' in name:
        parts = name.rsplit('-', 1)
        prefix = parts[0]
        idx_part = parts[1]
        idx_str, _, group_str = idx_part.partition('_')
        try:
            idx = int(idx_str)
        except ValueError:
            idx = 0
        if group_str:
            try:
                group = int(group_str)
            except ValueError:
                group = None
    return prefix, idx, group


def parse_kml_features(filepath):
    """
    Parses a KML file and extracts placemarks with their names and (lat, lon, alt).
    Uses ElementTree to guarantee zero-dependency execution in any Blender environment,
    with fastkml support if available.
    Returns: list of dicts: [{'name': name, 'lat': lat, 'lon': lon, 'alt': alt, 'type': prefix, 'idx': idx, 'group': group}]
    """
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"KML file not found: {filepath}")

    features = []

    # Parse with ElementTree
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()

        # Handle XML namespaces dynamically
        namespaces = {}
        if root.tag.startswith('{'):
            ns_uri = root.tag.split('}')[0].strip('{')
            namespaces = {'kml': ns_uri}
            placemark_tags = root.findall('.//kml:Placemark', namespaces)
        else:
            placemark_tags = root.findall('.//Placemark')

        for pm in placemark_tags:
            # Get name
            name_elem = pm.find('kml:name', namespaces) if namespaces else pm.find('name')
            name = name_elem.text.strip() if name_elem is not None and name_elem.text else "unnamed"

            # Look for Point coordinates first
            coords_elem = None
            point_elem = pm.find('kml:Point', namespaces) if namespaces else pm.find('Point')
            if point_elem is not None:
                coords_elem = point_elem.find('kml:coordinates', namespaces) if namespaces else point_elem.find('coordinates')

            lon, lat, alt = None, None, 0.0

            if coords_elem is not None and coords_elem.text:
                parts = coords_elem.text.strip().split(',')
                if len(parts) >= 2:
                    lon = float(parts[0])
                    lat = float(parts[1])
                    alt = float(parts[2]) if len(parts) > 2 else 0.0
            else:
                # Fallback to LookAt if Point is missing
                lookat_elem = pm.find('kml:LookAt', namespaces) if namespaces else pm.find('LookAt')
                if lookat_elem is not None:
                    lon_e = lookat_elem.find('kml:longitude', namespaces) if namespaces else lookat_elem.find('longitude')
                    lat_e = lookat_elem.find('kml:latitude', namespaces) if namespaces else lookat_elem.find('latitude')
                    alt_e = lookat_elem.find('kml:altitude', namespaces) if namespaces else lookat_elem.find('altitude')
                    if lon_e is not None and lat_e is not None:
                        lon = float(lon_e.text)
                        lat = float(lat_e.text)
                        alt = float(alt_e.text) if alt_e is not None and alt_e.text else 0.0

            if lat is not None and lon is not None:
                # Parse prefix and index (e.g. "purple_cone-1" -> "purple_cone", 1)
                prefix, idx, group = _parse_name_prefix_idx_group(name)

                features.append({
                    'name': name,
                    'type': prefix,
                    'idx': idx,
                    'group': group,
                    'lat': lat,
                    'lon': lon,
                    'alt': alt,
                })

    except Exception as e:
        # If ElementTree fails and fastkml is present, try fastkml
        if HAS_FASTKML:
            k = fastkml.KML.parse(filepath)
            for feature in fastkml.find_all(k, of_type=fastkml.Placemark):
                name = feature.name if feature.name else "unnamed"
                geom = feature.geometry
                if geom is not None and hasattr(geom, 'x') and hasattr(geom, 'y'):
                    lon = geom.x
                    lat = geom.y
                    alt = geom.z if getattr(geom, 'has_z', False) else 0.0
                    prefix, idx, group = _parse_name_prefix_idx_group(name)
                    features.append({
                        'name': name,
                        'type': prefix,
                        'idx': idx,
                        'group': group,
                        'lat': lat,
                        'lon': lon,
                        'alt': alt,
                    })
        else:
            raise e

    return features


# =========================================================================
# BLENDER SCENE & MODEL UTILITIES
# =========================================================================

def get_or_create_collection(name, parent_collection=None):
    """Retrieve or create a collection linked to the scene."""
    if name in bpy.data.collections:
        col = bpy.data.collections[name]
    else:
        col = bpy.data.collections.new(name)
        if parent_collection:
            parent_collection.children.link(col)
        else:
            bpy.context.scene.collection.children.link(col)
    return col


def collect_collection_meshes(collection):
    """Recursively gather all mesh objects from a collection."""
    objs = set()
    for obj in collection.objects:
        if obj.type == "MESH":
            objs.add(obj)
    for child in collection.children:
        objs.update(collect_collection_meshes(child))
    return objs


def load_model_template(filepath):
    """
    Imports a 3D model once and caches it in a hidden template collection.
    Returns the root template object(s).
    Supports .obj, .fbx, .glb, .gltf.
    """
    if not filepath or not os.path.exists(filepath):
        return []

    template_col = get_or_create_collection("_FSAE_Model_Templates")
    template_col.hide_viewport = True
    template_col.hide_render = True

    base_name = os.path.splitext(os.path.basename(filepath))[0]
    
    # Check if this template was already loaded into the template collection
    existing_templates = [
        obj for obj in template_col.objects
        if obj.name == base_name or obj.name.startswith(f"{base_name}_template")
    ]
    if existing_templates:
        return existing_templates

    ext = os.path.splitext(filepath)[1].lower()
    names_before = set(bpy.data.objects.keys())

    # Import file
    if ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=filepath)
        else:
            bpy.ops.import_scene.obj(filepath=filepath)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=filepath)
    elif ext in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=filepath)
    else:
        raise ValueError(f"Unsupported model extension '{ext}'. Supported: .obj, .fbx, .glb, .gltf")

    new_names = set(bpy.data.objects.keys()) - names_before
    imported_objects = [bpy.data.objects[n] for n in new_names if n in bpy.data.objects]

    mesh_objects = [obj for obj in imported_objects if obj.type == "MESH"]

    # Some model files contain multiple separate bodies (e.g. left_cone.obj / right_cone.obj
    # import as two distinct objects: "1" and "1.001"). Only the first was ever used as the
    # template, so the second body was silently dropped when placing cones. Join them into a
    # single object here (while still selectable/visible, before moving into the hidden
    # template collection) so the complete model is used as the template.
    if len(mesh_objects) > 1:
        for obj in bpy.context.selected_objects:
            obj.select_set(False)
        for obj in mesh_objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = mesh_objects[0]
        bpy.ops.object.join()
        mesh_objects = [bpy.context.view_layer.objects.active]
        # Objects merged away by the join are deleted outright, so any Python reference to
        # them is now invalid - even reading .name on one raises ReferenceError. Rebuild from
        # the name strings captured before the join (safe membership check) instead of
        # re-touching the stale references.
        imported_objects = [bpy.data.objects[n] for n in new_names if n in bpy.data.objects]

    # Move imported templates to the template collection and unlink from scene
    for obj in imported_objects:
        for col in list(obj.users_collection):
            col.objects.unlink(obj)
        template_col.objects.link(obj)

    return mesh_objects


def create_colored_material(name, rgba):
    """Create or return a simple principled BSDF material with the given color."""
    if name in bpy.data.materials:
        return bpy.data.materials[name]
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = rgba
        if "Roughness" in bsdf.inputs:
            bsdf.inputs["Roughness"].default_value = 0.4
    return mat


def get_default_cone_material(cone_type):
    """Return a standard color material for FSAE cone classifications."""
    c_type = cone_type.lower()
    if "orange" in c_type:
        return create_colored_material("FSAE_Orange_Mat", (1.0, 0.35, 0.0, 1.0))
    elif "purple" in c_type or "blue" in c_type:
        return create_colored_material("FSAE_Blue_Mat", (0.05, 0.2, 0.9, 1.0))
    elif "yellow" in c_type:
        return create_colored_material("FSAE_Yellow_Mat", (1.0, 0.85, 0.0, 1.0))
    elif "white" in c_type:
        return create_colored_material("FSAE_White_Mat", (0.9, 0.9, 0.9, 1.0))
    return None


def create_fallback_cone_mesh():
    """Generates a procedural cone mesh if no external 3D model is provided."""
    mesh_name = "_FSAE_Default_Cone_Mesh"
    if mesh_name in bpy.data.meshes:
        return bpy.data.meshes[mesh_name]

    # Simple 16-segment cone with 0.15m base radius and 0.35m height
    import bmesh
    bm = bmesh.new()
    bmesh.ops.create_cone(
        bm,
        cap_ends=True,
        cap_tris=False,
        segments=16,
        radius1=0.15,
        radius2=0.02,
        depth=0.35,
        matrix=Matrix.Translation((0, 0, 0.175)),
    )
    mesh = bpy.data.meshes.new(mesh_name)
    bm.to_mesh(mesh)
    bm.free()
    return mesh


def raycast_downward_surface(
    depsgraph,
    scene,
    x,
    y,
    start_z=2000.0,
    max_distance=10000.0,
    valid_target_names=None,
    ignored_names=None,
    max_steps=30,
):
    """
    Performs downward pass-through raycasting from (x, y, start_z) to accurately find
    track surfaces even if track coordinates are negative (z < 0) or obstructed by overhead objects.
    
    - valid_target_names: set of string names. If provided, passes through all non-target objects
      until a matching mesh is hit.
    - ignored_names: set of string names (e.g. templates, placed cones) to ignore.
    """
    direction = Vector((0.0, 0.0, -1.0))
    curr_origin = Vector((x, y, start_z))
    dist_traveled = 0.0

    if ignored_names is None:
        ignored_names = set()

    for _ in range(max_steps):
        remaining_dist = max_distance - dist_traveled
        if remaining_dist <= 0:
            break

        hit, loc, norm, idx, obj, mat = scene.ray_cast(
            depsgraph,
            curr_origin,
            direction,
            distance=remaining_dist,
        )

        if not hit or obj is None:
            break

        obj_name = obj.name
        orig_name = getattr(obj, "original", obj).name

        # Ignore template objects and newly placed cones
        if obj_name in ignored_names or orig_name in ignored_names:
            step_dist = (loc - curr_origin).length + 0.005
            dist_traveled += step_dist
            curr_origin = loc + direction * 0.005
            continue

        # If a specific target filter is active
        if valid_target_names is not None:
            if obj_name in valid_target_names or orig_name in valid_target_names:
                return True, loc, norm, obj
            else:
                # Step through non-matching objects until we hit the target
                step_dist = (loc - curr_origin).length + 0.005
                dist_traveled += step_dist
                curr_origin = loc + direction * 0.005
                continue
        else:
            # ANY mode: accept first non-ignored mesh object
            return True, loc, norm, obj

    return False, None, None, None


# =========================================================================
# BLENDER UI & OPERATORS (Active when running inside Blender)
# =========================================================================

if bpy is not None:

    class FSAEConeTypeSettings(bpy.types.PropertyGroup):
        """Optional per-cone-type model override, falling back to the global model/template when unset."""
        model_filepath: StringProperty(
            name="Model (.obj/.fbx/.glb)",
            description="Optional 3D model to use for this cone type. Falls back to the global model/template if left empty",
            subtype='FILE_PATH',
            default="",
        )

        template_scene_object: PointerProperty(
            name="Scene Object Template",
            type=bpy.types.Object,
            description="Optional existing scene object to use as the template for this cone type",
        )


    class FSAETrackVirtualiserSettings(bpy.types.PropertyGroup):
        kml_filepath: StringProperty(
            name="KML File",
            description="Path to the .kml track coordinates file",
            subtype='FILE_PATH',
            default="",
        )
        
        model_filepath: StringProperty(
            name="Cone Model (.obj/.fbx/.glb)",
            description="Optional 3D cone model to import. If left empty, a standard FSAE cone mesh will be generated",
            subtype='FILE_PATH',
            default="",
        )

        template_scene_object: PointerProperty(
            name="Scene Object Template",
            type=bpy.types.Object,
            description="Optional existing object in the scene to use as the cone template",
        )

        use_raycast: BoolProperty(
            name="Snap to Track Surface",
            description="Use raycasting to snap cone bases to the track surface mesh",
            default=True,
        )

        raycast_target_mode: EnumProperty(
            name="Target Mode",
            description="How to choose track surfaces to raycast against",
            items=[
                ('ANY', "Any Surface Below", "Snap to any mesh surface beneath the cone (Default)"),
                ('SELECTED', "Selected Objects", "Snap only to objects currently selected in 3D Viewport"),
                ('COLLECTION', "Specific Collection", "Snap to meshes in a chosen track collection"),
                ('OBJECT', "Specific Object", "Snap to a specific track object"),
            ],
            default='ANY',
        )

        target_collection: PointerProperty(
            name="Track Collection",
            type=bpy.types.Collection,
            description="Collection containing track surface meshes",
        )

        target_object: PointerProperty(
            name="Track Object",
            type=bpy.types.Object,
            description="Target mesh object for track surface",
        )

        track_target: StringProperty(
            name="Track Name (Fallback)",
            description="Optional fallback name of track mesh or collection to snap cones onto",
            default="",
        )

        ray_start_height: FloatProperty(
            name="Ray Start Height (m)",
            description="Height above global origin from which downward raycasting begins",
            default=1000.0,
            precision=1,
        )

        miss_fallback: EnumProperty(
            name="If Ray Misses",
            description="What height to use if raycast does not hit any surface",
            items=[
                ('GPS_Z', "Keep GPS / Origin Z", "Use the calculated GPS Cartesian height (world Z)"),
                ('ZERO', "Set to 0.0", "Default to flat 0.0 height"),
            ],
            default='GPS_Z',
        )

        align_to_normal: BoolProperty(
            name="Align to Track Normal",
            description="Align cone upright direction with the track surface normal",
            default=False,
        )

        z_offset: FloatProperty(
            name="Z Offset (m)",
            description="Vertical height offset above ground or track surface",
            default=0.01,
            precision=3,
        )

        scale_vector: FloatVectorProperty(
            name="Cone Scale",
            description="Scale factor applied to imported cones",
            default=(1.0, 1.0, 1.0),
            size=3,
            precision=3,
        )

        random_yaw_range: FloatProperty(
            name="Random Yaw",
            description="Randomly rotate each cone by up to this angle around its own vertical axis for more natural-looking placement",
            default=math.radians(15.0),
            min=0.0,
            max=math.radians(180.0),
            subtype='ANGLE',
            unit='ROTATION',
        )

        ref_mode: EnumProperty(
            name="Reference Mode",
            description="How to determine the GPS origin point",
            items=[
                ('AUTO', "Auto (reference_position in KML)", "Use placemark named 'reference_position' or 'reference_position-0'"),
                ('FIRST', "First Placemark", "Use the very first placemark in the KML as GPS origin"),
                ('MANUAL_NAME', "Placemark by Name", "Specify a custom placemark name to use as GPS reference"),
            ],
            default='AUTO',
        )

        custom_ref_name: StringProperty(
            name="Reference Placemark Name",
            description="Exact name of placemark to use as reference (e.g. orange_cone-1)",
            default="",
        )

        origin_cartesian: FloatVectorProperty(
            name="Track Origin (X, Y, Z)",
            description="Blender 3D world coordinates for the reference GPS position",
            default=(0.0, 0.0, 0.0),
            size=3,
            precision=3,
        )

        filter_features: BoolProperty(
            name="Filter Known Features Only",
            description="Only import placemarks matching standard names (orange_cone, purple_cone, blue_cone, yellow_cone)",
            default=False,
        )

        apply_fsae_materials: BoolProperty(
            name="Assign Cone Colors",
            description="Assign automatic colored materials (orange/blue/yellow). Disable to preserve custom 3D model textures",
            default=True,
        )

        master_collection_name: StringProperty(
            name="Collection Name",
            description="Master collection name for virtualised track cones",
            default="FSAE_Track_Virtualised",
        )

        orange_cone_settings: PointerProperty(type=FSAEConeTypeSettings)
        purple_cone_settings: PointerProperty(type=FSAEConeTypeSettings)
        left_cone_settings: PointerProperty(type=FSAEConeTypeSettings)
        right_cone_settings: PointerProperty(type=FSAEConeTypeSettings)


    # =========================================================================
    # OPERATORS
    # =========================================================================

    class FSAE_OT_InspectKml(bpy.types.Operator):
        """Inspect KML file and display summary of placemarks"""
        bl_idname = "fsae.inspect_kml"
        bl_label = "Inspect KML File"
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            settings = context.scene.fsae_track_settings
            filepath = bpy.path.abspath(settings.kml_filepath)

            if not filepath or not os.path.exists(filepath):
                self.report({'ERROR'}, "Please specify a valid KML file path.")
                return {'CANCELLED'}

            try:
                features = parse_kml_features(filepath)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to parse KML: {e}")
                return {'CANCELLED'}

            type_counts = {}
            for f in features:
                t = f['type']
                type_counts[t] = type_counts.get(t, 0) + 1

            summary = ", ".join(f"{k}: {v}" for k, v in type_counts.items())
            self.report({'INFO'}, f"Parsed {len(features)} placemarks: {summary}")
            return {'FINISHED'}


    class FSAE_OT_ClearVirtualisedCones(bpy.types.Operator):
        """Delete previously generated virtualised cones and collections"""
        bl_idname = "fsae.clear_cones"
        bl_label = "Clear Virtualised Cones"
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            settings = context.scene.fsae_track_settings
            col_name = settings.master_collection_name

            if col_name in bpy.data.collections:
                master_col = bpy.data.collections[col_name]
                # Gather all objects in collection recursively
                all_objs = collect_collection_meshes(master_col)
                for obj in list(master_col.all_objects):
                    all_objs.add(obj)

                # Delete objects
                for obj in all_objs:
                    bpy.data.objects.remove(obj, do_unlink=True)

                # Recursively remove sub-collections
                def remove_col_recursive(col):
                    for child in list(col.children):
                        remove_col_recursive(child)
                    bpy.data.collections.remove(col)

                remove_col_recursive(master_col)
                self.report({'INFO'}, f"Removed collection '{col_name}' and its contents.")
            else:
                self.report({'INFO'}, f"No existing collection named '{col_name}' found.")

            return {'FINISHED'}


    class FSAE_OT_ClearModelTemplates(bpy.types.Operator):
        """Delete the hidden template collection used to cache imported cone models.

        Placed cones are copies whose mesh data is shared with these template objects,
        but the *objects* themselves are only ever needed while placing cones - if left
        in the scene, some exporters (FBX, glTF, etc.) still include hidden objects,
        so they can silently show up in exported files. Removing just the template
        objects (not their mesh data) cleans this up without affecting already-placed cones.
        """
        bl_idname = "fsae.clear_model_templates"
        bl_label = "Clear Model Templates"
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            col_name = "_FSAE_Model_Templates"

            if col_name not in bpy.data.collections:
                self.report({'INFO'}, "No model template collection found.")
                return {'FINISHED'}

            template_col = bpy.data.collections[col_name]
            template_objs = list(template_col.objects)

            for obj in template_objs:
                bpy.data.objects.remove(obj, do_unlink=True)

            bpy.data.collections.remove(template_col)

            self.report({'INFO'}, f"Removed {len(template_objs)} template object(s) and the '{col_name}' collection.")
            return {'FINISHED'}


    class FSAE_OT_VirtualiseTrack(bpy.types.Operator):
        """Translate GPS KML coordinates and place 3D cones onto track in Blender"""
        bl_idname = "fsae.virtualise_track"
        bl_label = "Virtualise Track Cones"
        bl_options = {'REGISTER', 'UNDO'}

        def execute(self, context):
            settings = context.scene.fsae_track_settings
            kml_path = bpy.path.abspath(settings.kml_filepath)

            if not kml_path or not os.path.exists(kml_path):
                self.report({'ERROR'}, "Please specify a valid KML file path.")
                return {'CANCELLED'}

            # 1. Parse KML
            try:
                features = parse_kml_features(kml_path)
            except Exception as e:
                self.report({'ERROR'}, f"Error reading KML file: {e}")
                return {'CANCELLED'}

            if not features:
                self.report({'WARNING'}, "No placemarks found in the specified KML file.")
                return {'CANCELLED'}

            # 2. Determine GPS reference point
            ref_feature = None
            if settings.ref_mode == 'AUTO':
                for f in features:
                    if f['name'].lower() in {"reference_position", "reference_position-0", "reference"}:
                        ref_feature = f
                        break
                if ref_feature is None:
                    # Fallback to first if auto not found
                    ref_feature = features[0]
                    self.report({'WARNING'}, f"No 'reference_position' found. Using '{ref_feature['name']}' as reference.")
            elif settings.ref_mode == 'MANUAL_NAME':
                target_name = settings.custom_ref_name.strip()
                for f in features:
                    if f['name'] == target_name:
                        ref_feature = f
                        break
                if ref_feature is None:
                    self.report({'ERROR'}, f"Placemark '{target_name}' not found in KML.")
                    return {'CANCELLED'}
            else:
                ref_feature = features[0]

            ref_lat = ref_feature['lat']
            ref_lon = ref_feature['lon']
            ref_alt = ref_feature['alt']
            ref_x, ref_y, ref_z = settings.origin_cartesian

            # 3. Setup Target Track Meshes for Raycasting
            target_mesh_names = None
            if settings.use_raycast:
                mode = settings.raycast_target_mode
                if mode == 'SELECTED':
                    selected_mesh_names = {
                        o.name for o in context.selected_objects
                        if o.type == 'MESH'
                    }
                    if selected_mesh_names:
                        target_mesh_names = selected_mesh_names
                    else:
                        self.report({'WARNING'}, "Target mode is 'Selected Objects' but no mesh is selected. Raycasting against any surface.")
                elif mode == 'COLLECTION':
                    if settings.target_collection:
                        target_mesh_names = {
                            o.name for o in collect_collection_meshes(settings.target_collection)
                        }
                    elif settings.track_target.strip() in bpy.data.collections:
                        col = bpy.data.collections[settings.track_target.strip()]
                        target_mesh_names = {o.name for o in collect_collection_meshes(col)}
                    else:
                        self.report({'WARNING'}, "No valid collection specified. Raycasting against any surface.")
                elif mode == 'OBJECT':
                    if settings.target_object and settings.target_object.type == 'MESH':
                        target_mesh_names = {settings.target_object.name}
                    elif settings.track_target.strip() in bpy.data.objects:
                        obj = bpy.data.objects[settings.track_target.strip()]
                        if obj.type == 'MESH':
                            target_mesh_names = {obj.name}
                    else:
                        self.report({'WARNING'}, "No valid object specified. Raycasting against any surface.")
                elif mode == 'ANY':
                    target_mesh_names = None

            # 4. Prepare Cone Template Object / Mesh (default, used when a cone type has no override)
            default_template_obj = None
            default_is_custom_model = False
            model_path = bpy.path.abspath(settings.model_filepath)

            if settings.template_scene_object and settings.template_scene_object.type == 'MESH':
                default_template_obj = settings.template_scene_object
                default_is_custom_model = True
            elif model_path and os.path.exists(model_path):
                try:
                    imported_objs = load_model_template(model_path)
                    if imported_objs:
                        default_template_obj = imported_objs[0]
                        default_is_custom_model = True
                except Exception as e:
                    self.report({'WARNING'}, f"Could not load custom 3D model ({e}). Using generated fallback mesh.")

            fallback_mesh = None

            # Per cone-type overrides: orange_cone/purple_cone/left_cone/right_cone can each use
            # their own model or scene object, falling back to the default template above.
            cone_type_overrides = {
                "orange_cone": settings.orange_cone_settings,
                "purple_cone": settings.purple_cone_settings,
                "left_cone": settings.left_cone_settings,
                "right_cone": settings.right_cone_settings,
            }
            template_cache = {}

            def resolve_template_for_type(c_type_key):
                key = c_type_key.lower()
                if key in template_cache:
                    return template_cache[key]

                tmpl_obj = None
                is_custom = False
                cfg = cone_type_overrides.get(key)
                if cfg is not None:
                    if cfg.template_scene_object and cfg.template_scene_object.type == 'MESH':
                        tmpl_obj = cfg.template_scene_object
                        is_custom = True
                    else:
                        cfg_model_path = bpy.path.abspath(cfg.model_filepath) if cfg.model_filepath else ""
                        if cfg_model_path and os.path.exists(cfg_model_path):
                            try:
                                imported_objs = load_model_template(cfg_model_path)
                                if imported_objs:
                                    tmpl_obj = imported_objs[0]
                                    is_custom = True
                            except Exception as e:
                                self.report({'WARNING'}, f"Could not load model for '{c_type_key}' ({e}). Using default template.")

                if tmpl_obj is None:
                    tmpl_obj = default_template_obj
                    is_custom = default_is_custom_model

                template_cache[key] = (tmpl_obj, is_custom)
                return template_cache[key]

            # 5. Create Master and Sub-collections
            master_col = get_or_create_collection(settings.master_collection_name)
            sub_collections = {}

            # Objects to ignore during raycasting (templates, existing virtualised cones)
            ignored_names = set()
            if "_FSAE_Model_Templates" in bpy.data.collections:
                for o in bpy.data.collections["_FSAE_Model_Templates"].all_objects:
                    ignored_names.add(o.name)
            if settings.master_collection_name in bpy.data.collections:
                for o in bpy.data.collections[settings.master_collection_name].all_objects:
                    ignored_names.add(o.name)
            for scene_obj in (
                settings.template_scene_object,
                settings.orange_cone_settings.template_scene_object,
                settings.purple_cone_settings.template_scene_object,
                settings.left_cone_settings.template_scene_object,
                settings.right_cone_settings.template_scene_object,
            ):
                if scene_obj is not None:
                    ignored_names.add(scene_obj.name)

            # 5b. Chicane Alignment Vectors: left_cone/right_cone placemarks sharing the same
            # "_<group>" suffix (e.g. left_cone-3_2, right_cone-1_2) form one chicane set. Fit
            # the best-fit 2D direction line through all of that group's world positions (the
            # principal axis of the point cloud), so every cone in the set can be yawed to face
            # along it, aligning them in a line instead of each getting its own random spin.
            chicane_positions = {}
            for f in features:
                if f['type'].lower() not in {"left_cone", "right_cone"} or f.get('group') is None:
                    continue
                x_rel, y_rel, _ = gps_to_cartesian(f['lat'], f['lon'], f['alt'], ref_lat, ref_lon, ref_alt)
                chicane_positions.setdefault(f['group'], []).append((f['idx'], x_rel + ref_x, y_rel + ref_y))

            chicane_headings = {}
            for group_id, pts in chicane_positions.items():
                if len(pts) < 2:
                    continue
                mean_x = sum(p[1] for p in pts) / len(pts)
                mean_y = sum(p[2] for p in pts) / len(pts)
                sxx = sum((p[1] - mean_x) ** 2 for p in pts)
                syy = sum((p[2] - mean_y) ** 2 for p in pts)
                sxy = sum((p[1] - mean_x) * (p[2] - mean_y) for p in pts)
                # Principal axis angle of the point cloud's 2x2 covariance matrix. This axis is
                # undirected (equally valid pointing +-180 deg), so a KML recorded in the
                # opposite direction around the track would otherwise flip every heading.
                angle = 0.5 * math.atan2(2 * sxy, sxx - syy)
                axis = Vector((math.cos(angle), math.sin(angle), 0.0))

                # Anchor the sign to the lowest-idx cone in the set (e.g. left_cone-1_1 over
                # left_cone-2_1) so the heading is always "as seen from" that cone, regardless
                # of which direction the KML placemarks were recorded in.
                anchor_idx, anchor_x, anchor_y = min(pts, key=lambda p: p[0])
                ref_dx = mean_x - anchor_x
                ref_dy = mean_y - anchor_y
                if axis.x * ref_dx + axis.y * ref_dy < 0:
                    axis = -axis
                chicane_headings[group_id] = axis

            # 6. Filter & Place Cones
            valid_prefixes = {"orange_cone", "purple_cone", "blue_cone", "yellow_cone", "small_orange_cone", "big_orange_cone", "left_cone", "right_cone"}
            placed_count = 0
            depsgraph = context.evaluated_depsgraph_get()

            for f in features:
                name = f['name']
                c_type = f['type']

                # Skip reference placemark itself if desired or filter
                if c_type.lower().startswith("reference_position"):
                    continue
                if settings.filter_features and c_type.lower() not in valid_prefixes:
                    continue

                # Convert coordinates
                x_rel, y_rel, z_rel = gps_to_cartesian(f['lat'], f['lon'], f['alt'], ref_lat, ref_lon, ref_alt)
                world_x = x_rel + ref_x
                world_y = y_rel + ref_y
                world_z = z_rel + ref_z

                # Get / create subcollection per cone type
                if c_type not in sub_collections:
                    sub_collections[c_type] = get_or_create_collection(c_type.replace('_', ' ').title(), parent_collection=master_col)
                target_col = sub_collections[c_type]

                # Instantiate cone (copy the type-specific template, preserving materials/hierarchy,
                # or create from the procedural fallback mesh)
                template_obj, is_custom_model = resolve_template_for_type(c_type)
                if template_obj is not None:
                    cone_obj = template_obj.copy()
                    cone_obj.name = name
                    target_col.objects.link(cone_obj)
                else:
                    if fallback_mesh is None:
                        fallback_mesh = create_fallback_cone_mesh()
                    cone_obj = bpy.data.objects.new(name, fallback_mesh)
                    target_col.objects.link(cone_obj)

                # Apply scale
                cone_obj.scale = settings.scale_vector

                # Material handling: apply FSAE colored material if fallback mesh, or if user explicitly requested material override
                if (not is_custom_model) or settings.apply_fsae_materials:
                    mat = get_default_cone_material(c_type)
                    if mat:
                        if not is_custom_model:
                            # Assign directly for procedural cones
                            if cone_obj.data.materials:
                                cone_obj.data.materials[0] = mat
                            else:
                                cone_obj.data.materials.append(mat)
                        elif settings.apply_fsae_materials:
                            # For custom models, only override if materials are empty
                            if not cone_obj.material_slots:
                                cone_obj.data.materials.append(mat)

                # Raycast snapping
                placed_z = world_z
                hit_normal = Vector((0.0, 0.0, 1.0))
                hit_found = False
                hit_loc = None

                if settings.use_raycast:
                    # Start ray high enough above world_z and reference origin
                    start_z = max(settings.ray_start_height, world_z + 500.0, ref_z + 500.0)
                    hit, hit_loc, hit_norm, hit_obj = raycast_downward_surface(
                        depsgraph=depsgraph,
                        scene=context.scene,
                        x=world_x,
                        y=world_y,
                        start_z=start_z,
                        max_distance=10000.0,
                        valid_target_names=target_mesh_names,
                        ignored_names=ignored_names,
                    )

                    if hit and hit_loc is not None:
                        hit_found = True
                        hit_normal = hit_norm
                    elif settings.miss_fallback == 'ZERO':
                        placed_z = 0.0

                # Determine final rotation before computing the bbox offset below, so the offset
                # reflects the actual orientation. Compose everything on top of the template's
                # existing rotation rather than replacing it, so any import axis-correction isn't
                # lost (this was previously causing cones to end up rotated 90 degrees).
                # matrix_basis (not rotation_euler/rotation_quaternion) is used here since it is
                # always correct regardless of the template's rotation_mode (e.g. AXIS_ANGLE was
                # silently ignored before, discarding the template's upright orientation).
                base_quat = cone_obj.matrix_basis.to_quaternion()
                final_quat = base_quat

                if settings.align_to_normal and hit_found:
                    align_quat = Vector((0.0, 0.0, 1.0)).rotation_difference(hit_normal)
                    final_quat = align_quat @ final_quat

                # Small random spin around the cone's final "up" direction (world Z, or the
                # surface normal once aligned) applied outermost, so placement looks less
                # uniform. Spinning about a LOCAL mesh axis instead would only look like a
                # vertical yaw if the template's local Z happens to already be "up" - for
                # models whose import rotation maps local Z elsewhere, it looked like the
                # cone was tumbling around a horizontal (e.g. Y) axis instead.
                # Left/right cones mark a chicane: if they belong to a "_<group>" chicane set
                # with a fitted heading, yaw them to face along it instead of spinning randomly
                # so the set lines up; only orange/purple (or ungrouped left/right) cones fall
                # back to the random spin.
                is_chicane_type = c_type.lower() in {"left_cone", "right_cone"}
                chicane_heading = chicane_headings.get(f.get('group')) if is_chicane_type else None
                yaw_axis = hit_normal if (settings.align_to_normal and hit_found) else Vector((0.0, 0.0, 1.0))

                if chicane_heading is not None:
                    # Add a 180 deg flip on top of the fitted heading - the anchor-relative
                    # axis above otherwise points left/right cones the wrong way round.
                    target_yaw = math.atan2(chicane_heading.y, chicane_heading.x) + math.pi
                    final_quat = Quaternion(yaw_axis, target_yaw) @ final_quat
                elif settings.random_yaw_range > 0.0 and not is_chicane_type:
                    yaw_angle = random.uniform(-settings.random_yaw_range, settings.random_yaw_range)
                    final_quat = Quaternion(yaw_axis, yaw_angle) @ final_quat

                cone_obj.rotation_mode = 'QUATERNION'
                cone_obj.rotation_quaternion = final_quat

                if settings.use_raycast and hit_found:
                    # cone_obj was just copied/linked this iteration, so its matrix_world is not
                    # yet evaluated - force an update or the scale/rotation set above won't be
                    # reflected, throwing off the bbox offset below.
                    context.view_layer.update()
                    bbox_min_z = min((cone_obj.matrix_world @ Vector(corner)).z for corner in cone_obj.bound_box)
                    placed_z = hit_loc.z - bbox_min_z + settings.z_offset

                cone_obj.location = (world_x, world_y, placed_z)
                ignored_names.add(cone_obj.name)

                placed_count += 1

            self.report({'INFO'}, f"Successfully placed {placed_count} cones in collection '{settings.master_collection_name}'.")
            return {'FINISHED'}


    # =========================================================================
    # UI PANEL
    # =========================================================================

    class FSAE_PT_TrackVirtualiserPanel(bpy.types.Panel):
        """Sidebar Panel for FSAE Track Virtualiser"""
        bl_label = "FSAE Track Virtualiser"
        bl_idname = "FSAE_PT_track_virtualiser"
        bl_space_type = 'VIEW_3D'
        bl_region_type = 'UI'
        bl_category = "FSAE Track"

        def draw(self, context):
            layout = self.layout
            settings = context.scene.fsae_track_settings

            # 1. KML & Model Files
            box = layout.box()
            box.label(text="Input Files & Templates", icon='FILE_FOLDER')
            box.prop(settings, "kml_filepath")
            
            row = box.row(align=True)
            row.operator("fsae.inspect_kml", text="Inspect KML Placemarks", icon='VIEWZOOM')

            box.separator()
            box.prop(settings, "model_filepath")
            box.prop(settings, "template_scene_object")
            box.prop(settings, "apply_fsae_materials")

            box.separator()
            box.label(text="Per Cone-Type Overrides (optional)", icon='OUTLINER_OB_MESH')
            for label, prop_name in (
                ("Orange Cone", "orange_cone_settings"),
                ("Purple Cone", "purple_cone_settings"),
                ("Left Cone", "left_cone_settings"),
                ("Right Cone", "right_cone_settings"),
            ):
                sub = box.box()
                sub.label(text=label)
                cone_settings = getattr(settings, prop_name)
                sub.prop(cone_settings, "model_filepath")
                sub.prop(cone_settings, "template_scene_object")

            # 2. GPS Reference & Origin Alignment
            box = layout.box()
            box.label(text="GPS & Track Alignment", icon='ORIENTATION_GLOBAL')
            box.prop(settings, "ref_mode")
            if settings.ref_mode == 'MANUAL_NAME':
                box.prop(settings, "custom_ref_name")
            box.prop(settings, "origin_cartesian")

            # 3. Raycasting & Track Surface Snapping
            box = layout.box()
            box.label(text="Surface Raycast Snapping", icon='SNAP_ON')
            box.prop(settings, "use_raycast")
            if settings.use_raycast:
                box.prop(settings, "raycast_target_mode")
                if settings.raycast_target_mode == 'ANY':
                    box.label(text="Snaps to any mesh surface below cone", icon='INFO')
                elif settings.raycast_target_mode == 'SELECTED':
                    box.label(text="Snaps to objects selected in 3D Viewport", icon='RESTRICT_SELECT_OFF')
                elif settings.raycast_target_mode == 'COLLECTION':
                    box.prop(settings, "target_collection")
                    box.prop(settings, "track_target", text="Or by Name")
                elif settings.raycast_target_mode == 'OBJECT':
                    box.prop(settings, "target_object")
                    box.prop(settings, "track_target", text="Or by Name")

                row = box.row()
                row.prop(settings, "z_offset")
                row.prop(settings, "align_to_normal")

                box.prop(settings, "ray_start_height")
                box.prop(settings, "miss_fallback")

            # 4. Transform & Filters
            box = layout.box()
            box.label(text="Cone Settings & Organization", icon='MOD_SOLIDIFY')
            box.prop(settings, "scale_vector")
            box.prop(settings, "random_yaw_range")
            box.prop(settings, "filter_features")
            box.prop(settings, "master_collection_name")

            # 5. Action Operators
            layout.separator()
            col = layout.column(align=True)
            col.scale_y = 1.5
            col.operator("fsae.virtualise_track", text="Virtualise Track / Place Cones", icon='PLAY')
            
            layout.separator()
            layout.operator("fsae.clear_cones", text="Clear Virtualised Cones", icon='TRASH')
            layout.operator("fsae.clear_model_templates", text="Clear Model Templates (before export)", icon='TRASH')


    # =========================================================================
    # REGISTRATION
    # =========================================================================

    classes = (
        FSAEConeTypeSettings,
        FSAETrackVirtualiserSettings,
        FSAE_OT_InspectKml,
        FSAE_OT_ClearVirtualisedCones,
        FSAE_OT_ClearModelTemplates,
        FSAE_OT_VirtualiseTrack,
        FSAE_PT_TrackVirtualiserPanel,
    )

    def register():
        for cls in classes:
            bpy.utils.register_class(cls)
        bpy.types.Scene.fsae_track_settings = PointerProperty(type=FSAETrackVirtualiserSettings)


    def unregister():
        for cls in reversed(classes):
            bpy.utils.unregister_class(cls)
        del bpy.types.Scene.fsae_track_settings


if __name__ == "__main__":
    if bpy is not None:
        register()
