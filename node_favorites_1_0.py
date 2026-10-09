bl_info = {
    "name": "Node Favorites",
    "author": "eikonic",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "Node Editors > Sidebar > Node Favorites",
    "description": "Editor window shelf for saving and recalling favorite single nodes, or groups of nodes.",
    "category": "Node",
}

import colorsys
import json
import os
import random
import textwrap
import textwrap
import threading
import urllib.request
from contextlib import contextmanager

import bpy
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy.types import Operator, Panel, UIList, PropertyGroup, AddonPreferences
from bpy.props import (
    StringProperty,
    IntProperty,
    BoolProperty,
    FloatVectorProperty,
    CollectionProperty,
    EnumProperty,
)

ADDON_ID = __package__ or __name__

# (node idname, settings string)
DEFAULT_FAVORITES = (
    ("ShaderNodeValToRGB", ""),
    ("ShaderNodeMath", "operation=ADD"),
    ("ShaderNodeMixShader", ""),
)

# Nodes that can't be meaningfully created from a bare idname.
EXCLUDED = {
    "ShaderNodeGroup", "ShaderNodeCustomGroup",
    "GeometryNodeGroup", "GeometryNodeCustomGroup",
    "CompositorNodeGroup", "CompositorNodeCustomGroup",
    # Zone nodes must come as linked input/output pairs, so they aren't offered.
    "GeometryNodeSimulationInput", "GeometryNodeSimulationOutput",
    "GeometryNodeRepeatInput", "GeometryNodeRepeatOutput",
    "GeometryNodeForeachGeometryElementInput", "GeometryNodeForeachGeometryElementOutput",
    "GeometryNodeClosureInput", "GeometryNodeClosureOutput",
}

# Supported node editors: tree type -> (label, preference flag)
TREE_TYPES = {
    "ShaderNodeTree": ("Shader Editor", "editor_shader"),
    "GeometryNodeTree": ("Geometry Nodes", "editor_geometry"),
    "CompositorNodeTree": ("Compositor", "editor_compositor"),
}

# Node idname prefixes that belong in each editor (creation is verified at runtime).
TREE_PREFIXES = {
    "ShaderNodeTree": ("ShaderNode",),
    "GeometryNodeTree": ("GeometryNode", "FunctionNode", "ShaderNode"),
    "CompositorNodeTree": ("CompositorNode", "ShaderNode"),
}

# Extra node idnames per editor that don't follow the prefixes.
EXTRA_NODES = {
    "GeometryNodeTree": ("NodeGroupInput", "NodeGroupOutput"),
}

# Nodes with a "mode" that gets its own list entries (Math: Power, ...).
# Value = the main enum property. Mix is special-cased (data_type + blend_type).
MODE_PROPS = {
    "ShaderNodeMath": "operation",
    "ShaderNodeVectorMath": "operation",
    "ShaderNodeMapRange": "interpolation_type",
    "ShaderNodeClamp": "clamp_type",
    "ShaderNodeMix": None,
    "FunctionNodeBooleanMath": "operation",
    "CompositorNodeMath": "operation",
    "CompositorNodeMixRGB": "blend_type",
}

# Classes each editor uses for "sort by class". The default order is the order listed;
# the user can change it per editor in the add-on preferences.
CATEGORIES = {
    "ShaderNodeTree": (
        "input_node", "output_node", "shader_node", "texture_node",
        "color_node", "vector_node", "converter_node", "script_node",
    ),
    "GeometryNodeTree": (
        "input_node", "output_node", "geometry_node", "attribute_node",
        "texture_node", "color_node", "vector_node", "converter_node",
        "interface_node",
    ),
    "CompositorNodeTree": (
        "input_node", "output_node", "color_node", "converter_node",
        "vector_node", "filter_node", "distor_node", "matte_node",
    ),
}

# tree type -> (order list property, selected-row property) on the preferences
ORDER_PROPS = {
    "ShaderNodeTree": ("category_order_shader", "category_index_shader"),
    "GeometryNodeTree": ("category_order_geometry", "category_index_geometry"),
    "CompositorNodeTree": ("category_order_compositor", "category_index_compositor"),
}

ORDER_LABELS = {
    "ShaderNodeTree": "Shader Nodes",
    "GeometryNodeTree": "Geometry Nodes",
    "CompositorNodeTree": "Compositor Nodes",
}

CATEGORY_LABELS = {
    "input_node": "Input",
    "output_node": "Output",
    "shader_node": "Shader",
    "geometry_node": "Geometry",
    "attribute_node": "Attribute",
    "texture_node": "Texture",
    "color_node": "Color",
    "vector_node": "Vector",
    "converter_node": "Converter",
    "filter_node": "Filter",
    "distor_node": "Distort",
    "matte_node": "Matte",
    "pattern_node": "Pattern",
    "layout_node": "Layout",
    "interface_node": "Group Input/Output",
    "group_node": "Group",
    "script_node": "Script",
}


# ------------------------------------------------------------------------
# Preferences (global, shared by all files if prefs are saved)
# ------------------------------------------------------------------------

# ------------------------------------------------------------------------
# Disk storage (survives uninstall/update and doesn't need Auto-Save Prefs)
# ------------------------------------------------------------------------

_loading = False  # suppresses saving while we load from disk


def storage_path():
    """.../blender/node_favorites/favorites.json (shared by all Blender versions)."""
    root = os.path.dirname(bpy.utils.resource_path("USER"))
    return os.path.join(root, "node_favorites", "favorites.json")


@contextmanager
def suspend_saving():
    global _loading
    prev = _loading
    _loading = True
    try:
        yield
    finally:
        _loading = prev


def _slot_str(value):
    try:
        n = int(value)
    except Exception:
        n = 0
    return str(n) if 0 <= n <= 10 else "0"


def _item_entry(i):
    entry = {
        "tree_type": i.tree_type,
        "node_id": i.node_id,
        "settings": i.settings,
        "is_group": i.is_group,
        "name": i.name,
        "slot": int(i.slot),
        "color": [round(c, 4) for c in i.color],
    }
    if i.is_group:
        entry["preset"] = json.loads(i.preset)  # raises if broken; callers skip the item
        entry["description"] = i.description
    return entry


def build_data(p, nodes=True, groups=True, settings=True):
    """Everything to save. nodes/groups/settings choose what goes into an export."""
    items = []
    for i in p.items:
        if (i.is_group and not groups) or (not i.is_group and not nodes):
            continue
        try:
            items.append(_item_entry(i))
        except Exception:
            continue
    data = {
        "version": 2,
        "includes": {"nodes": bool(nodes), "groups": bool(groups)},
        "items": items,
        "category_order": {
            tree_type: [c.key for c in order_collection(p, tree_type)]
            for tree_type in TREE_TYPES
        },
        "editors": {
            tree_type: bool(getattr(p, attr))
            for tree_type, (_label, attr) in TREE_TYPES.items()
        },
        "settings": {
            "check_updates": bool(p.check_updates),
            "group_color_mode": p.group_color_mode,
            "group_default_color": [round(c, 4) for c in p.group_default_color],
            "group_last_color": [round(c, 4) for c in p.group_last_color],
        },
        "shortcuts": {
            "enabled": p.use_shortcuts,
            "group_modifier": p.group_modifier,
            "keys": list(_last_node_keys or (_kmi_cfg or {}).get("keys") or DEFAULT_KEYS),
        },
    }
    if not settings:
        for key in ("category_order", "editors", "settings", "shortcuts"):
            del data[key]
    return data


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def save_to_disk(p=None):
    if _loading or not _ready:
        return
    p = p or prefs()
    if p is None:
        return
    try:
        write_json(storage_path(), build_data(p))
    except Exception as exc:
        print(f"Node Favorites: could not save favorites: {exc}")


def parse_data(data):
    """(items, orders, editors) from a favorites file (format v1 or v2).
    orders maps each editor's tree type to its list of class keys."""
    if "items" in data:
        items = data.get("items", [])
        order = data.get("category_order", {})
    else:  # version 1 was shader-only
        block = data.get("shader", {})
        items = [
            dict(e, tree_type="ShaderNodeTree")
            for e in block.get("items", []) if isinstance(e, dict)
        ]
        order = block.get("category_order", [])

    orders = {}
    for tree_type in TREE_TYPES:
        if isinstance(order, dict):
            keys = order.get(tree_type, [])
        elif isinstance(order, list):  # older single combined list: keep its order
            keys = order
        else:
            keys = []
        orders[tree_type] = [
            k for k in keys if isinstance(k, str) and k in CATEGORIES[tree_type]
        ]

    editors = data.get("editors", {})
    return (
        [e for e in items if isinstance(e, dict)],
        orders,
        editors if isinstance(editors, dict) else {},
    )


def _entry_key(e):
    tree = e.get("tree_type") or "ShaderNodeTree"
    if e.get("is_group"):
        return ("G", tree, e.get("name", ""), json.dumps(e.get("preset"), sort_keys=True))
    return ("N", tree, e.get("node_id", ""), e.get("settings", ""))


