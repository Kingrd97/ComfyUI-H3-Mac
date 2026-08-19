"""ComfyUI-H3-Mac custom node entrypoint."""

# ComfyUI imports custom-node folders as packages. Pytest may inspect this file
# as a standalone module because the repository name contains hyphens; avoid
# importing the heavy ComfyUI runtime in that special test-collection case.
if __package__:
    from .nodes import comfy_entrypoint

    __all__ = ["comfy_entrypoint"]
else:
    comfy_entrypoint = None
    __all__ = []
