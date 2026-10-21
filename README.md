# Eksempel-tæller til HomewrightHQ (Python)

En lille ekstern integration til [HomewrightHQ](https://github.com/magnusdunker/homewrighthq), skrevet i Python. Den er til at afprøve, hvordan integrationer installeres fra GitHub, og som udgangspunkt for at skrive sin egen i Python. Den gør det samme som [Rust-eksemplet](https://github.com/magnusdunker/thehome-example-integration).

Hver opsætning giver en enhed med:

- en sensor, **Antal**, der tæller én op med et fast interval
- en kontakt, **Tæller**, der sætter tællingen på pause
- en knap, **Nulstil**, der sætter tallet til 0 og sender hændelsen `example_counter_python_reset`

Desuden har den tjenesten `example_counter_python.set`, der sætter en tæller til en bestemt værdi, og et options-forløb, hvor intervallet kan ændres. Tallet gemmes i integrationens datamappe, så det overlever en genstart.

## Installation

I HomewrightHQ: **Indstillinger → Integrationer → Fra GitHub**. Indsæt

```
https://github.com/magnusdunker/homewrighthq-example-integration-python
```

og tryk **Installér**. Tilføj derefter **Eksempel-tæller (Python)** under **Tilføj integration**.

Brugeren skal ikke have Python installeret. Releasen indeholder en færdig binær til hver platform, bygget med PyInstaller.

## Sådan virker det

En ekstern integration er et selvstændigt program. HomewrightHQ starter det og taler med det over JSON-RPC på stdin og stdout. Protokollen er beskrevet i HomewrightHQ's [docs/custom-integrations.md](https://github.com/magnusdunker/homewrighthq/blob/main/docs/custom-integrations.md).

Repoet indeholder:

- `thehome.json`: domæne, navn og protokolversion
- `thehome_plugin/`: SDK'en, der klarer protokollen med asyncio. Den bruger kun standardbiblioteket.
- `example_counter_python/`: selve integrationen
- `main.py`: indgangen, som PyInstaller bygger
- `tests/`: tests af SDK'en og opsætningsforløbet
- `.github/workflows/release.yml`: bygger en binær til hver platform og lægger dem på en release

## Lav din egen

1. Kopiér repoet og ret `domain` og `name` i `thehome.json`. Domænet må kun indeholde små bogstaver, tal og `_` og må ikke være en indbygget integrations domæne.
2. Ret `DOMAIN` øverst i `release.yml`.
3. Lav en underklasse af `Integration` fra `thehome_plugin`. Kun `setup_entry` er påkrævet. Alle metoder er `async`.
4. Push et tag, f.eks. `git tag v0.1.0 && git push --tags`. Workflowet bygger til Windows, Linux (x86_64 og arm64) og macOS og opretter releasen.

HomewrightHQ henter altid den nyeste release. Binærerne skal hedde `<domæne>-<target>`, f.eks. `example_counter_python-aarch64-unknown-linux-gnu`.

Afhængigheder fra PyPI kan bruges. Installér dem i workflowet før PyInstaller, så kommer de med i binæren.

### Linux og glibc

PyInstaller-binærer bruger systemets glibc og kører kun med samme eller nyere version end den, de blev bygget med. Workflowet bygger derfor på Ubuntu 22.04 (glibc 2.35). HomewrightHQ's Docker-image er Debian bookworm (glibc 2.36), så binæren kører der. Byg ikke på `ubuntu-latest`, der har en nyere glibc.

## Prøv lokalt

```
python -m unittest discover -s tests
python -m pip install pyinstaller
pyinstaller --onefile --name example_counter_python main.py
```

Programmet venter på beskeder fra HomewrightHQ på stdin, så det gør ingenting, når det startes alene.

## Licens

MIT