def append_entries(p, entries, skip_existing):
    """Add file entries to the favorites. Returns how many were added."""
    seen = set()
    used = set()
    for i in p.items:
        if skip_existing:
            try:
                seen.add(_entry_key(_item_entry(i)))
            except Exception:
                pass
        if i.slot != "0":
            used.add((i.tree_type, i.is_group, i.slot))

    added = 0
    with suspend_saving():
        for e in entries:
            tree = e.get("tree_type") or "ShaderNodeTree"
            is_group = bool(e.get("is_group", False))
            if tree not in TREE_TYPES:
                continue
            preset = e.get("preset")
            if is_group:
                if not isinstance(preset, dict) or not preset.get("nodes"):
                    continue
            elif not e.get("node_id"):
                continue
            key = _entry_key(e)
            if key in seen:
                continue
            seen.add(key)

            item = p.items.add()
            item.node_id = str(e.get("node_id", ""))
            item.settings = str(e.get("settings", ""))
            item.is_group = is_group      # before slot: slot choices depend on it
            item.tree_type = tree
            slot = _slot_str(e.get("slot", 0))
            if slot != "0" and (tree, is_group, slot) in used:
                slot = "0"                 # already taken: keep the existing owner
            if slot != "0":
                used.add((tree, is_group, slot))
            item.slot = slot
            item.name = str(e.get("name", ""))
            if is_group:
                item.preset = json.dumps(preset)
                item.description = str(e.get("description", ""))
            try:
                item.color = tuple(e.get("color", (0.5, 0.5, 0.5)))[:3]
            except Exception:
                pass
            added += 1
    return added


def apply_prefs_data(p, orders, editors, settings, is_import=False):
    """Apply class orders, editor toggles and settings from a favorites file.
    On import, anything the file doesn't contain is left as it is."""
    with suspend_saving():
        for tree_type in TREE_TYPES:
            keys = orders.get(tree_type, [])
            if is_import and not keys:
                continue
            coll = order_collection(p, tree_type)
            coll.clear()
            for key in keys:
                coll.add().key = key

        for tree_type, (_label, attr) in TREE_TYPES.items():
            if isinstance(editors.get(tree_type), bool):
                setattr(p, attr, editors[tree_type])

        if not isinstance(settings, dict):
            settings = {}
        if isinstance(settings.get("check_updates"), bool):
            p.check_updates = settings["check_updates"]
        if settings.get("group_color_mode") in ("DEFAULT", "RANDOM", "LAST"):
            p.group_color_mode = settings["group_color_mode"]

        default_color = _valid_color(settings.get("group_default_color"))
        last_color = _valid_color(settings.get("group_last_color"))
        if default_color:
            p.group_default_color = default_color
        elif not is_import:
            p.group_default_color = theme_converter_color()
        if last_color:
            p.group_last_color = last_color
        elif not is_import:
            p.group_last_color = tuple(p.group_default_color)

        ensure_category_order(p)


def load_from_disk(p):
    """Replace the in-memory lists with the file's contents. False if no usable file."""
    path = storage_path()
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        items, order, editors = parse_data(raw)
        settings = raw.get("settings", {})
    except Exception as exc:
        print(f"Node Favorites: could not read favorites: {exc}")
        return False

    with suspend_saving():
        p.items.clear()
        append_entries(p, items, skip_existing=False)

        apply_prefs_data(p, order, editors, settings)

        p.active_index = 0
        p.seeded = True
    return True


def _color_updated(self, context):
    # Preset colors are user-chosen, so persist them when edited.
    if self.is_group:
        save_to_disk()


def _editors_updated(self, context):
    save_to_disk()


def _valid_color(value):
    """(r, g, b) from a saved list, or None if it isn't a usable color."""
    try:
        color = tuple(float(c) for c in value)[:3]
        if len(color) == 3 and all(0.0 <= c <= 1.0 for c in color):
            return color
    except Exception:
        pass
    return None


def theme_converter_color(context=None):
    """The theme's Converter node color (the starting Default Color for groups)."""
    context = context or bpy.context
    try:
        return tuple(context.preferences.themes[0].node_editor.converter_node)[:3]
    except Exception:
        return (0.5, 0.5, 0.5)


def new_group_color(p):
    """Color the new-group dialog opens with, per the Group Colors mode."""
    if p.group_color_mode == "RANDOM":
        h = random.random()
        s = random.uniform(0.45, 0.8)
        v = random.uniform(0.6, 0.85)
        return colorsys.hsv_to_rgb(h, s, v)
    if p.group_color_mode == "LAST":
        return tuple(p.group_last_color)
    return tuple(p.group_default_color)


# ------------------------------------------------------------------------
# Shortcuts: slots 1-10 for nodes, the same keys + a modifier for groups
# ------------------------------------------------------------------------

SLOT_OP_ID = "node_favorites.slot_insert"
DEFAULT_KEYS = [
    "ONE", "TWO", "THREE", "FOUR", "FIVE",
    "SIX", "SEVEN", "EIGHT", "NINE", "ZERO",
]
KEY_NAMES = dict(zip(DEFAULT_KEYS, "1234567890"))

_ready = False           # True once favorites are loaded (saving earlier could overwrite them)
_kmi_cfg = None          # shortcut config read from disk at startup
_last_node_keys = None   # last seen node-shortcut keys (to propagate to groups)
_last_modifier = None    # last applied group modifier
_saved_state = None      # last shortcut state written to disk
_addon_keymaps = []      # (keymap, item) pairs we registered
_slot_items_old = []     # keep replaced enum item lists alive (Blender holds the strings)


def key_name(key_type):
    if key_type in KEY_NAMES:
        return KEY_NAMES[key_type]
    try:
        return bpy.types.KeyMapItem.bl_rna.properties["type"].enum_items[key_type].name
    except Exception:
        return key_type.replace("_", " ").title()


def shortcut_label(kmi, slot, full=False):
    if kmi is None:
        return str(slot % 10)
    mods = []
    if kmi.ctrl:
        mods.append("Ctrl" if full else "Ct")
    if kmi.alt:
        mods.append("Alt" if full else "Al")
    if kmi.shift:
        mods.append("Shift" if full else "Sh")
    return "+".join(mods + [key_name(kmi.type)])


def _build_slot_items(kmis, is_group):
    items = [("0", "-", "No shortcut")]
    for s in range(1, 11):
        kmi = kmis.get((is_group, s))
        items.append((
            str(s),
            shortcut_label(kmi, s),
            "Shortcut: " + shortcut_label(kmi, s, full=True),
        ))
    return items


_slot_items = {
    False: _build_slot_items({}, False),
    True: _build_slot_items({}, True),
}


def _refresh_slot_labels(kmis):
    for is_group in (False, True):
        new = _build_slot_items(kmis, is_group)
        if new != _slot_items[is_group]:
            _slot_items_old.append(_slot_items[is_group])
            _slot_items[is_group] = new


def _slot_items_cb(self, context):
    return _slot_items[bool(self.is_group)]


def _slot_updated(self, context):
    """One favorite per slot (per section): taking a slot frees it elsewhere."""
    if _loading:
        return
    p = prefs()
    if p is None:
        return
    with suspend_saving():
        if self.slot != "0":
            me = self.as_pointer()
            for other in p.items:
                if (
                    other.as_pointer() != me
                    and other.is_group == self.is_group
                    and other.tree_type == self.tree_type
                    and other.slot == self.slot
                ):
                    other.slot = "0"
    save_to_disk(p)


def user_slot_kmis():
    """{(is_group, slot): keymap item} from the user keyconfig (what the keymap editor shows)."""
    out = {}
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.user if wm else None
    km = kc.keymaps.get("Node Editor") if kc else None
    if km is None:
        return out
    for kmi in km.keymap_items:
        if kmi.idname == SLOT_OP_ID:
            try:
                out[(bool(kmi.properties.is_group), int(kmi.properties.slot))] = kmi
            except Exception:
                pass
    return out


def read_shortcut_config():
    try:
        with open(storage_path(), "r", encoding="utf-8") as f:
            block = json.load(f).get("shortcuts", {})
    except Exception:
        block = {}
    return shortcut_config_from(block)


def shortcut_config_from(block):
    cfg = {"enabled": True, "group_modifier": "SHIFT", "keys": list(DEFAULT_KEYS)}
    if not isinstance(block, dict):
        return cfg
    valid = {i.identifier for i in bpy.types.KeyMapItem.bl_rna.properties["type"].enum_items}
    if isinstance(block.get("enabled"), bool):
        cfg["enabled"] = block["enabled"]
    if block.get("group_modifier") in ("SHIFT", "CTRL", "ALT"):
        cfg["group_modifier"] = block["group_modifier"]
    keys = block.get("keys")
    if isinstance(keys, list) and len(keys) == 10:
        cfg["keys"] = [k if k in valid else DEFAULT_KEYS[i] for i, k in enumerate(keys)]
    return cfg


def _mod_flags(modifier):
    return int(modifier == "SHIFT"), int(modifier == "CTRL"), int(modifier == "ALT")


def register_keymaps(cfg):
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon if wm else None
    if kc is None:
        return
    km = kc.keymaps.new(name="Node Editor", space_type="NODE_EDITOR")
    shift, ctrl, alt = _mod_flags(cfg["group_modifier"])
    for is_group in (False, True):
        for s in range(1, 11):
            kmi = km.keymap_items.new(
                SLOT_OP_ID, type=cfg["keys"][s - 1], value="PRESS",
                shift=shift if is_group else 0,
                ctrl=ctrl if is_group else 0,
                alt=alt if is_group else 0,
            )
            kmi.properties.slot = s
            kmi.properties.is_group = is_group
            _addon_keymaps.append((km, kmi))


