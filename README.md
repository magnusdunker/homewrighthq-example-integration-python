# Example counter for HomewrightHQ (Python)

A small external integration for [HomewrightHQ](https://github.com/magnusdunker/homewrighthq), written in Python. It is for trying out how integrations are installed from GitHub, and as a starting point for writing your own in Python. It does the same as the [Rust example](https://github.com/magnusdunker/thehome-example-integration).

Each setup creates a device with:

- a sensor, **Antal** (count), that counts up by one at a fixed interval
- a switch, **Tæller** (counter), that pauses the counting
- a button, **Nulstil** (reset), that sets the number to 0 and fires the event `example_counter_python_reset`

It also has the service `example_counter_python.set`, which sets a counter to a given value, and an options flow where you can change the interval. The number is stored in the integration's data folder, so it survives a restart.

## Installation

In HomewrightHQ: **Settings → Integrations → From GitHub**. Paste

```
https://github.com/magnusdunker/homewrighthq-example-integration-python
```

and press **Install**. Then add **Eksempel-tæller (Python)** under **Add integration**.

The user does not need Python installed. The release contains a ready-made binary for each platform, built with PyInstaller.

## How it works

An external integration is a standalone program. HomewrightHQ starts it and talks to it over JSON-RPC on stdin and stdout. The protocol is described in HomewrightHQ's [docs/custom-integrations.md](https://github.com/magnusdunker/homewrighthq/blob/main/docs/custom-integrations.md).

The repo contains:

- `thehome.json`: domain, name and protocol version
- `thehome_plugin/`: the SDK, which handles the protocol with asyncio. It uses only the standard library.
- `example_counter_python/`: the integration itself
- `main.py`: the entry point that PyInstaller builds
- `tests/`: tests of the SDK and the setup flow
- `.github/workflows/release.yml`: builds a binary for each platform and puts them on a release

## Make your own

1. Copy the repo and change `domain` and `name` in `thehome.json`. The domain may only contain lowercase letters, digits and `_`, and must not be the domain of a built-in integration.
2. Change `DOMAIN` at the top of `release.yml`.
3. Make a subclass of `Integration` from `thehome_plugin`. Only `setup_entry` is required. All methods are `async`.
4. Push a tag, e.g. `git tag v0.1.0 && git push --tags`. The workflow builds for Windows, Linux (x86_64 and arm64) and macOS and creates the release.

HomewrightHQ always fetches the newest release. The binaries must be named `<domain>-<target>`, e.g. `example_counter_python-aarch64-unknown-linux-gnu`.

You can use dependencies from PyPI. Install them in the workflow before PyInstaller, and they end up in the binary.

### Linux and glibc

PyInstaller binaries use the system's glibc and only run with the same or a newer version than the one they were built with. The workflow therefore builds on Ubuntu 22.04 (glibc 2.35). HomewrightHQ's Docker image is Debian bookworm (glibc 2.36), so the binary runs there. Do not build on `ubuntu-latest`, which has a newer glibc.

## Try it locally

```
python -m unittest discover -s tests
python -m pip install pyinstaller
pyinstaller --onefile --name example_counter_python main.py
```

The program waits for messages from HomewrightHQ on stdin, so it does nothing when started on its own.

## License

MIT
