# Node Favorites
A Blender add-on that adds a shelf to the Shader, Geometry Nodes, or Compositor node editor sidebar for saving and recalling your favorite single nodes, or groups of connected nodes.  

## Features
- **Nodes tab:** save any node as a favorite. Nodes with modes can be saved once per mode (for example Math: Power, Math: Subtract,  Mix: Overlay, Mix: Screen).
- **Groups tab:** select several nodes and save them, with their connections, as a reusable group. Positions are not saved, and these are not native node groups.
 
- **Add Selected Node(s) button:** one selected node goes to the Nodes tab. Several selected nodes open a dialog where you set a name, description, and color for the new group.
 
- **Click to place:** click a favorite and it attaches to the cursor until you drop it in the graph, like Blender's own add-node menu.
- **Group tooltips:** hover over a group to see its description and the list of nodes it contains.
- **Colors:** each favorite has a color swatch. New groups can start from a default color, a random color, or the last color you used.  This option is set in the addon preferences.
- **Sorting:** sort A-Z, or by node class (the class order can be changed per editor in the add-on settings).
- **Reordering and removing:** use the arrow buttons to reorder, and the X on each row to remove an entry.
- **Keyboard shortcuts:** the top-row number keys `1`-`0` insert favorite nodes, and a modifier plus `1`-`0` (Shift by default) inserts groups. Each favorite shows its shortcut number. Keys and modifier can be changed in the add-on settings or in Blender's keymap.

- **Backup and restore:** export and import your nodes, groups, and add-on preferences as a JSON file, with options to export or import only some of them.
- **Persistent storage:** favorites are kept in a separate file, so they survive restarting Blender, updating the add-on, and uninstalling and reinstalling it.
- **Update check:** the add-on settings can check GitHub for a newer release (optional, and only if Blender's online access is enabled).

## Requirements
Blender 4.2 or newer.

## Installation
1. Download `node_favorites.py` from the latest [release](../../releases).
2. In Blender, open **Edit > Preferences > Add-ons**.
3. Use **Install from Disk** (in the dropdown at the top right) and choose `node_favorites.py`.
4. Enable **Node Favorites**.

## Usage
Open a node editor, press `N` to show the sidebar, and open the **Node Favorites** tab.

- To add a node: select it and click **Add Selected Node(s)**.
- To add a group of nodes: select several nodes and click **Add Selected Node(s)**, then fill in the dialog.
- To use a favorite: click it, move the mouse to where you want it, and click to drop it.

## Where favorites are saved
Favorites are saved to `node_favorites/favorites.json` in Blender's user configuration folder, shared by all Blender versions. The **Open Folder** button in the add-on settings opens it.

## Author

eikonic