def unregister_keymaps():
    for km, kmi in _addon_keymaps:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _addon_keymaps.clear()


def _poll_shortcuts():
    """Runs every ~1.5s: keeps group keys in step with node keys, refreshes
    badge labels, and saves shortcut changes (wherever they were edited)."""
    global _last_node_keys, _last_modifier, _saved_state
    p = prefs()
    kmis = user_slot_kmis()
    if p is None or not kmis:
        return 1.5

    node_keys = [
        kmis[(False, s)].type if (False, s) in kmis else DEFAULT_KEYS[s - 1]
        for s in range(1, 11)
    ]

    # Group shortcuts follow the node keys when a node key changes.
    if _last_node_keys is None:
        _last_node_keys = list(node_keys)
    for s in range(1, 11):
        if node_keys[s - 1] != _last_node_keys[s - 1] and (True, s) in kmis:
            kmis[(True, s)].type = node_keys[s - 1]
    _last_node_keys = list(node_keys)

    # Apply a changed group modifier to all group shortcuts.
    if _last_modifier is None:
        _last_modifier = p.group_modifier
    elif p.group_modifier != _last_modifier:
        shift, ctrl, alt = _mod_flags(p.group_modifier)
        for (is_group, _s), kmi in kmis.items():
            if is_group:
                kmi.shift, kmi.ctrl, kmi.alt = shift, ctrl, alt
        _last_modifier = p.group_modifier

    _refresh_slot_labels(kmis)

    state = (p.use_shortcuts, p.group_modifier, tuple(node_keys))
    if _saved_state is None:
        _saved_state = state
    elif state != _saved_state:
        _saved_state = state
        save_to_disk(p)
    return 1.5


def apply_imported_shortcuts(p, raw):
    """Apply the shortcut settings (on/off, group modifier, the 10 keys) from an import."""
    block = raw.get("shortcuts") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        return
    cfg = shortcut_config_from(block)
    with suspend_saving():
        if isinstance(block.get("enabled"), bool):
            p.use_shortcuts = cfg["enabled"]
        if block.get("group_modifier") in ("SHIFT", "CTRL", "ALT"):
            p.group_modifier = cfg["group_modifier"]
        keys = block.get("keys")
        if isinstance(keys, list) and len(keys) == 10:
            kmis = user_slot_kmis()
            for s in range(1, 11):
                for is_group in (False, True):
                    kmi = kmis.get((is_group, s))
                    if kmi is not None:
                        kmi.type = cfg["keys"][s - 1]
        _poll_shortcuts()


def _shortcut_pref_updated(self, context):
    _poll_shortcuts()


class NF_Item(PropertyGroup):
    node_id: StringProperty(name="Node Type")
    settings: StringProperty(name="Mode Settings")   # "prop=VALUE;prop=VALUE"
    is_group: BoolProperty(default=False)             # True = multi-node preset
    name: StringProperty(name="Name")                 # preset name
    preset: StringProperty()                          # preset data (JSON)
    description: StringProperty(name="Description")   # optional group description (tooltip)
    tree_type: StringProperty(default="ShaderNodeTree")   # which editor it belongs to
    slot: EnumProperty(
        name="Shortcut",
        description="Keyboard shortcut slot for this favorite",
        items=_slot_items_cb,
        update=_slot_updated,
    )
    # Copy of the theme color for this node's category (see refresh_colors).
    color: FloatVectorProperty(
        name="Color", subtype="COLOR_GAMMA", size=3,
        min=0.0, max=1.0, default=(0.5, 0.5, 0.5),
        update=_color_updated,
    )


# ------------------------------------------------------------------------
# Update check (GitHub releases). Shown in the add-on preferences only.
# ------------------------------------------------------------------------

# GitHub "owner/repo" used for update checks. If emptied, update checks are switched off.
UPDATE_REPO = "eikonic/NodeFavorites"

_update = {"state": "idle", "latest": "", "url": ""}  # idle/checking/current/available/failed/offline


def current_version():
    return tuple(bl_info["version"])


def parse_version(tag):
    """'v1.7.0' -> (1, 7, 0); None if it isn't a version number."""
    try:
        return tuple(int(part) for part in str(tag).strip().lstrip("vV").split("."))
    except Exception:
        return None


