SYMMETRIC TEXTURE MIRROR - BLENDER ADD-ON (v1.1)
=================================================

INSTALL
1. In Blender, open Edit > Preferences.
2. Open Add-ons / Get Extensions, then choose Install from Disk.
3. Select symmetric_texture_mirror_addon.py and enable the add-on.
   (If your Blender version uses legacy 'Install...', use that.)
4. Return to a 3D Viewport, press N, choose the 'Symmetry' tab.

HOW TO USE
1. Import your original textured GLB and select the original mesh.
2. Click 'Use Selected Mesh' in the Symmetry sidebar tab.
   Alternatively choose your model from the 'Model' object picker.
3. The original is temporarily hidden. The visible result is a LIVE PREVIEW
   with the geometry bisected and mirrored, retaining the good side's UVs.
4. Choose X/Y/Z (local model axis). Use Swap Sides to pick the good half.
5. Adjust Cut Position if the default centre isn't the intended seam.
6. An orange rectangle marks the bisect plane; cyan lines mark the actual
   intersection with the model. Preview updates when any setting changes.
7. Click GENERATE to apply the mirror and DELETE the original source mesh.
   The mirror-plane helper is deleted too. Only the generated result remains
   for that selected mesh (other scene objects are not touched).
8. Export the final mesh from Blender with File > Export > glTF 2.0 (.glb),
   or another format of your choice. Export is intentionally not automatic.

PREVIEW / REVERSIBILITY
- Turn OFF 'Live Preview' to discard the preview and restore the original.
- Before Generate, nothing about the source geometry or original UVs is edited.
- Generate is destructive to the source OBJECT, so save a .blend backup first.
- If Generate fails while applying the modifier, the original is retained.

IMPORTANT LIMITATIONS
- Intended for approximately symmetric geometry with one desired textured
  half. It DOES replace geometry on the discarded side.
- Not suitable when opposite sides intentionally have different geometry.
- UV and material data are kept, but input UVs/textures must already exist
  on the GOOD half. This tool does not paint, repair textures or make them
  printable by itself.
- Existing holes, self-intersections and non-manifold topology are NOT fixed.
- Mesh modifiers or shape keys on source are baked into the preview/result.
- Object's LOCAL axes determine the mirror direction, not camera axes.
- For multipart models, process the desired mesh only.

