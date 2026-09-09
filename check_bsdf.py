import bpy

mat = bpy.data.materials.new("test")
mat.use_nodes = True
nodes = mat.node_tree.nodes
print("\n=== All node types in material ===")
for n in nodes:
    print(f"  {n.name}: type={n.type}")

bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
if bsdf:
    print("\n=== Principled BSDF inputs ===")
    for inp in bsdf.inputs:
        print(f"  '{inp.name}' (type={inp.type}, id={inp.identifier})")
else:
    print("\nNo BSDF_PRINCIPLED found")