def _update_worker():
    try:
        request = urllib.request.Request(
            f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
            headers={"User-Agent": "NodeFavorites", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.load(response)
        tag = str(data.get("tag_name", ""))
        latest = parse_version(tag)
        _update["latest"] = tag
        _update["url"] = data.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases"
        if latest is None:
            _update["state"] = "failed"
        else:
            _update["state"] = "available" if latest > current_version() else "current"
    except Exception:
        _update["state"] = "failed"


def _redraw_when_done():
    if _update["state"] == "checking":
        return 0.5
    wm = bpy.context.window_manager
    for window in (wm.windows if wm else []):
        for area in window.screen.areas:
            area.tag_redraw()
    return None


def start_update_check():
    """Look for a newer release in the background; never blocks Blender."""
    if not UPDATE_REPO or _update["state"] == "checking":
        return
    if not getattr(bpy.app, "online_access", True):
        _update["state"] = "offline"  # respect Blender's "Allow Online Access" setting
        return
    _update["state"] = "checking"
    threading.Thread(target=_update_worker, daemon=True).start()
    bpy.app.timers.register(_redraw_when_done, first_interval=0.5)


def draw_wrapped(context, layout, text, padding=60):
    """Label text wrapped to the region width (Blender labels don't wrap by themselves)."""
    ui = context.preferences.system.ui_scale
    width = context.region.width if context.region else 600
    chars = max(20, int((width - padding * ui) / (5.0 * ui)))  # ~5 px per character per UI scale
    col = layout.column(align=True)
    col.scale_y = 0.85
    for line in textwrap.wrap(text, chars):
        col.label(text=line)


BUTTON_GAP = 0.6   # space between buttons in a row
ROW_GAP = 0.3      # gap (in UI units, ~1 = one button width) between end-of-row items
KEY_GAP = 0.15     # gap (in UI units) between the shortcut key fields


def gap_h(layout, size):
    """Exact horizontal gap inside an aligned row (size in UI units)."""
    sp = layout.row()
    sp.ui_units_x = size
    sp.label(text="")


def gap_v(layout, size):
    """Exact vertical gap inside an aligned column (size = fraction of a row height)."""
    sp = layout.row()
    sp.scale_y = size
    sp.label(text="")


def line_separator(layout):
    try:
        layout.separator(type="LINE")
    except TypeError:  # older Blender without separator types
        layout.separator()


class NF_Category(PropertyGroup):
    key: StringProperty()


class NF_Preferences(AddonPreferences):
    bl_idname = ADDON_ID

    items: CollectionProperty(type=NF_Item)
    active_index: IntProperty(default=0)
    seeded: BoolProperty(default=False)
    tab: EnumProperty(
        items=[
            ("NODES", "Nodes", "Single node favorites"),
            ("GROUPS", "Groups", "Multi-node groups"),
        ],
        default="NODES",
    )
    category_order_shader: CollectionProperty(type=NF_Category)
    category_index_shader: IntProperty(default=0)
    category_order_geometry: CollectionProperty(type=NF_Category)
    category_index_geometry: IntProperty(default=0)
    category_order_compositor: CollectionProperty(type=NF_Category)
    category_index_compositor: IntProperty(default=0)
    check_updates: BoolProperty(
        name="Check on startup",
        description="Look for a newer version on GitHub when Blender starts",
        default=True,
        update=_editors_updated,
    )
    editor_shader: BoolProperty(name="Shader Editor", default=True, update=_editors_updated)
    editor_geometry: BoolProperty(name="Geometry Nodes", default=True, update=_editors_updated)
    editor_compositor: BoolProperty(name="Compositor", default=False, update=_editors_updated)
    use_shortcuts: BoolProperty(
        name="Use Shortcuts",
        description="Enable keyboard shortcuts for favorites",
        default=True,
        update=_shortcut_pref_updated,
    )
    group_color_mode: EnumProperty(
        name="Group Color Mode",
        description="Which color the new-group dialog starts with",
        items=[
            ("DEFAULT", "Default", "New groups start with the Default Color swatch"),
            ("RANDOM", "Random per New Group", "New groups start with a new random color"),
            ("LAST", "Use Last", "New groups start with the color used for the previous group"),
        ],
        default="DEFAULT",
        update=_editors_updated,
    )
    group_default_color: FloatVectorProperty(
        name="Default Color", description="Starting color for new groups in Default mode",
        subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0,
        default=(0.5, 0.5, 0.5), update=_editors_updated,
    )
    group_last_color: FloatVectorProperty(
        name="Last Color", description="Color used for the most recently added group",
        subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0,
        default=(0.5, 0.5, 0.5),
    )
    group_modifier: EnumProperty(
        name="Group Modifier",
        description="Modifier key that makes a shortcut insert a group instead of a node",
        items=[("SHIFT", "Shift", ""), ("CTRL", "Ctrl", ""), ("ALT", "Alt", "")],
        default="SHIFT",
        update=_shortcut_pref_updated,
    )

    def draw(self, context):
        layout = self.layout

        # Editors the favorites panel is used in
        row = layout.row()
        for _tree_type, (_label, attr) in TREE_TYPES.items():
            row.prop(self, attr)

        line_separator(layout)

        # Save file
        col = layout.column(align=True)
        col.label(text="Preferences JSON")
        col.label(text=storage_path(), icon="FILE")
        row = layout.row()
        row.operator("node_favorites.export_json", text="Export...", icon="EXPORT")
        row.separator(factor=BUTTON_GAP)
        row.operator("node_favorites.import_json", text="Import...", icon="IMPORT")
        row.separator(factor=BUTTON_GAP)
        op = row.operator("wm.path_open", text="Open Folder", icon="FILE_FOLDER")
        op.filepath = os.path.dirname(storage_path())

        # Group colors
        box = layout.box()
        box.label(text="New Group Color-Mode:")
        row = box.row()
        first = row.row(align=True)   # Default button + its swatch stay together
        first.prop_enum(self, "group_color_mode", "DEFAULT")
        sub = first.row(align=True)
        sub.ui_units_x = 2.5
        sub.prop(self, "group_default_color", text="")
        row.separator(factor=BUTTON_GAP)
        row.prop_enum(self, "group_color_mode", "RANDOM")
        row.separator(factor=BUTTON_GAP)
        row.prop_enum(self, "group_color_mode", "LAST")

        # Shortcuts
        box = layout.box()
        box.prop(self, "use_shortcuts")
        draw_wrapped(
            context, box,
            "Shortcut keys can be set for 10 favorite entries in the Favorites Panel. "
            "The default keys are 0 through 9, with a modifier key. "
            "Regular shortcut keys call a Node Favorite, "
            "and the modifier shortcut calls a Group Favorite.",
        )
        if self.use_shortcuts:
            box.label(text="Groups Modifier Key:")
            row = box.row()
            for n, mod in enumerate(("SHIFT", "CTRL", "ALT")):
                if n:
                    row.separator(factor=BUTTON_GAP)
                row.prop_enum(self, "group_modifier", mod)

            box.label(text="Shortcut Favorites Keys:")
            col = box.column(align=True)
            kmis = user_slot_kmis()
            for start in (1, 6):
                if start != 1:
                    gap_v(col, KEY_GAP)
                row = col.row(align=True)
                for s in range(start, start + 5):
                    if s != start:
                        gap_h(row, KEY_GAP)
                    kmi = kmis.get((False, s))
                    if kmi is not None:
                        row.prop(kmi, "type", text="", event=True)
                    else:
                        row.label(text="-")
            box.label(text="Also editable under Keymap > Node Editor.")

        # Class order for "sort by class": one column per enabled editor, side by side
        enabled = [
            tree_type for tree_type, (_label, attr) in TREE_TYPES.items()
            if getattr(self, attr)
        ]
        if enabled:
            box = layout.box()
            box.label(text="Node Class order for Class Sorting Option:")
            cols = box.row()
            for tree_type in enabled:
                order_prop, index_prop = ORDER_PROPS[tree_type]
                coll = getattr(self, order_prop)

                col = cols.column()
                col.label(text=ORDER_LABELS[tree_type])
                col.template_list(
                    "NF_UL_Categories", tree_type, self, order_prop, self, index_prop,
                    rows=max(3, min(9, len(coll))),
                )
                row = col.row(align=True)
                op = row.operator("node_favorites.category_move", text="", icon="TRIA_UP")
                op.tree_type, op.direction = tree_type, -1
                op = row.operator("node_favorites.category_move", text="", icon="TRIA_DOWN")
                op.tree_type, op.direction = tree_type, 1
                op = row.operator("node_favorites.category_reset", text="", icon="LOOP_BACK")
                op.tree_type = tree_type

        # Version / update status (only once a GitHub repo has been set)
        if UPDATE_REPO:
            line_separator(layout)
            row = layout.row(align=True)
            row.label(text="Version " + ".".join(str(n) for n in current_version()))
            row.prop(self, "check_updates")
            row.operator("node_favorites.check_update", text="Check Now", icon="FILE_REFRESH")

            state = _update["state"]
            if state == "available":
                row = layout.row()
                row.label(text=f"Update available: {_update['latest']}", icon="INFO")
                op = row.operator("wm.url_open", text="Download", icon="URL")
                op.url = _update["url"]
            elif state == "current":
                layout.label(text="You have the latest version.", icon="CHECKMARK")
            elif state == "checking":
                layout.label(text="Checking for updates...")
            elif state == "failed":
                layout.label(text="Couldn't check for updates.", icon="ERROR")
            elif state == "offline":
                layout.label(
                    text="Online access is off (Preferences > System > Network).",
                    icon="ERROR",
                )


def prefs(context=None):
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    return addon.preferences if addon else None


# ------------------------------------------------------------------------
# Labels, modes, colors
# ------------------------------------------------------------------------

def node_label(node_id):
    rna = getattr(bpy.types, node_id, None)
    if rna is not None:
        try:
            return rna.bl_rna.name
        except Exception:
            pass
    return node_id


def parse_settings(s):
    out = []
    for pair in (s or "").split(";"):
        key, sep, value = pair.partition("=")
        if sep:
            out.append((key, value))
    return out


def apply_settings(node, settings):
    for key, value in parse_settings(settings):
        try:
            setattr(node, key, value)
        except Exception:
            pass


def capture_settings(node):
    """Mode settings string for a node (empty if it has no modes)."""
    node_id = node.bl_idname
    if node_id == "ShaderNodeMix":
        if node.data_type == "RGBA":
            return f"data_type=RGBA;blend_type={node.blend_type}"
        return f"data_type={node.data_type}"
    prop = MODE_PROPS.get(node_id)
    if prop:
        return f"{prop}={getattr(node, prop)}"
    return ""


def mode_entries(node_id):
    """[(settings string, mode name)] for a node that has modes."""
    props = getattr(bpy.types, node_id).bl_rna.properties
    if node_id == "ShaderNodeMix":
        out = []
        for dt in props["data_type"].enum_items:
            if dt.identifier == "RGBA":
                for bl in props["blend_type"].enum_items:
                    out.append((f"data_type=RGBA;blend_type={bl.identifier}", bl.name))
            else:
                out.append((f"data_type={dt.identifier}", dt.name))
        return out
    prop = MODE_PROPS[node_id]
    return [(f"{prop}={it.identifier}", it.name) for it in props[prop].enum_items]


def settings_label(node_id, settings):
    base = node_label(node_id)
    pairs = dict(parse_settings(settings))
    if not pairs:
        return base
    if node_id == "ShaderNodeMix":
        key = "blend_type" if "blend_type" in pairs else "data_type"
    else:
        key = MODE_PROPS.get(node_id)
    if key not in pairs:
        return base
    try:
        rna = getattr(bpy.types, node_id).bl_rna
        name = rna.properties[key].enum_items[pairs[key]].name
    except Exception:
        name = pairs[key].replace("_", " ").title()
    return f"{base}: {name}"


def item_label(item):
    if item.is_group:
        return item.name or "Group"
    return settings_label(item.node_id, item.settings)


# Blender doesn't expose a node's header-color class to Python, so map by name.
_INPUT = {
    "ShaderNodeAmbientOcclusion", "ShaderNodeAttribute", "ShaderNodeBevel",
    "ShaderNodeCameraData", "ShaderNodeVertexColor", "ShaderNodeFresnel",
    "ShaderNodeNewGeometry", "ShaderNodeHairInfo", "ShaderNodeLayerWeight",
    "ShaderNodeLightPath", "ShaderNodeObjectInfo", "ShaderNodeParticleInfo",
    "ShaderNodePointInfo", "ShaderNodeRGB", "ShaderNodeTangent",
    "ShaderNodeTexCoord", "ShaderNodeUVMap", "ShaderNodeValue",
    "ShaderNodeVolumeInfo", "ShaderNodeWireframe",
}
_OUTPUT = {
    "ShaderNodeOutputMaterial", "ShaderNodeOutputWorld",
    "ShaderNodeOutputLight", "ShaderNodeOutputLineStyle", "ShaderNodeOutputAOV",
}
_COLOR = {
    "ShaderNodeBrightContrast", "ShaderNodeGamma", "ShaderNodeHueSaturation",
    "ShaderNodeInvert", "ShaderNodeLightFalloff", "ShaderNodeMix",
    "ShaderNodeMixRGB", "ShaderNodeRGBCurve",
}
_VECTOR = {
    "ShaderNodeBump", "ShaderNodeDisplacement", "ShaderNodeMapping",
    "ShaderNodeNormal", "ShaderNodeNormalMap", "ShaderNodeVectorCurve",
    "ShaderNodeVectorDisplacement", "ShaderNodeVectorRotate",
    "ShaderNodeVectorTransform",
}
_SHADER = {
    "ShaderNodeEmission", "ShaderNodeBackground", "ShaderNodeHoldout",
    "ShaderNodeMixShader", "ShaderNodeAddShader", "ShaderNodeSubsurfaceScattering",
    "ShaderNodeVolumeAbsorption", "ShaderNodeVolumeScatter",
    "ShaderNodeVolumePrincipled", "ShaderNodeEeveeSpecular",
}


_GEO_INPUT = {
    "GeometryNodeObjectInfo", "GeometryNodeCollectionInfo", "GeometryNodeImageInfo",
    "GeometryNodeIsViewport", "GeometryNodeSelfObject",
}
_GEO_ATTRIBUTE = {
    "GeometryNodeCaptureAttribute", "GeometryNodeStoreNamedAttribute",
    "GeometryNodeRemoveAttribute", "GeometryNodeAttributeStatistic",
    "GeometryNodeAttributeDomainSize", "GeometryNodeBlurAttribute",
    "GeometryNodeSampleIndex", "GeometryNodeSampleNearest",
    "GeometryNodeSampleNearestSurface", "GeometryNodeSampleUVSurface",
    "GeometryNodeSampleCurve", "GeometryNodeProximity", "GeometryNodeRaycast",
    "GeometryNodeAccumulateField", "GeometryNodeFieldAtIndex",
    "GeometryNodeFieldOnDomain", "GeometryNodeEvaluateAtIndex",
    "GeometryNodeEvaluateOnDomain",
}
_COMP_INPUT = {
    "CompositorNodeRLayers", "CompositorNodeImage", "CompositorNodeMovieClip",
    "CompositorNodeMask", "CompositorNodeValue", "CompositorNodeRGB",
    "CompositorNodeTime", "CompositorNodeTrackPos", "CompositorNodeTexture",
    "CompositorNodeBokehImage", "CompositorNodeSceneTime",
}
_COMP_OUTPUT = {
    "CompositorNodeComposite", "CompositorNodeViewer", "CompositorNodeOutputFile",
    "CompositorNodeSplit", "CompositorNodeLevels",
}
_COMP_MATTE = {
    "CompositorNodeKeying", "CompositorNodeColorSpill", "CompositorNodeDoubleEdgeMask",
    "CompositorNodeCryptomatte", "CompositorNodeCryptomatteV2",
    "CompositorNodeBoxMask", "CompositorNodeEllipseMask",
}
_COMP_DISTORT = {
    "CompositorNodeCrop", "CompositorNodeFlip", "CompositorNodeRotate",
    "CompositorNodeScale", "CompositorNodeTransform", "CompositorNodeTranslate",
    "CompositorNodeMovieDistortion", "CompositorNodeLensdist", "CompositorNodeDisplace",
    "CompositorNodeMapUV", "CompositorNodeCornerPin", "CompositorNodePlaneTrackDeform",
    "CompositorNodeStabilize", "CompositorNodeStabilize2D",
}
_COMP_FILTER = {
    "CompositorNodeBlur", "CompositorNodeBilateralblur", "CompositorNodeDBlur",
    "CompositorNodeVecBlur", "CompositorNodeDilateErode", "CompositorNodeFilter",
    "CompositorNodeGlare", "CompositorNodeInpaint", "CompositorNodeDespeckle",
    "CompositorNodeDenoise", "CompositorNodeSunBeams", "CompositorNodePixelate",
    "CompositorNodeDefocus", "CompositorNodeBokehBlur", "CompositorNodeAntiAliasing",
    "CompositorNodeKuwahara",
}
_COMP_COLOR = {
    "CompositorNodeAlphaOver", "CompositorNodeColorBalance", "CompositorNodeColorCorrection",
    "CompositorNodeGamma", "CompositorNodeHueCorrect", "CompositorNodeHueSat",
    "CompositorNodeInvert", "CompositorNodeMixRGB", "CompositorNodeCurveRGB",
    "CompositorNodeBrightContrast", "CompositorNodeTonemap", "CompositorNodeZcombine",
    "CompositorNodePosterize", "CompositorNodeExposure",
}
_COMP_VECTOR = {
    "CompositorNodeNormal", "CompositorNodeMapValue", "CompositorNodeMapRange",
    "CompositorNodeNormalize", "CompositorNodeCurveVec",
}


def _geometry_attr(node_id):
    if node_id in ("NodeGroupInput", "NodeGroupOutput"):
        return "interface_node"
    if node_id == "GeometryNodeViewer":
        return "output_node"
    if node_id.startswith("FunctionNode"):
        return "converter_node"
    if node_id in _GEO_ATTRIBUTE:
        return "attribute_node"
    if node_id.startswith(("GeometryNodeInput", "GeometryNodeTool")) or node_id in _GEO_INPUT:
        return "input_node"
    return "geometry_node"


def _compositor_attr(node_id):
    if node_id in _COMP_INPUT:
        return "input_node"
    if node_id in _COMP_OUTPUT:
        return "output_node"
    if "Matte" in node_id or node_id in _COMP_MATTE:
        return "matte_node"
    if node_id in _COMP_DISTORT:
        return "distor_node"
    if node_id in _COMP_FILTER:
        return "filter_node"
    if node_id in _COMP_COLOR:
        return "color_node"
    if node_id in _COMP_VECTOR:
        return "vector_node"
    return "converter_node"


def node_theme_attr(node_id):
    """Name of the Theme > Node Editor color property for this node's category.
    Blender doesn't expose this to Python, so it's a best-effort mapping by name."""
    if node_id.startswith("CompositorNode"):
        return _compositor_attr(node_id)
    if node_id.startswith(("GeometryNode", "FunctionNode")) or node_id in ("NodeGroupInput", "NodeGroupOutput"):
        return _geometry_attr(node_id)
    if node_id in _OUTPUT:
        return "output_node"
    if node_id in _INPUT:
        return "input_node"
    if node_id in _COLOR:
        return "color_node"
    if node_id in _VECTOR:
        return "vector_node"
    if node_id in _SHADER or "Bsdf" in node_id:
        return "shader_node"
    if node_id == "ShaderNodeScript":
        return "script_node"
    if node_id.startswith("ShaderNodeTex"):
        return "texture_node"
    return "converter_node"


def refresh_colors(context=None):
    """Copy current theme node colors into each single-node favorite."""
    context = context or bpy.context
    p = prefs(context)
    if p is None:
        return
    theme = context.preferences.themes[0].node_editor
    for item in p.items:
        if item.is_group:
            continue
        c = getattr(theme, node_theme_attr(item.node_id), None)
        if c is not None:
            item.color = tuple(c)[:3]


# ------------------------------------------------------------------------
# Node type discovery (cached)
# ------------------------------------------------------------------------

_node_types_cache = {}


def node_types(tree_type):
    """Creatable node idnames for an editor type. Computed once per type, then cached."""
    if tree_type in _node_types_cache:
        return _node_types_cache[tree_type]

    prefixes = TREE_PREFIXES.get(tree_type, ("ShaderNode",))
    candidates = set(EXTRA_NODES.get(tree_type, ()))

    # __subclasses__() misses built-in nodes, so scan every registered type.
    for name in dir(bpy.types):
        if not name.startswith(prefixes) or name in EXCLUDED:
            continue
        cls = getattr(bpy.types, name, None)
        try:
            if isinstance(cls, type) and issubclass(cls, bpy.types.Node):
                candidates.add(name)
        except TypeError:
            pass

    valid = []
    temp = bpy.data.node_groups.new("__NF_VALIDATE__", tree_type)
    try:
        for node_id in candidates:
            try:
                temp.nodes.remove(temp.nodes.new(node_id))
                valid.append(node_id)
            except Exception:
                pass
    finally:
        bpy.data.node_groups.remove(temp)

    _node_types_cache[tree_type] = sorted(valid, key=lambda n: node_label(n).lower())
    return _node_types_cache[tree_type]


_enum_cache = {}  # tree type -> items; must stay referenced or Blender shows garbage strings


def enum_items(self, context):
    tree_type = getattr(self, "tree_type", "") or "ShaderNodeTree"
    items = _enum_cache.get(tree_type)
    if items is None:
        items = []
        for node_id in node_types(tree_type):
            if node_id in MODE_PROPS:
                base = node_label(node_id)
                try:
                    for settings, name in mode_entries(node_id):
                        items.append((f"{node_id}|{settings}", f"{base}: {name}", ""))
                    continue
                except Exception:
                    pass
            items.append((node_id, node_label(node_id), ""))
        _enum_cache[tree_type] = items
    return items


# ------------------------------------------------------------------------
# Editor / list helpers
# ------------------------------------------------------------------------

def current_tree_type(context):
    """Tree type of the node editor we're in, if it's one we support."""
    space = context.space_data
    if space and space.type == "NODE_EDITOR" and space.tree_type in TREE_TYPES:
        return space.tree_type
    return None


def in_node_editor(context):
    """True in a supported node editor that's enabled in the add-on preferences."""
    tree_type = current_tree_type(context)
    p = prefs(context)
    return bool(tree_type and p and getattr(p, TREE_TYPES[tree_type][1], False))


def find_node_editor(context, tree_type):
    """Return (area, WINDOW region) of a node editor of this type, preferring the current area."""
    areas = []
    if context.area:
        areas.append(context.area)
    if context.screen:
        areas.extend(context.screen.areas)

    for area in areas:
        if area.type == "NODE_EDITOR" and area.spaces.active.tree_type == tree_type:
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region:
                return area, region
    return None, None


def add_favorite(context, node_id, settings="", tree_type=None):
    p = prefs(context)
    if p is None:
        return False
    tree_type = tree_type or current_tree_type(context) or "ShaderNodeTree"
    for i, item in enumerate(p.items):
        if (
            not item.is_group
            and item.tree_type == tree_type
            and item.node_id == node_id
            and item.settings == settings
        ):
            p.active_index = i
            return False
    item = p.items.add()
    item.node_id = node_id
    item.settings = settings
    item.tree_type = tree_type
    p.active_index = len(p.items) - 1
    refresh_colors(context)
    save_to_disk(p)
    return True


def active_item(context):
    p = prefs(context)
    if p and 0 <= p.active_index < len(p.items):
        return p.items[p.active_index]
    return None


# ------------------------------------------------------------------------
# Presets (multi-node setups: node types, modes, links, relative layout)
# ------------------------------------------------------------------------

def is_supported_node(node_id, tree_type):
    if node_id in EXCLUDED:
        return False
    if node_id == "NodeReroute":
        return True
    if node_id in EXTRA_NODES.get(tree_type, ()):
        return True
    return node_id.startswith(TREE_PREFIXES.get(tree_type, ("ShaderNode",)))


def preset_allowed(node, tree_type):
    return is_supported_node(node.bl_idname, tree_type)


def capture_preset(tree, selected, tree_type):
    """Return (json string, number of skipped nodes)."""
    nodes = [n for n in selected if preset_allowed(n, tree_type)]
    skipped = len(selected) - len(nodes)
    if not nodes:
        return "", skipped

    left = min(n.location.x for n in nodes)
    top = max(n.location.y for n in nodes)
    index = {n.name: i for i, n in enumerate(nodes)}

    data = {
        "nodes": [
            {
                "id": n.bl_idname,
                "settings": capture_settings(n),
                "dx": n.location.x - left,
                "dy": n.location.y - top,
            }
            for n in nodes
        ],
        "links": [
            [
                index[l.from_node.name], l.from_socket.identifier,
                index[l.to_node.name], l.to_socket.identifier,
            ]
            for l in tree.links
            if l.from_node.name in index and l.to_node.name in index
        ],
    }
    return json.dumps(data), skipped


def place_preset(space, data):
    """Create the preset's nodes at the editor cursor, all selected."""
    tree = space.edit_tree
    origin = space.cursor_location
    ox, oy = origin.x, origin.y

    for n in tree.nodes:
        n.select = False

    created = []
    for entry in data["nodes"]:
        node = tree.nodes.new(entry["id"])
        apply_settings(node, entry.get("settings", ""))
        node.location = (ox + entry["dx"], oy + entry["dy"])
        node.select = True
        created.append(node)

    for from_i, from_id, to_i, to_id in data["links"]:
        out = next((s for s in created[from_i].outputs if s.identifier == from_id), None)
        inp = next((s for s in created[to_i].inputs if s.identifier == to_id), None)
        if out and inp:
            tree.links.new(out, inp)

    if created:
        tree.nodes.active = created[0]


# ------------------------------------------------------------------------
# Operators
# ------------------------------------------------------------------------

class NF_OT_SearchAdd(Operator):
    """Search all nodes of this editor and add one to the favorites"""
    bl_idname = "node_favorites.search_add"
    bl_label = "Add Node to Favorites"
    bl_property = "node_type"

    node_type: EnumProperty(name="Node", items=enum_items)
    tree_type: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def invoke(self, context, event):
        self.tree_type = current_tree_type(context) or "ShaderNodeTree"
        context.window_manager.invoke_search_popup(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        node_id, _, settings = self.node_type.partition("|")
        if add_favorite(context, node_id, settings, self.tree_type or None):
            self.report({"INFO"}, f"Added {settings_label(node_id, settings)}")
        return {"FINISHED"}


class NF_OT_AddSelected(Operator):
    bl_idname = "node_favorites.add_selected"
    bl_label = "Add Selected Node(s)"
    bl_description = (
        "Add the selected node(s) to your favorites. "
        "One node goes in the Nodes tab (including its mode, e.g. Math: Power). "
        "Several nodes go in the Groups tab, keeping their connections"
    )

    name: StringProperty(name="Name", default="Group")
    desc: StringProperty(name="Description", description="Shown at the top of the group's tooltip")
    color: FloatVectorProperty(
        name="Color", subtype="COLOR_GAMMA", size=3,
        min=0.0, max=1.0, default=(0.5, 0.5, 0.5),
    )
    data: StringProperty(options={"HIDDEN", "SKIP_SAVE"})
    tree_type: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context):
        return in_node_editor(context) and bool(context.selected_nodes)

    def invoke(self, context, event):
        p = prefs(context)
        tree_type = current_tree_type(context)
        if p is None or tree_type is None:
            return {"CANCELLED"}

        selected = list(context.selected_nodes)
        supported = [n for n in selected if preset_allowed(n, tree_type)]
        if not supported:
            self.report({"WARNING"}, "Nothing to add (node groups, zones and frames aren't supported)")
            return {"CANCELLED"}

        # One node: add it to the Nodes tab, no dialog.
        if len(supported) == 1:
            node = supported[0]
            if node.bl_idname == "NodeReroute":
                self.report({"WARNING"}, "This node type can't be saved as a favorite")
                return {"CANCELLED"}
            add_favorite(context, node.bl_idname, capture_settings(node), tree_type)
            p.tab = "NODES"
            return {"FINISHED"}

        # Several nodes: ask for a name and color, then add to the Groups tab.
        data, skipped = capture_preset(context.space_data.edit_tree, selected, tree_type)
        if not data:
            return {"CANCELLED"}
        if skipped:
            self.report({"INFO"}, f"{skipped} unsupported node(s) skipped (frames, node groups, zones)")
        self.data = data
        self.tree_type = tree_type
        self.name = "Group"
        self.desc = ""
        self.color = new_group_color(p)
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        p = prefs(context)
        if p is None or not self.data:
            return {"CANCELLED"}
        item = p.items.add()
        item.is_group = True
        item.tree_type = self.tree_type or "ShaderNodeTree"
        item.name = self.name.strip() or "Group"
        item.preset = self.data
        item.description = self.desc.strip()
        item.color = self.color
        p.group_last_color = tuple(self.color)
        p.active_index = len(p.items) - 1
        p.tab = "GROUPS"
        save_to_disk(p)
        return {"FINISHED"}


class NF_OT_EditPreset(Operator):
    """Edit this group's name, description and color"""
    bl_idname = "node_favorites.edit_preset"
    bl_label = "Edit Group"

    index: IntProperty(default=-1, options={"HIDDEN"})
    name: StringProperty(name="Name")
    desc: StringProperty(name="Description", description="Shown at the top of the group's tooltip")
    color: FloatVectorProperty(
        name="Color", subtype="COLOR_GAMMA", size=3,
        min=0.0, max=1.0, default=(0.5, 0.5, 0.5),
    )

    def _item(self, context):
        p = prefs(context)
        if p and 0 <= self.index < len(p.items) and p.items[self.index].is_group:
            return p.items[self.index]
        return None

    def invoke(self, context, event):
        item = self._item(context)
        if item is None:
            return {"CANCELLED"}
        self.name = item.name
        self.desc = item.description
        self.color = item.color
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        item = self._item(context)
        if item is None:
            return {"CANCELLED"}
        item.name = self.name.strip() or "Group"
        item.description = self.desc.strip()
        item.color = self.color
        save_to_disk()
        return {"FINISHED"}


def insert_item(op, context, event, index):
    """Add favorite #index at the mouse and start the carry-until-click grab."""
    p = prefs(context)
    if p is None or not (0 <= index < len(p.items)):
        return {"CANCELLED"}
    p.active_index = index
    item = p.items[index]
    refresh_colors(context)

    area, region = find_node_editor(context, item.tree_type)
    if not area:
        op.report({"ERROR"}, "No matching node editor found")
        return {"CANCELLED"}
    space = area.spaces.active
    if space.edit_tree is None:
        op.report({"ERROR"}, "No editable shader node tree")
        return {"CANCELLED"}

    # Window-space mouse -> graph coordinates (works from the sidebar too).
    space.cursor_location_from_region(
        event.mouse_x - region.x,
        event.mouse_y - region.y,
    )

    with context.temp_override(area=area, region=region, space_data=space):
        try:
            if item.is_group:
                place_preset(space, json.loads(item.preset))
            else:
                bpy.ops.node.add_node("EXEC_DEFAULT", type=item.node_id, use_transform=False)
                node = space.edit_tree.nodes.active
                if node and item.settings:
                    apply_settings(node, item.settings)
        except Exception as exc:
            op.report({"ERROR"}, f"Could not add: {exc}")
            return {"CANCELLED"}
        # Same grab Blender's own add menu starts: nodes follow the
        # mouse until click (confirm) or Esc/right-click (cancel+delete).
        bpy.ops.node.translate_attach_remove_on_cancel("INVOKE_DEFAULT")

    return {"FINISHED"}


class NF_OT_Insert(Operator):
    """Add this node or group at the mouse and carry it until you click to drop it"""
    bl_idname = "node_favorites.insert"
    bl_label = "Insert Node"
    bl_options = {"UNDO"}

    index: IntProperty(default=-1)

    @classmethod
    def description(cls, context, properties):
        try:
            p = prefs(context)
            item = p.items[properties.index]
            if item.is_group:
                data = json.loads(item.preset)
                counts = {}
                for entry in data["nodes"]:
                    label = settings_label(entry["id"], entry.get("settings", ""))
                    counts[label] = counts.get(label, 0) + 1
                lines = []
                if item.description:
                    lines += textwrap.wrap(item.description, 60) + [""]
                for label, n in list(counts.items())[:15]:
                    lines.append(f"- {label}" + (f" x{n}" if n > 1 else ""))
                if len(counts) > 15:
                    lines.append("...")
                return "\n".join(lines)
            return f"Add {item_label(item)} at the mouse and carry it until you click to drop it"
        except Exception:
            return "Add this at the mouse and carry it until you click to drop it"

    @classmethod
    def poll(cls, context):
        return in_node_editor(context)

    def invoke(self, context, event):
        return insert_item(self, context, event, self.index)


class NF_OT_SlotInsert(Operator):
    """Insert the favorite assigned to this shortcut slot at the mouse"""
    bl_idname = SLOT_OP_ID
    bl_label = "Node Favorites: Insert Slot"
    bl_options = {"UNDO"}

    slot: IntProperty(default=1, min=1, max=10)
    is_group: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        p = prefs(context)
        return bool(p and p.use_shortcuts and in_node_editor(context))

    def invoke(self, context, event):
        p = prefs(context)
        tree_type = current_tree_type(context)
        for i, item in enumerate(p.items):
            if (
                item.is_group == self.is_group
                and item.tree_type == tree_type
                and item.slot == str(self.slot)
            ):
                return insert_item(self, context, event, i)
        return {"PASS_THROUGH"}  # nothing assigned: let the key do whatever else it does


class NF_OT_Remove(Operator):
    """Remove this favorite from the list"""
    bl_idname = "node_favorites.remove"
    bl_label = "Remove Favorite"

    index: IntProperty(default=-1, options={"HIDDEN", "SKIP_SAVE"})  # -1 = selected entry

    @classmethod
    def poll(cls, context):
        p = prefs(context)
        return bool(p and len(p.items))

    def execute(self, context):
        p = prefs(context)
        index = self.index if self.index >= 0 else p.active_index
        if not 0 <= index < len(p.items):
            return {"CANCELLED"}
        p.items.remove(index)
        if index < p.active_index:
            p.active_index -= 1
        p.active_index = min(p.active_index, max(0, len(p.items) - 1))
        save_to_disk(p)
        return {"FINISHED"}


class NF_OT_Move(Operator):
    bl_idname = "node_favorites.move"
    bl_label = "Move Favorite"

    direction: IntProperty()             # -1 up, 1 down
    to_end: BoolProperty(default=False)  # jump to top/bottom of its section

    @classmethod
    def poll(cls, context):
        return active_item(context) is not None

    def execute(self, context):
        p = prefs(context)
        old = p.active_index
        kind = p.items[old].is_group
        # Only move among entries of the same kind (nodes vs presets).
        tree = p.items[old].tree_type
        same = [
            i for i, it in enumerate(p.items)
            if it.is_group == kind and it.tree_type == tree
        ]
        pos = same.index(old)
        if self.to_end:
            new_pos = 0 if self.direction < 0 else len(same) - 1
        else:
            new_pos = pos + self.direction
        if 0 <= new_pos < len(same) and new_pos != pos:
            new = same[new_pos]
            p.items.move(old, new)
            p.active_index = new
            save_to_disk(p)
        return {"FINISHED"}


def order_collection(p, tree_type):
    return getattr(p, ORDER_PROPS[tree_type][0])


def get_category_order(p, tree_type):
    """User-defined class order for an editor, with any missing classes appended."""
    keys = [c.key for c in order_collection(p, tree_type)] if p else []
    keys += [k for k in CATEGORIES[tree_type] if k not in keys]
    return keys


def reset_category_order(p, tree_type):
    coll = order_collection(p, tree_type)
    coll.clear()
    for key in CATEGORIES[tree_type]:
        coll.add().key = key
    setattr(p, ORDER_PROPS[tree_type][1], 0)


def category_rank(item, order):
    if item.is_group:
        return 0
    attr = node_theme_attr(item.node_id)
    return order.index(attr) if attr in order else len(order)


class NF_OT_Sort(Operator):
    bl_idname = "node_favorites.sort"
    bl_label = "Sort Favorites"

    is_group: BoolProperty(default=False)   # which section to sort
    mode: EnumProperty(
        items=[("ALPHA", "A-Z", ""), ("CLASS", "Class", "")],
        default="ALPHA",
    )

    @classmethod
    def description(cls, context, properties):
        if properties.mode == "CLASS":
            return "Sort by node class in the order set in the add-on preferences, then A-Z"
        return "Sort A-Z"

    @classmethod
    def poll(cls, context):
        p = prefs(context)
        return bool(p and len(p.items) > 1)

    def execute(self, context):
        p = prefs(context)
        tree_type = current_tree_type(context)
        if tree_type is None:
            return {"CANCELLED"}
        order = get_category_order(p, tree_type)
        active = active_item(context)
        active_key = (active.node_id, active.settings, active.name, active.preset) if active else None

        records = [
            {
                "node_id": i.node_id, "settings": i.settings, "is_group": i.is_group,
                "name": i.name, "preset": i.preset, "label": item_label(i),
                "rank": category_rank(i, order), "color": tuple(i.color),
                "slot": i.slot, "tree_type": i.tree_type,
                "description": i.description,
            }
            for i in p.items
        ]
        def mine(r):
            return r["is_group"] == self.is_group and r["tree_type"] == tree_type

        target = [r for r in records if mine(r)]
        rest = [r for r in records if not mine(r)]

        if self.mode == "CLASS":
            target.sort(key=lambda r: (r["rank"], r["label"].lower()))
        else:
            target.sort(key=lambda r: r["label"].lower())
        records = rest + target

        new_active = 0
        with suspend_saving():
            p.items.clear()
            for n, r in enumerate(records):
                item = p.items.add()
                item.node_id = r["node_id"]
                item.settings = r["settings"]
                item.is_group = r["is_group"]   # before slot: slot choices depend on it
                item.tree_type = r["tree_type"]
                item.slot = r["slot"]
                item.name = r["name"]
                item.preset = r["preset"]
                item.description = r["description"]
                item.color = r["color"]   # keeps user-chosen preset colors
                if active_key == (r["node_id"], r["settings"], r["name"], r["preset"]):
                    new_active = n
        p.active_index = new_active
        refresh_colors(context)
        save_to_disk(p)
        return {"FINISHED"}


class NF_OT_CategoryMove(Operator):
    """Move the selected class up or down in the sort order"""
    bl_idname = "node_favorites.category_move"
    bl_label = "Move Class"

    tree_type: StringProperty()
    direction: IntProperty()

    @classmethod
    def poll(cls, context):
        return prefs(context) is not None

    def execute(self, context):
        p = prefs(context)
        if p is None or self.tree_type not in ORDER_PROPS:
            return {"CANCELLED"}
        coll = order_collection(p, self.tree_type)
        index_prop = ORDER_PROPS[self.tree_type][1]
        old = getattr(p, index_prop)
        new = old + self.direction
        if 0 <= old < len(coll) and 0 <= new < len(coll):
            coll.move(old, new)
            setattr(p, index_prop, new)
            save_to_disk(p)
        return {"FINISHED"}


class NF_OT_CategoryReset(Operator):
    """Restore the default class order for this editor"""
    bl_idname = "node_favorites.category_reset"
    bl_label = "Reset Class Order"

    tree_type: StringProperty()

    @classmethod
    def poll(cls, context):
        return prefs(context) is not None

    def execute(self, context):
        p = prefs(context)
        if p is None or self.tree_type not in ORDER_PROPS:
            return {"CANCELLED"}
        reset_category_order(p, self.tree_type)
        save_to_disk(p)
        return {"FINISHED"}


class NF_OT_CheckUpdate(Operator):
    """Check GitHub for a newer version"""
    bl_idname = "node_favorites.check_update"
    bl_label = "Check for Updates"

    def execute(self, context):
        start_update_check()
        return {"FINISHED"}


class NF_OT_Export(Operator, ExportHelper):
    """Export favorites, groups and addon preferences to a JSON file"""
    bl_idname = "node_favorites.export_json"
    bl_label = "Export Favorites"

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    export_nodes: BoolProperty(name="Export Nodes", default=True)
    export_groups: BoolProperty(name="Export Groups", default=True)
    export_settings: BoolProperty(name="Export Addon Preferences", default=True)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "export_nodes")
        layout.prop(self, "export_groups")
        layout.prop(self, "export_settings")

    def invoke(self, context, event):
        self.filepath = "node_favorites.json"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        p = prefs(context)
        if p is None:
            return {"CANCELLED"}
        if not (self.export_nodes or self.export_groups or self.export_settings):
            self.report({"WARNING"}, "Nothing selected to export")
            return {"CANCELLED"}
        try:
            write_json(
                self.filepath,
                build_data(p, self.export_nodes, self.export_groups, self.export_settings),
            )
        except Exception as exc:
            self.report({"ERROR"}, f"Could not export: {exc}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Exported to {self.filepath}")
        return {"FINISHED"}


class NF_OT_Import(Operator, ImportHelper):
    """Import favorites, groups and settings from a JSON file"""
    bl_idname = "node_favorites.import_json"
    bl_label = "Import Favorites"

    filename_ext = ".json"
    filter_glob: StringProperty(default="*.json", options={"HIDDEN"})
    mode: EnumProperty(
        name="Import Mode",
        items=[
            ("REPLACE", "Replace All Nodes", "Clear all node/group favorites and replace"),
            ("APPEND", "Append to Existing", "File's nodes/groups will be added to the existing lists"),
        ],
        default="REPLACE",
    )
    import_nodes: BoolProperty(name="Import Nodes", default=True)
    import_groups: BoolProperty(name="Import Groups", default=True)
    import_settings: BoolProperty(
        name="Import Addon Preferences",
        description="Also restore the settings saved in the file: class order, editors, "
                    "group colors and shortcuts",
        default=True,
    )

    def draw(self, context):
        layout = self.layout
        row = layout.row()
        row.prop_enum(self, "mode", "REPLACE")
        row.separator(factor=BUTTON_GAP)
        row.prop_enum(self, "mode", "APPEND")
        layout.separator()
        layout.prop(self, "import_nodes")
        layout.prop(self, "import_groups")
        layout.prop(self, "import_settings")

    def execute(self, context):
        p = prefs(context)
        if p is None:
            return {"CANCELLED"}
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                raw = json.load(f)
            items, order, editors = parse_data(raw)
            settings = raw.get("settings", {})
        except Exception as exc:
            self.report({"ERROR"}, f"Could not read file: {exc}")
            return {"CANCELLED"}

        if not (self.import_nodes or self.import_groups or self.import_settings):
            self.report({"WARNING"}, "Nothing selected to import")
            return {"CANCELLED"}
        items = [
            e for e in items
            if (self.import_groups if e.get("is_group") else self.import_nodes)
        ]
        replace = self.mode == "REPLACE"
        if replace:
            # Only clear the kinds the file contains (a nodes-only file keeps your groups).
            inc = raw.get("includes") if isinstance(raw, dict) else None
            inc = inc if isinstance(inc, dict) else {}
            clear_nodes = self.import_nodes and inc.get("nodes", True) is not False
            clear_groups = self.import_groups and inc.get("groups", True) is not False
            with suspend_saving():
                for n in range(len(p.items) - 1, -1, -1):
                    if (p.items[n].is_group and clear_groups) or (
                        not p.items[n].is_group and clear_nodes
                    ):
                        p.items.remove(n)
        added = append_entries(p, items, skip_existing=not replace)
        p.active_index = 0
        refresh_colors(context)
        if self.import_settings:
            apply_prefs_data(p, order, editors, settings, is_import=True)
            apply_imported_shortcuts(p, raw)
        save_to_disk(p)
        has_prefs = isinstance(raw, dict) and ("settings" in raw or "shortcuts" in raw)
        extra = " and preferences" if self.import_settings and has_prefs else ""
        self.report({"INFO"}, f"Imported {added} favorite(s){extra}")
        return {"FINISHED"}


# ------------------------------------------------------------------------
# UI
# ------------------------------------------------------------------------

class NF_UL_Base(UIList):
    show_groups = False  # subclasses choose which kind of entry they list

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        tree_type = current_tree_type(context)
        flags = [
            self.bitflag_filter_item
            if bool(i.is_group) == self.show_groups and i.tree_type == tree_type
            else 0
            for i in items
        ]
        return flags, []

    def draw_filter(self, context, layout):
        pass

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)

        # Single nodes: swatch of the node class color. Presets: user-chosen color.
        lead = row.row(align=True)
        lead.ui_units_x = 1.0
        lead.prop(item, "color", text="")

        op = row.operator(
            "node_favorites.insert",
            text=item_label(item),
            emboss=False,
        )
        op.index = index  # real collection index, even in a filtered list

        # Trailing items (shortcut dropdown, gear, X): an aligned row with exact-width spacers.
        tail = row.row(align=True)
        p = prefs(context)
        if p and p.use_shortcuts:
            sub = tail.row(align=True)
            sub.ui_units_x = 2.4
            sub.prop(item, "slot", text="")
            gap_h(tail, ROW_GAP)

        if item.is_group:
            op = tail.operator(
                "node_favorites.edit_preset",
                text="",
                icon="PREFERENCES",
                emboss=False,
            )
            op.index = index
            gap_h(tail, ROW_GAP)

        op = tail.operator(
            "node_favorites.remove",
            text="",
            icon="X",
            emboss=False,
        )
        op.index = index


