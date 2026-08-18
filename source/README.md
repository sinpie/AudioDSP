# AudioDSP source layout

`source/common` is the only editable source of shared runtime files and tests.
The `releases` directories contain only platform release inputs such as the OS
image, CamillaDSP binary and SD writer. Complete payloads are generated under
the ignored `build` directory and must not be edited directly.

Platform differences live under `source/platforms`:

- `pi2`: ARMv7, low-memory defaults, Ethernet recovery helper
- `pi3`: inherits the Pi 2 runtime with a larger performance margin
- `pi4`: ARM64, Pi 4 performance defaults and MIMO support
- `pi5`: inherits the Pi 4 runtime with a larger performance margin

Build and verify a platform bundle with:

```text
python tools/materialize_releases.py --platform pi2 --assemble
python tools/materialize_releases.py --platform pi2 --check
```

Use `pi3`, `pi4` or `pi5` for the other models. Pi 3 inherits the Pi 2 low-memory
overlay; Pi 5 inherits the Pi 4 ARM64 overlay. The platform-specific CamillaDSP
binary is copied from the matching release directory and is intentionally not
tracked by Git.