class NF_UL_Nodes(NF_UL_Base):
    show_groups = False


class NF_UL_Presets(NF_UL_Base):
    show_groups = True


class NF_UL_Categories(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.label(text=CATEGORY_LABELS.get(item.key, item.key))


def draw_section(layout, context, is_group):
    p = prefs(context)
    tree_type = current_tree_type(context)
    count = (
        sum(1 for i in p.items if i.is_group == is_group and i.tree_type == tree_type)
        if p else 0
    )
    if not count:
        layout.label(text="No groups saved." if is_group else "No node favorites.")
        return

    active = active_item(context)
    owns = bool(active and active.is_group == is_group and active.tree_type == tree_type)
    row = layout.row()
    row.template_list(
        "NF_UL_Presets" if is_group else "NF_UL_Nodes",
        "", p, "items", p, "active_index",
        rows=max(3, min(12, count)),
    )

    col = row.column(align=True)

    # Acts on the selected entry, so greyed out if it's in the other tab.
    sub = col.column(align=True)
    sub.enabled = owns
    op = sub.operator("node_favorites.move", text="", icon="TRIA_UP")
    op.direction, op.to_end = -1, False
    op = sub.operator("node_favorites.move", text="", icon="TRIA_DOWN")
    op.direction, op.to_end = 1, False
    sub.separator()
    op = sub.operator("node_favorites.move", text="", icon="TRIA_UP_BAR")
    op.direction, op.to_end = -1, True
    op = sub.operator("node_favorites.move", text="", icon="TRIA_DOWN_BAR")
    op.direction, op.to_end = 1, True

    col.separator()
    op = col.operator("node_favorites.sort", text="", icon="SORTALPHA")
    op.is_group, op.mode = is_group, "ALPHA"
    if not is_group:
        op = col.operator("node_favorites.sort", text="", icon="COLOR")
        op.is_group, op.mode = False, "CLASS"



class NF_PT_Shelf(Panel):
    bl_idname = "NF_PT_shelf"
    bl_label = "Node Favorites"
    bl_space_type = "NODE_EDITOR"
    bl_region_type = "UI"
    bl_category = "Node Favorites"

    @classmethod
    def poll(cls, context):
        return in_node_editor(context)

    def draw(self, context):
        p = prefs(context)
        layout = self.layout

        col = layout.column(align=True)
        col.scale_y = 1.2
        col.operator("node_favorites.search_add", text="Search Node +", icon="VIEWZOOM")
        col.operator("node_favorites.add_selected", text="Add Selected Node(s)", icon="ADD")

        if p is None:
            return

        layout.separator()
        layout.row().prop(p, "tab", expand=True)
        draw_section(layout, context, p.tab == "GROUPS")


# ------------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------------

classes = (
    NF_Item,
    NF_Category,
    NF_Preferences,
    NF_OT_SearchAdd,
    NF_OT_AddSelected,
    NF_OT_EditPreset,
    NF_OT_Insert,
    NF_OT_SlotInsert,
    NF_OT_Export,
    NF_OT_Import,
    NF_OT_CheckUpdate,
    NF_OT_Remove,
    NF_OT_Move,
    NF_OT_Sort,
    NF_OT_CategoryMove,
    NF_OT_CategoryReset,
    NF_UL_Nodes,
    NF_UL_Presets,
    NF_UL_Categories,
    NF_PT_Shelf,
)


def ensure_category_order(p):
    """Make sure each editor's list holds all of its classes (new ones go at the end)."""
    for tree_type in TREE_TYPES:
        coll = order_collection(p, tree_type)
        have = {c.key for c in coll}
        for key in CATEGORIES[tree_type]:
            if key not in have:
                coll.add().key = key


def _seed_defaults():
    """Runs once shortly after enable; preferences may not exist during register()."""
    global _ready
    p = prefs()
    if p is None:
        return 0.2  # retry

    cfg = read_shortcut_config()
    loaded = load_from_disk(p)

    with suspend_saving():
        if p.use_shortcuts != cfg["enabled"]:
            p.use_shortcuts = cfg["enabled"]
        if p.group_modifier != cfg["group_modifier"]:
            p.group_modifier = cfg["group_modifier"]
        if not loaded:
            # First run: use whatever is already in preferences, else the defaults.
            if not p.seeded:
                if len(p.items) == 0:
                    for node_id, settings in DEFAULT_FAVORITES:
                        item = p.items.add()
                        item.node_id = node_id
                        item.settings = settings
                p.seeded = True
        if not loaded:
            p.group_default_color = theme_converter_color()
            p.group_last_color = tuple(p.group_default_color)
        ensure_category_order(p)

    _ready = True
    if not loaded:
        save_to_disk(p)
    refresh_colors()
    if p.check_updates:
        start_update_check()
    return None


def register():
    global _kmi_cfg
    for cls in classes:
        bpy.utils.register_class(cls)
    _kmi_cfg = read_shortcut_config()
    register_keymaps(_kmi_cfg)
    bpy.app.timers.register(_seed_defaults, first_interval=0.2)
    bpy.app.timers.register(_poll_shortcuts, first_interval=1.0)


def unregister():
    global _ready
    global _kmi_cfg, _last_node_keys, _last_modifier, _saved_state
    _node_types_cache.clear()
    _enum_cache.clear()
    _ready = False
    _kmi_cfg = _last_node_keys = _last_modifier = _saved_state = None
    _update.update(state="idle", latest="", url="")
    for fn in (_seed_defaults, _poll_shortcuts, _redraw_when_done):
        if bpy.app.timers.is_registered(fn):
            bpy.app.timers.unregister(fn)
    unregister_keymaps()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
